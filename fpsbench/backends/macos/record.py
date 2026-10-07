"""macOS recorder: a listen-only ``CGEventTap``.

A listen-only tap sees the user's real input without consuming it, so the user
can play normally while recording.  Two details matter:

* **look deltas** - once a game switches the cursor to relative mode the cursor
  position stops changing, so ``kCGMouseEventDeltaX/Y`` is the only usable
  source.  ``CGEventGetLocation`` differences are a fallback for windowed games
  that leave the cursor alone.
* **autorepeat** - ``kCGKeyboardEventAutorepeat`` down events are dropped; the
  trace stays a clean press/release pair which replays as one continuous hold.

The tap runs on its own thread with its own run loop so it never blocks the game.
"""

from __future__ import annotations

import threading
import time
from typing import Any, List, Optional

from ...core.keycodes import canonical_for_mac_vk
from ...core.trace import TraceEvent
from ..base import BackendUnavailable, RecorderBackend

try:  # pragma: no cover - macOS + pyobjc only
    import Quartz
except ImportError as exc:  # pragma: no cover
    raise BackendUnavailable(
        "pyobjc (Quartz) is required for macOS recording: %s" % exc,
        hint='python3 -m pip install -e ".[macos]"',
    ) from exc

__all__ = ["MacRecorderBackend", "BACKEND"]

_FLAG_SHIFT = Quartz.kCGEventFlagMaskShift
_FLAG_CTRL = Quartz.kCGEventFlagMaskControl
_FLAG_ALT = Quartz.kCGEventFlagMaskAlternate
_FLAG_CMD = Quartz.kCGEventFlagMaskCommand

#: ⌃⌥ + 1..9 drops a label at the current position, ⌃⌥ + Enter finishes the run.
_HOTKEY_MODS = _FLAG_CTRL | _FLAG_ALT

_MODIFIER_KEYS = {"shift": _FLAG_SHIFT, "ctrl": _FLAG_CTRL, "alt": _FLAG_ALT, "cmd": _FLAG_CMD}

#: CoreGraphics ''other'' mouse button numbers.
_OTHER_BUTTONS = {2: "middle", 3: "x1", 4: "x2"}

_TAP_DISABLED = (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput)


def is_secure_event_input_enabled() -> bool:
    """True when Secure Input is on (recording/injection will not reach the app)."""
    value = getattr(Quartz, "IsSecureEventInputEnabled", None)
    if value is not None:
        return bool(value())
    import ctypes

    try:  # HIToolbox export, no extra dependency needed
        lib = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        function = lib.IsSecureEventInputEnabled
        function.restype = ctypes.c_bool
        return bool(function())
    except Exception:
        return False


class MacRecorderBackend(RecorderBackend):
    name = "CGEventTap"
    label_hotkey = "ctrl+alt+1..9"
    finish_hotkey = "ctrl+alt+enter"
    records_immediately = False
    supports_labels = True

    def __init__(self, *, clock_ns=time.perf_counter_ns) -> None:
        self._clock_ns = clock_ns
        self._events: List[TraceEvent] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop = None
        self._tap = None
        self._t0_ns: Optional[int] = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._warnings: List[str] = []
        self._down_keys: set = set()
        self._mods: set = set()
        self._hotkey_digits: set = set()
        self._finish = threading.Event()
        self._finish_pressed = False
        self._recording = False
        self._last_cursor = None
        self._source = None
        # Lifetime counters: ``poll()`` drains ``_events``, so end-of-run
        # diagnostics must not read it (it is empty by then).
        self._seen_total = 0
        self._seen_key_down = 0
        self._seen_look = 0

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        self._events = []
        self._warnings = []
        self._error = None
        self._stop.clear()
        self._ready.clear()
        self._loop = None
        self._tap = None
        self._source = None
        self._down_keys = set()
        self._mods = set()
        self._hotkey_digits = set()
        self._finish.clear()
        self._finish_pressed = False
        self._recording = self.records_immediately
        self._t0_ns = self._clock_ns()
        self._seen_total = 0
        self._seen_key_down = 0
        self._seen_look = 0
        if is_secure_event_input_enabled():
            self._warnings.append(
                "macOS Secure Input is enabled - keystrokes will not be recorded until it is "
                "switched off (some apps enable it for password fields)"
            )
        self._thread = threading.Thread(target=self._run, name="fpsbench-cgeventtap", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise BackendUnavailable("the CGEventTap thread did not start")
        if self._error:
            raise BackendUnavailable(self._error, hint=_PERMISSION_HINT)

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None:
            try:
                Quartz.CFRunLoopStop(loop)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        # Release the run loop source and the mach port: dropping the Python
        # reference alone leaves the CoreFoundation objects alive for the process.
        if self._source is not None:
            try:
                Quartz.CFRunLoopSourceInvalidate(self._source)
            except Exception:
                pass
            self._source = None
        if self._tap is not None:
            try:
                Quartz.CGEventTapEnable(self._tap, False)
            except Exception:
                pass
            try:
                Quartz.CFMachPortInvalidate(self._tap)
            except Exception:
                pass
            self._tap = None
        self._topology_warnings()

    def recording(self) -> bool:
        return self._recording

    def stop_requested(self) -> bool:
        return self._finish.is_set()

    def _topology_warnings(self) -> None:
        if not self._seen_total:
            self._warnings.append("no events were recorded at all - check the permissions and that "
                                  "the game window had focus")
        elif self._seen_key_down and not self._seen_look:
            self._warnings.append(
                "keys were recorded but no mouse movement: if the route needs turning, check that "
                "the game was focused and moving the mouse actually turns the camera"
            )
        elif self._seen_look and not self._seen_key_down:
            self._warnings.append("mouse movement was recorded but no key presses")

    # -- tap thread -------------------------------------------------------- #
    def _run(self) -> None:
        mask = (
            Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
            | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
            | Quartz.CGEventMaskBit(Quartz.kCGEventMouseMoved)
            | Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseDragged)
            | Quartz.CGEventMaskBit(Quartz.kCGEventRightMouseDragged)
            | Quartz.CGEventMaskBit(Quartz.kCGEventOtherMouseDragged)
            | Quartz.CGEventMaskBit(Quartz.kCGEventScrollWheel)
            | Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseUp)
            | Quartz.CGEventMaskBit(Quartz.kCGEventRightMouseDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventRightMouseUp)
            | Quartz.CGEventMaskBit(Quartz.kCGEventOtherMouseDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventOtherMouseUp)
        )
        self._loop = Quartz.CFRunLoopGetCurrent()
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            self._callback,
            None,
        )
        if tap is None:
            self._error = (
                "CGEventTapCreate returned NULL: the process needs Accessibility (and Input "
                "Monitoring) permission"
            )
            self._ready.set()
            return
        self._tap = tap
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._source = source
        Quartz.CFRunLoopAddSource(self._loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        self._ready.set()
        while not self._stop.is_set():
            Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.1, False)
        if self._tap is not None:
            Quartz.CGEventTapEnable(self._tap, False)

    def _callback(self, proxy, event_type, event, refcon):
        try:
            self._handle(event_type, event)
        except Exception as exc:  # never let the callback die
            self._error = "%s: %s" % (type(exc).__name__, exc)
        if event_type in _TAP_DISABLED and self._tap is not None:
            Quartz.CGEventTapEnable(self._tap, True)
        return event

    # -- event translation ------------------------------------------------- #
    def _t_ms(self) -> float:
        t0 = self._t0_ns if self._t0_ns is not None else self._clock_ns()
        return max(0.0, (self._clock_ns() - t0) / 1e6)

    def arm(self) -> None:
        """Begin capturing without the start hotkey (``measure`` starts on input).

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

    def _emit(self, event: TraceEvent) -> None:
        with self._lock:
            self._seen_total += 1
            if event.kind == "key" and event.down:
                self._seen_key_down += 1
            elif event.kind == "look":
                self._seen_look += 1
            if self._recording:
                self._events.append(event)

    def _handle(self, event_type: Any, event: Any) -> None:
        if event_type == Quartz.kCGEventKeyDown:
            self._on_key(event, down=True)
        elif event_type == Quartz.kCGEventKeyUp:
            self._on_key(event, down=False)
        elif event_type == Quartz.kCGEventFlagsChanged:
            self._on_flags(event)
        elif event_type in (
            Quartz.kCGEventMouseMoved,
            Quartz.kCGEventLeftMouseDragged,
            Quartz.kCGEventRightMouseDragged,
            Quartz.kCGEventOtherMouseDragged,
        ):
            self._on_mouse_moved(event)
        elif event_type == Quartz.kCGEventScrollWheel:
            self._on_scroll(event)
        elif event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventRightMouseDown):
            button = "left" if event_type == Quartz.kCGEventLeftMouseDown else "right"
            self._emit(TraceEvent(kind="button", t_ms=self._t_ms(), button=button, down=True))
        elif event_type in (Quartz.kCGEventLeftMouseUp, Quartz.kCGEventRightMouseUp):
            button = "left" if event_type == Quartz.kCGEventLeftMouseUp else "right"
            self._emit(TraceEvent(kind="button", t_ms=self._t_ms(), button=button, down=False))
        elif event_type in (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp):
            number = int(
                Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventButtonNumber)
            )
            button = _OTHER_BUTTONS.get(number)
            if button is not None:
                self._emit(
                    TraceEvent(
                        kind="button",
                        t_ms=self._t_ms(),
                        button=button,
                        down=event_type == Quartz.kCGEventOtherMouseDown,
                    )
                )

    def _on_key(self, event: Any, *, down: bool) -> None:
        if down and Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
            return  # autorepeat: keep one press, replay as a continuous hold
        vk = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
        name = canonical_for_mac_vk(vk)
        if name is None:
            return  # unmapped key (media keys, ...) - a route could not replay it
        t_ms = self._t_ms()
        flags = int(Quartz.CGEventGetFlags(event))
        mods = sorted(self._mods)
        if name in _MODIFIER_KEYS:
            # Modifier state comes from flagsChanged; a key event for a modifier
            # is only a duplicate of that state change.
            return
        if name.isdigit() and (flags & _HOTKEY_MODS) == _HOTKEY_MODS:
            if down:
                self._hotkey_digits.add(name)
                self._emit(TraceEvent(kind="label", t_ms=t_ms, text=name))
                return
            if name in self._hotkey_digits:  # swallow the matching release
                self._hotkey_digits.discard(name)
                return
        if name == "enter" and (flags & _HOTKEY_MODS) == _HOTKEY_MODS:
            if down:
                self._finish_pressed = True
                self._toggle()
                return
            if self._finish_pressed:  # swallow the matching release
                self._finish_pressed = False
                return
        if down:
            self._down_keys.add(name)
            self._emit(
                TraceEvent(kind="key", t_ms=t_ms, key=name, down=True, mods=mods, native={"vk": vk})
            )
        else:
            self._down_keys.discard(name)
            self._emit(
                TraceEvent(kind="key", t_ms=t_ms, key=name, down=False, mods=mods, native={"vk": vk})
            )

    def _on_flags(self, event: Any) -> None:
        flags = int(Quartz.CGEventGetFlags(event))
        vk = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
        name = canonical_for_mac_vk(vk)
        if name not in _MODIFIER_KEYS:
            return
        pressed = bool(flags & _MODIFIER_KEYS[name])
        already = name in self._mods
        if pressed and not already:
            self._mods.add(name)
        elif not pressed and already:
            self._mods.discard(name)
        else:
            return  # unchanged - flags changed for a different modifier
        t_ms = self._t_ms()
        self._emit(
            TraceEvent(
                kind="key",
                t_ms=t_ms,
                key=name,
                down=pressed,
                mods=sorted(self._mods),
                native={"vk": vk},
            )
        )

    def _on_mouse_moved(self, event: Any) -> None:
        dx = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaX)
        dy = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventDeltaY)
        if dx == 0 and dy == 0:
            # Windowed games that keep a normal cursor: fall back to position deltas.
            position = Quartz.CGEventGetLocation(event)
            if self._last_cursor is not None:
                dx = position.x - self._last_cursor.x
                dy = position.y - self._last_cursor.y
            self._last_cursor = position
        if dx == 0 and dy == 0:
            return
        self._emit(
            TraceEvent(
                kind="look",
                t_ms=self._t_ms(),
                dx=int(round(dx)),
                dy=int(round(dy)),
                mods=sorted(self._mods),
            )
        )

    def _on_scroll(self, event: Any) -> None:
        # DeltaAxis1 counts wheel lines/notches, which is the unit the injector
        # posts with (kCGScrollEventUnitLine).  Point deltas are the fallback for
        # free-spinning wheels; ~10 points per line is the usual ratio.
        lines = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventDeltaAxis1)
        if lines == 0:
            points = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGScrollWheelEventPointDeltaAxis1
            )
            lines = points / 10.0
        if not lines:
            return
        self._emit(
            TraceEvent(
                kind="scroll",
                t_ms=self._t_ms(),
                dy=round(float(lines), 2),
                mods=sorted(self._mods),
            )
        )

    # -- results ----------------------------------------------------------- #
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


_PERMISSION_HINT = (
    "grant your terminal/IDE Accessibility and Screen Recording (and Input Monitoring) "
    "permission in System Settings > Privacy & Security, then restart it"
)

BACKEND = MacRecorderBackend
