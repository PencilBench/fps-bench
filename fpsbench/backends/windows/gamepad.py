"""Windows virtual gamepad injection through XInput (ViGEmBus + ``vgamepad``).

``vgamepad`` talks to ViGEmBus, which presents a virtual **Xbox 360** controller to
every XInput consumer - the input path most Windows games use.  Two caveats are
part of the design, not fine print:

* **ViGEmBus is archived upstream.** The driver still works but is unmaintained,
  so this backend is a validation candidate, not an assumed long-term dependency.
  It is never installed automatically; ``doctor`` reports whether it is present.
* **A virtual XInput pad is not a DualSense.** Buttons are laid out for XInput and
  a game that checks for a DualShock may see something else, or nothing.  That is
  why the mapping is recorded in the trace metadata and why a missing driver is a
  hard failure instead of a silent fallback to keyboard/mouse.

Sign convention: trace states use SDL's axes (y positive *down*), XInput thumbs
are y positive *up*, so Y is negated on the way out.  Triggers are 0..255 in
XInput and 0..1 in the trace.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from ...core.gamepad import GAMEPAD_BUTTONS, NEUTRAL_STATE, GamepadState
from ..base import BackendUnavailable, GamepadBackend

if not sys.platform.startswith("win"):
    raise BackendUnavailable(
        "the Windows gamepad backend can only run on Windows (this is %s)" % sys.platform,
        hint="run fpsbench on Windows 11, or use the keyboard/mouse route",
    )

try:  # pragma: no cover - Windows only
    import vgamepad as vg
except ImportError as exc:  # pragma: no cover
    raise BackendUnavailable(
        "vgamepad is required for virtual gamepad injection: %s" % exc,
        hint=(
            'py -m pip install -e ".[gamepad]"; ViGEmBus 1.22+ must be installed separately '
            "(the driver is archived upstream - verify it before relying on it)"
        ),
    ) from exc

__all__ = ["WindowsGamepadBackend", "BACKEND", "capability", "BUTTON_NAMES"]

#: Gamepad button -> XInput constant name.
BUTTON_NAMES: Dict[str, str] = {
    "a": "XUSB_GAMEPAD_A",
    "b": "XUSB_GAMEPAD_B",
    "x": "XUSB_GAMEPAD_X",
    "y": "XUSB_GAMEPAD_Y",
    "leftshoulder": "XUSB_GAMEPAD_LEFT_SHOULDER",
    "rightshoulder": "XUSB_GAMEPAD_RIGHT_SHOULDER",
    "dpup": "XUSB_GAMEPAD_DPAD_UP",
    "dpdown": "XUSB_GAMEPAD_DPAD_DOWN",
    "dpleft": "XUSB_GAMEPAD_DPAD_LEFT",
    "dpright": "XUSB_GAMEPAD_DPAD_RIGHT",
    "back": "XUSB_GAMEPAD_BACK",
    "start": "XUSB_GAMEPAD_START",
    "guide": "XUSB_GAMEPAD_GUIDE",
    "leftstick": "XUSB_GAMEPAD_LEFT_THUMB",
    "rightstick": "XUSB_GAMEPAD_RIGHT_THUMB",
}

_THUMB_MAX = 32767
_TRIGGER_MAX = 255


def capability() -> Dict[str, Any]:
    """What ``doctor`` and ``probe-gamepad`` print on this machine."""
    try:
        buttons = vg.XUSB_BUTTON
    except AttributeError:  # pragma: no cover - unexpected package layout
        return {"platform": "windows", "ok": False, "reason": "vgamepad has no XUSB_BUTTON table"}
    missing = [name for name in BUTTON_NAMES.values() if not hasattr(buttons, name)]
    return {
        "platform": "windows",
        "ok": not missing,
        "backend": "vgamepad/ViGEmBus (virtual Xbox 360)",
        "driver": "ViGEmBus (archived upstream; verify before relying on it)",
        "missing_constants": missing,
    }


class WindowsGamepadBackend(GamepadBackend):
    name = "vgamepad/ViGEmBus (virtual Xbox 360)"

    def __init__(self) -> None:
        self._pad: Optional[Any] = None
        self._buttons: Dict[str, int] = {}
        self._state: Optional[GamepadState] = None
        self._notes: List[str] = []

    def prepare(self, *, window_title: Optional[str] = None) -> None:
        info = capability()
        if not info.get("ok"):
            raise BackendUnavailable(
                "this vgamepad build is missing XInput button constants: %s"
                % ", ".join(info.get("missing_constants") or []),
                hint='upgrade it: py -m pip install -U "vgamepad>=0.1.0"',
            )
        table = vg.XUSB_BUTTON
        missing = [name for name in GAMEPAD_BUTTONS if not hasattr(table, BUTTON_NAMES[name])]
        if missing:
            raise BackendUnavailable(
                "vgamepad cannot express these buttons: %s" % ", ".join(missing)
            )
        self._buttons = {name: getattr(table, BUTTON_NAMES[name]) for name in GAMEPAD_BUTTONS}
        try:  # pragma: no cover - opens a ViGEmBus device
            self._pad = vg.VX360Gamepad()
        except Exception as exc:  # pragma: no cover
            raise BackendUnavailable(
                "could not create the virtual Xbox 360 pad: %s" % exc,
                hint=(
                    "install ViGEmBus 1.22+ (it is archived upstream - verify the driver before "
                    "relying on it), then retry; the game must not be running with an exclusive "
                    "controller claim"
                ),
            ) from exc
        self._notes.append(
            "virtual Xbox 360 pad through ViGEmBus: the game must accept XInput, and the "
            "DualSense triggers/buttons are mapped onto it"
        )
        self._write(NEUTRAL_STATE)

    def _write(self, state: GamepadState) -> None:
        assert self._pad is not None
        pad = self._pad
        pad.left_joystick(
            x_value=int(round(state.lx * _THUMB_MAX)), y_value=int(round(-state.ly * _THUMB_MAX))
        )
        pad.right_joystick(
            x_value=int(round(state.rx * _THUMB_MAX)), y_value=int(round(-state.ry * _THUMB_MAX))
        )
        pad.left_trigger(value=int(round(state.l2 * _TRIGGER_MAX)))
        pad.right_trigger(value=int(round(state.r2 * _TRIGGER_MAX)))
        held = set(state.buttons)
        for name in GAMEPAD_BUTTONS:
            button = self._buttons[name]
            if name in held:
                pad.press_button(button=button)
            else:
                pad.release_button(button=button)
        pad.update()
        self._state = state

    def set_state(self, state: GamepadState) -> None:
        if self._pad is None:
            raise RuntimeError("prepare() was not called")
        self._write(state)

    def neutral(self) -> None:
        if self._pad is None:
            return
        self._write(NEUTRAL_STATE)

    def teardown(self) -> None:
        if self._pad is None:
            return
        try:  # pragma: no cover - driver call
            self._write(NEUTRAL_STATE)
        except Exception:
            pass
        try:  # pragma: no cover - driver call
            reset = getattr(self._pad, "reset", None)
            if callable(reset):
                reset()
        except Exception:
            pass
        self._pad = None

    def notes(self) -> List[str]:
        return list(self._notes)

    def capabilities(self) -> Dict[str, Any]:
        return capability()


BACKEND = WindowsGamepadBackend
