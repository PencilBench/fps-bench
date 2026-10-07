"""Frame-quantised input programs: exact totals, whole-frame holds, no lost taps."""

from __future__ import annotations

import pytest

from fpsbench.core.program import (
    REFERENCE_HZ,
    frame_count,
    frame_index,
    route_to_frames,
    trace_to_frames,
)
from fpsbench.core.route import Route, Step
from fpsbench.core.trace import Trace, TraceEvent


def motion(frames):
    return sum(frame.dx for frame in frames), sum(frame.dy for frame in frames)


def test_frame_count_matches_the_reference_rate():
    assert frame_count(1000, 60) == 60
    assert frame_count(1000, 120) == 120
    assert frame_count(1010, 60) == 61  # a partial frame still needs one
    assert frame_count(0, 60) == 1  # an empty program still has one (empty) frame


def test_frame_index_is_clamped_to_the_last_frame():
    assert frame_index(0, 60) == 0
    assert frame_index(16, 60) == 0
    assert frame_index(17, 60) == 1
    assert frame_index(10_000, 60, count=5) == 4


def test_trace_motion_is_conserved_and_inside_frames():
    trace = Trace(
        name="t",
        events=[
            TraceEvent(kind="look", t_ms=5, dx=3, dy=-1),
            TraceEvent(kind="look", t_ms=17, dx=4, dy=0),
            TraceEvent(kind="look", t_ms=35, dx=-7, dy=2),
        ],
    )
    frames = trace_to_frames(trace, rate_hz=60)
    assert len(frames) == 3
    assert motion(frames) == (0, 1)
    assert (frames[0].dx, frames[1].dx, frames[2].dx) == (3, 4, -7)


def test_trace_scroll_is_quantised_per_frame():
    trace = Trace(
        name="t",
        events=[
            TraceEvent(kind="scroll", t_ms=5, dy=-1.5, dx=0.25),
            TraceEvent(kind="scroll", t_ms=25, dy=-0.5),
        ],
    )
    frames = trace_to_frames(trace, rate_hz=60)
    assert sum(frame.scroll_dy for frame in frames) == pytest.approx(-2.0)
    assert sum(frame.scroll_dx for frame in frames) == pytest.approx(0.25)


def test_a_tap_shorter_than_a_frame_survives_as_a_whole_frame_hold():
    """A 5 ms tap must not disappear when the game renders at 15 FPS."""
    trace = Trace(
        name="tap",
        events=[
            TraceEvent(kind="key", t_ms=100, key="space", down=True),
            TraceEvent(kind="key", t_ms=105, key="space", down=False),
        ],
    )
    frames = trace_to_frames(trace, rate_hz=60)
    held = [index for index, frame in enumerate(frames) if "space" in frame.keys]
    assert held == [6]


def test_key_hold_covers_every_overlapping_frame():
    trace = Trace(
        name="hold",
        events=[
            TraceEvent(kind="key", t_ms=0, key="w", down=True),
            TraceEvent(kind="key", t_ms=50, key="w", down=False),
            TraceEvent(kind="look", t_ms=100, dx=1, dy=0),
        ],
    )
    frames = trace_to_frames(trace, rate_hz=60)
    assert all("w" in frame.keys for frame in frames[:3])
    assert frames[3].keys == ()


def test_unreleased_key_is_held_to_the_end():
    trace = Trace(
        name="open",
        events=[
            TraceEvent(kind="key", t_ms=0, key="w", down=True),
            TraceEvent(kind="look", t_ms=100, dx=1, dy=0),
        ],
    )
    frames = trace_to_frames(trace, rate_hz=60)
    assert all("w" in frame.keys for frame in frames)


def test_buttons_are_quantised_to_frames():
    trace = Trace(
        name="click",
        events=[
            TraceEvent(kind="button", t_ms=20, button="left", down=True),
            TraceEvent(kind="button", t_ms=30, button="left", down=False),
        ],
    )
    frames = trace_to_frames(trace, rate_hz=60)
    assert [index for index, frame in enumerate(frames) if frame.buttons == ("left",)] == [1]


def test_labels_land_in_the_frame_that_contains_them():
    trace = Trace(
        name="l",
        events=[
            TraceEvent(kind="look", t_ms=0, dx=1, dy=0),
            TraceEvent(kind="label", t_ms=20, text="arena"),
            TraceEvent(kind="look", t_ms=40, dx=1, dy=0),
        ],
    )
    frames = trace_to_frames(trace, rate_hz=60)
    assert [index for index, frame in enumerate(frames) if frame.labels] == [1]
    assert frames[1].labels == ("arena",)


def test_route_turn_is_spread_proportionally_and_sums_exactly():
    route = Route(name="r", steps=[Step.turn(100, 0, 50)])
    frames = route_to_frames(route, rate_hz=60)
    assert len(frames) == 3
    assert [frame.dx for frame in frames] == [33, 34, 33]
    assert motion(frames) == (100, 0)


def test_route_keys_are_held_over_the_whole_step():
    route = Route(name="r", steps=[Step.key(["w"], 100), Step.wait(50), Step.key(["w", "shift"], 50)])
    frames = route_to_frames(route, rate_hz=60)
    assert all("w" in frame.keys for frame in frames[:6])
    assert all(frame.keys == () for frame in frames[6:9])
    assert "shift" not in frames[0].keys
    assert "shift" in frames[9].keys  # the second hold starts at 150 ms
    assert frames[-1].keys == ("shift", "w")


def test_route_button_click_stays_one_frame_when_it_is_shorter_than_one():
    route = Route(name="r", steps=[Step.click("right", 5)])
    frames = route_to_frames(route, rate_hz=60)
    assert [index for index, frame in enumerate(frames) if frame.buttons == ("right",)] == [0]


def test_route_scroll_is_distributed_and_conserved():
    route = Route(name="r", steps=[Step.scroll(-6, 60)])
    frames = route_to_frames(route, rate_hz=60)
    assert sum(frame.scroll_dy for frame in frames) == pytest.approx(-6)


def test_route_labels_markers_are_kept_in_order():
    route = Route(name="r", steps=[Step.key(["w"], 50, label="straight"), Step.marker("arena")])
    frames = route_to_frames(route, rate_hz=60)
    labels = [(index, text) for index, frame in enumerate(frames) for text in frame.labels]
    assert labels == [(0, "straight"), (2, "arena")]


def test_reference_rate_default_is_sixty():
    assert REFERENCE_HZ == 60.0


# --------------------------------------------------------------------------- #
# gamepad states
# --------------------------------------------------------------------------- #
def test_trace_frames_carry_only_the_frames_where_the_state_changed():
    from fpsbench.core.gamepad import GamepadState

    state = GamepadState(lx=1.0, buttons=("a",))
    trace = Trace(
        name="pad",
        events=[
            TraceEvent(kind="gamepad", t_ms=0, state=GamepadState()),
            TraceEvent(kind="gamepad", t_ms=100, state=state),
        ],
        end_ms=500.0,
    )
    frames = trace_to_frames(trace, rate_hz=60)
    assert len(frames) == 30  # the explicit duration, not the last change
    changed = [(frame.index, frame.gamepad) for frame in frames if frame.gamepad is not None]
    assert [(index, value.buttons) for index, value in changed] == [(0, ()), (6, ("a",))]
    assert changed[1][1] == state


def test_route_frames_never_carry_gamepad_state():
    route = Route(name="r", steps=[Step.key(["w"], 100)])
    assert all(frame.gamepad is None for frame in route_to_frames(route, rate_hz=60))
