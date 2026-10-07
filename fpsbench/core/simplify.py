"""Compress a recorded trace into an editable declarative route.

The compression works on *intervals*, which is what makes concurrent inputs
representable in a sequential step list:

* every held key becomes an interval ``[down, up]``;
* consecutive ``look`` events become a ``turn`` interval;
* scrolls and mouse buttons become short intervals;
* labels become zero-length markers.

All interval boundaries are collected, and the timeline is cut at every
boundary.  Inside one slice the active keys are unioned (so "W held, Shift
pressed 1 s in" becomes ``key(w)`` -> ``key(w, shift)`` -> ``key(w)``), and a
turn inside a key hold splits the hold into ``key`` / ``turn`` / ``key``.  The
player replays adjacent ``key`` steps with an identical key set as one
continuous hold, so nothing is released in between.

Overlaps that the sequential model cannot express (a turn while scrolling) are
resolved by priority and reported as warnings - never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .keycodes import MODIFIER_KEYS
from .route import FpsConfig, Region, Route, Step
from .trace import Trace, TraceEvent

__all__ = [
    "SimplificationResult",
    "Options",
    "simplify",
    "fps_config_from_metadata",
    "DEFAULT_OPTIONS",
]

#: kind priority when two non-key actions share a slice (higher wins).
_PRIORITY = {"turn": 3, "button": 2, "scroll": 1}


@dataclass(frozen=True)
class Options:
    """Knobs for the compression (all times in milliseconds)."""

    #: Idle gaps at least this long become an explicit ``wait`` step.
    min_gap_ms: float = 50.0
    #: Consecutive look events closer than this are one continuous turn.
    turn_gap_ms: float = 120.0
    #: Duration given to an isolated look event.
    single_look_ms: float = 100.0
    #: Same-key holds separated by at most this (with nothing else happening)
    #: are merged into one hold.
    key_merge_gap_ms: float = 250.0
    #: Duration given to a mouse click and to a scroll burst.
    click_ms: float = 80.0
    scroll_ms: float = 60.0
    #: Round look/scroll deltas to integers (they are pixel counts).
    round_deltas: bool = True


DEFAULT_OPTIONS = Options()


def _round(value: float, digits: int = 1) -> float:
    out = round(float(value), digits)
    return 0.0 if out == 0 else out


@dataclass
class _Action:
    """One interval on the trace timeline."""

    start: float
    end: float
    kind: str
    payload: Dict[str, float] = field(default_factory=dict)


@dataclass
class SimplificationResult:
    route: Route
    warnings: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # a result is truthy; convenience for tests
        return True


def _key_sort(keys: Sequence[str]) -> List[str]:
    """Deterministic key order: main keys first (alphabetical), modifiers last."""
    return sorted(keys, key=lambda name: (name in MODIFIER_KEYS, name))


def _key_intervals(
    events: Sequence[TraceEvent], options: Options, warnings: List[str]
) -> List[_Action]:
    pending: Dict[str, float] = {}
    actions: List[_Action] = []
    order: List[str] = []
    last_t = float(events[-1].t_ms) if events else 0.0
    for event in events:
        if event.kind != "key" or event.key is None:
            continue
        if event.down:
            if event.key not in pending:
                pending[event.key] = float(event.t_ms)
                order.append(event.key)
        elif event.key in pending:
            actions.append(
                _Action(pending.pop(event.key), float(event.t_ms), "key", {"key": event.key})
            )
        else:  # a key up we never saw the down for - ignore, but say so
            warnings.append(
                "key %r released at %.0f ms without a recorded press; ignored"
                % (event.key, event.t_ms)
            )
    for key, start in pending.items():
        # The recording stopped while the key was held: close the hold, but never
        # produce a zero length step (a hold needs *some* duration to replay).
        end = max(last_t, start + options.min_gap_ms)
        warnings.append(
            "key %r was still held when recording stopped; closed at %.0f ms" % (key, end)
        )
        actions.append(_Action(start, end, "key", {"key": key}))
    actions.sort(key=lambda action: (action.start, action.kind))
    return _merge_keys(actions, options)


def _merge_keys(actions: List[_Action], options: Options) -> List[_Action]:
    """Merge same-key holds separated by a short gap with nothing else in it."""
    merged: List[_Action] = []
    for action in actions:
        if merged:
            previous = merged[-1]
            gap = action.start - previous.end
            if (
                previous.payload.get("key") == action.payload.get("key")
                and 0 <= gap <= options.key_merge_gap_ms
            ):
                merged[-1] = _Action(previous.start, action.end, "key", dict(previous.payload))
                continue
        merged.append(action)
    return merged


def _look_intervals(events: Sequence[TraceEvent], options: Options) -> List[_Action]:
    groups: List[_Action] = []
    current: Optional[_Action] = None
    previous_t: Optional[float] = None
    for event in events:
        if event.kind != "look":
            continue
        t_ms = float(event.t_ms)
        dx = float(event.dx or 0.0)
        dy = float(event.dy or 0.0)
        if current is not None and previous_t is not None and t_ms - previous_t <= options.turn_gap_ms:
            current.end = t_ms
            current.payload["dx"] += dx
            current.payload["dy"] += dy
        else:
            if current is not None:
                groups.append(current)
            end = t_ms + options.single_look_ms
            current = _Action(t_ms, end, "turn", {"dx": dx, "dy": dy})
        previous_t = t_ms
    if current is not None:
        groups.append(current)

    for group in groups:
        if group.end <= group.start:
            group.end = group.start + options.single_look_ms
        if options.round_deltas:
            group.payload["dx"] = float(round(group.payload["dx"]))
            group.payload["dy"] = float(round(group.payload["dy"]))
    return groups


def _point_intervals(events: Sequence[TraceEvent], options: Options, warnings: List[str]) -> List[_Action]:
    actions: List[_Action] = []
    pressed: Dict[str, float] = {}
    for event in events:
        t_ms = float(event.t_ms)
        if event.kind == "scroll":
            actions.append(
                _Action(
                    t_ms,
                    t_ms + options.scroll_ms,
                    "scroll",
                    {"dy": float(event.dy or 0.0), "dx": float(event.dx or 0.0)},
                )
            )
        elif event.kind == "button" and event.button is not None:
            if event.down:
                if event.button not in pressed:
                    pressed[event.button] = t_ms
            elif event.button in pressed:
                start = pressed.pop(event.button)
                end = t_ms if t_ms > start else start + options.click_ms
                actions.append(_Action(start, end, "button", {"button": event.button}))
    for button, start in pressed.items():
        warnings.append("mouse button %r was still down when recording stopped" % (button,))
        actions.append(_Action(start, start + options.click_ms, "button", {"button": button}))
    return actions


def simplify(
    trace: Trace,
    *,
    options: Options = DEFAULT_OPTIONS,
    warmup_ms: Optional[float] = None,
    repeat: int = 1,
    fps: Optional[FpsConfig] = None,
) -> SimplificationResult:
    """Compress ``trace`` into a :class:`Route` plus a list of warnings."""
    warnings: List[str] = []
    events = list(trace.events)
    if not events:
        raise ValueError("cannot simplify an empty trace")
    if any(event.kind == "gamepad" for event in events):
        # The route format has no gamepad steps.  Dropping them would still write a
        # valid-looking route that plays something else, which is exactly the kind
        # of silent substitution the trace format avoids.
        raise ValueError(
            "cannot simplify a trace that contains gamepad events: the route format has no "
            "gamepad steps, so the result would replay a different input path"
        )

    key_actions = _key_intervals(events, options, warnings)
    look_actions = _look_intervals(events, options)
    point_actions = _point_intervals(events, options, warnings)
    markers = [(float(event.t_ms), event.text or "") for event in events if event.kind == "label"]

    others = [action for action in (look_actions + point_actions) if action.end > action.start]

    boundaries = {0.0}
    for action in key_actions + look_actions + point_actions:
        boundaries.add(action.start)
        boundaries.add(action.end)
    for marker_t, _ in markers:
        boundaries.add(marker_t)
    ordered = sorted(boundaries)
    end_of_run = max(
        [float(trace.duration_ms)] + [action.end for action in key_actions + look_actions + point_actions]
    )

    markers_by_time: Dict[float, List[str]] = {}
    for marker_t, text in markers:
        markers_by_time.setdefault(marker_t, []).append(text)

    steps: List[Step] = []
    for index in range(len(ordered) - 1):
        start, end = ordered[index], ordered[index + 1]
        for text in markers_by_time.get(start, []):
            steps.append(Step.marker(text))
        duration = end - start
        if duration <= 0:
            continue

        keys: List[str] = []
        for action in key_actions:
            if action.start <= start and action.end >= end:
                key_name = action.payload.get("key")
                if isinstance(key_name, str) and key_name not in keys:
                    keys.append(key_name)

        active_others = [
            action for action in others if action.start <= start and action.end >= end
        ]
        chosen: Optional[_Action] = None
        for action in active_others:
            if chosen is None or _PRIORITY[action.kind] > _PRIORITY[chosen.kind]:
                chosen = action
        if len(active_others) > 1:
            kinds = sorted({action.kind for action in active_others})
            warnings.append(
                "overlapping %s inputs at %.0f-%.0f ms collapsed into one step; "
                "the route cannot express them in parallel" % ("+".join(kinds), start, end)
            )

        if chosen is not None:
            steps.append(_step_for(chosen, duration, options, keys))
        elif keys:
            steps.append(Step.key(_key_sort(keys), _round(duration)))
        elif duration >= options.min_gap_ms:
            steps.append(Step.wait(_round(duration)))

    # A label that sits at the very end of the trace still has to be emitted, and
    # the idle time before it must be kept so the marker lands at the right time.
    for text in markers_by_time.get(ordered[-1], []):
        steps.append(Step.marker(text))
    # Only a gap at the very end of the route is dropped.
    while steps and steps[-1].type == "wait":
        steps.pop()

    trailing_end = _route_end(steps)
    if trailing_end + options.min_gap_ms < end_of_run:
        warnings.append(
            "last %.0f ms of the trace are idle and were dropped" % (end_of_run - trailing_end)
        )

    metadata = dict(trace.metadata)
    metadata["source_trace"] = trace.name
    metadata["source_trace_hash"] = trace.hash()
    if trace.origin_platform:
        metadata["origin_platform"] = trace.origin_platform

    route = Route(
        name=trace.name,
        steps=steps,
        warmup_ms=float(warmup_ms if warmup_ms is not None else metadata.get("warmup_ms", 0.0) or 0.0),
        repeat=repeat,
        fps=fps if fps is not None else fps_config_from_metadata(trace.metadata),
        origin_platform=trace.origin_platform,
        metadata=metadata,
    )
    return SimplificationResult(route=route, warnings=warnings)


def _step_for(
    action: _Action, duration: float, options: Options, keys: Optional[List[str]] = None
) -> Step:
    sorted_keys = _key_sort(keys) if keys else None
    if action.kind == "turn":
        return Step.turn(
            _round(action.payload.get("dx", 0.0), 0),
            _round(action.payload.get("dy", 0.0), 0),
            _round(duration),
            keys=sorted_keys,
        )
    if action.kind == "scroll":
        # Wheel notches can be fractional on high-resolution wheels.
        return Step.scroll(_round(action.payload.get("dy", 0.0), 2), _round(duration), keys=sorted_keys)
    return Step.click(str(action.payload.get("button", "left")), _round(duration), keys=sorted_keys)


def _route_end(steps: Sequence[Step]) -> float:
    return float(sum(step.duration for step in steps))


def fps_config_from_metadata(metadata: Dict[str, object]) -> FpsConfig:
    """Carry the FPS settings a trace remembered into the derived route.

    The recorder stores the mode, the OCR region and the game's process name (the
    per-frame sources need the name, OCR needs the region), so replaying the trace
    - or a route simplified from it - does not ask for them again.
    """
    mode = str(metadata.get("fps_mode", "auto"))
    region = metadata.get("fps_region")
    process_name = metadata.get("process_name")
    presentmon_path = metadata.get("presentmon_path")
    overrides: Dict[str, object] = {}
    if isinstance(process_name, str) and process_name:
        overrides["process_name"] = process_name
    if isinstance(presentmon_path, str) and presentmon_path:
        overrides["presentmon_path"] = presentmon_path
    if isinstance(region, dict):
        try:
            return FpsConfig(
                mode=mode, region=Region.from_dict(region, "fps"), **overrides
            )
        except Exception:  # a malformed region in metadata is not fatal for simplify
            return FpsConfig(**overrides)
    return FpsConfig(mode=mode, **overrides)
