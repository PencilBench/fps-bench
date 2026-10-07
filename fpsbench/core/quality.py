"""Trace quality checks and the ``--trim-lead-in`` transformation.

Both operate on plain trace data, so they are testable without any platform
backend.  The checks exist because the two ways a recording is useless are
invisible otherwise: nothing was captured at all, or keys were captured but the
turns were not (the character then walks in a straight line and every repeat
looks the same in a boring way).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import List, Sequence

from .trace import Trace, TraceEvent

__all__ = ["analyse_trace", "trim_idle", "MIN_USEFUL_DURATION_MS"]

MIN_USEFUL_DURATION_MS = 3000.0


def analyse_trace(trace: Trace) -> List[str]:
    """Warnings about a recording that would make a bad benchmark."""
    warnings: List[str] = []
    counts = trace.counts()
    if not trace.events:
        return ["the recording is empty: nothing was captured"]

    keys_down = [event for event in trace.events if event.kind == "key" and event.down]
    looks = [event for event in trace.events if event.kind == "look"]
    pad_events = [event for event in trace.events if event.kind == "gamepad"]
    if not keys_down and not pad_events:
        warnings.append("no key presses were recorded: the character cannot be moved on replay")
    if not looks and not pad_events:
        warnings.append(
            "no mouse movement was recorded: if the route needs turning, the replay will run in a "
            "straight line (check the game had focus and that the mouse turns the camera)"
        )
    if pad_events and not any(
        event.state is not None and not event.state.is_neutral for event in pad_events
    ):
        warnings.append(
            "the gamepad was recorded but never left neutral: no stick, trigger or button "
            "movement reached the recorder"
        )
    if keys_down and looks:
        total_turn = sum(abs(event.dx or 0.0) + abs(event.dy or 0.0) for event in looks)
        if total_turn < 50:
            warnings.append(
                "almost no turning was recorded (%.0f px in total): the route may be a straight line"
                % total_turn
            )

    unmatched = _unmatched_keys(trace.events)
    for key in sorted(unmatched):
        warnings.append("key %r was pressed without a matching release" % key)

    if trace.duration_ms < MIN_USEFUL_DURATION_MS:
        warnings.append(
            "the recording is only %.0f ms long - too short to average FPS over; play for longer"
            % trace.duration_ms
        )
    if counts.get("label"):
        return warnings
    warnings.append(
        "no segment labels were recorded (hotkey was not used): the report cannot show which part "
        "of the map is slow"
    )
    return warnings


def _unmatched_keys(events: Sequence[TraceEvent]) -> List[str]:
    open_keys = []
    for event in events:
        if event.kind != "key" or event.key is None:
            continue
        if event.down:
            if event.key not in open_keys:
                open_keys.append(event.key)
        elif event.key in open_keys:
            open_keys.remove(event.key)
    return open_keys


@dataclass
class TrimResult:
    trace: Trace
    removed_lead_ms: float
    removed_tail_ms: float


def trim_idle(trace: Trace, *, min_tail_gap_ms: float = 1200.0) -> TrimResult:
    """Drop leading dead time (re-shifting to 0) and trailing idle.

    The recording starts when ``record`` starts, but the interesting part starts
    when the user actually moves, so a few seconds of lead-in are normal.  The
    tail is cut at the last event unless the final gap is longer than
    ``min_tail_gap_ms`` - a short tail is left alone so labels near the end keep
    their segment.
    """
    if not trace.events:
        return TrimResult(trace, 0.0, 0.0)
    first = float(trace.events[0].t_ms)
    last = float(trace.events[-1].t_ms)
    removed_lead = first
    removed_tail = 0.0
    events = [
        replace(event, t_ms=round(float(event.t_ms) - removed_lead, 3)) for event in trace.events
    ]
    if not any(event.kind == "gamepad" for event in trace.events) and (
        trace.duration_ms - last >= min_tail_gap_ms
    ):
        # A quiet tail is only idle for keyboard/mouse input.  For a controller a
        # quiet tail means a state is *held* (stick forward), so cutting it would
        # silently shorten the movement.
        removed_tail = trace.duration_ms - last
    end_ms = None
    if trace.end_ms is not None:
        # Keep the explicit end: a gamepad state still held at the end must not be
        # shortened to the last change by the trim.
        end_ms = max(last - removed_lead, float(trace.end_ms) - removed_lead - removed_tail)
    trimmed = Trace(
        name=trace.name,
        events=events,
        created=trace.created,
        origin_platform=trace.origin_platform,
        metadata=dict(trace.metadata),
        schema_version=trace.schema_version,
        end_ms=end_ms,
    )
    trimmed.metadata["trimmed_lead_ms"] = removed_lead
    if removed_tail:
        trimmed.metadata["trimmed_tail_ms"] = removed_tail
    return TrimResult(trimmed, removed_lead, removed_tail)
