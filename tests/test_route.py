"""Route model: strict codec, step semantics and the FPS configuration block."""

from __future__ import annotations

import json

import pytest

from fpsbench.core.route import (
    FpsConfig,
    Region,
    Route,
    RouteError,
    Step,
    dumps_route,
    loads_route,
)

SAMPLE = {
    "format": "route",
    "schema_version": 1,
    "name": "unit route",
    "created": "2026-10-06T00:00:00Z",
    "origin_platform": "macos",
    "warmup_ms": 8000,
    "settle_ms": 3000,
    "repeat": 3,
    "fps": {
        "mode": "ocr",
        "region": {"x": 10, "y": 20, "w": 120, "h": 40, "space": "points"},
        "sample_hz": 8,
        "min_fps": 1,
        "max_fps": 1000,
        "presentmon_path": "/tmp/PresentMon.exe",
        "process_name": "game.exe",
    },
    "metadata": {"display_hz": 120},
    "steps": [
        {"type": "key", "keys": ["w"], "hold_ms": 4000, "label": "straight"},
        {"type": "turn", "dx": 600, "dy": -20, "duration_ms": 300, "keys": ["w", "shift"]},
        {"type": "wait", "ms": 500},
        {"type": "scroll", "dy": -3, "duration_ms": 60},
        {"type": "button", "button": "left", "hold_ms": 80},
        {"type": "label", "text": "cliff"},
    ],
}


def test_route_roundtrip_is_lossless():
    route = Route.from_dict(json.loads(json.dumps(SAMPLE)))
    restored = loads_route(dumps_route(route))
    assert restored.to_dict() == route.to_dict()
    assert restored.hash() == route.hash()
    assert restored.repeat == 3
    assert restored.fps.region is not None
    assert restored.fps.region.space == "points"


def test_step_durations_add_up_to_the_route_duration():
    route = Route.from_dict(json.loads(json.dumps(SAMPLE)))
    assert route.duration_ms == pytest.approx(4000 + 300 + 500 + 60 + 80)


def test_labels_are_positioned_on_the_step_timeline():
    route = Route.from_dict(json.loads(json.dumps(SAMPLE)))
    # Step labels land at the step start, marker steps at their own position.
    assert route.labels() == [(0.0, "straight"), (4940.0, "cliff")]


def test_step_constructors_and_duration_property():
    assert Step.key(["w", "shift"], 1000).duration == 1000
    assert Step.turn(10, 0, 250).duration == 250
    assert Step.wait(120).duration == 120
    assert Step.scroll(2).duration == 100
    assert Step.click("left", 80).duration == 80
    assert Step.marker("x").duration == 0
    with pytest.raises(RouteError):
        Step(type="teleport")


def test_unknown_fields_and_types_are_rejected():
    payload = json.loads(json.dumps(SAMPLE))
    payload["steps"][0]["extra"] = 1
    with pytest.raises(RouteError) as excinfo:
        Route.from_dict(payload)
    assert "unknown field" in str(excinfo.value)

    payload = json.loads(json.dumps(SAMPLE))
    payload["surprise"] = True
    with pytest.raises(RouteError):
        Route.from_dict(payload)

    payload = json.loads(json.dumps(SAMPLE))
    payload["steps"][1]["duration_ms"] = -1
    with pytest.raises(RouteError):
        Route.from_dict(payload)

    payload = json.loads(json.dumps(SAMPLE))
    payload["steps"][0]["hold_ms"] = "4s"
    with pytest.raises(RouteError):
        Route.from_dict(payload)


def test_unknown_step_type_and_missing_fields_are_rejected():
    payload = json.loads(json.dumps(SAMPLE))
    payload["steps"][0]["type"] = "moonwalk"
    with pytest.raises(RouteError) as excinfo:
        Route.from_dict(payload)
    assert "unknown step type" in str(excinfo.value)

    payload = json.loads(json.dumps(SAMPLE))
    del payload["steps"][0]["hold_ms"]
    with pytest.raises(RouteError) as excinfo:
        Route.from_dict(payload)
    assert "missing 'hold_ms'" in str(excinfo.value)


def test_route_format_and_schema_are_enforced():
    payload = json.loads(json.dumps(SAMPLE))
    payload["format"] = "trace"
    with pytest.raises(RouteError):
        Route.from_dict(payload)

    payload = json.loads(json.dumps(SAMPLE))
    payload["schema_version"] = 2
    with pytest.raises(RouteError) as excinfo:
        Route.from_dict(payload)
    assert "schema_version" in str(excinfo.value)


def test_repeat_and_timings_are_validated():
    with pytest.raises(RouteError):
        Route(name="x", steps=[], repeat=0)
    with pytest.raises(RouteError):
        Route(name="x", steps=[], warmup_ms=-1)
    with pytest.raises(RouteError):
        Route(name="x", steps=["not a step"])  # type: ignore[list-item]


def test_keys_are_normalised_and_deduplicated():
    step = Step.from_dict({"type": "key", "keys": ["W", "shift", "w"], "hold_ms": 10}, "step")
    assert step.keys == ["w", "shift"]
    with pytest.raises(RouteError):
        Step.from_dict({"type": "key", "keys": [], "hold_ms": 10}, "step")
    with pytest.raises(RouteError):
        Step.from_dict({"type": "key", "keys": ["teleport"], "hold_ms": 10}, "step")


def test_signed_deltas_are_allowed_but_durations_are_not_negative():
    step = Step.from_dict({"type": "turn", "dx": -500, "dy": 40, "duration_ms": 100}, "step")
    assert step.dx == -500 and step.dy == 40
    with pytest.raises(RouteError):
        Step.from_dict({"type": "scroll", "dy": 1, "duration_ms": 0}, "step")


def test_region_and_fps_config_validation():
    with pytest.raises(RouteError):
        Region(x=0, y=0, w=0, h=10)
    with pytest.raises(RouteError):
        Region(x=0, y=0, w=10, h=10, space="bananas")
    with pytest.raises(RouteError):
        FpsConfig(mode="magic")
    with pytest.raises(RouteError):
        FpsConfig(sample_hz=0)
    with pytest.raises(RouteError):
        FpsConfig(min_fps=10, max_fps=5)
    with pytest.raises(RouteError):
        FpsConfig.from_dict({"region": {"x": 0, "y": 0, "w": 10, "h": 10, "nope": 1}}, "fps")


def test_every_platform_fps_mode_is_a_valid_route_mode():
    from fpsbench.core.route import FPS_MODES

    for mode in ("auto", "presentmon", "metalperftrace", "ocr"):
        assert mode in FPS_MODES
        assert FpsConfig(mode=mode).mode == mode
    # a region is optional: metalperftrace / PresentMon do not need one
    assert FpsConfig(mode="metalperftrace").region is None


def test_fps_config_merge_applies_only_given_overrides():
    base = FpsConfig.from_dict(json.loads(json.dumps(SAMPLE))["fps"], "fps")
    merged = base.merged(mode="presentmon", process_name="other.exe")
    assert merged.mode == "presentmon"
    assert merged.process_name == "other.exe"
    assert merged.region == base.region
    assert base.mode == "ocr"  # merged() must not mutate
    region = Region(x=1, y=2, w=3, h=4, space="pixels")
    assert base.merged(region=region).region == region


def test_non_button_steps_have_no_button_value():
    """The ``button`` field must default to None.

    A classmethod with the same name as the field would be picked up by
    ``@dataclass`` as the field default, which is why the constructor is called
    ``click``.
    """
    for step in (
        Step.key(["w"], 100),
        Step.turn(10, 0, 100),
        Step.wait(100),
        Step.scroll(1),
        Step.marker("x"),
    ):
        assert step.button is None
    assert Step.click("right", 50).button == "right"


def test_missing_fps_block_falls_back_to_defaults():
    payload = json.loads(json.dumps(SAMPLE))
    del payload["fps"]
    route = Route.from_dict(payload)
    assert route.fps.mode == "auto"
    assert route.fps.region is None
