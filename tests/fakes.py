"""Fake backends and a fake clock.

These are what make the *whole* pipeline (record -> simplify -> bench -> report)
testable on a machine that is not allowed to inject input or capture the screen.
They are also the only place where timing is simulated, so the scheduler tests
stay deterministic.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fpsbench.backends.base import (
    BackendUnavailable,
    Check,
    FpsBackend,
    GamepadBackend,
    GamepadRecorderBackend,
    InputBackend,
    OcrBackend,
    RecorderBackend,
)
from fpsbench.core.gamepad import NEUTRAL_STATE, GamepadState
from fpsbench.core.ocrparse import FpsParse, parse_fps_detail
from fpsbench.core.route import FpsConfig, Region
from fpsbench.core.samples import METALPERFTRACE, OCR, FpsSample, PRESENTMON
from fpsbench.core.trace import TraceEvent

__all__ = [
    "FakeInputBackend",
    "FakeRecorderBackend",
    "FakeOcrBackend",
    "FakeGamepadBackend",
    "FakeGamepadRecorder",
    "FakeGamepadModule",
    "FakeGamepadRecordModule",
    "ScriptedOcrFpsBackend",
    "FakePresentMonFpsBackend",
    "FakeMetalPerfTraceFpsBackend",
    "FakeClock",
    "FakePreflightModule",
    "make_preflight_module",
]


class FakeInputBackend(InputBackend):
    """Records every call so playback can be asserted exactly."""

    name = "fake-input"

    def __init__(self, clock_ns=time.perf_counter_ns) -> None:
        self.clock_ns = clock_ns
        self.calls: List[Tuple[str, Any, int]] = []
        self.prepared_with: Dict[str, Any] = {}
        self.torn_down = False
        self.windows: List[str] = ["Game Window"]
        self.activated: Optional[str] = None

    def _record(self, kind: str, payload: Any) -> None:
        self.calls.append((kind, payload, self.clock_ns()))

    def prepare(self, *, window_title=None, post_to_pid=None) -> None:
        self.prepared_with = {"window_title": window_title, "post_to_pid": post_to_pid}

    def key_down(self, key: str) -> None:
        self._record("key_down", key)

    def key_up(self, key: str) -> None:
        self._record("key_up", key)

    def look(self, dx: float, dy: float) -> None:
        self._record("look", (dx, dy))

    def scroll(self, dy: float, dx: float = 0.0) -> None:
        self._record("scroll", (dy, dx))

    def button_down(self, button: str) -> None:
        self._record("button_down", button)

    def button_up(self, button: str) -> None:
        self._record("button_up", button)

    def teardown(self) -> None:
        self.torn_down = True

    def find_window(self, title_substring: str) -> Optional[str]:
        for title in self.windows:
            if title_substring.lower() in title.lower():
                return title
        return None

    def activate_window(self, title_substring: str) -> bool:
        found = self.find_window(title_substring)
        self.activated = found
        return found is not None

    def kinds(self) -> List[str]:
        return [kind for kind, _, _ in self.calls]

    def payloads(self, kind: str) -> List[Any]:
        return [payload for call_kind, payload, _ in self.calls if call_kind == kind]


class FakeRecorderBackend(RecorderBackend):
    """Replays a scripted event list, one batch per ``poll``."""

    name = "fake-recorder"
    label_hotkey = "ctrl+alt+1..9"
    supports_labels = True

    def __init__(self, events: Sequence[TraceEvent] = (), warnings: Sequence[str] = ()) -> None:
        self._pending: List[TraceEvent] = list(events)
        self._warnings = list(warnings)
        self.started = False
        self.stopped = False
        self.armed = False

    def prepare(self, *, window_title=None) -> None:
        return None

    def start(self) -> None:
        self.started = True

    def arm(self) -> None:
        self.armed = True

    def poll(self) -> List[TraceEvent]:
        events, self._pending = self._pending, []
        return events

    def stop(self) -> None:
        self.stopped = True

    def warnings(self) -> List[str]:
        return list(self._warnings)


class FakeOcrBackend(OcrBackend):
    """Cycles through scripted texts (one per ``read_fps`` call)."""

    name = "fake-ocr"
    region_space = "pixels"

    def __init__(self, texts: Sequence[str], *, fail_after: Optional[int] = None) -> None:
        self.texts = list(texts)
        self.fail_after = fail_after
        self.calls = 0
        self.prepared = False

    def prepare(self) -> None:
        self.prepared = True

    def grab_region(self, region: Region) -> str:
        return "fake-image"

    def read_fps(self, region: Region, *, min_fps: float = 0.5, max_fps: float = 2000.0) -> FpsParse:
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise RuntimeError("scripted capture failure")
        text = self.texts[self.calls % len(self.texts)] if self.texts else ""
        self.calls += 1
        return parse_fps_detail(text, min_fps=min_fps, max_fps=max_fps)


class ScriptedOcrFpsBackend(FpsBackend):
    """An OCR FPS backend that emits scripted samples without touching the screen."""

    source = OCR

    def __init__(self, values: Sequence[float], *, interval_s: float = 0.02) -> None:
        self.values = list(values)
        self.interval_s = interval_s
        self.started = False
        self.stopped = False
        self._notes: List[str] = []

    def start(self, config: FpsConfig, *, process_name=None, presentmon_path=None) -> None:
        self.started = True

    def samples(self):
        for value in self.values:
            if self.stopped:
                return
            time.sleep(self.interval_s)
            if value <= 0 or value > 2000:  # what the real sampler rejects as a misread
                continue
            yield FpsSample(source=OCR, t_ns=time.perf_counter_ns(), value=value)

    def stop(self) -> None:
        self.stopped = True

    def final_samples(self):
        return None

    def notes(self) -> List[str]:
        return list(self._notes)


class FakePresentMonFpsBackend(FpsBackend):
    """PresentMon-shaped stream (frame intervals) without an actual process."""

    source = PRESENTMON

    def __init__(self, frames: int = 300, dt_ns: int = 10_000_000, *, interval_s: float = 0.002) -> None:
        self.frames = frames
        self.dt_ns = dt_ns
        self.interval_s = interval_s
        self.stopped = False
        self._notes: List[str] = ["PresentMon test double"]

    def start(self, config: FpsConfig, *, process_name=None, presentmon_path=None) -> None:
        return None

    def samples(self):
        base = time.perf_counter_ns()
        for index in range(self.frames):
            if self.stopped:
                return
            time.sleep(self.interval_s)
            dt = self.dt_ns * (3 if index % 50 == 0 else 1)
            yield FpsSample(source=PRESENTMON, t_ns=base + index * self.dt_ns, dt_ns=dt)

    def stop(self) -> None:
        self.stopped = True

    def final_samples(self):
        return None

    def notes(self) -> List[str]:
        return list(self._notes)


class FakeMetalPerfTraceFpsBackend(FpsBackend):
    """metalperftrace-shaped stream (reconstructed frame intervals) without a process."""

    source = METALPERFTRACE

    def __init__(self, frames: int = 300, dt_ns: int = 10_000_000, *, interval_s: float = 0.002) -> None:
        self.frames = frames
        self.dt_ns = dt_ns
        self.interval_s = interval_s
        self.stopped = False
        self._notes: List[str] = ["metalperftrace test double"]

    def start(self, config: FpsConfig, *, process_name=None, presentmon_path=None) -> None:
        return None

    def samples(self):
        base = time.perf_counter_ns()
        for index in range(self.frames):
            if self.stopped:
                return
            time.sleep(self.interval_s)
            dt = self.dt_ns * (3 if index % 50 == 0 else 1)
            yield FpsSample(source=METALPERFTRACE, t_ns=base + index * self.dt_ns, dt_ns=dt)

    def stop(self) -> None:
        self.stopped = True

    def final_samples(self):
        return None

    def notes(self) -> List[str]:
        return list(self._notes)


class FakeGamepadBackend(GamepadBackend):
    """Records every state the player publishes (virtual device stand-in)."""

    name = "fake-gamepad"

    def __init__(self, *, fail_on_prepare: bool = False) -> None:
        self.states: List[GamepadState] = []
        self.prepared = False
        self.torn_down = False
        self.neutral_sent = 0
        self.fail_on_prepare = fail_on_prepare

    def prepare(self, *, window_title=None) -> None:
        if self.fail_on_prepare:
            raise BackendUnavailable(
                "no virtual gamepad here (test double)",
                hint="install the platform virtual gamepad first",
            )
        self.prepared = True

    def set_state(self, state: GamepadState) -> None:
        self.states.append(state)

    def neutral(self) -> None:
        self.neutral_sent += 1
        self.states.append(NEUTRAL_STATE)

    def teardown(self) -> None:
        self.torn_down = True

    def notes(self) -> List[str]:
        return ["fake gamepad: no real device was opened"]


class FakeGamepadRecorder(GamepadRecorderBackend):
    """Emits scripted controller changes, one batch per ``poll``."""

    name = "fake-gamepad-recorder"

    def __init__(self, batches: Sequence[Sequence[Tuple[float, GamepadState]]] = ()) -> None:
        self._batches = [list(batch) for batch in batches]
        self.started = False
        self.stopped = False
        self.polls_before_start = 0
        self.prepared_with: Dict[str, Any] = {}
        self._end_ms: Optional[float] = None

    def prepare(self, *, device: Optional[str] = None, poll_hz: Optional[float] = None) -> None:
        self.prepared_with = {"device": device, "poll_hz": poll_hz}

    def start(self) -> None:
        self.started = True

    def poll(self) -> List[TraceEvent]:
        if not self.started:
            self.polls_before_start += 1
        if not self._batches:
            return []
        batch = self._batches.pop(0)
        out = [
            TraceEvent(kind="gamepad", t_ms=t_ms, state=state) for t_ms, state in batch
        ]
        if batch:
            self._end_ms = max(self._end_ms or 0.0, float(batch[-1][0]))
        return out

    def stop(self) -> None:
        self.stopped = True

    def end_ms(self) -> Optional[float]:
        return self._end_ms

    def device_info(self) -> Dict[str, Any]:
        return {
            "device": "Fake Pad",
            "source": self.name,
            "axes": {"range": [-1.0, 1.0]},
            "triggers": {"range": [0.0, 1.0]},
            "poll_hz": 250.0,
        }

    def notes(self) -> List[str]:
        return ["fake gamepad recorder: no hardware was read"]


class FakeGamepadModule:
    """Stand-in for a platform ``gamepad`` module (doctor / probe-gamepad)."""

    @staticmethod
    def capability() -> Dict[str, Any]:
        return {
            "platform": "fake",
            "ok": False,
            "reason": "no virtual gamepad in the test double",
            "hint": "use a real backend",
        }

    @staticmethod
    def BACKEND(**kwargs: Any) -> FakeGamepadBackend:
        return FakeGamepadBackend(**kwargs)


class FakeGamepadRecordModule:
    """Stand-in for a platform ``gamepad_record`` module."""

    @staticmethod
    def probe():
        return [Check("SDL gamepad reading", True, "SDL GameController: Fake Pad")]

    @staticmethod
    def BACKEND(**kwargs: Any) -> FakeGamepadRecorder:
        return FakeGamepadRecorder(**kwargs)


class FakeClock:
    """Deterministic clock whose ``sleep`` overshoots like a real scheduler.

    Every read advances the clock by ``quantum_ns`` so the scheduler's busy-wait
    loop terminates.  Coarse sleeps overshoot by ``overshoot_ns``, which is what
    the "no accumulated drift" test depends on.
    """

    def __init__(
        self,
        start_ns: int = 1_000_000_000,
        *,
        quantum_ns: int = 50_000,
        overshoot_ns: int = 400_000,
    ) -> None:
        self.now = int(start_ns)
        self.quantum_ns = int(quantum_ns)
        self.overshoot_ns = int(overshoot_ns)
        self.sleeps: List[float] = []

    def __call__(self) -> int:
        value = self.now
        self.now += self.quantum_ns
        return value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        requested = int(seconds * 1e9)
        self.now += requested + self.overshoot_ns if requested > 0 else self.quantum_ns

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * 1e9)


class FakePreflightModule:
    """Stands in for a platform ``preflight`` module in CLI tests."""

    def __init__(self, *, info: Optional[Dict[str, Any]] = None, checks: Sequence[Any] = ()) -> None:
        self.info = info or {"gpu": "Fake GPU", "resolution": "1920x1080", "refresh_hz": 120}
        self._checks = list(checks)
        self.picked: Optional[Region] = None

    def preflight(self, *, window_title=None, region=None):
        return list(self._checks)

    def platform_info(self) -> Dict[str, Any]:
        return dict(self.info)

    def trial_read(self, region: Region):
        return parse_fps_detail("144 FPS"), None

    def pick_region(self, *, message: str = "") -> Optional[Region]:
        return self.picked

    def manual_region(self, prompt: str = "") -> Optional[Region]:
        return self.picked

    def list_window_titles(self) -> List[str]:
        return ["Game Window"]


def make_preflight_module(**kwargs: Any) -> FakePreflightModule:
    return FakePreflightModule(**kwargs)
