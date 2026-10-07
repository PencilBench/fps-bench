"""trace -> route compression."""

from __future__ import annotations

import pytest

from fpsbench.core import simplify
from fpsbench.core.route import loads_route, dumps_route
from fpsbench.core.trace import Trace, TraceEvent


def events(*spec):
    out = []
    for item in spec:
        out.append(TraceEvent(**item))
    return out


def trace_of(*spec, name="unit", metadata=None):
    return Trace(
        name=name,
        events=list(spec),
        origin_platform="macos",
        metadata=dict(metadata or {}),
    )


def test_a_hold_with_a_turn_inside_keeps_the_timing_and_the_keys():
    trace = trace_of(
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="look", t_ms=100, dx=40, dy=-2),
        TraceEvent(kind="look", t_ms=150, dx=60, dy=1),
        TraceEvent(kind="key", t_ms=4000, key="w", down=False),
    )
    result = simplify.simplify(trace)
    assert result.warnings == []
    steps = result.route.steps
    assert [step.type for step in steps] == ["key", "turn", "key"]
    assert steps[0].keys == ["w"] and steps[0].hold_ms == 100
    assert steps[1].keys == ["w"] and steps[1].duration_ms == 50
    assert (steps[1].dx, steps[1].dy) == (100, -1)
    assert steps[2].hold_ms == 3850
    # The compression must not move the end of the run.
    assert result.route.duration_ms == pytest.approx(trace.duration_ms)


def test_pressing_a_second_key_during_a_hold_unions_the_key_set():
    trace = trace_of(
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="key", t_ms=1000, key="shift", down=True),
        TraceEvent(kind="key", t_ms=2000, key="shift", down=False),
        TraceEvent(kind="key", t_ms=4000, key="w", down=False),
    )
    steps = simplify.simplify(trace).route.steps
    assert [(step.type, step.keys, step.hold_ms) for step in steps] == [
        ("key", ["w"], 1000),
        ("key", ["w", "shift"], 1000),
        ("key", ["w"], 2000),
    ]


def test_modifiers_are_sorted_after_the_main_key():
    trace = trace_of(
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="key", t_ms=0, key="shift", down=True),
        TraceEvent(kind="key", t_ms=500, key="w", down=False),
        TraceEvent(kind="key", t_ms=500, key="shift", down=False),
    )
    steps = simplify.simplify(trace).route.steps
    assert steps[0].keys == ["w", "shift"]


def test_labels_become_markers_at_their_exact_time():
    trace = trace_of(
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="label", t_ms=500, text="boss"),
        TraceEvent(kind="key", t_ms=1000, key="w", down=False),
    )
    steps = simplify.simplify(trace).route.steps
    assert [(step.type, step.duration) for step in steps] == [
        ("key", 500),
        ("label", 0),
        ("key", 500),
    ]
    assert steps[1].text == "boss"
    assert simplify.simplify(trace).route.labels() == [(500.0, "boss")]


def test_idle_gaps_become_wait_steps_and_the_trailing_idle_is_dropped():
    trace = trace_of(
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="key", t_ms=1000, key="w", down=False),
        TraceEvent(kind="key", t_ms=2000, key="w", down=True),
        TraceEvent(kind="key", t_ms=3000, key="w", down=False),
        TraceEvent(kind="label", t_ms=9000, text="late"),
    )
    result = simplify.simplify(trace)
    types = [step.type for step in result.route.steps]
    assert types == ["key", "wait", "key", "wait", "label"]
    assert [step.duration for step in result.route.steps] == [1000, 1000, 1000, 6000, 0]


def test_gaps_shorter_than_min_gap_are_compressed_away():
    trace = trace_of(
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="key", t_ms=1000, key="w", down=False),
        TraceEvent(kind="key", t_ms=1020, key="space", down=True),
        TraceEvent(kind="key", t_ms=2000, key="space", down=False),
    )
    result = simplify.simplify(trace)
    # Two distinct keys cannot be merged, and a 20 ms gap is below min_gap_ms so
    # it does not earn a wait step.
    assert [step.type for step in result.route.steps] == ["key", "key"]
    assert [step.keys for step in result.route.steps] == [["w"], ["space"]]
    assert [step.hold_ms for step in result.route.steps] == [1000, 980]


def test_short_same_key_gaps_are_merged():
    trace = trace_of(
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="key", t_ms=1000, key="w", down=False),
        TraceEvent(kind="key", t_ms=1100, key="w", down=True),
        TraceEvent(kind="key", t_ms=2000, key="w", down=False),
    )
    steps = simplify.simplify(trace).route.steps
    assert len(steps) == 1 and steps[0].hold_ms == 2000


def test_scroll_and_buttons_survive_the_compression():
    trace = trace_of(
        TraceEvent(kind="scroll", t_ms=100, dy=-3),
        TraceEvent(kind="button", t_ms=500, button="left", down=True),
        TraceEvent(kind="button", t_ms=560, button="left", down=False),
    )
    steps = simplify.simplify(trace).route.steps
    assert [step.type for step in steps] == ["scroll", "button", "wait"] or "scroll" in [
        step.type for step in steps
    ]
    kinds = {step.type: step for step in steps}
    assert kinds["scroll"].dy == -3
    assert kinds["button"].button == "left"
    assert kinds["button"].hold_ms == 60


def test_unmatched_events_produce_warnings_not_crashes():
    trace = trace_of(
        TraceEvent(kind="key", t_ms=10, key="w", down=False),  # release without press
        TraceEvent(kind="key", t_ms=100, key="space", down=True),  # never released
    )
    result = simplify.simplify(trace)
    text = " | ".join(result.warnings)
    assert "without a recorded press" in text
    assert "still held" in text
    # The open hold is closed with a minimum duration so it stays replayable.
    assert [step.type for step in result.route.steps] == ["wait", "key"]
    assert result.route.steps[1].keys == ["space"]
    assert result.route.steps[1].hold_ms == 50


def test_metadata_carries_the_trace_identity_and_fps_region():
    region = {"x": 10, "y": 20, "w": 100, "h": 30, "space": "points"}
    trace = trace_of(
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="key", t_ms=1000, key="w", down=False),
        metadata={"display_hz": 120, "fps_region": region, "fps_mode": "ocr"},
    )
    route = simplify.simplify(trace, warmup_ms=500, repeat=2).route
    assert route.name == trace.name
    assert route.origin_platform == "macos"
    assert route.warmup_ms == 500
    assert route.repeat == 2
    assert route.fps.region is not None
    assert route.fps.region.w == 100
    assert route.fps.mode == "ocr"
    assert route.metadata["source_trace_hash"] == trace.hash()
    assert route.metadata["display_hz"] == 120


def test_malformed_fps_region_in_metadata_is_ignored():
    trace = trace_of(
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="key", t_ms=100, key="w", down=False),
        metadata={"fps_region": {"x": 1}},
    )
    route = simplify.simplify(trace).route
    assert route.fps.region is None


def test_empty_trace_cannot_be_simplified():
    with pytest.raises(ValueError):
        simplify.simplify(trace_of())


def test_compressed_route_round_trips_through_json():
    trace = trace_of(
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="look", t_ms=100, dx=40, dy=0),
        TraceEvent(kind="label", t_ms=300, text="a"),
        TraceEvent(kind="key", t_ms=800, key="w", down=False),
    )
    route = simplify.simplify(trace).route
    restored = loads_route(dumps_route(route))
    assert restored.to_dict() == route.to_dict()
    assert restored.hash() == route.hash()


def test_options_control_the_compression():
    trace = trace_of(
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="look", t_ms=100, dx=10, dy=0),
        TraceEvent(kind="look", t_ms=300, dx=10, dy=0),
        TraceEvent(kind="key", t_ms=1000, key="w", down=False),
    )
    split = simplify.simplify(trace, options=simplify.Options(turn_gap_ms=50)).route
    merged = simplify.simplify(trace, options=simplify.Options(turn_gap_ms=500)).route
    assert len([step for step in split.steps if step.type == "turn"]) == 2
    assert len([step for step in merged.steps if step.type == "turn"]) == 1
    assert sum(step.duration for step in merged.steps) == pytest.approx(1000)


def test_isolated_look_gets_a_default_duration():
    trace = trace_of(
        TraceEvent(kind="look", t_ms=100, dx=200, dy=0),
        TraceEvent(kind="key", t_ms=1000, key="w", down=True),
        TraceEvent(kind="key", t_ms=2000, key="w", down=False),
    )
    steps = simplify.simplify(trace, options=simplify.Options(single_look_ms=120)).route.steps
    turn = [step for step in steps if step.type == "turn"][0]
    assert turn.duration_ms == 120
    assert turn.dx == 200
