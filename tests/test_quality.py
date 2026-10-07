"""Trace quality analysis and the ``--trim-lead-in`` transformation."""

from __future__ import annotations

from fpsbench.core import quality
from fpsbench.core.quality import analyse_trace, trim_idle
from fpsbench.core.trace import Trace, TraceEvent


def make(events, name="t", metadata=None):
    ordered = sorted(events, key=lambda event: event.t_ms)
    return Trace(name=name, events=ordered, origin_platform="macos", metadata=dict(metadata or {}))


def good_recording():
    events = [TraceEvent(kind="key", t_ms=0, key="w", down=True)]
    for position in range(30):
        events.append(TraceEvent(kind="look", t_ms=100 + position * 100, dx=20, dy=1))
    events.append(TraceEvent(kind="label", t_ms=1500, text="mid"))
    events.append(TraceEvent(kind="key", t_ms=4000, key="w", down=False))
    return make(events)


def test_a_good_recording_produces_no_warnings():
    assert analyse_trace(good_recording()) == []


def test_empty_recording_is_reported():
    assert analyse_trace(make([])) == ["the recording is empty: nothing was captured"]


def test_keys_without_looks_and_vice_versa():
    keys_only = make(
        [
            TraceEvent(kind="key", t_ms=0, key="w", down=True),
            TraceEvent(kind="label", t_ms=1000, text="a"),
            TraceEvent(kind="key", t_ms=5000, key="w", down=False),
        ]
    )
    warnings = analyse_trace(keys_only)
    assert any("no mouse movement" in warning for warning in warnings)

    looks_only = make(
        [
            TraceEvent(kind="look", t_ms=0, dx=100, dy=0),
            TraceEvent(kind="label", t_ms=1000, text="a"),
            TraceEvent(kind="look", t_ms=5000, dx=100, dy=0),
        ]
    )
    warnings = analyse_trace(looks_only)
    assert any("no key presses" in warning for warning in warnings)


def test_a_straight_line_route_is_flagged():
    events = [
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="look", t_ms=100, dx=1, dy=0),
        TraceEvent(kind="label", t_ms=500, text="a"),
        TraceEvent(kind="key", t_ms=5000, key="w", down=False),
    ]
    warnings = analyse_trace(make(events))
    assert any("almost no turning" in warning for warning in warnings)


def test_unmatched_key_and_short_recording_are_flagged():
    events = [
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="look", t_ms=100, dx=60, dy=0),
        TraceEvent(kind="label", t_ms=500, text="a"),
    ]
    warnings = analyse_trace(make(events))
    assert any("without a matching release" in warning for warning in warnings)
    assert any("too short" in warning for warning in warnings)


def test_missing_labels_are_flagged():
    events = [TraceEvent(kind="key", t_ms=0, key="w", down=True)]
    for position in range(30):
        events.append(TraceEvent(kind="look", t_ms=100 + position * 100, dx=20, dy=1))
    events.append(TraceEvent(kind="key", t_ms=4000, key="w", down=False))
    warnings = analyse_trace(make(events))
    assert any("no segment labels" in warning for warning in warnings)


# --------------------------------------------------------------------------- #
# trim_idle
# --------------------------------------------------------------------------- #
def test_trim_shifts_the_first_event_to_zero():
    trace = make(
        [
            TraceEvent(kind="key", t_ms=3000, key="w", down=True),
            TraceEvent(kind="key", t_ms=5000, key="w", down=False),
        ]
    )
    result = trim_idle(trace)
    assert result.removed_lead_ms == 3000
    assert [event.t_ms for event in result.trace.events] == [0, 2000]
    assert result.trace.metadata["trimmed_lead_ms"] == 3000
    assert result.trace.duration_ms == 2000


def test_trim_reports_the_tail_idle_it_cannot_cut():
    trace = make(
        [
            TraceEvent(kind="key", t_ms=0, key="w", down=True),
            TraceEvent(kind="key", t_ms=1000, key="w", down=False),
            TraceEvent(kind="label", t_ms=6000, text="late"),
        ]
    )
    result = trim_idle(trace)
    assert result.removed_lead_ms == 0
    # The label is the last event, so there is nothing to cut behind it.
    assert result.removed_tail_ms == 0
    assert result.trace.duration_ms == 6000


def test_trim_of_an_empty_trace_is_a_noop():
    trace = make([])
    result = trim_idle(trace)
    assert result.removed_lead_ms == 0 and result.trace.events == []


# --------------------------------------------------------------------------- #
# gamepad recordings
# --------------------------------------------------------------------------- #
def gamepad_trace(end_ms=6000.0):
    from fpsbench.core.gamepad import GamepadState

    return Trace(
        name="pad",
        events=[
            TraceEvent(kind="gamepad", t_ms=500, state=GamepadState()),
            TraceEvent(kind="gamepad", t_ms=2000, state=GamepadState(lx=1.0, buttons=("a",))),
        ],
        end_ms=end_ms,
    )


def test_a_gamepad_only_recording_is_not_called_a_straight_line():
    warnings = quality.analyse_trace(gamepad_trace())
    joined = " ".join(warnings)
    assert "no key presses" not in joined
    assert "no mouse movement" not in joined
    assert "never left neutral" not in joined


def test_a_neutral_gamepad_recording_is_reported():
    from fpsbench.core.gamepad import GamepadState

    trace = Trace(
        name="pad",
        events=[TraceEvent(kind="gamepad", t_ms=0, state=GamepadState())],
        end_ms=5000.0,
    )
    assert any("never left neutral" in warning for warning in quality.analyse_trace(trace))


def test_trimming_a_gamepad_trace_keeps_the_explicit_end():
    trace = gamepad_trace(end_ms=6000.0)
    result = quality.trim_idle(trace)
    # 500 ms of lead-in goes; the quiet tail is a *held* state, so it stays (a
    # keyboard trace would have trimmed the 4000 ms after the last event).
    assert result.removed_tail_ms == 0.0
    assert result.trace.duration_ms == 5500.0
    assert result.trace.schema_version == 2
    assert result.trace.counts()["gamepad"] == 2
