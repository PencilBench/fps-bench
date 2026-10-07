"""Gamepad state: the vocabulary a trace, a recorder and a virtual device share.

Scope is deliberately small - two sticks, two analog triggers, the d-pad and the
ordinary buttons.  Touchpad coordinates, gyro, rumble and adaptive triggers are
not part of the format, so nothing can quietly pretend to carry them (a DS4
touchpad click is therefore not recorded either: it has no canonical name yet).

Values are normalised **once**, when they are recorded:

* sticks are ``-1.0 .. 1.0`` using SDL's sign convention (x to the right, y
  downwards - the platform backends do the sign flip their API needs);
* triggers are ``0.0 .. 1.0`` (rest is 0).

The source range and the exact mapping are stored next to the recording
(``trace.metadata["gamepad"]``) so a replay can be audited instead of guessed.
A state is a **full snapshot**: a change replaces the whole state - nothing is
accumulated, interpolated, smoothed or dead-zoned, which is the difference
between a gamepad and the relative mouse deltas in the same trace.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

__all__ = [
    "GAMEPAD_BUTTONS",
    "GAMEPAD_BUTTON_SET",
    "CONTROLLER_AXES",
    "STICK_LIMIT",
    "TRIGGER_MAX",
    "STATE_DIGITS",
    "GamepadError",
    "GamepadState",
    "NEUTRAL_STATE",
    "state_from_controller",
]

#: Canonical button names (SDL's GameController vocabulary, in report order).
#: Analog triggers are ``l2``/``r2``; their digital duplicates are not recorded.
GAMEPAD_BUTTONS: Tuple[str, ...] = (
    "a",
    "b",
    "x",
    "y",
    "leftshoulder",
    "rightshoulder",
    "dpup",
    "dpdown",
    "dpleft",
    "dpright",
    "back",
    "start",
    "guide",
    "leftstick",
    "rightstick",
)
GAMEPAD_BUTTON_SET = frozenset(GAMEPAD_BUTTONS)
_BUTTON_ORDER = {name: index for index, name in enumerate(GAMEPAD_BUTTONS)}

#: Axis names a controller-like reader is asked for (SDL GameController order).
CONTROLLER_AXES: Tuple[str, ...] = (
    "leftx",
    "lefty",
    "rightx",
    "righty",
    "triggerleft",
    "triggerright",
)

STICK_LIMIT = 1.0
TRIGGER_MAX = 1.0
#: Decimal places kept when serialising: int16 axis resolution is ~3.05e-5.
STATE_DIGITS = 5


class GamepadError(ValueError):
    """Raised when a gamepad state violates the format."""


def _finite(value: Any, where: str, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GamepadError("%s: %s must be a number" % (where, what))
    number = float(value)
    if not math.isfinite(number):
        raise GamepadError("%s: %s must be finite, got %r" % (where, what, value))
    return number


def _stick(value: Any, where: str, what: str) -> float:
    number = _finite(value, where, what)
    if number < -STICK_LIMIT or number > STICK_LIMIT:
        raise GamepadError("%s: %s must be within -1..1, got %r" % (where, what, value))
    return round(number, STATE_DIGITS)


def _trigger(value: Any, where: str, what: str) -> float:
    number = _finite(value, where, what)
    if number < 0.0 or number > TRIGGER_MAX:
        raise GamepadError("%s: %s must be within 0..1, got %r" % (where, what, value))
    return round(number, STATE_DIGITS)


def _buttons(value: Iterable[str], where: str) -> Tuple[str, ...]:
    if isinstance(value, str):
        raise GamepadError("%s: buttons must be an array of names" % where)
    seen = set()
    for item in value:
        if not isinstance(item, str):
            raise GamepadError("%s: button names must be strings" % where)
        name = item.strip().lower()
        if name not in GAMEPAD_BUTTON_SET:
            raise GamepadError(
                "%s: unknown gamepad button %r (expected one of %s)" % (where, item, GAMEPAD_BUTTONS)
            )
        seen.add(name)
    return tuple(sorted(seen, key=_BUTTON_ORDER.__getitem__))


@dataclass(frozen=True)
class GamepadState:
    """One complete controller state (see the module docstring for the ranges)."""

    lx: float = 0.0
    ly: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    l2: float = 0.0
    r2: float = 0.0
    buttons: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        where = "gamepad state"
        for name in ("lx", "ly", "rx", "ry"):
            object.__setattr__(self, name, _stick(getattr(self, name), where, name))
        for name in ("l2", "r2"):
            object.__setattr__(self, name, _trigger(getattr(self, name), where, name))
        object.__setattr__(self, "buttons", _buttons(self.buttons, where))

    @property
    def is_neutral(self) -> bool:
        """True when every axis is centred, both triggers rest and no button is held."""
        return not (self.lx or self.ly or self.rx or self.ry or self.l2 or self.r2 or self.buttons)

    def moved(self, other: "GamepadState") -> bool:
        """True when ``other`` differs in any field (no tolerance, no dead zone)."""
        return self != other

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "lx": self.lx,
            "ly": self.ly,
            "rx": self.rx,
            "ry": self.ry,
            "l2": self.l2,
            "r2": self.r2,
        }
        if self.buttons:
            out["buttons"] = list(self.buttons)
        return out

    @classmethod
    def from_dict(cls, data: Any, where: str) -> "GamepadState":
        if not isinstance(data, dict):
            raise GamepadError("%s: gamepad state must be an object" % where)
        allowed = {"lx", "ly", "rx", "ry", "l2", "r2", "buttons"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise GamepadError("%s: unknown gamepad field %r" % (where, unknown[0]))
        missing = sorted(allowed - set(data) - {"buttons"})
        if missing:
            raise GamepadError("%s: gamepad state is missing %r" % (where, missing[0]))
        return cls(
            lx=_stick(data["lx"], where, "lx"),
            ly=_stick(data["ly"], where, "ly"),
            rx=_stick(data["rx"], where, "rx"),
            ry=_stick(data["ry"], where, "ry"),
            l2=_trigger(data["l2"], where, "l2"),
            r2=_trigger(data["r2"], where, "r2"),
            buttons=_buttons(data.get("buttons") or [], where),
        )

    def describe(self) -> str:
        held = ",".join(self.buttons) or "-"
        return "lx=%+.2f ly=%+.2f rx=%+.2f ry=%+.2f l2=%.2f r2=%.2f [%s]" % (
            self.lx,
            self.ly,
            self.rx,
            self.ry,
            self.l2,
            self.r2,
            held,
        )


NEUTRAL_STATE = GamepadState()


def state_from_controller(
    read_axis: Callable[[str], float],
    read_button: Callable[[str], bool],
) -> GamepadState:
    """Build a :class:`GamepadState` from a GameController-style reader.

    ``read_axis`` must already be normalised (sticks ``-1..1`` with y down,
    triggers ``0..1``): the SDL adapter divides the int16 values SDL reports by
    32767, and unit tests pass plain callables, so the mapping itself is testable
    without a controller attached and without guessing a driver's convention.
    """

    def axis(name: str) -> float:
        value = read_axis(name)
        if value is None:
            return 0.0
        # A driver may report a hair outside the nominal range; clamp here, once,
        # so the recorded value is always inside the format.
        return min(1.0, max(-1.0, float(value)))

    def trigger(name: str) -> float:
        value = read_axis(name)
        if value is None:
            return 0.0
        return min(1.0, max(0.0, float(value)))

    return GamepadState(
        lx=axis("leftx"),
        ly=axis("lefty"),
        rx=axis("rightx"),
        ry=axis("righty"),
        l2=trigger("triggerleft"),
        r2=trigger("triggerright"),
        buttons=tuple(name for name in GAMEPAD_BUTTONS if read_button(name)),
    )


def device_metadata(
    *,
    device: str,
    source: str,
    axes: Optional[Dict[str, Any]] = None,
    triggers: Optional[Dict[str, Any]] = None,
    poll_hz: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Trace metadata describing how a gamepad recording was produced."""
    out: Dict[str, Any] = {
        "device": device,
        "source": source,
        "axes": dict(axes or {"range": [-1.0, 1.0], "y": "down"}),
        "triggers": dict(triggers or {"range": [0.0, 1.0]}),
    }
    if poll_hz:
        out["poll_hz"] = float(poll_hz)
    if extra:
        out.update(extra)
    return out
