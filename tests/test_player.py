"""Playback scheduling: absolute timeline, key coalescing, look interpolation."""

from __future__ import annotations

import pytest

from fpsbench.core import player as player_module
from fpsbench.core.program import InputFrame
from fpsbench.core.gamepad import NEUTRAL_STATE, GamepadState
from fpsbench.core.player import (
    LOOK_RATE_HZ,
    LOOK_SPLIT_RAW,
    Player,
    ScheduledEvent,
    chunk_look,
    route_to_timeline,
    split_look,
    split_scalar,
    trace_to_timeline,
)
from fpsbench.core.route import Route, Step
from fpsbench.core.trace import Trace, TraceEvent

from tests.fakes import FakeClock, FakeGamepadBackend, FakeInputBackend

MS = 1_000_000


def make_player(clock: FakeClock, backend=None, **kwargs):
    backend = backend or FakeInputBackend(clock_ns=clock)
    return Player(
        backend,
        clock_ns=clock,
        sleep_until=lambda target: player_module.default_sleep_until(
            clock, target, kwargs.pop("spin_ns", 1_500_000), sleep=clock.sleep
        ),
    ), backend


# --------------------------------------------------------------------------- #
# timeline expansion
# --------------------------------------------------------------------------- #
def test_route_expansion_holds_keys_for_the_step_duration():
    route = Route(
        name="r",
        steps=[Step.key(["w", "shift"], 1000), Step.wait(500), Step.key(["space"], 250)],
    )
    timeline = route_to_timeline(route)
    assert [(event.t_ms, event.kind) for event in timeline] == [
        (0.0, "key_down"),
        (0.0, "key_down"),
        (1000.0, "key_up"),
        (1000.0, "key_up"),
        (1500.0, "key_down"),
        (1750.0, "key_up"),
    ]


def test_turn_steps_interpolate_at_the_look_rate_and_sum_exactly():
    route = Route(name="r", steps=[Step.turn(600, -30, 250)])
    timeline = route_to_timeline(route)
    assert len(timeline) == pytest.approx(round(250 * LOOK_RATE_HZ / 1000), abs=1)
    assert sum(event.payload["dx"] for event in timeline) == 600
    assert sum(event.payload["dy"] for event in timeline) == -30
    # Strictly ordered, and the last chunk lands at the end of the step.
    times = [event.t_ms for event in timeline]
    assert times == sorted(times)
    assert times[-1] == pytest.approx(250, abs=1.0)


def test_split_look_carries_rounding_remainders():
    chunks = split_look(100, 100, 1000)
    assert sum(chunk[1] for chunk in chunks) == 100
    assert sum(chunk[2] for chunk in chunks) == 100
    assert split_look(1, 0, 1)[-1][1] == 1


def test_scroll_step_is_split_over_its_duration():
    route = Route(name="r", steps=[Step.scroll(-6, 60)])
    timeline = route_to_timeline(route)
    assert sum(event.payload["dy"] for event in timeline) == -6


def test_keys_on_a_motion_step_are_held_for_its_whole_duration():
    route = Route(name="r", steps=[Step.turn(60, 0, 200, keys=["w"])])
    timeline = route_to_timeline(route)
    downs = [event for event in timeline if event.kind == "key_down"]
    ups = [event for event in timeline if event.kind == "key_up"]
    looks = [event for event in timeline if event.kind == "look"]
    assert [(event.t_ms, event.payload["key"]) for event in downs] == [(0.0, "w")]
    assert [(event.t_ms, event.payload["key"]) for event in ups] == [(200.0, "w")]
    assert looks and all(0 < event.t_ms <= 200.0 for event in looks)
    assert sum(event.payload["dx"] for event in looks) == 60


def test_button_and_label_events_are_expanded():
    route = Route(
        name="r", steps=[Step.click("right", 80, label="aim"), Step.marker("done")]
    )
    timeline = route_to_timeline(route)
    assert [(event.t_ms, event.kind) for event in timeline] == [
        (0.0, "label"),
        (0.0, "button_down"),
        (80.0, "label"),
        (80.0, "button_up"),
    ]
    assert [event.payload.get("text") for event in timeline if event.kind == "label"] == [
        "aim",
        "done",
    ]


def test_trace_expansion_chunks_large_look_deltas():
    trace = Trace(
        name="t",
        events=[
            TraceEvent(kind="look", t_ms=100, dx=900, dy=15),
            TraceEvent(kind="key", t_ms=200, key="w", down=True),
        ],
    )
    timeline = trace_to_timeline(trace)
    looks = [event for event in timeline if event.kind == "look"]
    assert len(looks) > 1
    assert sum(event.payload["dx"] for event in looks) == 900
    assert all(abs(event.payload["dx"]) < 900 for event in looks)


def test_chunk_look_keeps_small_deltas_as_one_event():
    assert chunk_look(5, -3) == [(0.0, 5.0, -3.0)]


def test_raw_look_split_posts_each_recorded_delta_at_its_recorded_time():
    """Fidelity mode: a flick must keep its recorded time, not drift past the
    events recorded after it (which is what chunking does)."""
    trace = Trace(
        name="t",
        events=[
            TraceEvent(kind="key", t_ms=0, key="w", down=True),
            TraceEvent(kind="look", t_ms=100, dx=900, dy=15),
            TraceEvent(kind="look", t_ms=110, dx=-40, dy=0),
            TraceEvent(kind="key", t_ms=120, key="w", down=False),
        ],
    )
    timeline = trace_to_timeline(trace, look_split=LOOK_SPLIT_RAW)
    assert [(event.t_ms, event.kind) for event in timeline] == [
        (0.0, "key_down"),
        (100.0, "look"),
        (110.0, "look"),
        (120.0, "key_up"),
    ]
    assert [event.payload for event in timeline if event.kind == "look"] == [
        {"dx": 900.0, "dy": 15.0},
        {"dx": -40.0, "dy": 0.0},
    ]
    # Same trace, default strategy: the chunks of the flick outlive the key-up.
    chunked = trace_to_timeline(trace)
    assert max(event.t_ms for event in chunked if event.kind == "look") > 120
    assert sum(event.payload["dx"] for event in chunked if event.kind == "look") == 860


def test_trace_expansion_rejects_an_unknown_look_split_strategy():
    with pytest.raises(ValueError) as excinfo:
        trace_to_timeline(Trace(name="t", events=[]), look_split="smooth")
    assert "look_split" in str(excinfo.value)


def test_trace_expansion_keeps_labels_and_relative_order():
    trace = Trace(
        name="t",
        events=[
            TraceEvent(kind="key", t_ms=0, key="w", down=True),
            TraceEvent(kind="label", t_ms=100, text="a"),
            TraceEvent(kind="key", t_ms=100, key="w", down=False),
        ],
    )
    timeline = trace_to_timeline(trace)
    assert [(event.t_ms, event.kind) for event in timeline] == [
        (0.0, "key_down"),
        (100.0, "label"),
        (100.0, "key_up"),
    ]


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def test_player_dispatches_in_order_and_releases_keys_at_the_end():
    clock = FakeClock()
    player, backend = make_player(clock)
    timeline = [
        ScheduledEvent(0.0, "key_down", {"key": "w"}),
        ScheduledEvent(100.0, "look", {"dx": 10, "dy": -2}),
        ScheduledEvent(200.0, "key_up", {"key": "w"}),
    ]
    stats = player.play(timeline, start_ns=clock.now)
    assert backend.kinds() == ["key_down", "look", "key_up"]
    assert backend.payloads("look") == [(10.0, -2.0)]
    assert stats.events == 3
    assert not stats.aborted


def test_adjacent_identical_holds_replay_as_one_continuous_hold():
    clock = FakeClock()
    player, backend = make_player(clock)
    route = Route(name="r", steps=[Step.key(["w"], 500), Step.key(["w"], 500)])
    timeline = route_to_timeline(route)
    # The redundant release/press pair in the middle is never generated.
    assert [(event.t_ms, event.kind) for event in timeline] == [(0.0, "key_down"), (1000.0, "key_up")]
    stats = player.play(timeline, start_ns=clock.now)
    assert backend.kinds() == ["key_down", "key_up"]
    assert backend.calls[1][2] - backend.calls[0][2] >= 1_000 * MS - clock.quantum_ns
    assert stats.events == 2


def test_absolute_timeline_does_not_accumulate_lateness():
    """Lateness must stay bounded: a drifting scheduler would multiply it."""
    clock = FakeClock(overshoot_ns=400_000)
    player, _backend = make_player(clock)
    timeline = [
        ScheduledEvent(float(index * 100), "label", {"text": str(index)}) for index in range(6)
    ]
    start = clock.now
    collected = []
    player.play(
        timeline, start_ns=start, on_label=lambda text, t_ns: collected.append((int(text), t_ns))
    )
    assert [index for index, _ in collected] == [0, 1, 2, 3, 4, 5]
    lateness = [t_ns - (start + index * 100 * MS) for index, t_ns in collected]
    assert all(value >= 0 for value in lateness)
    assert max(lateness) < 2 * clock.overshoot_ns


def test_abort_stops_playback_and_releases_everything():
    clock = FakeClock()
    player, backend = make_player(clock)
    calls = {"count": 0}

    def abort():
        calls["count"] += 1
        return calls["count"] > 2

    timeline = [
        ScheduledEvent(0.0, "key_down", {"key": "w"}),
        ScheduledEvent(50.0, "key_down", {"key": "shift"}),
        ScheduledEvent(100.0, "look", {"dx": 5, "dy": 0}),
    ]
    stats = player.play(timeline, start_ns=clock.now, abort=abort)
    assert stats.aborted is True
    assert "key_up" in backend.kinds()  # release_all ran
    assert player.held_keys == set()


def test_release_all_after_an_exception_in_the_backend():
    clock = FakeClock()

    class Exploding(FakeInputBackend):
        def look(self, dx, dy):
            raise RuntimeError("boom")

    backend = Exploding(clock_ns=clock)
    player, _ = make_player(clock, backend)
    timeline = [
        ScheduledEvent(0.0, "key_down", {"key": "w"}),
        ScheduledEvent(10.0, "look", {"dx": 1, "dy": 0}),
    ]
    with pytest.raises(RuntimeError):
        player.play(timeline, start_ns=clock.now)
    assert player.held_keys == set()
    assert "key_up" in backend.kinds()


def test_button_coalescing_and_scroll_dispatch():
    clock = FakeClock()
    player, backend = make_player(clock)
    timeline = [
        ScheduledEvent(0.0, "button_down", {"button": "left"}),
        ScheduledEvent(10.0, "button_down", {"button": "left"}),
        ScheduledEvent(20.0, "button_up", {"button": "left"}),
        ScheduledEvent(30.0, "scroll", {"dy": -2.0}),
    ]
    player.play(timeline, start_ns=clock.now)
    assert backend.kinds() == ["button_down", "button_up", "scroll"]
    assert backend.payloads("scroll") == [(-2.0, 0.0)]


def test_player_reports_lateness_for_every_event():
    clock = FakeClock()
    player, _backend = make_player(clock)
    stats = player.play([ScheduledEvent(10.0, "label", {"text": "x"})], start_ns=clock.now)
    assert stats.max_lateness_ns >= 0
    assert stats.started_ns <= stats.ended_ns


def test_split_scalar_sums_exactly():
    chunks = split_scalar(-7, 120)
    assert sum(value for _offset, value in chunks) == -7


def test_look_scale_scales_injected_deltas_without_biasing_the_total():
    """A calibration factor must scale the whole turn, not each event on its own:
    halving four 1-point steps has to turn 2, not 0 (round) or 4."""
    backend = FakeInputBackend()
    player = Player(backend, look_scale=0.5)
    timeline = [
        ScheduledEvent(float(index * 10), "look", {"dx": 1.0, "dy": -1.0}) for index in range(4)
    ]
    player.play(timeline)
    looks = [payload for kind, payload, _ in backend.calls if kind == "look"]
    assert sum(dx for dx, _dy in looks) == 2
    assert sum(dy for _dx, dy in looks) == -2


def test_look_scale_of_one_is_a_no_op():
    backend = FakeInputBackend()
    player = Player(backend, look_scale=1.0)
    player.play([ScheduledEvent(0.0, "look", {"dx": 7, "dy": -3})])
    assert [payload for kind, payload, _ in backend.calls] == [(7.0, -3.0)]


# --------------------------------------------------------------------------- #
# frame-paced playback
# --------------------------------------------------------------------------- #
def test_play_frames_delivers_one_sample_per_period():
    clock = FakeClock()
    player, backend = make_player(clock)
    frames = [
        InputFrame(index=0, dx=10, dy=0, keys=("w",)),
        InputFrame(index=1, dx=10, dy=0, keys=("w",)),
        InputFrame(index=2, dx=10, dy=0, keys=()),
    ]
    stats = player.play_frames(frames, period_ns=10 * MS, start_ns=clock.now)
    assert stats.frames == 3
    assert backend.kinds() == ["key_down", "look", "look", "key_up", "look"]
    look_times = [t_ns for kind, _payload, t_ns in backend.calls if kind == "look"]
    assert look_times[1] - look_times[0] == pytest.approx(10 * MS, abs=2 * clock.overshoot_ns)
    assert look_times[2] - look_times[1] == pytest.approx(10 * MS, abs=2 * clock.overshoot_ns)
    # One look per frame, never batched: the game sees a fixed amount per frame.
    assert backend.payloads("look") == [(10.0, 0.0), (10.0, 0.0), (10.0, 0.0)]


def test_play_frames_consults_the_period_callable_for_every_frame():
    clock = FakeClock()
    player, backend = make_player(clock)
    periods = iter([10 * MS, 30 * MS, 20 * MS])
    frames = [InputFrame(index=index, dx=1) for index in range(3)]
    player.play_frames(frames, period_ns=lambda: next(periods), start_ns=clock.now)
    timestamps = [t_ns for _kind, _payload, t_ns in backend.calls]
    assert timestamps[1] - timestamps[0] == pytest.approx(10 * MS, abs=2 * clock.overshoot_ns)
    assert timestamps[2] - timestamps[1] == pytest.approx(30 * MS, abs=2 * clock.overshoot_ns)


def test_play_frames_keeps_held_keys_across_frames():
    clock = FakeClock()
    player, backend = make_player(clock)
    frames = [
        InputFrame(index=0, keys=("w", "shift")),
        InputFrame(index=1, keys=("shift",)),
        InputFrame(index=2),
    ]
    player.play_frames(frames, period_ns=10 * MS, start_ns=clock.now)
    assert backend.kinds() == ["key_down", "key_down", "key_up", "key_up"]
    assert backend.payloads("key_up") == ["w", "shift"]


def test_play_frames_releases_everything_when_aborted():
    clock = FakeClock()
    player, backend = make_player(clock)
    calls = {"count": 0}

    def abort():
        calls["count"] += 1
        return calls["count"] > 2

    frames = [InputFrame(index=index, keys=("w",), dx=1) for index in range(4)]
    stats = player.play_frames(frames, period_ns=10 * MS, start_ns=clock.now, abort=abort)
    assert stats.aborted is True
    assert player.held_keys == set()
    assert "key_up" in backend.kinds()


def test_play_frames_emits_labels_and_scrolls():
    clock = FakeClock()
    player, backend = make_player(clock)
    frames = [
        InputFrame(index=0, labels=("arena",), scroll_dy=-1.5, scroll_dx=0.5),
        InputFrame(index=1, labels=("boss",)),
    ]
    markers = []
    player.play_frames(
        frames,
        period_ns=10 * MS,
        start_ns=clock.now,
        on_label=lambda text, t_ns: markers.append((text, t_ns)),
    )
    assert [text for text, _ in markers] == ["arena", "boss"]
    assert backend.payloads("scroll") == [(-1.5, 0.5)]


def test_play_frames_scales_look_deltas_without_biasing_the_total():
    backend = FakeInputBackend()
    player = Player(backend, look_scale=0.5)
    frames = [InputFrame(index=index, dx=1, dy=-1) for index in range(4)]
    player.play_frames(frames, period_ns=10 * MS)
    looks = [payload for kind, payload, _ in backend.calls if kind == "look"]
    assert sum(dx for dx, _dy in looks) == 2
    assert sum(dy for _dx, dy in looks) == -2


def test_play_frames_with_no_frames_returns_immediately():
    backend = FakeInputBackend()
    player = Player(backend)
    stats = player.play_frames([], period_ns=10 * MS)
    assert stats.frames == 0
    assert backend.calls == []


# --------------------------------------------------------------------------- #
# gamepad state
# --------------------------------------------------------------------------- #
def gamepad_trace(states):
    return Trace(
        name="pad",
        events=[
            TraceEvent(kind="gamepad", t_ms=float(index * 10), state=state)
            for index, state in enumerate(states)
        ],
    )


def test_gamepad_states_are_published_once_per_change():
    backend = FakeInputBackend()
    gamepad = FakeGamepadBackend()
    player = Player(backend, gamepad=gamepad)
    turned = GamepadState(lx=1.0, buttons=("a",))
    timeline = trace_to_timeline(gamepad_trace([GamepadState(), turned, turned]))
    player.play(timeline)
    # A repeated snapshot is not re-sent, and the run ends centred.
    assert gamepad.states == [GamepadState(), turned, NEUTRAL_STATE]
    assert backend.calls == []


def test_gamepad_is_centred_when_playback_is_aborted():
    gamepad = FakeGamepadBackend()
    player = Player(FakeInputBackend(), gamepad=gamepad)
    timeline = trace_to_timeline(gamepad_trace([GamepadState(ly=-1.0), GamepadState(ly=-1.0, r2=0.5)]))
    player.play(timeline, abort=lambda: len(gamepad.states) >= 1)
    assert gamepad.neutral_sent >= 1
    assert gamepad.states[-1] == NEUTRAL_STATE


def test_gamepad_events_without_a_gamepad_backend_fail_loudly():
    player = Player(FakeInputBackend())
    with pytest.raises(RuntimeError) as excinfo:
        player.play([ScheduledEvent(0.0, "gamepad", {"state": GamepadState(lx=1.0)})])
    assert "gamepad backend" in str(excinfo.value)


def test_play_frames_delivers_gamepad_changes_and_centres_at_the_end():
    clock = FakeClock()
    gamepad = FakeGamepadBackend()
    player = Player(
        FakeInputBackend(clock_ns=clock),
        gamepad=gamepad,
        clock_ns=clock,
        sleep_until=lambda target: player_module.default_sleep_until(
            clock, target, 1_500_000, sleep=clock.sleep
        ),
    )
    frames = [
        InputFrame(index=0, gamepad=GamepadState()),
        InputFrame(index=1),
        InputFrame(index=2, gamepad=GamepadState(buttons=("a",))),
    ]
    stats = player.play_frames(frames, period_ns=10 * MS, start_ns=clock.now)
    assert stats.frames == 3
    assert gamepad.states == [GamepadState(), GamepadState(buttons=("a",)), NEUTRAL_STATE]
