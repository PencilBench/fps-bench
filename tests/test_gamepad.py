"""Gamepad state format, SDL mapping and the macOS CoreHID helper boundary.

The interesting part is *not* that a controller reports numbers; it is that a
number means the same thing when it is recorded as when it is replayed.  These
tests pin the normalisation (sticks, triggers, button vocabulary), the rejection
of anything outside the format, and the two adapters that are most likely to be
wrong: SDL GameController name mapping and the macOS helper protocol.
"""

from __future__ import annotations

import platform
import stat
import time
import types

import pytest

from fpsbench.backends import _sdl_gamepad
from fpsbench.backends.base import BackendUnavailable
from fpsbench.core.gamepad import (
    CONTROLLER_AXES,
    GAMEPAD_BUTTONS,
    GamepadError,
    GamepadState,
    NEUTRAL_STATE,
    state_from_controller,
)

# --------------------------------------------------------------------------- #
# state model
# --------------------------------------------------------------------------- #
def test_neutral_state_is_all_zero_and_named_buttons_are_ordered():
    state = GamepadState(buttons=("rightshoulder", "a", "a"))
    assert state.buttons == ("a", "rightshoulder")
    assert not state.is_neutral
    assert NEUTRAL_STATE.is_neutral


def test_sticks_and_triggers_are_normalised_once_and_rounded():
    state = GamepadState(lx=1.0, ly=-1.0, l2=0.1234567, r2=1.0)
    assert state.lx == 1.0
    assert state.ly == -1.0
    assert state.l2 == 0.12346  # 5 decimals: int16 resolution
    assert state.r2 == 1.0


def test_controller_reader_clamps_values_a_driver_reports_outside_the_range():
    axis, button = _reader({"leftx": 1.0000004, "lefty": -1.0000004})
    state = state_from_controller(axis, button)
    assert state.lx == 1.0
    assert state.ly == -1.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lx": 1.5},
        {"ly": -1.2},
        {"l2": -0.1},
        {"r2": 1.1},
        {"lx": float("nan")},
        {"ly": float("inf")},
        {"lx": True},
        {"buttons": ("trigger",)},
        {"buttons": "a"},
    ],
)
def test_values_outside_the_format_are_rejected(kwargs):
    with pytest.raises(GamepadError):
        GamepadState(**kwargs)


def test_state_roundtrip_keeps_the_whole_snapshot():
    state = GamepadState(lx=0.5, ly=-0.25, rx=-1.0, ry=0.75, l2=1.0, r2=0.5, buttons=("b", "dpup"))
    payload = state.to_dict()
    assert payload["buttons"] == ["b", "dpup"]
    assert GamepadState.from_dict(payload, "test") == state


def test_state_decoding_rejects_missing_and_unknown_fields():
    payload = NEUTRAL_STATE.to_dict()
    del payload["r2"]
    with pytest.raises(GamepadError) as excinfo:
        GamepadState.from_dict(payload, "test")
    assert "missing" in str(excinfo.value)

    payload = NEUTRAL_STATE.to_dict()
    payload["gyro"] = 1.0
    with pytest.raises(GamepadError) as excinfo:
        GamepadState.from_dict(payload, "test")
    assert "unknown gamepad field" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# SDL GameController -> state
# --------------------------------------------------------------------------- #
def _reader(axes=None, buttons=()):
    axes = dict(axes or {})
    held = set(buttons)
    return (lambda name: axes.get(name, 0.0)), (lambda name: name in held)


def test_normalised_triggers_are_taken_as_they_are():
    axis, button = _reader({"triggerleft": 0.0, "triggerright": 1.0})
    state = state_from_controller(axis, button)
    assert state.l2 == 0.0
    assert state.r2 == 1.0


def test_sticks_and_buttons_come_through_unchanged():
    axis, button = _reader(
        {"leftx": 0.5, "lefty": -0.5, "rightx": -1.0, "righty": 1.0}, buttons=("a", "dpleft")
    )
    state = state_from_controller(axis, button)
    assert (state.lx, state.ly, state.rx, state.ry) == (0.5, -0.5, -1.0, 1.0)
    assert state.buttons == ("a", "dpleft")


class _FakeController:
    """SDL-shaped reader: get_axis/get_button are addressed by enum index."""

    def __init__(self, axes=None, pressed=()):
        self.axes = dict(axes or {})
        self.pressed = set(pressed)

    def get_axis(self, axis_id):
        return self.axes.get(axis_id, 0)

    def get_button(self, button_id):
        return 1 if button_id in self.pressed else 0


def test_controller_adapter_uses_sdl_enum_indices_and_normalises_int16():
    """pygame exposes no SDL names, and get_axis returns raw int16 values."""
    controller = _FakeController(
        axes={
            0: 32767,  # leftx fully right
            1: -32767,  # lefty fully up
            2: 16384,  # rightx half right
            4: 0,  # left trigger at rest
            5: 32767,  # right trigger pressed
        },
        pressed=(0, 11),  # a, dpup
    )
    adapter = _sdl_gamepad.ControllerAdapter(types.SimpleNamespace(), controller)
    state = adapter.state()
    assert (state.lx, state.ly) == (1.0, -1.0)
    assert state.rx == pytest.approx(0.5, abs=1e-3)
    assert (state.l2, state.r2) == (0.0, 1.0)
    assert state.buttons == ("a", "dpup")


def test_controller_adapter_prefers_constants_the_binding_exports():
    module = types.SimpleNamespace()
    for index, name in enumerate(CONTROLLER_AXES):
        setattr(module, "CONTROLLER_AXIS_" + name.upper(), index + 10)
    for index, name in enumerate(GAMEPAD_BUTTONS):
        setattr(module, "CONTROLLER_BUTTON_" + name.upper(), index + 100)
    controller = _FakeController(axes={10: 32767}, pressed=(100,))
    adapter = _sdl_gamepad.ControllerAdapter(module, controller)
    assert adapter.axis("leftx") == 1.0
    assert adapter.button("a") is True
    assert adapter.button("b") is False


# --------------------------------------------------------------------------- #
# macOS CoreHID helper boundary
# --------------------------------------------------------------------------- #
HELPER_SCRIPT = """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    reply = {"ok": True, "device": "Fake HID gamepad", "seen": message.get("cmd")}
    print(json.dumps(reply), flush=True)
"""


def _write_helper(tmp_path):
    path = tmp_path / "helper.py"
    path.write_text(HELPER_SCRIPT)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS helper path")
def test_macos_helper_client_sends_full_states(tmp_path, monkeypatch):
    from fpsbench.backends.macos import gamepad as mac_gamepad

    helper = _write_helper(tmp_path)
    monkeypatch.setenv(mac_gamepad.HELPER_ENV, helper)
    monkeypatch.setattr(mac_gamepad, "_macos_major", lambda: mac_gamepad.MIN_MACOS_MAJOR)

    backend = mac_gamepad.MacGamepadBackend(helper=helper)
    backend.prepare()
    assert "Fake HID gamepad" in backend.notes()[-1]
    backend.set_state(GamepadState(lx=1.0, buttons=("x",)))
    backend.neutral()
    assert backend.notes()
    backend.teardown()


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS helper path")
def test_macos_without_a_helper_refuses_instead_of_degrading(monkeypatch):
    from fpsbench.backends.macos import gamepad as mac_gamepad

    monkeypatch.delenv(mac_gamepad.HELPER_ENV, raising=False)
    monkeypatch.setattr(mac_gamepad, "_macos_major", lambda: 12)  # below CoreHID
    info = mac_gamepad.capability()
    assert info["ok"] is False
    assert "macOS" in info["reason"]
    with pytest.raises(BackendUnavailable) as excinfo:
        mac_gamepad.MacGamepadBackend().prepare()
    assert "blocked" in str(excinfo.value)
    assert excinfo.value.hint


def test_macos_helper_refuses_a_missing_binary():
    from fpsbench.backends.macos import gamepad as mac_gamepad

    with pytest.raises(BackendUnavailable):
        mac_gamepad.MacGamepadBackend(helper="/nonexistent/helper").prepare()


# --------------------------------------------------------------------------- #
# SDL recorder lifecycle (fake SDL, no hardware)
# --------------------------------------------------------------------------- #
class _FakePygame:
    class joystick:
        devices = ["DualSense Edge Wireless Controller", "Xbox Wireless Controller"]

        @classmethod
        def init(cls):
            return None

        @classmethod
        def get_count(cls):
            return len(cls.devices)

        @classmethod
        def Joystick(cls, index):
            class _J:
                def init(self):
                    return None

                def get_name(self):
                    return _FakePygame.joystick.devices[index]

            return _J()


class _FakeControllerModule:
    CONTROLLER_AXIS_LEFTX = 0
    CONTROLLER_AXIS_LEFTY = 1
    CONTROLLER_AXIS_RIGHTX = 2
    CONTROLLER_AXIS_RIGHTY = 3
    CONTROLLER_AXIS_TRIGGERLEFT = 4
    CONTROLLER_AXIS_TRIGGERRIGHT = 5

    def __init__(self):
        for index, name in enumerate(GAMEPAD_BUTTONS):
            setattr(self, "CONTROLLER_BUTTON_" + name.upper(), index)
        self.opened = []
        self.updates = 0

    def init(self):
        return None

    def get_count(self):
        return len(_FakePygame.joystick.devices)

    def Controller(self, index):
        self.opened.append(index)
        return self._controller

    def update(self):
        self.updates += 1

    _controller = None


def _fake_sdl(monkeypatch):
    controller = _FakeController(axes={0: 0, 1: 0})
    module = _FakeControllerModule()
    module._controller = controller
    monkeypatch.setattr(_sdl_gamepad, "_load_pygame", lambda: _FakePygame)
    monkeypatch.setattr(_sdl_gamepad, "_controller_module", lambda pygame: module)
    return controller, module


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_sdl_recorder_records_the_initial_state_and_each_change(monkeypatch):
    controller, module = _fake_sdl(monkeypatch)
    recorder = _sdl_gamepad.SdlGamepadRecorder(poll_hz=200)
    recorder.prepare()
    assert recorder.device_info()["device"] == "DualSense Edge Wireless Controller"
    events = []

    def drain(minimum):
        events.extend(recorder.poll())
        return len(events) >= minimum

    recorder.start()
    try:
        assert _wait_for(lambda: drain(1))  # the initial full state
        controller.axes[0] = 32767
        assert _wait_for(lambda: drain(2))  # exactly one change
    finally:
        recorder.stop()
    events.extend(recorder.poll())
    assert events[0].state.is_neutral
    assert [event.state.lx for event in events] == [0.0, 1.0]
    assert recorder.end_ms() is not None and recorder.end_ms() >= 0
    assert module.updates > 0


def test_sdl_recorder_can_select_a_device_by_name(monkeypatch):
    controller, module = _fake_sdl(monkeypatch)
    recorder = _sdl_gamepad.SdlGamepadRecorder(poll_hz=200)
    recorder.prepare(device="xbox")
    assert module.opened == [1]
    assert recorder.device_info()["device"] == "Xbox Wireless Controller"


def test_sdl_recorder_reports_an_unknown_device(monkeypatch):
    _fake_sdl(monkeypatch)
    recorder = _sdl_gamepad.SdlGamepadRecorder()
    with pytest.raises(BackendUnavailable) as excinfo:
        recorder.prepare(device="steam deck")
    assert "DualSense Edge" in str(excinfo.value)
