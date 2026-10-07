"""Windows input injection via ``SendInput`` + scancodes (ctypes only).

Unity, Unreal and DirectInput based games read *scan codes*, so injecting virtual
key codes produces nothing at all.  Every key is therefore sent with
``KEYEVENTF_SCANCODE`` (plus ``KEYEVENTF_EXTENDEDKEY`` for the 0xE0 block).

Relative look uses ``MOUSEEVENTF_MOVE`` with ``MOUSEEVENTF_MOVE_NOCOALESCE`` so
the system does not merge (and thereby lose) quickly repeated turns.

``SetProcessDPIAware()`` is called first: without it the process is virtualised
and every screen coordinate (including the FPS region) is scaled.

When the target has a higher integrity level (elevated game, protected process)
UIPI silently drops our input - ``doctor`` checks for that and says so.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any, Dict, List, Optional

from ...core.keycodes import KeycodeError, win_scancode
from .._win_types import (
    INPUT,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_XDOWN,
    MOUSEEVENTF_XUP,
    WHEEL_DELTA,
    XBUTTON1,
    XBUTTON2,
    key_input,
    mouse_button_input,
    mouse_move_input,
    mouse_wheel_input,
    install_prototypes,
)
from ..base import BackendUnavailable, InputBackend

if not sys.platform.startswith("win"):
    raise BackendUnavailable(
        "the Windows input backend can only run on Windows (this is %s)" % sys.platform,
        hint="run fpsbench on Windows 11",
    )

user32 = ctypes.WinDLL("user32", use_last_error=True)

# Pointer-sized arguments (HWND, LPARAM) must be declared or ctypes truncates
# them to 32 bits; see fpsbench/backends/_win_types.py.
install_prototypes(user32)

_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
    "x1": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
    "x2": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
}


class WindowsInputBackend(InputBackend):
    name = "SendInput(scancode)"

    def __init__(self) -> None:
        self._dpi_aware = False
        self._post_to_window: Optional[int] = None
        self._unsupported: List[str] = []

    # -- lifecycle --------------------------------------------------------- #
    def prepare(self, *, window_title: Optional[str] = None, post_to_pid: Optional[int] = None) -> None:
        try:
            user32.SetProcessDPIAware()
            self._dpi_aware = True
        except Exception:
            self._dpi_aware = False
        if post_to_pid:
            # Windows has no ``CGEventPostToPid`` equivalent - ``SendInput`` always
            # targets the foreground window - so the useful thing to do with a pid
            # is to bring that process's window to the front instead.
            self._post_to_window = _window_for_pid(int(post_to_pid))
            if self._post_to_window:
                user32.ShowWindow(self._post_to_window, 9)  # SW_RESTORE
                user32.SetForegroundWindow(self._post_to_window)
        if window_title:
            self.activate_window(window_title)

    def teardown(self) -> None:
        return None

    # -- keyboard ---------------------------------------------------------- #
    def key_down(self, key: str) -> None:
        self._send(self._key(key, down=True))

    def key_up(self, key: str) -> None:
        self._send(self._key(key, down=False))

    def _key(self, key: str, *, down: bool) -> INPUT:
        try:
            code, extended = win_scancode(key)
        except KeycodeError:
            if key not in self._unsupported:  # not injectable here: skip, do not abort
                self._unsupported.append(key)
            code, extended = 0, False
        return key_input(code, extended=extended, down=down)

    # -- mouse ------------------------------------------------------------- #
    def look(self, dx: float, dy: float) -> None:
        self._send(mouse_move_input(int(round(dx)), int(round(dy))))

    def scroll(self, dy: float, dx: float = 0.0) -> None:
        if dy:
            self._send(mouse_wheel_input(int(round(dy * WHEEL_DELTA))))
        if dx:
            self._send(mouse_wheel_input(int(round(dx * WHEEL_DELTA)), horizontal=True))

    def button_down(self, button: str) -> None:
        self._button(button, down=True)

    def button_up(self, button: str) -> None:
        self._button(button, down=False)

    def _button(self, button: str, *, down: bool) -> None:
        entry = _BUTTONS.get(button)
        if entry is None:
            raise BackendUnavailable("unsupported mouse button %r" % (button,))
        down_flag, up_flag, data = entry
        self._send(mouse_button_input(down_flag, up_flag, data, down=down))

    @staticmethod
    def _send(*inputs: INPUT) -> None:
        array = (INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            raise ctypes.WinError(ctypes.get_last_error())

    # -- windows ----------------------------------------------------------- #
    def find_window(self, title_substring: str) -> Optional[str]:
        needle = title_substring.lower()
        for _handle, title in list_windows():
            if needle in title.lower():
                return title
        return None

    def activate_window(self, title_substring: str) -> bool:
        needle = title_substring.lower()
        for handle, title in list_windows():
            if needle not in title.lower():
                continue
            user32.ShowWindow(handle, 9)  # SW_RESTORE
            if user32.SetForegroundWindow(handle):
                return True
            # SetForegroundWindow is refused while another process owns the
            # foreground; an ALT press is the documented workaround.
            self._send(self._key("alt", down=True), self._key("alt", down=False))
            return bool(user32.SetForegroundWindow(handle))
        return False

    def unsupported_keys(self) -> List[str]:
        return list(self._unsupported)

    def capabilities(self) -> Dict[str, Any]:
        return {
            "dpi_aware": self._dpi_aware,
            "scancode_injection": True,
            # Documented so ``--post-to-pid`` is not mistaken for per-process posting.
            "post_to_pid": False,
            "activate_window_by_pid": self._post_to_window is not None,
        }


def _enum_windows(callback) -> None:
    user32.EnumWindows(callback, 0)


def _window_for_pid(pid: int) -> Optional[int]:
    found: List[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_ssize_t)
    def callback(handle, _lparam):
        owner = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(handle):
            found.append(int(handle))
            return False
        return True

    _enum_windows(callback)
    return found[0] if found else None


def list_windows() -> List[tuple]:
    """``[(hwnd, title), ...]`` for visible top-level windows that have a title."""
    out: List[tuple] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_ssize_t)
    def callback(handle, _lparam):
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        if buffer.value:
            out.append((int(handle), buffer.value))
        return True

    _enum_windows(callback)
    return out


BACKEND = WindowsInputBackend
