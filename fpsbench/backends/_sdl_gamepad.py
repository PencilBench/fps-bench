"""SDL2 gamepad reading (shared by both platforms).

A gamepad is read through SDL's **GameController** API rather than through raw
HID reports: SDL already knows the button/axis layout of the common controllers
(DualSense, DualShock 4, Xbox), it applies that mapping once, and the same code
runs on macOS and Windows.  Raw HID report parsing is deliberately out of scope -
a wrong descriptor would silently swap two axes, which is exactly the kind of
error a trajectory comparison cannot afford.

``pygame`` is used as the SDL binding (an optional dependency, ``.[gamepad]``).
The module is imported lazily so the tool keeps working without it.

Two behaviours of that API are part of the contract here, because getting them
wrong is silent:

* ``Controller.get_axis`` returns the **raw int16** value SDL reports
  (``-32768..32767``; trigger axes rest at ``0`` and go up to ``32767``).  The
  adapter normalises once - sticks by ``value / 32767``, triggers to ``0..1`` -
  and records what it did in the trace metadata;
* pygame does not re-export the SDL axis/button enums, so the adapter uses the
  documented ``SDL_GameControllerAxis``/``Button`` values and *prefers* constants
  from the binding when it has them.

Reading runs on its own thread: the game keeps the foreground and the poller just
drains state, so recording never blocks the game.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional

from ..core.gamepad import (
    CONTROLLER_AXES,
    GAMEPAD_BUTTONS,
    GamepadState,
    device_metadata,
    state_from_controller,
)
from ..core.trace import TraceEvent
from .base import BackendUnavailable, Check, GamepadRecorderBackend

__all__ = [
    "DEFAULT_POLL_HZ",
    "SdlGamepadRecorder",
    "ControllerAdapter",
    "probe",
    "BACKEND",
]

#: 250 Hz is well above any game's input poll rate, and cheap (a few axis reads).
DEFAULT_POLL_HZ = 250.0

#: SDL_GameControllerGetAxis returns Sint16, so this is full deflection.
_AXIS_MAX = 32767.0
#: SDL reports trigger axes as 0 (rest) .. 32767 (fully pressed).
_TRIGGER_REST = 0.0

#: SDL's mapping keys differ from its enum names for the triggers.
_MAPPING_KEYS = {"triggerleft": "lefttrigger", "triggerright": "righttrigger"}

#: SDL_GameControllerAxis values (ABI stable; pygame exposes no names).
_AXIS_IDS = {"leftx": 0, "lefty": 1, "rightx": 2, "righty": 3, "triggerleft": 4, "triggerright": 5}

#: SDL_GameControllerButton values (A=0 .. DPAD_RIGHT=14; touchpads/paddles are
#: outside the v1 button vocabulary).
_BUTTON_IDS = {
    "a": 0,
    "b": 1,
    "x": 2,
    "y": 3,
    "back": 4,
    "guide": 5,
    "start": 6,
    "leftstick": 7,
    "rightstick": 8,
    "leftshoulder": 9,
    "rightshoulder": 10,
    "dpup": 11,
    "dpdown": 12,
    "dpleft": 13,
    "dpright": 14,
}

_INSTALL_HINT = 'python3 -m pip install -e ".[gamepad]"'


def _load_pygame():
    # The banner pygame prints on import would land in the middle of the CLI output.
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    try:
        import pygame  # noqa: PLC0415 - optional dependency, imported on demand
    except ImportError as exc:
        raise BackendUnavailable(
            "pygame (SDL2) is needed to read a gamepad: %s" % exc, hint=_INSTALL_HINT
        ) from exc
    return pygame


def _controller_module(pygame):
    try:
        from pygame._sdl2 import controller  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise BackendUnavailable(
            "this pygame build has no SDL GameController API (pygame._sdl2.controller): %s" % exc,
            hint='upgrade it: python3 -m pip install -U "pygame>=2.1"',
        ) from exc
    return controller


def _constant(module: Any, name: str, fallback: int) -> int:
    """The binding's own constant when it has one, else the SDL enum value."""
    value = getattr(module, name, None)
    return fallback if value is None else int(value)


class ControllerAdapter:
    """Maps SDL GameController names onto one opened controller.

    Kept separate from pygame so the mapping (enum ids, int16 normalisation) is
    testable with a fake controller - the part most likely to be wrong is the
    numerics, not the OS binding.
    """

    def __init__(self, module: Any, controller: Any) -> None:
        self._controller = controller
        self._axes = {
            name: _constant(module, "CONTROLLER_AXIS_" + name.upper(), _AXIS_IDS[name])
            for name in CONTROLLER_AXES
        }
        self._buttons = {
            name: _constant(module, "CONTROLLER_BUTTON_" + name.upper(), _BUTTON_IDS[name])
            for name in GAMEPAD_BUTTONS
        }

    def axis(self, name: str) -> float:
        """Normalised axis value (sticks ``-1..1`` with y down, triggers ``0..1``)."""
        value = float(self._controller.get_axis(self._axes[name])) / _AXIS_MAX
        if name in ("triggerleft", "triggerright"):
            return min(1.0, max(0.0, value))
        return min(1.0, max(-1.0, value))

    def button(self, name: str) -> bool:
        return bool(self._controller.get_button(self._buttons[name]))

    def state(self) -> GamepadState:
        return state_from_controller(self.axis, self.button)


def _init_subsystems(pygame) -> List[str]:
    """Initialise the SDL subsystems the poller needs - and nothing else.

    Deliberately *no* video subsystem: ``SDL_PumpEvents`` (pygame's ``event.pump``)
    has to run on the main thread on macOS and aborts the process otherwise, so
    the poller refreshes state with ``SDL_GameControllerUpdate`` instead, which
    touches the joystick devices only.
    """
    try:
        pygame.joystick.init()
    except Exception as exc:  # pragma: no cover - depends on the SDL build
        raise BackendUnavailable("cannot initialise SDL joystick support: %s" % exc) from exc
    return []


def _device_names(pygame) -> List[str]:
    names: List[str] = []
    for index in range(pygame.joystick.get_count()):
        try:
            joystick = pygame.joystick.Joystick(index)
            joystick.init()
            names.append(str(joystick.get_name()))
        except Exception:  # pragma: no cover - a joystick can disappear mid-enumeration
            names.append("controller %d" % index)
    return names


def _open(pygame, device: Optional[str]):
    controller_module = _controller_module(pygame)
    try:
        controller_module.init()
        count = int(controller_module.get_count())
    except Exception as exc:  # pragma: no cover - SDL failure
        raise BackendUnavailable("SDL could not enumerate game controllers: %s" % exc) from exc
    if count <= 0:
        raise BackendUnavailable(
            "no gamepad is visible to SDL",
            hint=(
                "connect the controller (USB or Bluetooth), make sure no other application "
                "grabbed it exclusively, and check `fpsbench probe-gamepad`"
            ),
        )
    names = _device_names(pygame)
    index = 0
    if device:
        wanted = device.strip().lower()
        matches = [position for position, name in enumerate(names) if wanted in name.lower()]
        if not matches:
            raise BackendUnavailable(
                "no connected gamepad matches %r (found: %s)"
                % (device, ", ".join(names) or "none"),
                hint="run `fpsbench probe-gamepad` to list the connected devices",
            )
        index = matches[0]
    try:
        opened = controller_module.Controller(index)
    except Exception as exc:  # pragma: no cover - SDL failure
        raise BackendUnavailable(
            "SDL could not open gamepad %d (%s): %s"
            % (index, names[index] if index < len(names) else "?", exc)
        ) from exc
    name = names[index] if index < len(names) else "controller %d" % index
    return opened, name, names


def _read_mapping(controller: Any) -> Dict[str, str]:
    """The SDL mapping SDL itself applied ("lefttrigger": "a4", "a": "b0", ...)."""
    getter = getattr(controller, "get_mapping", None)
    try:
        mapping = getter() if callable(getter) else None
    except Exception:  # pragma: no cover - an SDL failure must not break recording
        return {}
    if not isinstance(mapping, dict):
        return {}
    return {str(key): str(value) for key, value in mapping.items()}


def probe() -> List[Check]:
    """``doctor`` check: can this machine *read* a gamepad at all?"""
    try:
        pygame = _load_pygame()
    except BackendUnavailable as exc:
        return [Check("SDL gamepad reading", False, str(exc), fix=exc.hint)]
    try:
        _controller_module(pygame)
    except BackendUnavailable as exc:
        return [Check("SDL gamepad reading", False, str(exc), fix=exc.hint)]
    try:
        pygame.joystick.init()
        names = _device_names(pygame)
    except Exception as exc:  # pragma: no cover - SDL failure
        return [Check("SDL gamepad reading", False, "%s: %s" % (type(exc).__name__, exc))]
    if not names:
        return [
            Check(
                "SDL gamepad reading",
                False,
                "SDL is installed but sees no controller (connect it, then retry)",
            )
        ]
    return [Check("SDL gamepad reading", True, "SDL GameController: %s" % ", ".join(names))]


class SdlGamepadRecorder(GamepadRecorderBackend):
    """Polls a real controller and records a ``gamepad`` event on every change."""

    name = "SDL2 gamepad"

    def __init__(self, *, clock_ns=time.perf_counter_ns, poll_hz: float = DEFAULT_POLL_HZ) -> None:
        self._clock_ns = clock_ns
        self._poll_hz = float(poll_hz)
        self._pygame = None
        self._controller_module = None
        self._adapter: Optional[ControllerAdapter] = None
        self._controller = None
        self._device_name = ""
        self._mapping: Dict[str, str] = {}
        self._notes: List[str] = []
        self._events: List[TraceEvent] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._t0_ns: Optional[int] = None
        self._end_ms: Optional[float] = None
        self._error: Optional[BaseException] = None
        self._last: Optional[GamepadState] = None
        self._changes = 0

    # -- lifecycle --------------------------------------------------------- #
    def prepare(self, *, device: Optional[str] = None, poll_hz: Optional[float] = None) -> None:
        if poll_hz:
            if poll_hz <= 0:
                raise BackendUnavailable("gamepad poll rate must be > 0, got %r" % (poll_hz,))
            self._poll_hz = float(poll_hz)
        self._pygame = _load_pygame()
        self._notes.extend(_init_subsystems(self._pygame))
        controller_module = _controller_module(self._pygame)
        self._controller_module = controller_module
        opened, name, names = _open(self._pygame, device)
        self._controller = opened
        self._device_name = name
        self._adapter = ControllerAdapter(controller_module, opened)
        self._mapping = _read_mapping(opened)
        if len(names) > 1:
            self._notes.append(
                "several gamepads are connected (%s); recording %r - pass --gamepad-device "
                "to choose another" % (", ".join(names), name)
            )

    def start(self) -> None:
        self._events = []
        self._stop.clear()
        self._error = None
        self._end_ms = None
        self._last = None
        self._changes = 0
        self._t0_ns = self._clock_ns()
        self._thread = threading.Thread(target=self._run, name="fpsbench-gamepad", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        interval = 1.0 / self._poll_hz
        assert self._adapter is not None
        assert self._controller_module is not None
        try:
            while not self._stop.is_set():
                try:
                    self._controller_module.update()
                except Exception as exc:  # pragma: no cover - SDL failure mid-run
                    self._notes.append("SDL controller update failed: %s" % exc)
                    break
                state = self._adapter.state()
                if state != self._last:
                    self._record(state)
                time.sleep(interval)
        except BaseException as exc:  # keep the failure for the report
            self._error = exc

    def _record(self, state: GamepadState) -> None:
        t0 = self._t0_ns if self._t0_ns is not None else self._clock_ns()
        t_ms = round((self._clock_ns() - t0) / 1e6, 3)
        with self._lock:
            self._events.append(TraceEvent(kind="gamepad", t_ms=t_ms, state=state))
        self._last = state
        self._changes += 1

    def poll(self) -> List[TraceEvent]:
        with self._lock:
            events, self._events = self._events, []
        return events

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._t0_ns is not None:
            self._end_ms = round((self._clock_ns() - self._t0_ns) / 1e6, 3)
        if self._error is not None:
            self._notes.append("gamepad sampling stopped: %s" % self._error)

    def end_ms(self) -> Optional[float]:
        return self._end_ms

    def device_info(self) -> Dict[str, Any]:
        return device_metadata(
            device=self._device_name or "unknown",
            source=self.name,
            axes={
                "source": "sdl-gamecontroller (int16)",
                "raw_range": [-32768, 32767],
                "range": [-1.0, 1.0],
                "y": "down",
                "normalisation": "value / 32767",
            },
            triggers={
                "source": "sdl-gamecontroller (int16)",
                "raw_range": [0, 32767],
                "range": [0.0, 1.0],
                "rest": _TRIGGER_REST,
                "normalisation": "value / 32767",
            },
            poll_hz=self._poll_hz,
            extra={
                "recorded_changes": self._changes,
                # Only the controls the format can carry; the rest (touchpad,
                # paddles, misc) are visible in the mapping but are not recorded.
                "mapping": {
                    name: self._mapping[_MAPPING_KEYS.get(name, name)]
                    for name in CONTROLLER_AXES + GAMEPAD_BUTTONS
                    if _MAPPING_KEYS.get(name, name) in self._mapping
                },
            },
        )

    def notes(self) -> List[str]:
        return list(self._notes)


BACKEND = SdlGamepadRecorder
