"""macOS input injection via ``CGEvent`` (pyobjc Quartz).

Key points that make this work with games rather than just with normal apps:

* **Relative look** - ``CGAssociateMouseAndMouseCursorPosition(False)`` decouples
  the cursor, so posting a mouse-moved event with ``kCGMouseEventDeltaX/Y``
  delivers raw deltas: the cursor does not travel and never hits a screen edge.
  The association is restored in :meth:`teardown` and the cursor is put back
  where it was.
* **Low latency** - ``CGEventSourceSetLocalEventsSuppressionInterval(0)`` drops
  the default 0.25 s suppression window that applies to locally posted events.
* **``--post-to-pid``** - some games only see events posted directly to their
  process (``CGEventPostToPid``) instead of to the HID tap.

Requires the Accessibility (and, for recording, Input Monitoring) permission.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...core.keycodes import KeycodeError, mac_vk
from ..base import BackendUnavailable, InputBackend

try:  # pragma: no cover - exercised only on macOS with pyobjc installed
    import Quartz
except ImportError as exc:  # pragma: no cover
    raise BackendUnavailable(
        "pyobjc (Quartz) is required for macOS input injection: %s" % exc,
        hint='python3 -m pip install -e ".[macos]"',
    ) from exc


_BUTTONS = {
    "left": (Quartz.kCGMouseButtonLeft, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp),
    "right": (Quartz.kCGMouseButtonRight, Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp),
    "middle": (
        Quartz.kCGMouseButtonCenter,
        Quartz.kCGEventOtherMouseDown,
        Quartz.kCGEventOtherMouseUp,
    ),
}


class MacInputBackend(InputBackend):
    name = "CGEvent"

    def __init__(self) -> None:
        self._source = None
        self._post_to_pid: Optional[int] = None
        self._cursor = None
        self._detached = False
        self._unsupported: List[str] = []

    # -- lifecycle --------------------------------------------------------- #
    def prepare(self, *, window_title: Optional[str] = None, post_to_pid: Optional[int] = None) -> None:
        self._source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        interval = getattr(Quartz, "CGEventSourceSetLocalEventsSuppressionInterval", None)
        if interval is not None and self._source is not None:
            interval(self._source, 0.0)
        self._post_to_pid = int(post_to_pid) if post_to_pid else None

        event = Quartz.CGEventCreate(None)
        if event is not None:
            self._cursor = Quartz.CGEventGetLocation(event)
        Quartz.CGAssociateMouseAndMouseCursorPosition(False)
        self._detached = True
        if window_title:
            self.activate_window(window_title)

    def teardown(self) -> None:
        if self._detached:
            Quartz.CGAssociateMouseAndMouseCursorPosition(True)
            self._detached = False
        if self._cursor is not None:
            try:
                Quartz.CGWarpMouseCursorPosition(self._cursor)
            except Exception:
                pass

    # -- input ------------------------------------------------------------- #
    def key_down(self, key: str) -> None:
        self._post_key(key, True)

    def key_up(self, key: str) -> None:
        self._post_key(key, False)

    def _post_key(self, key: str, down: bool) -> None:
        try:
            code = mac_vk(key)
        except KeycodeError:
            if key not in self._unsupported:  # e.g. "pause": no such key on macOS
                self._unsupported.append(key)
            return
        event = Quartz.CGEventCreateKeyboardEvent(self._source, code, down)
        if event is None:
            raise BackendUnavailable("CGEventCreateKeyboardEvent returned None for %r" % (key,))
        self._post(event)

    def look(self, dx: float, dy: float) -> None:
        position = self._cursor if self._cursor is not None else Quartz.CGPointMake(0.0, 0.0)
        event = Quartz.CGEventCreateMouseEvent(self._source, Quartz.kCGEventMouseMoved, position, 0)
        if event is None:
            raise BackendUnavailable("CGEventCreateMouseEvent returned None")
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventDeltaX, int(round(dx)))
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventDeltaY, int(round(dy)))
        self._post(event)

    def scroll(self, dy: float, dx: float = 0.0) -> None:
        wheel1 = int(round(dy))
        wheel2 = int(round(dx))
        try:
            event = Quartz.CGEventCreateScrollWheelEvent(
                self._source, Quartz.kCGScrollEventUnitLine, 2, wheel1, wheel2
            )
        except TypeError:  # single wheel fallback
            event = Quartz.CGEventCreateScrollWheelEvent(
                self._source, Quartz.kCGScrollEventUnitLine, 1, wheel1
            )
        if event is None:
            raise BackendUnavailable("CGEventCreateScrollWheelEvent returned None")
        self._post(event)

    def button_down(self, button: str) -> None:
        self._post_button(button, True)

    def button_up(self, button: str) -> None:
        self._post_button(button, False)

    def _post_button(self, button: str, down: bool) -> None:
        entry = _BUTTONS.get(button)
        if entry is None:
            raise BackendUnavailable("unsupported mouse button %r" % (button,))
        native_button, down_type, up_type = entry
        position = self._cursor if self._cursor is not None else Quartz.CGPointMake(0.0, 0.0)
        event = Quartz.CGEventCreateMouseEvent(
            self._source, down_type if down else up_type, position, native_button
        )
        self._post(event)

    def _post(self, event: Any) -> None:
        if self._post_to_pid:
            Quartz.CGEventPostToPid(self._post_to_pid, event)
        else:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    # -- windows ----------------------------------------------------------- #
    def find_window(self, title_substring: str) -> Optional[str]:
        for pid, title in _window_list():
            if title_substring.lower() in title.lower():
                return title
        return None

    def activate_window(self, title_substring: str) -> bool:
        needle = title_substring.lower()
        for pid, title in _window_list():
            if needle not in title.lower():
                continue
            try:
                from AppKit import NSRunningApplication

                app = NSRunningApplication.runningApplicationWithProcessId_(pid)
                if app is not None:
                    if hasattr(app, "activateWithOptions_"):
                        app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
                    else:
                        app.activate()
                    return True
            except Exception:
                return False
        return False

    def unsupported_keys(self) -> List[str]:
        return list(self._unsupported)

    def capabilities(self) -> Dict[str, Any]:
        return {"post_to_pid": self._post_to_pid is not None, "detached_cursor": self._detached}


def list_window_titles() -> List[str]:
    """Visible window titles (macOS only exposes titles with Screen Recording)."""
    return [title for _pid, title in _window_list()]


def _window_list():
    """``[(pid, title), ...]`` for on-screen windows (best effort).

    ``kCGWindowName`` is redacted without the Screen Recording permission, so this
    can legitimately return an empty list on a machine where the permission is
    missing - ``doctor`` reports that as a permission problem.
    """
    out = []
    try:
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
    except Exception:
        return out
    for window in windows or []:
        title = window.get("kCGWindowName") or ""
        pid = window.get("kCGWindowOwnerPID")
        if title and pid is not None:
            out.append((int(pid), str(title)))
    return out


def activate_window(title_substring: str) -> bool:  # convenience for the CLI
    backend = MacInputBackend()
    backend.prepare()
    try:
        return backend.activate_window(title_substring)
    finally:
        backend.teardown()


BACKEND = MacInputBackend
