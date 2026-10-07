"""Cross-platform core tests: key codes and the trace codec."""

from __future__ import annotations

import json

import pytest

from fpsbench.core import keycodes, trace
from fpsbench.core.keycodes import KeycodeError
from fpsbench.core.trace import Trace, TraceError, TraceEvent

EVENTS = [
    {"t_ms": 0, "kind": "key", "key": "w", "down": True, "native": {"vk": 13}, "mods": []},
    {"t_ms": 100, "kind": "look", "dx": 40, "dy": -3},
    {"t_ms": 150, "kind": "look", "dx": 60, "dy": 0, "mods": ["shift"]},
    {"t_ms": 200, "kind": "scroll", "dy": 2, "dx": 1},
    {"t_ms": 250, "kind": "button", "button": "left", "down": True},
    {"t_ms": 300, "kind": "button", "button": "left", "down": False},
    {"t_ms": 400, "kind": "label", "text": "cliff"},
    {"t_ms": 4000, "kind": "key", "key": "w", "down": False, "native": {"vk": 13}, "mods": []},
]


def make_trace(events=EVENTS, **kwargs):
    payload = {
        "format": "trace",
        "schema_version": 1,
        "name": kwargs.pop("name", "unit"),
        "created": kwargs.pop("created", "2026-10-06T00:00:00Z"),
        "origin_platform": kwargs.pop("origin_platform", "macos"),
        "metadata": kwargs.pop("metadata", {"display_hz": 120}),
        "events": events,
    }
    payload.update(kwargs)
    return Trace.from_dict(payload)


# --------------------------------------------------------------------------- #
# keycodes
# --------------------------------------------------------------------------- #
def test_canonical_names_normalise_aliases():
    assert keycodes.normalize("W") == "w"
    assert keycodes.normalize("Control") == "ctrl"
    assert keycodes.normalize("Escape") == "esc"
    assert keycodes.normalize("option") == "alt"
    assert keycodes.normalize("windows") == "cmd"
    assert keycodes.normalize("Keypad5") == "kp5"
    assert keycodes.normalize("shift_left") == "shift"


def test_unknown_key_names_are_rejected():
    with pytest.raises(KeycodeError):
        keycodes.normalize("frobnicate")
    with pytest.raises(KeycodeError):
        keycodes.normalize("")
    with pytest.raises(KeycodeError):
        keycodes.mac_vk("f24")  # no macOS mapping for f24
    with pytest.raises(KeycodeError):
        keycodes.mac_vk("pause")  # no pause key on macOS
    with pytest.raises(KeycodeError):
        keycodes.win_scancode("frobnicate")


def test_platform_tables_agree_on_the_common_vocabulary():
    both = set(keycodes.MAC_VK) & set(keycodes.WIN_SCANCODE)
    # Every canonical name that both platforms implement must round-trip.
    for name in sorted(both):
        assert keycodes.canonical_for_mac_vk(keycodes.MAC_VK[name]) is not None
        code, extended = keycodes.win_scancode(name)
        assert keycodes.canonical_for_win_scancode(code, extended) is not None
    for name in ("w", "a", "s", "d", "shift", "space", "1", "esc", "f5", "up"):
        assert name in both


def test_reverse_lookup_prefers_the_unambiguous_name():
    assert keycodes.canonical_for_mac_vk(13) == "w"
    assert keycodes.canonical_for_mac_vk(60) == "shift"  # right shift, no forward mapping
    assert keycodes.canonical_for_win_scancode(0x11) == "w"
    assert keycodes.canonical_for_win_scancode(0x48, True) == "up"
    assert keycodes.canonical_for_win_scancode(999) is None


def test_native_code_payloads():
    assert keycodes.native_code("w", "macos") == {"vk": 13}
    assert keycodes.native_code("up", "windows") == {"scancode": 0x48, "ext": True}
    with pytest.raises(KeycodeError):
        keycodes.native_code("w", "linux")


# --------------------------------------------------------------------------- #
# trace codec
# --------------------------------------------------------------------------- #
def test_trace_roundtrip_is_lossless():
    original = make_trace()
    text = trace.dumps(original)
    restored = trace.loads(text)
    assert restored.to_dict() == original.to_dict()
    assert restored.counts() == {
        "key": 2,
        "look": 2,
        "scroll": 1,
        "button": 2,
        "label": 1,
        "gamepad": 0,
    }
    assert restored.duration_ms == 4000
    assert restored.labels() == [(400.0, "cliff")]
    assert restored.hash() == original.hash()


def test_trace_hash_covers_playback_content_only():
    a = make_trace(name="one")
    b = make_trace(name="two", metadata={"display_hz": 60})
    assert a.hash() == b.hash()
    shorter = make_trace(events=EVENTS[:-1])
    assert a.hash() != shorter.hash()


def test_unknown_fields_are_rejected_not_ignored():
    payload = json.loads(trace.dumps(make_trace()))
    payload["surprise"] = 1
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert "unknown field" in str(excinfo.value)

    payload = json.loads(trace.dumps(make_trace()))
    payload["events"][0]["extra"] = True
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert "unknown field 'extra'" in str(excinfo.value)


def test_missing_required_fields_are_rejected():
    payload = json.loads(trace.dumps(make_trace()))
    del payload["events"][0]["down"]
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert "missing 'down'" in str(excinfo.value)


def test_unknown_event_kind_is_rejected():
    payload = json.loads(trace.dumps(make_trace()))
    payload["events"][0]["kind"] = "telepathy"
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert "unknown event kind" in str(excinfo.value)


def test_schema_version_and_format_are_enforced():
    payload = json.loads(trace.dumps(make_trace()))
    payload["schema_version"] = 99
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert "schema_version" in str(excinfo.value)

    payload = json.loads(trace.dumps(make_trace()))
    payload["format"] = "route"
    with pytest.raises(TraceError):
        Trace.from_dict(payload)


def test_timestamps_must_be_monotonic():
    payload = json.loads(trace.dumps(make_trace()))
    payload["events"][1]["t_ms"] = -5
    with pytest.raises(TraceError):
        Trace.from_dict(payload)

    payload = json.loads(trace.dumps(make_trace()))
    payload["events"][0]["t_ms"] = 5000  # after the following event
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert "sorted by t_ms" in str(excinfo.value)


def test_booleans_are_not_accepted_as_numbers():
    payload = json.loads(trace.dumps(make_trace()))
    payload["events"][1]["dx"] = True
    with pytest.raises(TraceError):
        Trace.from_dict(payload)


def test_bad_key_name_is_rejected():
    payload = json.loads(trace.dumps(make_trace()))
    payload["events"][0]["key"] = "warp drive"
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert "unknown key name" in str(excinfo.value)


def test_key_names_are_normalised_on_load():
    payload = json.loads(trace.dumps(make_trace()))
    payload["events"][0]["key"] = "W"
    restored = Trace.from_dict(payload)
    assert restored.events[0].key == "w"


def test_unknown_modifier_is_rejected():
    payload = json.loads(trace.dumps(make_trace()))
    payload["events"][0]["mods"] = ["hyper"]
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert "unknown modifier" in str(excinfo.value)


def test_trace_survives_a_cross_platform_roundtrip():
    """A macOS trace must be replayable on Windows (and vice versa)."""
    mac_trace = make_trace(origin_platform="macos")
    payload = json.loads(trace.dumps(mac_trace))
    windows_trace = Trace.from_dict(payload)
    assert windows_trace.origin_platform == "macos"
    for event in windows_trace.events:
        if event.kind == "key":
            assert event.key is not None
            keycodes.win_scancode(event.key)  # resolvable on the replay platform
            keycodes.mac_vk(event.key)


def test_new_trace_sets_created_timestamp():
    created = trace.new_trace("x", [TraceEvent(kind="label", t_ms=0, text="hi")])
    assert created.created and created.created.endswith("Z")
    assert created.schema_version == trace.SCHEMA_VERSION


def test_save_and_load(tmp_path):
    path = tmp_path / "t.trace.json"
    original = make_trace()
    original.save(str(path))
    restored = trace.load(str(path))
    assert restored.to_dict() == original.to_dict()


def test_invalid_json_reports_a_clear_error():
    with pytest.raises(TraceError) as excinfo:
        trace.loads("{not json")
    assert "invalid JSON" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# gamepad state (schema 2)
# --------------------------------------------------------------------------- #
def gamepad_trace(*, end_ms=2000.0):
    from fpsbench.core.gamepad import GamepadState

    return Trace(
        name="pad",
        events=[
            TraceEvent(kind="gamepad", t_ms=0, state=GamepadState()),
            TraceEvent(
                kind="gamepad",
                t_ms=500,
                state=GamepadState(lx=1.0, l2=0.25, buttons=("b", "dpright")),
            ),
        ],
        end_ms=end_ms,
    )


def test_gamepad_trace_roundtrips_with_its_explicit_duration():
    original = gamepad_trace()
    restored = trace.loads(trace.dumps(original))
    assert restored.to_dict() == original.to_dict()
    assert restored.schema_version == 2
    assert restored.duration_ms == 2000.0
    assert restored.counts()["gamepad"] == 2
    assert restored.hash() == original.hash()
    assert restored.events[1].state.buttons == ("b", "dpright")


def test_gamepad_hash_covers_how_long_the_last_state_is_held():
    short = gamepad_trace(end_ms=1500.0)
    long = gamepad_trace(end_ms=4000.0)
    assert short.hash() != long.hash()


def test_a_version_one_trace_still_loads_and_keeps_its_version():
    restored = make_trace()
    assert restored.schema_version == 1
    assert restored.duration_ms == 4000.0
    assert trace.loads(trace.dumps(restored)).schema_version == 1


def test_gamepad_events_need_schema_version_two():
    payload = json.loads(trace.dumps(gamepad_trace()))
    payload["schema_version"] = 1
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert "schema_version 2" in str(excinfo.value)


def test_explicit_duration_needs_schema_version_two():
    payload = json.loads(trace.dumps(make_trace()))
    payload["duration_ms"] = 5000
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert "schema_version 2" in str(excinfo.value)


def test_explicit_duration_cannot_end_before_the_last_event():
    with pytest.raises(TraceError) as excinfo:
        gamepad_trace(end_ms=100.0)
    assert "before the last event" in str(excinfo.value)


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"lx": 2.0}, "within -1..1"),
        ({"l2": -0.5}, "within 0..1"),
        ({"ly": float("nan")}, "must be finite"),
        ({"buttons": ["trigger"]}, "unknown gamepad button"),
        ({"gyro": 1.0}, "unknown field 'gyro'"),
    ],
)
def test_bad_gamepad_values_are_rejected(mutation, message):
    payload = json.loads(trace.dumps(gamepad_trace()))
    payload["events"][1].update(mutation)
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert message in str(excinfo.value)


def test_a_gamepad_event_must_carry_a_full_state():
    payload = json.loads(trace.dumps(gamepad_trace()))
    del payload["events"][1]["r2"]
    with pytest.raises(TraceError) as excinfo:
        Trace.from_dict(payload)
    assert "missing 'r2'" in str(excinfo.value)
