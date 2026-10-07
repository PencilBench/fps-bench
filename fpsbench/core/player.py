"""Playback scheduling: expand a route/trace into events and drive them on an
absolute monotonic timeline.

Two properties matter and are covered by tests:

* **Absolute timeline** - every event is aligned to ``t0 + t_ms`` instead of
  sleeping for deltas in between, so a lateness of one event never accumulates
  over a long route.
* **Identical expansion for both inputs** - a trace and the simplified route of
  that trace produce the same *kind* of event stream, so both go through the
  same scheduler (recorded runs and hand-written routes stay comparable).
* **Frame-paced playback** - :meth:`Player.play_frames` drives the frame-quantised
  program from :mod:`fpsbench.core.program` on a frame clock instead of the
  absolute timeline, one input frame per rendered frame, so the per-frame input
  sequence does not depend on the device's frame rate.  The absolute-time path
  stays the reference for trajectory comparison (`--pacing time`).

Look timing is a strategy, not a detail: :func:`trace_to_timeline` can either
split a large recorded delta into chunks *after* its recorded time (the default,
for games that clamp one delta per frame) or post every delta once at exactly
its recorded time (``LOOK_SPLIT_RAW``), which keeps the recording's distribution
of turn against movement.  The chosen strategy is reported.

A trace may also carry ``gamepad`` events (full controller snapshots).  They go
through the same scheduler and are delivered to a second, optional backend
(:class:`Player` ``gamepad=``), because a virtual gamepad is a different device
from the keyboard/mouse injection path.  The state is held by the virtual device
between events - exactly like a real controller - so only changes are sent, and
everything is centred again when the run ends.

Adjacent ``key`` steps that hold an identical key set replay as one continuous
hold (``key_down`` is idempotent while a key is already down), which is what
makes the simplification "split a hold around a turn" lossless in practice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from .gamepad import GamepadState
from .program import InputFrame
from .route import Route
from .trace import Trace

__all__ = [
    "ScheduledEvent",
    "PlayStats",
    "Player",
    "route_to_timeline",
    "trace_to_timeline",
    "split_look",
    "chunk_look",
    "LOOK_RATE_HZ",
    "LOOK_SPLIT_CHUNK",
    "LOOK_SPLIT_RAW",
    "LOOK_SPLIT_STRATEGIES",
    "COALESCE_MS",
]

#: Interpolation rate for turning and for large recorded deltas.
LOOK_RATE_HZ = 120.0

#: Adjacent key steps with the same key set closer than this replay as one hold.
COALESCE_MS = 2.0

#: A single recorded look delta larger than this is split over several frames.
LOOK_CHUNK_THRESHOLD = 60.0

#: Keep a large recorded delta as one event at its recorded time.
LOOK_SPLIT_RAW = "raw"
#: Split a large recorded delta into evenly spaced chunks after its recorded time.
LOOK_SPLIT_CHUNK = "chunk"
LOOK_SPLIT_STRATEGIES = (LOOK_SPLIT_CHUNK, LOOK_SPLIT_RAW)


@dataclass(frozen=True)
class ScheduledEvent:
    """One backend call scheduled at ``t_ms`` after the run started."""

    t_ms: float
    kind: str
    payload: Dict[str, object] = field(default_factory=dict)

    def describe(self) -> str:
        if self.kind == "key_down":
            return "key down %s" % self.payload.get("key")
        if self.kind == "key_up":
            return "key up %s" % self.payload.get("key")
        if self.kind == "look":
            return "look %+.0f %+.0f" % (self.payload.get("dx", 0), self.payload.get("dy", 0))
        if self.kind == "scroll":
            return "scroll %+.0f" % (self.payload.get("dy", 0),)
        if self.kind == "button_down":
            return "button down %s" % self.payload.get("button")
        if self.kind == "button_up":
            return "button up %s" % self.payload.get("button")
        if self.kind == "label":
            return "label %r" % (self.payload.get("text"),)
        if self.kind == "gamepad":
            state = self.payload.get("state")
            return "gamepad %s" % (state.describe() if isinstance(state, GamepadState) else "?")
        return self.kind


def split_look(dx: float, dy: float, duration_ms: float, rate_hz: float = LOOK_RATE_HZ) -> List[tuple]:
    """Spread ``(dx, dy)`` over ``duration_ms`` at ``rate_hz``.

    Returns ``[(t_offset_ms, dx_i, dy_i), ...]`` whose deltas sum exactly to the
    input (integer rounding remainders are carried, never dropped).
    """
    steps = max(1, int(round(duration_ms * rate_hz / 1000.0)))
    out: List[tuple] = []
    previous_x = 0.0
    previous_y = 0.0
    for index in range(1, steps + 1):
        target_x = round(dx * index / steps)
        target_y = round(dy * index / steps)
        out.append(
            (
                duration_ms * index / steps,
                target_x - previous_x,
                target_y - previous_y,
            )
        )
        previous_x = target_x
        previous_y = target_y
    return out


def split_scalar(value: float, duration_ms: float, rate_hz: float = LOOK_RATE_HZ) -> List[tuple]:
    """Spreading helper for scroll bursts; ``[(t_offset_ms, value_i), ...]``."""
    steps = max(1, int(round(duration_ms * rate_hz / 1000.0)))
    out: List[tuple] = []
    previous = 0.0
    for index in range(1, steps + 1):
        target = round(value * index / steps)
        out.append((duration_ms * index / steps, target - previous))
        previous = target
    return out


def chunk_look(
    dx: float,
    dy: float,
    *,
    threshold: float = LOOK_CHUNK_THRESHOLD,
    interval_ms: float = 1000.0 / LOOK_RATE_HZ,
) -> List[tuple]:
    """Split one large recorded delta so the game cannot swallow it.

    Games commonly clamp a single mouse delta per frame; replaying a 900 px flick
    as one event can be ignored (or produce a snap).  Chunks are emitted
    ``interval_ms`` apart, which matches the interpolation rate — so the turn
    finishes later than it was recorded.  That is why
    :func:`trace_to_timeline` exposes this as a strategy
    (:data:`LOOK_SPLIT_CHUNK` vs :data:`LOOK_SPLIT_RAW`).
    """
    magnitude = max(abs(dx), abs(dy))
    if magnitude <= threshold or threshold <= 0:
        return [(0.0, dx, dy)]
    chunks = int(magnitude // threshold) + 1
    out: List[tuple] = []
    previous_x = 0.0
    previous_y = 0.0
    for index in range(1, chunks + 1):
        target_x = round(dx * index / chunks)
        target_y = round(dy * index / chunks)
        out.append(((index - 1) * interval_ms, target_x - previous_x, target_y - previous_y))
        previous_x = target_x
        previous_y = target_y
    return out


def route_to_timeline(route: Route, *, repeat_index: int = 1) -> List[ScheduledEvent]:
    """Expand one repeat of ``route`` into scheduled events.

    Adjacent steps holding the same key (``key`` -> ``turn`` with ``keys`` ->
    ``key``, which is how a hold split around a turn looks) produce *one*
    continuous hold: the key is only released once the following step no longer
    needs it.  That is what makes the simplification lossless in practice.
    """
    events: List[ScheduledEvent] = []
    steps = list(route.steps)
    held: Set[str] = set()
    cursor = 0.0
    for index, step in enumerate(steps):
        if step.label is not None:
            events.append(ScheduledEvent(_round_ms(cursor), "label", {"text": step.label, "repeat": repeat_index}))
        if step.type == "label":
            events.append(
                ScheduledEvent(_round_ms(cursor), "label", {"text": step.text or "", "repeat": repeat_index})
            )
            continue

        following = steps[index + 1] if index + 1 < len(steps) else None
        next_keys: Set[str] = set(following.keys) if following is not None else set()

        for key in step.keys:
            if key in held:
                continue
            events.append(ScheduledEvent(_round_ms(cursor), "key_down", {"key": key}))
            held.add(key)
        if step.type == "turn":
            for offset, dx_i, dy_i in split_look(step.dx or 0.0, step.dy or 0.0, step.duration):
                if dx_i == 0 and dy_i == 0:
                    continue
                events.append(
                    ScheduledEvent(_round_ms(cursor + offset), "look", {"dx": dx_i, "dy": dy_i})
                )
        elif step.type == "scroll":
            for offset, value in split_scalar(step.dy or 0.0, step.duration):
                if value == 0:
                    continue
                events.append(ScheduledEvent(_round_ms(cursor + offset), "scroll", {"dy": value}))
        elif step.type == "button":
            events.append(
                ScheduledEvent(_round_ms(cursor), "button_down", {"button": step.button or "left"})
            )
            events.append(
                ScheduledEvent(
                    _round_ms(cursor + step.duration), "button_up", {"button": step.button or "left"}
                )
            )

        end = cursor + step.duration
        for key in sorted(held):
            if key in next_keys:
                continue
            events.append(ScheduledEvent(_round_ms(end), "key_up", {"key": key}))
            held.discard(key)
        cursor = end
    for key in sorted(held):  # defensive: a route should always end with no keys held
        events.append(ScheduledEvent(_round_ms(cursor), "key_up", {"key": key}))
    return _sort_timeline(events)


def trace_to_timeline(
    trace: Trace,
    *,
    look_split: str = LOOK_SPLIT_CHUNK,
) -> List[ScheduledEvent]:
    """Expand a recorded trace into scheduled events.

    ``look_split`` decides how the recorded ``look`` deltas are timed:

    * :data:`LOOK_SPLIT_CHUNK` (default) splits a delta larger than
      :data:`LOOK_CHUNK_THRESHOLD` into chunks spaced ``1000 / LOOK_RATE_HZ`` ms
      apart.  That keeps a game that clamps one delta per frame from swallowing a
      fast flick, but the injected turn finishes *after* the recorded time, so it
      no longer lines up with whatever the recording did next.
    * :data:`LOOK_SPLIT_RAW` posts every recorded delta once, at its recorded
      ``t_ms``.  The event distribution is the recording's own, which is what a
      trajectory comparison needs (pair it with ``--pacing time``).
    """
    if look_split not in LOOK_SPLIT_STRATEGIES:
        raise ValueError(
            "look_split must be one of %s, got %r" % (LOOK_SPLIT_STRATEGIES, look_split)
        )
    events: List[ScheduledEvent] = []
    for event in trace.events:
        t_ms = float(event.t_ms)
        if event.kind == "key":
            assert event.key is not None and event.down is not None
            events.append(
                ScheduledEvent(_round_ms(t_ms), "key_down" if event.down else "key_up", {"key": event.key})
            )
        elif event.kind == "look":
            dx = float(event.dx or 0.0)
            dy = float(event.dy or 0.0)
            if look_split == LOOK_SPLIT_RAW:
                if dx or dy:
                    events.append(ScheduledEvent(_round_ms(t_ms), "look", {"dx": dx, "dy": dy}))
                continue
            for offset, dx_i, dy_i in chunk_look(dx, dy):
                if dx_i == 0 and dy_i == 0:
                    continue
                events.append(ScheduledEvent(_round_ms(t_ms + offset), "look", {"dx": dx_i, "dy": dy_i}))
        elif event.kind == "scroll":
            events.append(
                ScheduledEvent(
                    _round_ms(t_ms), "scroll", {"dy": float(event.dy or 0.0), "dx": float(event.dx or 0.0)}
                )
            )
        elif event.kind == "button":
            events.append(
                ScheduledEvent(
                    _round_ms(t_ms),
                    "button_down" if event.down else "button_up",
                    {"button": event.button or "left"},
                )
            )
        elif event.kind == "label":
            events.append(ScheduledEvent(_round_ms(t_ms), "label", {"text": event.text or ""}))
        elif event.kind == "gamepad":
            assert event.state is not None
            events.append(ScheduledEvent(_round_ms(t_ms), "gamepad", {"state": event.state}))
    return _sort_timeline(events)


def _round_ms(value: float) -> float:
    return round(float(value), 3)


def _sort_timeline(events: List[ScheduledEvent]) -> List[ScheduledEvent]:
    """Stable sort by time; up-events of one step stay after their down-events."""
    indexed = list(enumerate(events))
    indexed.sort(key=lambda pair: (pair[1].t_ms, _kind_rank(pair[1].kind), pair[0]))
    return [event for _, event in indexed]


def _kind_rank(kind: str) -> int:
    # At the same timestamp: markers first (so the input that follows belongs to
    # the new segment), then releases before presses, then motion.
    if kind == "label":
        return 0
    if kind.endswith("_up"):
        return 1
    if kind in ("key_down", "button_down"):
        return 2
    return 3


@dataclass
class PlayStats:
    """What the player actually did (used for the report and for dry-run)."""

    events: int = 0
    started_ns: int = 0
    ended_ns: int = 0
    labels: List[tuple] = field(default_factory=list)
    aborted: bool = False
    max_lateness_ns: int = 0
    #: Input frames delivered (frame-paced playback only; ``0`` on the time path).
    frames: int = 0

    @property
    def duration_ns(self) -> int:
        return max(0, self.ended_ns - self.started_ns)


def default_sleep_until(
    clock_ns: Callable[[], int],
    target_ns: int,
    spin_threshold_ns: int,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Coarse sleep up to ``spin_threshold_ns`` before the target, then spin."""
    while True:
        remaining = target_ns - clock_ns()
        if remaining <= 0:
            return
        if remaining > spin_threshold_ns:
            sleep(min(remaining / 1e9, 0.01))
        else:
            break
    while clock_ns() < target_ns:
        pass


class Player:
    """Drives an :class:`~fpsbench.backends.base.InputBackend` on an absolute timeline."""

    def __init__(
        self,
        backend,
        *,
        gamepad=None,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        sleep_until: Optional[Callable[[int], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
        spin_threshold_ns: int = 1_500_000,
        on_event: Optional[Callable[[ScheduledEvent], None]] = None,
        look_scale: float = 1.0,
    ) -> None:
        self._backend = backend
        self._gamepad = gamepad
        self._gamepad_state: Optional[GamepadState] = None
        self._clock_ns = clock_ns
        self._spin_threshold_ns = spin_threshold_ns
        self._sleep_until = sleep_until or (
            lambda target: default_sleep_until(self._clock_ns, target, self._spin_threshold_ns, sleep)
        )
        self._on_event = on_event
        self._held: Set[str] = set()
        self._buttons: Set[str] = set()
        self._look_scale = float(look_scale)
        self._look_residual = [0.0, 0.0]

    def _set_gamepad(self, state: GamepadState) -> None:
        """Send a full controller state - only when it differs from the last one."""
        if self._gamepad is None:
            raise RuntimeError(
                "this trace contains gamepad events but no gamepad backend was loaded"
            )
        if state == self._gamepad_state:
            return
        self._gamepad.set_state(state)
        self._gamepad_state = state

    def _scaled_look(self, dx: float, dy: float) -> Tuple[float, float]:
        """Apply :attr:`look_scale`, carrying the rounding remainder into the next call.

        Carrying matters: a per-event ``round`` of a scaled delta would bias the total
        (halving 1,1,1,1 must still turn 2, not 0 or 4).
        """
        out = []
        for index, value in enumerate((dx, dy)):
            scaled = value * self._look_scale + self._look_residual[index]
            sent = float(round(scaled))
            self._look_residual[index] = scaled - sent
            out.append(sent)
        return out[0], out[1]

    @property
    def held_keys(self) -> Set[str]:
        return set(self._held)

    def play(
        self,
        events: Sequence[ScheduledEvent],
        *,
        start_ns: Optional[int] = None,
        on_label: Optional[Callable[[str, int], None]] = None,
        abort: Optional[Callable[[], bool]] = None,
    ) -> PlayStats:
        """Dispatch ``events`` relative to ``start_ns`` (defaults to now)."""
        stats = PlayStats()
        if not events:
            stats.started_ns = stats.ended_ns = self._clock_ns()
            return stats
        t0 = self._clock_ns() if start_ns is None else start_ns
        stats.started_ns = t0
        try:
            for event in events:
                if abort is not None and abort():
                    stats.aborted = True
                    break
                target_ns = t0 + int(round(event.t_ms * 1e6))
                self._sleep_until(target_ns)
                now = self._clock_ns()
                stats.max_lateness_ns = max(stats.max_lateness_ns, now - target_ns)
                self._dispatch(event, now, stats, on_label)
                stats.events += 1
        finally:
            self.release_all()
            stats.ended_ns = self._clock_ns()
        return stats

    def play_frames(
        self,
        frames: Sequence[InputFrame],
        *,
        period_ns: Union[int, Callable[[], int]],
        start_ns: Optional[int] = None,
        on_label: Optional[Callable[[str, int], None]] = None,
        abort: Optional[Callable[[], bool]] = None,
    ) -> PlayStats:
        """Drive a frame-quantised program: one input frame per rendered frame.

        ``period_ns`` is either a fixed nanosecond period or a callable that is
        asked for the current best estimate before every frame, so a pace that is
        refined while the run progresses (measured frame intervals) follows the
        device.  Each frame is dispatched as a state diff (releases, then
        presses) plus one motion sample, which keeps the amount of input a frame
        sees independent of the frame rate - the whole point of the
        frame-quantised program.
        """
        period = period_ns if callable(period_ns) else (lambda: int(period_ns))
        stats = PlayStats()
        if not frames:
            stats.started_ns = stats.ended_ns = self._clock_ns()
            return stats
        t0 = self._clock_ns() if start_ns is None else start_ns
        stats.started_ns = t0
        target_ns = t0
        try:
            for frame in frames:
                if abort is not None and abort():
                    stats.aborted = True
                    break
                self._sleep_until(target_ns)
                now = self._clock_ns()
                stats.max_lateness_ns = max(stats.max_lateness_ns, now - target_ns)
                self._dispatch_frame(frame, (now - t0) / 1e6, now, stats, on_label)
                stats.frames += 1
                target_ns += max(1, int(period()))
        finally:
            self.release_all()
            stats.ended_ns = self._clock_ns()
        return stats

    def _dispatch_frame(
        self,
        frame: InputFrame,
        t_ms: float,
        now_ns: int,
        stats: PlayStats,
        on_label: Optional[Callable[[str, int], None]],
    ) -> None:
        wanted_keys = set(frame.keys)
        for key in sorted(self._held - wanted_keys):
            self._backend.key_up(key)
            self._held.discard(key)
        for key in sorted(wanted_keys - self._held):
            self._backend.key_down(key)
            self._held.add(key)

        wanted_buttons = set(frame.buttons)
        for button in sorted(self._buttons - wanted_buttons):
            self._backend.button_up(button)
            self._buttons.discard(button)
        for button in sorted(wanted_buttons - self._buttons):
            self._backend.button_down(button)
            self._buttons.add(button)

        if frame.dx or frame.dy:
            dx, dy = self._scaled_look(float(frame.dx), float(frame.dy))
            if dx or dy:
                if self._on_event is not None:
                    self._on_event(ScheduledEvent(t_ms, "look", {"dx": dx, "dy": dy}))
                self._backend.look(dx, dy)
        if frame.scroll_dy or frame.scroll_dx:
            if self._on_event is not None:
                self._on_event(
                    ScheduledEvent(t_ms, "scroll", {"dy": frame.scroll_dy, "dx": frame.scroll_dx})
                )
            self._backend.scroll(float(frame.scroll_dy), float(frame.scroll_dx))
        if frame.gamepad is not None:
            if self._on_event is not None:
                self._on_event(ScheduledEvent(t_ms, "gamepad", {"state": frame.gamepad}))
            self._set_gamepad(frame.gamepad)
        for text in frame.labels:
            stats.labels.append((now_ns, text))
            if on_label is not None:
                on_label(text, now_ns)

    def _dispatch(
        self,
        event: ScheduledEvent,
        now_ns: int,
        stats: PlayStats,
        on_label: Optional[Callable[[str, int], None]],
    ) -> None:
        kind = event.kind
        payload = event.payload
        if self._on_event is not None:
            self._on_event(event)
        if kind == "key_down":
            key = str(payload["key"])
            if key in self._held:  # continuous hold across adjacent key steps
                return
            self._backend.key_down(key)
            self._held.add(key)
        elif kind == "key_up":
            key = str(payload["key"])
            if key not in self._held:
                return
            self._backend.key_up(key)
            self._held.discard(key)
        elif kind == "look":
            dx, dy = self._scaled_look(float(payload.get("dx", 0.0)), float(payload.get("dy", 0.0)))
            if dx or dy:
                self._backend.look(dx, dy)
        elif kind == "scroll":
            self._backend.scroll(
                float(payload.get("dy", 0.0)), float(payload.get("dx", 0.0))
            )
        elif kind == "button_down":
            button = str(payload.get("button", "left"))
            if button in self._buttons:
                return
            self._backend.button_down(button)
            self._buttons.add(button)
        elif kind == "button_up":
            button = str(payload.get("button", "left"))
            if button not in self._buttons:
                return
            self._backend.button_up(button)
            self._buttons.discard(button)
        elif kind == "label":
            text = str(payload.get("text", ""))
            stats.labels.append((now_ns, text))
            if on_label is not None:
                on_label(text, now_ns)
        elif kind == "gamepad":
            state = payload.get("state")
            if not isinstance(state, GamepadState):
                raise ValueError("gamepad event payload must carry a GamepadState")
            self._set_gamepad(state)

    def release_all(self) -> None:
        """Release anything still held - also runs when playback is interrupted."""
        for key in sorted(self._held):
            try:
                self._backend.key_up(key)
            finally:
                self._held.discard(key)
        for button in sorted(self._buttons):
            try:
                self._backend.button_up(button)
            finally:
                self._buttons.discard(button)
        if self._gamepad is not None:
            # Axes, triggers and buttons go back to rest even if the run failed:
            # a stuck virtual stick would keep the character walking.
            try:
                self._gamepad.neutral()
            finally:
                self._gamepad_state = None
