"""Windows recorder using **Raw Input** (not a mouse hook).

Why Raw Input: once a game switches the cursor to relative mode, the cursor is
clipped to the game window, so a low level mouse hook (``WH_MOUSE_LL``) only ever
sees clipped absolute coordinates and the real turn deltas are lost.
``GetRawInputData`` on ``WM_INPUT`` delivers ``RAWMOUSE.lLastX/lLastY`` - the
device's own deltas - which is exactly what has to be replayed.

A hidden message-only window is registered with ``RIDEV_INPUTSINK`` so input is
delivered even while the game holds the foreground, and the message pump runs on
its own thread so recording never blocks the game.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import List, Optional

from ...core.keycodes import MODIFIER_KEYS, canonical_for_win_scancode
from ...core.trace import TraceEvent
from .._win_types import (
    HWND_MESSAGE,
    MOUSE_BUTTON_FLAGS,
    MOUSE_MOVE_ABSOLUTE,
    RAWINPUT,
    RAWINPUTDEVICE,
    RAWINPUTHEADER,
    RAWKEYBOARD,
    RAWMOUSE,
    RI_KEY_BREAK,
    RI_KEY_E0,
    RI_KEY_E1,
    RI_MOUSE_HWHEEL,
    RI_MOUSE_WHEEL,
    RIDEV_INPUTSINK,
    RID_INPUT,
    RIM_TYPEKEYBOARD,
    RIM_TYPEMOUSE,
    WHEEL_DELTA,
    WM_CLOSE,
    WM_DESTROY,
    WM_INPUT,
    WM_QUIT,
    WNDCLASSW,
    WNDPROC,
    install_prototypes,
)
from ..base import BackendUnavailable, RecorderBackend

if not sys.platform.startswith("win"):
    raise BackendUnavailable(
        "the Windows recorder can only run on Windows (this is %s)" % sys.platform,
        hint="run fpsbench on Windows 11",
    )

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Without prototypes ctypes truncates pointer-sized arguments (LPARAM, HRAWINPUT,
# HWND) to 32 bits, which silently kills Raw Input recording.
install_prototypes(user32)
install_prototypes(kernel32)

#: Ctrl+Alt + 1..9 drops a label, Ctrl+Alt + Enter finishes the run.  ``cmd`` is
#: deliberately not part of the chord: the hotkey is the same on both platforms.
_HOTKEY_MODS = frozenset({"ctrl", "alt"})
MODIFIER_NAMES = frozenset(MODIFIER_KEYS)


def _notches(raw_delta: int) -> float:
    """Convert a raw wheel delta (multiples of 120) into wheel notches.

    Traces store notches on both platforms: the Windows input backend multiplies
    by ``WHEEL_DELTA`` again on the way out and CoreGraphics works in lines, so a
    recorded scroll means the same thing everywhere.
    """
    value = ctypes.c_short(int(raw_delta)).value
    return round(value / float(WHEEL_DELTA), 2) if value else 0.0


class MSG(ctypes.Structure):
    """The subset of ``MSG`` the pump touches (``GetMessageW`` fills the rest)."""

    _fields_ = (
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt_x", ctypes.c_int32),
        ("pt_y", ctypes.c_int32),
    )


class WindowsRecorderBackend(RecorderBackend):
    name = "RawInput"
    label_hotkey = "ctrl+alt+1..9"
    finish_hotkey = "ctrl+alt+enter"
    records_immediately = False
    supports_labels = True

    def __init__(self, *, clock_ns=time.perf_counter_ns) -> None:
        self._clock_ns = clock_ns
        self._events: List[TraceEvent] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._hwnd: Optional[int] = None
        self._wndproc = None
        self._class_atom = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._warnings: List[str] = []
        self._t0_ns: Optional[int] = None
        self._down_keys: set = set()
        self._mods: set = set()
        self._hotkey_digits: set = set()
        self._finish = threading.Event()
        self._finish_pressed = False
        self._recording = False
        self._last_absolute: Optional[tuple] = None
        # Lifetime counters: ``poll()`` drains ``_events``, so the end-of-run
        # diagnostics must not read it (it is always empty by then).
        self._seen_total = 0
        self._seen_key_down = 0
        self._seen_look = 0

    # -- lifecycle --------------------------------------------------------- #
    def prepare(self, *, window_title: Optional[str] = None) -> None:
        return None

    def start(self) -> None:
        self._events = []
        self._warnings = []
        self._error = None
        self._stop.clear()
        self._ready.clear()
        self._down_keys = set()
        self._mods = set()
        self._hotkey_digits = set()
        self._finish.clear()
        self._finish_pressed = False
        self._recording = self.records_immediately
        self._last_absolute = None
        self._seen_total = 0
        self._seen_key_down = 0
        self._seen_look = 0
        self._t0_ns = self._clock_ns()
        self._thread = threading.Thread(target=self._run, name="fpsbench-rawinput", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise BackendUnavailable("the Raw Input thread did not start")
        if self._error:
            raise BackendUnavailable(self._error, hint=_UIPI_HINT)

    def stop(self) -> None:
        self._stop.set()
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._unregister_class()
        if not self._seen_total:
            self._warnings.append(
                "no events were recorded: check that the game had focus and that this process is "
                "not blocked by UIPI (run as administrator if the game is elevated)"
            )
        elif self._seen_look and not self._seen_key_down:
            self._warnings.append("mouse movement was recorded but no key presses")
        elif self._seen_key_down and not self._seen_look:
            self._warnings.append(
                "keys were recorded but no mouse movement - if the route needs turning, check the "
                "game's camera control uses the mouse"
            )

    def recording(self) -> bool:
        return self._recording

    def stop_requested(self) -> bool:
        return self._finish.is_set()

    def arm(self) -> None:
        """Begin capturing without the start hotkey (the first press does this).

        The hotkey then only stops the capture.  The clock is re-anchored here for
        the same reason the starting press re-anchors it: what happened while the
        user was getting ready must not count.
        """
        with self._lock:
            self._events = []
            self._t0_ns = self._clock_ns()
            self._recording = True

    def _toggle(self) -> None:
        """Start recording, or finish it when it is already running.

        The clock is re-anchored on the starting press, so the trace begins at 0 ms
        no matter how long the user spent getting ready.
        """
        if not self._recording:
            self.arm()
            return
        with self._lock:
            self._recording = False
        self._finish.set()

    # -- window + message pump --------------------------------------------- #
    def _run(self) -> None:
        try:
            self._create_window()
        except OSError as exc:
            self._error = "could not create the message window: %s" % exc
            self._ready.set()
            return
        try:
            self._register_devices()
        except Exception as exc:
            # ``ctypes.WinError`` is an OSError; either way the thread must report
            # it instead of leaving ``start()`` to time out with a generic message.
            self._error = "could not register for Raw Input: %s" % exc
            self._ready.set()
            return
        self._ready.set()
        message = MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def _create_window(self) -> None:
        self._thread_id = int(kernel32.GetCurrentThreadId())
        instance = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._window_proc)
        class_name = "fpsbenchRawInputWindow"
        wndclass = WNDCLASSW()
        wndclass.lpfnWndProc = self._wndproc
        wndclass.hInstance = instance
        wndclass.lpszClassName = class_name
        self._class_atom = int(user32.RegisterClassW(ctypes.byref(wndclass)))
        if not self._class_atom:
            raise ctypes.WinError(ctypes.get_last_error())
        handle = user32.CreateWindowExW(
            0, class_name, "fpsbench", 0, 0, 0, 0, 0, HWND_MESSAGE, None, instance, None
        )
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = int(handle)

    def _register_devices(self) -> None:
        devices = (RAWINPUTDEVICE * 2)(
            RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, self._hwnd),  # mouse
            RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, self._hwnd),  # keyboard
        )
        if not user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(RAWINPUTDEVICE)):
            raise ctypes.WinError(ctypes.get_last_error())

    def _unregister_class(self) -> None:
        if self._class_atom:
            try:
                kernel32.GetModuleHandleW(None)
                user32.UnregisterClassW("fpsbenchRawInputWindow", kernel32.GetModuleHandleW(None))
            except Exception:
                pass
            self._class_atom = None

    def _window_proc(self, hwnd, message, wparam, lparam) -> int:
        try:
            if message == WM_INPUT:
                self._on_raw_input(lparam)
            elif message == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            elif message == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as exc:  # a broken callback must not kill the pump
            self._error = "%s: %s" % (type(exc).__name__, exc)
        return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))

    def _on_raw_input(self, lparam) -> None:
        size = ctypes.c_uint32(0)
        user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        if size.value == 0:
            return
        buffer = ctypes.create_string_buffer(size.value)
        read = user32.GetRawInputData(
            lparam, RID_INPUT, buffer, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER)
        )
        if read == 0xFFFFFFFF:
            return
        raw = ctypes.cast(buffer, ctypes.POINTER(RAWINPUT)).contents
        if raw.header.dwType == RIM_TYPEMOUSE:
            self._on_mouse(raw.data.mouse)
        elif raw.header.dwType == RIM_TYPEKEYBOARD:
            self._on_keyboard(raw.data.keyboard)

    # -- translation -------------------------------------------------------- #
    def _t_ms(self) -> float:
        t0 = self._t0_ns if self._t0_ns is not None else self._clock_ns()
        return max(0.0, (self._clock_ns() - t0) / 1e6)

    def _emit(self, event: TraceEvent) -> None:
        with self._lock:
            self._seen_total += 1
            if event.kind == "key" and event.down:
                self._seen_key_down += 1
            elif event.kind == "look":
                self._seen_look += 1
            if self._recording:
                self._events.append(event)

    def _on_mouse(self, mouse: RAWMOUSE) -> None:
        t_ms = self._t_ms()
        if mouse.usFlags & MOUSE_MOVE_ABSOLUTE:
            # Remote desktop / tablet style devices report absolutes; convert.
            if self._last_absolute is not None:
                dx = mouse.lLastX - self._last_absolute[0]
                dy = mouse.lLastY - self._last_absolute[1]
                if dx or dy:
                    self._emit(TraceEvent(kind="look", t_ms=t_ms, dx=int(dx), dy=int(dy)))
            self._last_absolute = (mouse.lLastX, mouse.lLastY)
        else:
            if mouse.lLastX or mouse.lLastY:
                self._emit(
                    TraceEvent(kind="look", t_ms=t_ms, dx=int(mouse.lLastX), dy=int(mouse.lLastY))
                )
        flags = int(mouse.usButtonFlags)
        for bit, button, down in MOUSE_BUTTON_FLAGS:
            if flags & bit:
                self._emit(TraceEvent(kind="button", t_ms=t_ms, button=button, down=down))
        if flags & RI_MOUSE_WHEEL:
            notches = _notches(mouse.usButtonData)
            if notches:
                self._emit(TraceEvent(kind="scroll", t_ms=t_ms, dy=notches))
        if flags & RI_MOUSE_HWHEEL:
            notches = _notches(mouse.usButtonData)
            if notches:
                self._emit(TraceEvent(kind="scroll", t_ms=t_ms, dy=0.0, dx=notches))

    def _on_keyboard(self, keyboard: RAWKEYBOARD) -> None:
        flags = int(keyboard.Flags)
        if flags & RI_KEY_E1:
            return
        down = not (flags & RI_KEY_BREAK)
        extended = bool(flags & RI_KEY_E0)
        code = int(keyboard.MakeCode)
        if code == 0:
            return
        name = canonical_for_win_scancode(code, extended)
        if name is None:
            return
        t_ms = self._t_ms()
        # Raw Input reports modifiers with real make/break codes, so there is no
        # separate flags-changed path like on macOS.
        if name in MODIFIER_NAMES:
            if down:
                self._mods.add(name)
            else:
                self._mods.discard(name)
        elif down and name.isdigit() and _HOTKEY_MODS.issubset(self._mods):
            self._hotkey_digits.add(name)
            self._emit(TraceEvent(kind="label", t_ms=t_ms, text=name))
            return
        elif down and name == "enter" and _HOTKEY_MODS.issubset(self._mods):
            self._finish_pressed = True
            self._toggle()
            return
        elif not down and name in self._hotkey_digits:
            self._hotkey_digits.discard(name)
            return
        elif not down and name == "enter" and self._finish_pressed:
            self._finish_pressed = False
            return
        if down:
            self._down_keys.add(name)
        else:
            self._down_keys.discard(name)
        self._emit(
            TraceEvent(
                kind="key",
                t_ms=t_ms,
                key=name,
                down=down,
                mods=sorted(self._mods),
                native={"scancode": code, "ext": extended},
            )
        )

    # -- results ------------------------------------------------------------ #
    def poll(self) -> List[TraceEvent]:
        with self._lock:
            events, self._events = self._events, []
        events.sort(key=lambda event: event.t_ms)
        return events

    def warnings(self) -> List[str]:
        out = list(self._warnings)
        if self._error:
            out.append(self._error)
        return out


_UIPI_HINT = (
    "if the game runs elevated (or with an anti-cheat driver) Windows UIPI drops synthetic "
    "input: run your terminal as administrator and keep the game at the same integrity level"
)

BACKEND = WindowsRecorderBackend
