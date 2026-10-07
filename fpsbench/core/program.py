"""Frame-quantised input program: the frame-rate independent playback form.

A trace/route is a *time* function of input.  Replaying it on an absolute clock
keeps that function exact, but a game consumes input once per rendered frame, so
what one frame sees depends on how long it lasted: a 30 FPS frame accumulates
twice the delta of a 60 FPS frame, and any per-frame clamp, smoothing or
"last event wins" logic turns that into a different path.

This module turns the timeline into a fixed sequence of **input frames**: frame
``i`` carries everything the recording delivered in ``[i*T, (i+1)*T)`` with
``T = 1000 / rate_hz``.  Replaying one input frame per rendered frame gives every
device the same per-frame delta sequence, which removes the *input-side*
frame-rate dependence - and only that.  The replay clock is estimated from the
presented frames, the game may sample input on its own cadence, and its
simulation may still be frame-rate dependent, so the resulting path is
comparable across devices, not identical.

Quantisation is exact: every frame delta is derived from the *rounded cumulative*
motion, so the sums always equal the trace/route total (integer rounding
remainders are carried from frame to frame, never dropped).

Key and button holds are quantised to whole frames ("any overlap with the frame
counts").  That also removes the sub-frame tap problem: a 30 ms press cannot fall
between two polls of a 15 FPS game any more, it becomes a one-frame hold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from .gamepad import GamepadState
from .route import Route
from .trace import Trace

__all__ = [
    "REFERENCE_HZ",
    "InputFrame",
    "frame_count",
    "frame_index",
    "trace_to_frames",
    "route_to_frames",
]

#: Default reference rate of an input program: 60 input frames per second.
REFERENCE_HZ = 60.0


@dataclass(frozen=True)
class InputFrame:
    """Everything that is delivered at the start of one rendered frame."""

    index: int
    dx: float = 0.0
    dy: float = 0.0
    scroll_dy: float = 0.0
    scroll_dx: float = 0.0
    keys: Tuple[str, ...] = ()
    buttons: Tuple[str, ...] = ()
    labels: Tuple[str, ...] = ()
    #: Full controller state when it changed in this frame (``None`` = keep the
    #: state the virtual device already holds).
    gamepad: Optional[GamepadState] = None

    @property
    def is_empty(self) -> bool:
        """True when the frame carries no motion at all."""
        return not (self.dx or self.dy or self.scroll_dy or self.scroll_dx)


def frame_count(duration_ms: float, rate_hz: float = REFERENCE_HZ) -> int:
    """Number of input frames needed to cover ``duration_ms``."""
    if rate_hz <= 0:
        raise ValueError("rate_hz must be > 0")
    if duration_ms <= 0:
        return 1
    return max(1, int(math.ceil(duration_ms * rate_hz / 1000.0 - 1e-9)))


def frame_index(t_ms: float, rate_hz: float = REFERENCE_HZ, count: Optional[int] = None) -> int:
    """Index of the frame that contains ``t_ms`` (clamped to the last frame)."""
    if rate_hz <= 0:
        raise ValueError("rate_hz must be > 0")
    index = max(0, int(math.floor(t_ms * rate_hz / 1000.0 + 1e-9)))
    if count is not None:
        index = min(index, max(0, count - 1))
    return index


def _quantise(values: Sequence[float], digits: int = 0) -> List[float]:
    """Round a delta series without losing its total.

    Each step is derived from the rounded *cumulative* value, so the sum of the
    output equals ``round(sum(values))`` exactly.  ``digits`` keeps wheel notches
    fractional (they are stored with two decimals) while pixel deltas stay whole.
    """
    scale = float(10**digits)
    out: List[float] = []
    exact = 0.0
    sent = 0.0
    for value in values:
        exact += value * scale
        target = float(round(exact))
        out.append((target - sent) / scale)
        sent = target
    return out


def _ordered(names: Iterable[str]) -> Tuple[str, ...]:
    return tuple(sorted(names))


def trace_to_frames(trace: Trace, *, rate_hz: float = REFERENCE_HZ) -> List[InputFrame]:
    """Quantise a recorded trace into per-frame input."""
    count = frame_count(trace.duration_ms, rate_hz)
    dx = [0.0] * count
    dy = [0.0] * count
    scroll_dy = [0.0] * count
    scroll_dx = [0.0] * count
    keys = [set() for _ in range(count)]
    buttons = [set() for _ in range(count)]
    labels: List[List[str]] = [[] for _ in range(count)]
    gamepad: List[Optional[GamepadState]] = [None] * count

    key_down: dict = {}
    button_down: dict = {}
    last_t = float(trace.duration_ms)

    for event in trace.events:
        t_ms = float(event.t_ms)
        if event.kind == "look":
            index = frame_index(t_ms, rate_hz, count)
            dx[index] += float(event.dx or 0.0)
            dy[index] += float(event.dy or 0.0)
        elif event.kind == "scroll":
            index = frame_index(t_ms, rate_hz, count)
            scroll_dy[index] += float(event.dy or 0.0)
            scroll_dx[index] += float(event.dx or 0.0)
        elif event.kind == "key" and event.key is not None:
            if event.down:
                key_down.setdefault(event.key, t_ms)
            elif event.key in key_down:
                _hold(keys, key_down.pop(event.key), t_ms, rate_hz, count, event.key)
        elif event.kind == "button" and event.button is not None:
            if event.down:
                button_down.setdefault(event.button, t_ms)
            elif event.button in button_down:
                _hold(buttons, button_down.pop(event.button), t_ms, rate_hz, count, event.button)
        elif event.kind == "gamepad" and event.state is not None:
            # Change-only: the virtual device holds the state until the next one,
            # like a real controller does between two polls.
            gamepad[frame_index(t_ms, rate_hz, count)] = event.state
        elif event.kind == "label":
            labels[frame_index(t_ms, rate_hz, count)].append(event.text or "")

    for key, start in key_down.items():
        # Still held when the recording stopped: hold it to the end, like the
        # key-interval compression in simplify does.
        _hold(keys, start, max(last_t, start), rate_hz, count, key)
    for button, start in button_down.items():
        _hold(buttons, start, max(last_t, start), rate_hz, count, button)

    return _build(count, dx, dy, scroll_dy, scroll_dx, keys, buttons, labels, gamepad)


def _hold(
    buckets: List[set],
    start_ms: float,
    end_ms: float,
    rate_hz: float,
    count: int,
    name: str,
) -> None:
    """Mark ``name`` as held for every frame the interval overlaps.

    A press and release inside one frame still marks that frame, so a tap that
    is shorter than a frame of the game being benchmarked cannot vanish.
    """
    width_ms = 1000.0 / rate_hz
    first = frame_index(start_ms, rate_hz, count)
    last = max(first + 1, int(math.ceil(end_ms / width_ms - 1e-9)))
    for index in range(first, min(count, last)):
        buckets[index].add(name)


def _build(
    count: int,
    dx: Sequence[float],
    dy: Sequence[float],
    scroll_dy: Sequence[float],
    scroll_dx: Sequence[float],
    keys: Sequence[set],
    buttons: Sequence[set],
    labels: Sequence[Sequence[str]],
    gamepad: Optional[Sequence[Optional[GamepadState]]] = None,
) -> List[InputFrame]:
    quantised_dx = _quantise(dx)
    quantised_dy = _quantise(dy)
    quantised_scroll_dy = _quantise(scroll_dy, digits=2)
    quantised_scroll_dx = _quantise(scroll_dx, digits=2)
    return [
        InputFrame(
            index=index,
            dx=quantised_dx[index],
            dy=quantised_dy[index],
            scroll_dy=quantised_scroll_dy[index],
            scroll_dx=quantised_scroll_dx[index],
            keys=_ordered(keys[index]),
            buttons=_ordered(buttons[index]),
            labels=tuple(labels[index]),
            gamepad=gamepad[index] if gamepad is not None else None,
        )
        for index in range(count)
    ]


def route_to_frames(
    route: Route,
    *,
    rate_hz: float = REFERENCE_HZ,
    repeat_index: int = 1,
) -> List[InputFrame]:
    """Quantise one repeat of a declarative route into per-frame input."""
    count = frame_count(route.duration_ms, rate_hz)
    width_ms = 1000.0 / rate_hz
    dx = [0.0] * count
    dy = [0.0] * count
    scroll_dy = [0.0] * count
    scroll_dx = [0.0] * count
    keys = [set() for _ in range(count)]
    buttons = [set() for _ in range(count)]
    labels: List[List[str]] = [[] for _ in range(count)]

    cursor = 0.0
    for step in route.steps:
        start = cursor
        end = cursor + step.duration
        if step.label is not None:
            labels[frame_index(start, rate_hz, count)].append(step.label)
        if step.type == "label":
            labels[frame_index(start, rate_hz, count)].append(step.text or "")
            continue
        if step.duration <= 0:
            # A zero length hold still has to be visible for one frame.
            index = frame_index(start, rate_hz, count)
            if step.keys:
                keys[index].update(step.keys)
            if step.type == "button" and step.button is not None:
                buttons[index].add(step.button)
        else:
            first = frame_index(start, rate_hz, count)
            last = min(count, int(math.ceil(end / width_ms - 1e-9)))
            for index in range(first, last):
                frame_start = index * width_ms
                overlap = min(frame_start + width_ms, end) - max(frame_start, start)
                if overlap <= 0:
                    continue
                fraction = overlap / step.duration
                if step.keys:
                    keys[index].update(step.keys)
                if step.type == "turn":
                    dx[index] += float(step.dx or 0.0) * fraction
                    dy[index] += float(step.dy or 0.0) * fraction
                elif step.type == "scroll":
                    scroll_dy[index] += float(step.dy or 0.0) * fraction
                elif step.type == "button" and step.button is not None:
                    buttons[index].add(step.button)
        cursor = end

    return _build(count, dx, dy, scroll_dy, scroll_dx, keys, buttons, labels)
