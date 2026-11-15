"""End-to-end tests over fake backends: record -> simplify -> bench -> report.

These are the most valuable tests in the suite: they exercise the whole pipeline
(CLI, scheduler, sampling, statistics, report, exports) without needing a GPU, a
screen recording permission or an actual game.
"""

from __future__ import annotations

import csv
import io
import json
import time

import pytest

from fpsbench import cli
from fpsbench.backends import base as backends_base
from fpsbench.core.gamepad import NEUTRAL_STATE, GamepadState
from fpsbench.core.route import FpsConfig, Region, Route, Step
from fpsbench.core.samples import METALPERFTRACE, OCR, PRESENTMON
from fpsbench.core.trace import Trace, TraceEvent
from tests import fakes

MS = 1_000_000


@pytest.fixture()
def wired(monkeypatch):
    """Wire the CLI to fakes; returns the fakes it will use."""
    state = {
        "input": fakes.FakeInputBackend(),
        "recorder": fakes.FakeRecorderBackend(),
        "gamepad": fakes.FakeGamepadBackend(),
        "gamepad_recorder": fakes.FakeGamepadRecorder(),
        "fps": None,
        "preflight": fakes.FakePreflightModule(),
        "loaded": [],
    }

    def load_backend(role, *, platform=None, **kwargs):
        state["loaded"].append(role)
        if role == "input":
            return state["input"]
        if role == "record":
            return state["recorder"]
        if role == "gamepad":
            return state["gamepad"]
        if role == "gamepad_record":
            return state["gamepad_recorder"]
        if role == "fps":
            assert state["fps"] is not None, "the test must set state['fps'] first"
            return state["fps"]
        raise AssertionError("unexpected backend role %r" % role)

    class FakeTiming:
        @staticmethod
        def boost_timing():
            return ["timing boosted (test double)"]

        @staticmethod
        def restore_timing():
            state["timing_restored"] = True

    def load_module(name, *, platform=None):
        if name == "timing":
            return FakeTiming
        if name == "gamepad":
            return fakes.FakeGamepadModule
        if name == "gamepad_record":
            return fakes.FakeGamepadRecordModule
        return state["preflight"]

    monkeypatch.setattr(backends_base, "load_backend", load_backend)
    monkeypatch.setattr(backends_base, "load_module", load_module)
    monkeypatch.setattr(backends_base, "current_platform", lambda: "macos")
    return state


def scripted_recording():
    """A short, realistic recording: hold W, turn while holding, label, release."""
    events = [TraceEvent(kind="key", t_ms=0, key="w", down=True)]
    for position in range(6):
        events.append(TraceEvent(kind="look", t_ms=100 + position * 20, dx=30, dy=-1))
    events.append(TraceEvent(kind="label", t_ms=300, text="straight"))
    events.append(TraceEvent(kind="key", t_ms=700, key="shift", down=True))
    events.append(TraceEvent(kind="key", t_ms=1100, key="shift", down=False))
    events.append(TraceEvent(kind="key", t_ms=1300, key="w", down=False))
    return Trace(
        name="e2e",
        events=events,
        origin_platform="macos",
        metadata={"display_hz": 120, "fps_region": Region(x=10, y=20, w=100, h=30).to_dict()},
    )


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #
def test_record_writes_a_replayable_trace(wired, tmp_path, capsys):
    wired["recorder"] = fakes.FakeRecorderBackend(list(scripted_recording().events))
    out = tmp_path / "mine.trace.json"
    code = cli.main(
        ["record", "--out", str(out), "--name", "my route", "--region", "10,20,100,30", "--max-duration", "0.2"]
    )
    assert code == 0
    trace = Trace.load(str(out))
    assert trace.name == "my route"
    assert trace.origin_platform == "macos"
    assert trace.metadata["fps_region"]["w"] == 100
    assert trace.counts()["look"] == 6
    output = capsys.readouterr().out
    assert "wrote" in output
    assert "next: fpsbench bench" in output


def test_record_creates_the_output_directory(wired, tmp_path, capsys):
    """`record --out routes/liurnia.trace.json` must not need an existing `routes/`."""
    wired["recorder"] = fakes.FakeRecorderBackend(list(scripted_recording().events))
    out = tmp_path / "routes" / "deep" / "mine.trace.json"
    assert cli.main(["record", "--out", str(out), "--max-duration", "0.2"]) == 0
    assert out.exists()
    assert "wrote" in capsys.readouterr().out


def test_record_stops_when_the_finish_hotkey_is_pressed(wired, tmp_path, capsys):
    """The in-game finish hotkey ends the run without waiting for --max-duration."""
    recorder = fakes.FakeRecorderBackend(list(scripted_recording().events))
    recorder.finish_hotkey = "ctrl+alt+enter"
    recorder.stop_requested = lambda: True
    wired["recorder"] = recorder
    out = tmp_path / "hotkey.trace.json"
    code = cli.main(["record", "--out", str(out), "--max-duration", "60"])
    assert code == 0
    output = capsys.readouterr().out
    assert "press ctrl+alt+enter" in output
    assert "recording stopped (ctrl+alt+enter)" in output


def test_record_waits_for_the_start_hotkey(wired, tmp_path, capsys):
    """Nothing is captured until the hotkey starts the run, and the second press
    ends it - the trace only holds what was played in between."""

    class _ToggleRecorder(fakes.FakeRecorderBackend):
        records_immediately = False
        finish_hotkey = "ctrl+alt+enter"

        def __init__(self, events):
            super().__init__(list(events))
            self._recording = False
            self._polls = 0
            self._stopped = False

        def recording(self):
            return self._recording

        def stop_requested(self):
            return self._stopped

        def poll(self):
            self._polls += 1
            if self._polls == 1:
                return []  # still waiting for the start press
            if self._polls == 2:
                self._recording = True  # ctrl+alt+enter: start
                return []
            if self._polls == 3:
                self._stopped = True  # ctrl+alt+enter again: stop
                return super().poll()
            return []

    recorder = _ToggleRecorder(scripted_recording().events)
    wired["recorder"] = recorder
    out = tmp_path / "toggled.trace.json"
    assert cli.main(["record", "--out", str(out)]) == 0
    output = capsys.readouterr().out
    assert "waiting: press ctrl+alt+enter" in output
    assert "recording started - play the route now." in output
    assert "recording stopped (ctrl+alt+enter)" in output
    counts = Trace.load(str(out)).counts()
    assert (counts["key"], counts["look"], counts["label"]) == (4, 6, 1)


def test_record_reports_a_run_that_was_never_started(wired, tmp_path, capsys):
    class _NeverStarted(fakes.FakeRecorderBackend):
        records_immediately = False
        finish_hotkey = "ctrl+alt+enter"

        def recording(self):
            return False

        def stop_requested(self):
            return True

    # A recorder that was never started captured nothing, so it has nothing to give.
    wired["recorder"] = _NeverStarted([])
    out = tmp_path / "never.json"
    assert cli.main(["record", "--out", str(out)]) == 1
    error = capsys.readouterr().err
    assert "recording was never started" in error
    assert "ctrl+alt+enter" in error
    assert not out.exists()


def test_record_refuses_an_empty_recording(wired, tmp_path, capsys):
    wired["recorder"] = fakes.FakeRecorderBackend([])
    out = tmp_path / "empty.trace.json"
    code = cli.main(["record", "--out", str(out), "--max-duration", "0.2"])
    assert code == 1
    assert not out.exists()
    assert "nothing was recorded" in capsys.readouterr().err


def test_record_reports_suspicious_traces(wired, tmp_path, capsys):
    events = [
        TraceEvent(kind="key", t_ms=0, key="w", down=True),
        TraceEvent(kind="look", t_ms=100, dx=5, dy=0),
        TraceEvent(kind="label", t_ms=200, text="a"),
    ]
    wired["recorder"] = fakes.FakeRecorderBackend(events, warnings=["Secure Input is enabled"])
    out = tmp_path / "short.trace.json"
    assert cli.main(["record", "--out", str(out), "--max-duration", "0.2"]) == 0
    output = capsys.readouterr().out
    assert "Secure Input is enabled" in output
    assert "no key presses" not in output
    assert "without a matching release" in output or "too short" in output


# --------------------------------------------------------------------------- #
# bench
# --------------------------------------------------------------------------- #
class _GatedRecorder(fakes.FakeRecorderBackend):
    """Waits for the start hotkey like the real recorders do."""

    records_immediately = False
    finish_hotkey = "ctrl+alt+enter"

    def __init__(self, *, start_after_polls: Optional[int] = None) -> None:
        super().__init__([])
        self._start_after = start_after_polls
        self._checks = 0
        self.started_listening = False

    def start(self) -> None:
        super().start()
        self.started_listening = True

    def recording(self) -> bool:
        self._checks += 1
        return self._start_after is not None and self._checks >= self._start_after

    def poll(self):
        return []

    def stop_requested(self) -> bool:
        return False


def test_bench_creates_the_report_directory(wired, tmp_path, capsys):
    """The quick start writes `reports/baseline.json` before `reports/` exists."""
    route = Route(
        name="mkdir",
        steps=[Step.key(["w"], 120)],
        repeat=1,
        fps=FpsConfig(mode="ocr", region=Region(x=0, y=0, w=50, h=20, space="pixels"), sample_hz=50),
    )
    route_path = tmp_path / "route.json"
    route.save(str(route_path))

    wired["fps"] = fakes.ScriptedOcrFpsBackend([60.0] * 60, interval_s=0.005)
    json_out = tmp_path / "reports" / "baseline.json"
    csv_out = tmp_path / "reports" / "nested" / "baseline.csv"
    code = cli.main(
        [
            "bench",
            str(route_path),
            "--settle-ms",
            "0",
            "--warmup-ms",
            "0",
            "--json",
            str(json_out),
            "--csv",
            str(csv_out),
        ]
    )
    assert code == 0
    assert json_out.exists() and csv_out.exists()
    output = capsys.readouterr().out
    assert "wrote %s" % json_out in output


def test_bench_creates_the_directory_for_a_simplified_route(wired, tmp_path):
    trace = scripted_recording()
    trace_path = tmp_path / "t.trace.json"
    trace.save(str(trace_path))
    out = tmp_path / "routes" / "nested" / "liurnia.json"
    assert cli.main(["bench", str(trace_path), "--simplify", str(out), "--simplify-only"]) == 0
    assert Route.load(str(out)).steps


def test_bench_waits_for_the_start_hotkey(wired, tmp_path, capsys):
    """The run only begins once the hotkey is pressed; the wait is not measured."""
    route = Route(
        name="gated",
        steps=[Step.key(["w"], 120)],
        repeat=1,
        fps=FpsConfig(mode="ocr", region=Region(x=0, y=0, w=50, h=20, space="pixels"), sample_hz=50),
    )
    route_path = tmp_path / "route.json"
    route.save(str(route_path))

    recorder = _GatedRecorder(start_after_polls=3)
    wired["recorder"] = recorder
    wired["fps"] = fakes.ScriptedOcrFpsBackend([60.0] * 60, interval_s=0.005)
    json_out = tmp_path / "report.json"
    code = cli.main(
        ["bench", str(route_path), "--settle-ms", "0", "--warmup-ms", "0", "--json", str(json_out)]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "press ctrl+alt+enter (with the game focused) to start the benchmark" in output
    payload = json.loads(json_out.read_text())
    assert "benchmark started by the ctrl+alt+enter hotkey" in payload["notes"]
    assert recorder.stopped is True  # the listener is released before the run


def test_bench_reports_a_run_that_was_never_started(wired, tmp_path, capsys):
    route = Route(
        name="never",
        steps=[Step.key(["w"], 120)],
        repeat=1,
        fps=FpsConfig(mode="ocr", region=Region(x=0, y=0, w=50, h=20, space="pixels"), sample_hz=50),
    )
    route_path = tmp_path / "route.json"
    route.save(str(route_path))

    wired["recorder"] = _GatedRecorder()  # the hotkey is never pressed
    wired["fps"] = fakes.ScriptedOcrFpsBackend([60.0] * 60, interval_s=0.005)
    code = cli.main(["bench", str(route_path), "--settle-ms", "0", "--timeout", "0.3"])
    assert code == 1
    error = capsys.readouterr().err
    assert "the benchmark was never started" in error
    assert "ctrl+alt+enter" in error


def test_bench_end_to_end_with_ocr_samples(wired, tmp_path, capsys):
    route = Route(
        name="e2e",
        steps=[Step.key(["w"], 400, label="straight"), Step.turn(120, 0, 120, keys=["w"]), Step.key(["w"], 200)],
        repeat=2,
        fps=FpsConfig(mode="ocr", region=Region(x=0, y=0, w=50, h=20, space="pixels"), sample_hz=50),
    )
    route_path = tmp_path / "route.json"
    route.save(str(route_path))

    wired["fps"] = fakes.ScriptedOcrFpsBackend([60.0, 61.0, 0.0, 300.0, 59.0] * 40, interval_s=0.005)
    json_out = tmp_path / "report.json"
    csv_out = tmp_path / "samples.csv"
    code = cli.main(
        [
            "bench",
            str(route_path),
            "--settle-ms",
            "0",
            "--warmup-ms",
            "0",
            "--json",
            str(json_out),
            "--csv",
            str(csv_out),
            "--no-color",
        ]
    )
    assert code == 0
    payload = json.loads(json_out.read_text())
    assert payload["source"] == OCR
    assert payload["repeats"] == 2
    assert payload["overall"]["samples"] > 0
    assert payload["metadata"]["route_name"] == "e2e"
    assert payload["metadata"]["fps_source"] == OCR
    assert payload["metadata"]["platform_info"]["gpu"] == "Fake GPU"
    assert payload["metadata"]["warmup_ms"] == 0.0
    assert payload["metadata"]["repeats"] == 2
    assert payload["metadata"]["fps_config"]["region"]["w"] == 50
    # The platform timing boost is applied, reported and released again.
    assert "timing boosted (test double)" in payload["metadata"]["backend_notes"]
    assert wired["timing_restored"] is True
    assert wired["input"].torn_down is True

    rows = list(csv.DictReader(io.StringIO(csv_out.read_text())))
    assert len(rows) == payload["overall"]["samples"]
    assert all(row["source"] == OCR for row in rows)
    assert any(row["label"] == "straight" for row in rows)

    output = capsys.readouterr().out
    assert "FPS source:" in output
    assert "per repeat" in output
    assert "frame time timeline" in output


def test_bench_with_presentmon_samples(wired, tmp_path):
    route = Route(name="pm", steps=[Step.key(["w"], 300)], fps=FpsConfig(mode="presentmon"))
    route_path = tmp_path / "route.json"
    route.save(str(route_path))

    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=400, dt_ns=5 * MS, interval_s=0.004)
    json_out = tmp_path / "report.json"
    assert (
        cli.main(
            [
                "bench",
                str(route_path),
                "--settle-ms",
                "0",
                "--pacing",
                "time",
                "--json",
                str(json_out),
                "--no-color",
            ]
        )
        == 0
    )
    payload = json.loads(json_out.read_text())
    assert payload["source"] == PRESENTMON
    assert payload["overall"]["frame"]["frames"] > 0
    assert payload["overall"]["frame"]["stutters"] >= 1  # the fake inserts 3x frames
    assert any("presentmon" in note.lower() for note in payload["metadata"]["backend_notes"])


def test_bench_with_metalperftrace_samples(wired, tmp_path, capsys):
    route = Route(name="mpt", steps=[Step.key(["w"], 300)], fps=FpsConfig(mode="metalperftrace"))
    route_path = tmp_path / "route.json"
    route.save(str(route_path))

    wired["fps"] = fakes.FakeMetalPerfTraceFpsBackend(frames=400, dt_ns=5 * MS, interval_s=0.004)
    json_out = tmp_path / "report.json"
    assert (
        cli.main(["bench", str(route_path), "--settle-ms", "0", "--json", str(json_out), "--no-color"]) == 0
    )
    payload = json.loads(json_out.read_text())
    assert payload["source"] == METALPERFTRACE
    assert payload["overall"]["frame"]["frames"] > 0
    assert payload["metadata"]["fps_source"] == METALPERFTRACE
    assert any("metalperftrace" in note.lower() for note in payload["metadata"]["backend_notes"])
    output = capsys.readouterr().out
    assert "metalperftrace" in output
    assert "NOT per-frame data" not in output


def test_bench_simplifies_a_trace_and_can_stop_there(wired, tmp_path, capsys):
    trace = scripted_recording()
    assert trace.metadata["fps_region"] is not None
    trace_path = tmp_path / "t.trace.json"
    trace.save(str(trace_path))
    out = tmp_path / "t.json"

    # --simplify-only must not need an FPS backend or inject input at all.
    code = cli.main(["bench", str(trace_path), "--simplify", str(out), "--simplify-only", "--repeat", "2"])
    assert code == 0
    route = Route.load(str(out))
    assert route.repeat == 2
    assert route.fps.region is not None  # carried over from the trace metadata
    assert route.metadata["source_trace_hash"] == trace.hash()
    assert route.duration_ms == pytest.approx(trace.duration_ms)
    assert "wrote" in capsys.readouterr().out


def test_bench_on_a_trace_runs_the_same_pipeline(wired, tmp_path):
    trace = scripted_recording()
    trace_path = tmp_path / "t.trace.json"
    trace.save(str(trace_path))
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=600, dt_ns=4 * MS, interval_s=0.003)
    json_out = tmp_path / "trace-report.json"
    assert (
        cli.main(
            ["bench", str(trace_path), "--settle-ms", "0", "--json", str(json_out), "--no-color"]
        )
        == 0
    )
    payload = json.loads(json_out.read_text())
    assert payload["route_source"] == "trace"
    # The trace records where it was recorded, which is not necessarily where it
    # is replayed (a macOS trace can be benchmarked on Windows).
    assert payload["metadata"]["origin_platform"] == "macos"
    # The recorder's label shows up as a segment.
    labels = [segment["label"] for run in payload["runs"] for segment in run["segments"]]
    assert "straight" in labels


def test_bench_activates_the_requested_window(wired, tmp_path):
    route = Route(name="w", steps=[Step.key(["w"], 200)], fps=FpsConfig(mode="presentmon"))
    route_path = tmp_path / "route.json"
    route.save(str(route_path))
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=200, dt_ns=5 * MS, interval_s=0.003)
    assert (
        cli.main(
            ["bench", str(route_path), "--settle-ms", "0", "--window", "Game", "--no-color"]
        )
        == 0
    )
    assert wired["input"].prepared_with["window_title"] == "Game"


def test_bench_fails_when_the_window_is_missing(wired, tmp_path, capsys):
    route = Route(name="w", steps=[Step.key(["w"], 200)], fps=FpsConfig(mode="presentmon"))
    route_path = tmp_path / "route.json"
    route.save(str(route_path))
    wired["fps"] = fakes.FakePresentMonFpsBackend()
    assert cli.main(["bench", str(route_path), "--window", "nope", "--no-color"]) == 1
    assert "no window title contains" in capsys.readouterr().err


def test_bench_requires_a_region_for_ocr(wired, tmp_path, capsys):
    route = Route(name="r", steps=[Step.key(["w"], 100)], fps=FpsConfig(mode="ocr"))
    route_path = tmp_path / "route.json"
    route.save(str(route_path))
    wired["fps"] = fakes.ScriptedOcrFpsBackend([60.0])
    assert cli.main(["bench", str(route_path), "--no-color"]) == 1
    assert "no FPS region configured" in capsys.readouterr().err


def test_bench_fails_cleanly_when_no_samples_arrive(wired, tmp_path, capsys):
    route = Route(
        name="r",
        steps=[Step.key(["w"], 150)],
        fps=FpsConfig(mode="ocr", region=Region(x=0, y=0, w=10, h=10), sample_hz=50),
    )
    route_path = tmp_path / "route.json"
    route.save(str(route_path))
    wired["fps"] = fakes.ScriptedOcrFpsBackend([], interval_s=0.001)  # captures nothing
    assert cli.main(["bench", str(route_path), "--settle-ms", "0", "--no-color"]) == 1
    assert "no FPS samples were collected" in capsys.readouterr().err


def test_bench_rejects_a_broken_route_file(wired, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format": "route", "schema_version": 1, "name": "x", "steps": [], "oops": 1}))
    assert cli.main(["bench", str(bad)]) == 1
    assert "unknown field" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# dry-run / pick-region / doctor
# --------------------------------------------------------------------------- #
def test_dry_run_exercises_the_capture_chain(wired, capsys):
    wired["fps"] = fakes.ScriptedOcrFpsBackend([59.0, 60.0, 61.0] * 20, interval_s=0.005)
    code = cli.main(["dry-run", "--seconds", "0.2", "--fps", "ocr", "--region", "0,0,50,20"])
    assert code == 0
    output = capsys.readouterr().out
    assert "input backend: fake-input" in output
    assert "FPS source:" in output
    assert "usable samples" in output


def test_dry_run_reports_a_fractional_window_faithfully(wired, capsys):
    wired["fps"] = fakes.ScriptedOcrFpsBackend([59.0, 60.0, 61.0] * 20, interval_s=0.005)
    assert cli.main(["dry-run", "--seconds", "0.2", "--fps", "ocr", "--region", "0,0,50,20"]) == 0
    assert "in 0.2 s window" in capsys.readouterr().out


def test_dry_run_trusts_an_empty_authoritative_sample_set(wired, capsys):
    """The vetted OCR list is authoritative even when every reading was rejected;
    falling back to the raw stream would report samples the vetting threw away."""
    backend = fakes.ScriptedOcrFpsBackend([59.0, 60.0, 61.0] * 20, interval_s=0.005)
    backend.final_samples = lambda: []
    wired["fps"] = backend
    assert cli.main(["dry-run", "--seconds", "0.2", "--fps", "ocr", "--region", "0,0,50,20"]) == 1
    assert "no samples were collected" in capsys.readouterr().err


def test_dry_run_needs_a_region_for_ocr(wired, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FPSBENCH_CONFIG", str(tmp_path / "config.json"))
    assert cli.main(["dry-run", "--fps", "ocr"]) == 1
    assert "needs a region" in capsys.readouterr().err


def test_pick_region_writes_the_config_and_the_route(wired, tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("FPSBENCH_CONFIG", str(config_path))
    wired["preflight"].picked = Region(x=100, y=200, w=120, h=40, space="pixels")

    route = Route(name="r", steps=[Step.key(["w"], 100)])
    route_path = tmp_path / "route.json"
    route.save(str(route_path))

    assert cli.main(["pick-region", "--route", str(route_path), "--fps", "ocr"]) == 0
    output = capsys.readouterr().out
    assert "reads" in output

    config = json.loads(config_path.read_text())
    assert config["region"]["x"] == 100
    assert config["fps"]["mode"] == "ocr"

    reloaded = Route.load(str(route_path))
    assert reloaded.fps.region is not None
    assert reloaded.fps.region.w == 120
    assert reloaded.fps.mode == "ocr"


def test_pick_region_can_be_cancelled(wired, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FPSBENCH_CONFIG", str(tmp_path / "config.json"))
    wired["preflight"].picked = None
    assert cli.main(["pick-region", "--no-config"]) == 1
    assert "cancelled" in capsys.readouterr().out


def test_doctor_prints_checks(wired, capsys):
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "fpsbench" in output
    assert "Fake GPU" in output
    assert "no FPS region configured" in output


def test_doctor_list_windows_prints_the_titles(wired, capsys):
    """The hint for a missing target window points at ``--list-windows``, so that
    listing must actually print the titles on every platform."""
    assert cli.main(["doctor", "--list-windows"]) == 0
    output = capsys.readouterr().out
    assert "window titles:" in output
    assert "Game Window" in output


def test_doctor_creates_the_json_directory(wired, tmp_path):
    target = tmp_path / "out" / "deep" / "checks.json"
    assert cli.main(["doctor", "--json", str(target)]) == 0
    assert json.loads(target.read_text())["platform"] == "macos"


def test_doctor_exits_nonzero_on_a_fatal_check(wired, capsys):
    wired["preflight"] = fakes.FakePreflightModule(
        checks=[
            backends_base.Check("Accessibility permission", False, "not granted", fix="do the thing", fatal=True)
        ]
    )
    assert cli.main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "FAIL" in output
    assert "do the thing" in output


# --------------------------------------------------------------------------- #
# misc CLI surface
# --------------------------------------------------------------------------- #
def test_parse_region_accepts_several_spellings():
    assert cli.parse_region("10,20,120,40").w == 120
    assert cli.parse_region("10 20 120 40").y == 20
    assert cli.parse_region("120x40+10+20").x == 10
    with pytest.raises(ValueError):
        cli.parse_region("10,20")


def test_resolve_fps_config_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("FPSBENCH_CONFIG", str(tmp_path / "config.json"))
    cli.update_config(region={"x": 1, "y": 2, "w": 3, "h": 4, "space": "points"})

    class Args:
        fps = None
        region = None
        presentmon = None
        process_name = None

    route = Route(name="r", steps=[], fps=FpsConfig(mode="ocr", sample_hz=12))
    config = cli.resolve_fps_config(Args(), route)
    assert config.mode == "ocr"  # the route wins over the config file
    assert config.sample_hz == 12
    assert config.region.x == 1  # the config file supplies the region

    Args.fps = "presentmon"
    Args.region = "50,60,70,80"
    Args.process_name = "game.exe"
    config = cli.resolve_fps_config(Args(), route)
    assert config.mode == "presentmon"
    assert config.process_name == "game.exe"
    assert (config.region.x, config.region.w) == (50.0, 70.0)


def test_resolve_fps_config_fills_process_name_from_the_config_file(tmp_path, monkeypatch):
    """The per-frame sources need the process name; storing it once is enough."""
    monkeypatch.setenv("FPSBENCH_CONFIG", str(tmp_path / "config.json"))
    cli.update_config(process_name="eldenring", fps={"mode": "auto", "process_name": "eldenring"})

    class Args:
        fps = None
        region = None
        presentmon = None
        process_name = None

    route = Route(name="r", steps=[], fps=FpsConfig(mode="metalperftrace"))
    config = cli.resolve_fps_config(Args(), route)
    assert config.mode == "metalperftrace"  # an explicit route mode always wins
    assert config.process_name == "eldenring"
    assert config.region is None  # and no region is required for it


def test_config_fps_accepts_a_config_with_only_a_process_name(tmp_path, monkeypatch):
    monkeypatch.setenv("FPSBENCH_CONFIG", str(tmp_path / "config.json"))
    cli.update_config(process_name="eldenring")
    resolved = cli.config_fps()
    assert resolved is not None
    assert resolved.process_name == "eldenring"
    assert resolved.region is None


def test_simplify_carries_the_process_name_into_the_route():
    from fpsbench.core import simplify as simplify_module

    metadata = {"fps_mode": "auto", "process_name": "eldenring"}
    config = simplify_module.fps_config_from_metadata(metadata)
    assert config.process_name == "eldenring"
    assert config.region is None


def test_bad_cli_input_is_reported_without_a_traceback(wired, capsys):
    assert cli.main(["bench", "routes/example.json", "--region", "nonsense"]) == 1
    assert "expected x,y,w,h" in capsys.readouterr().err
    assert cli.main(["bench", "routes/does-not-exist.json"]) == 1
    assert "cannot read routes/does-not-exist.json" in capsys.readouterr().err
    assert cli.main(["pick-region", "--manual", "--region", "10,20"]) == 1
    assert "expected x,y,w,h" in capsys.readouterr().err


def test_load_playable_rejects_unknown_formats(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"format": "csv", "events": []}))
    with pytest.raises(ValueError) as excinfo:
        cli.load_playable(str(path))
    assert "expected 'route' or 'trace'" in str(excinfo.value)


def test_example_files_load_and_simplify(tmp_path):
    kind, route = cli.load_playable("routes/example.json")
    assert kind == "route" and route.duration_ms > 0
    kind, trace = cli.load_playable("routes/example.trace.json")
    assert kind == "trace"
    from fpsbench.core.simplify import simplify

    result = simplify(trace)
    assert result.route.steps
    assert result.route.duration_ms == pytest.approx(trace.duration_ms)


def test_record_releases_the_recorder_when_start_fails(wired, tmp_path, monkeypatch, capsys):
    """A half-started recorder owns a thread/window; failing to start must free it."""
    from fpsbench.backends.base import BackendUnavailable

    def boom():
        raise BackendUnavailable("CGEventTapCreate returned NULL", hint="grant Accessibility")

    monkeypatch.setattr(wired["recorder"], "start", boom)
    code = cli.main(["record", "--out", str(tmp_path / "never.json"), "--max-duration", "0.1"])
    assert code == 1
    assert wired["recorder"].stopped is True
    assert "Accessibility" in capsys.readouterr().err


def test_bench_applies_look_scale_to_injected_deltas(wired, tmp_path, capsys):
    """`--look-scale` calibrates a game whose mouse pipeline is not 1:1 with the
    event deltas we record (e.g. it reads raw device counts and over-turns)."""
    route = Route(
        name="scaled",
        steps=[Step.key(["w"], 100), Step.turn(200, 0, 100, keys=["w"])],
        repeat=1,
        fps=FpsConfig(mode="ocr", region=Region(x=0, y=0, w=50, h=20, space="pixels"), sample_hz=50),
    )
    route_path = tmp_path / "route.json"
    route.save(str(route_path))
    wired["fps"] = fakes.ScriptedOcrFpsBackend([60.0] * 60, interval_s=0.005)
    json_out = tmp_path / "report.json"

    code = cli.main(
        [
            "bench",
            str(route_path),
            "--settle-ms", "0",
            "--warmup-ms", "0",
            "--look-scale", "0.5",
            "--json", str(json_out),
        ]
    )
    assert code == 0
    injected = [payload for kind, payload, _ in wired["input"].calls if kind == "look"]
    assert sum(dx for dx, _dy in injected) == 100  # 200 points of turn, halved
    assert "scaled by 0.5" in " ".join(json.loads(json_out.read_text())["notes"])


# --------------------------------------------------------------------------- #
# frame pacing
# --------------------------------------------------------------------------- #
def test_bench_frame_pacing_quantises_input_and_reports_the_whole_trace(wired, tmp_path, capsys):
    """The default replay quantises the route into input frames: one sample per
    rendered frame, so the path does not depend on the device's frame rate."""
    trace = scripted_recording()
    trace_path = tmp_path / "t.trace.json"
    trace.save(str(trace_path))
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=600, dt_ns=20 * MS, interval_s=0.002)
    json_out = tmp_path / "report.json"

    code = cli.main(
        [
            "bench",
            str(trace_path),
            "--pacing",
            "frames",
            "--frame-period-ms",
            "1",
            "--settle-ms",
            "0",
            "--json",
            str(json_out),
            "--no-color",
        ]
    )
    assert code == 0
    payload = json.loads(json_out.read_text())
    assert payload["metadata"]["input_pacing"] == "frames"
    assert payload["metadata"]["fps_window"] == "whole trace"
    assert payload["metadata"]["frame_period_ms"] == pytest.approx(1.0)
    assert payload["metadata"]["look_split"] == "frame-quantised"
    # 1300 ms at the 60 Hz reference program -> 78 input frames.
    assert payload["metadata"]["input_frames"] == 78
    assert payload["whole_trace"]["samples"] > 0

    looks = [payload for kind, payload, _ in wired["input"].calls if kind == "look"]
    assert 0 < len(looks) <= 78  # never a burst of events inside one frame
    assert sum(dx for dx, _dy in looks) == 180  # 6 recorded looks of 30 px
    output = capsys.readouterr().out
    assert "whole trace" in output
    assert "frame-paced replay" in output


def test_bench_time_pacing_keeps_the_recorded_timeline(wired, tmp_path):
    trace = scripted_recording()
    trace_path = tmp_path / "t.trace.json"
    trace.save(str(trace_path))
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=600, dt_ns=5 * MS, interval_s=0.002)
    json_out = tmp_path / "report.json"

    code = cli.main(
        [
            "bench",
            str(trace_path),
            "--pacing",
            "time",
            "--settle-ms",
            "0",
            "--json",
            str(json_out),
            "--no-color",
        ]
    )
    assert code == 0
    payload = json.loads(json_out.read_text())
    assert payload["metadata"]["input_pacing"] == "time"
    assert payload["metadata"]["look_split"] == "chunk"
    assert "frame_period_ms" not in payload["metadata"]
    looks = [payload for kind, payload, _ in wired["input"].calls if kind == "look"]
    assert len(looks) == 6  # the recorded events, one for one


def test_bench_look_split_raw_posts_each_recorded_delta_at_its_recorded_time(wired, tmp_path, capsys):
    """Trajectory comparison: a 900 px flick must not be spread past the movement
    recorded after it, and the strategy has to be visible in the report."""
    trace = Trace(
        name="flick",
        events=[
            TraceEvent(kind="key", t_ms=0, key="w", down=True),
            TraceEvent(kind="look", t_ms=100, dx=900, dy=0),
            TraceEvent(kind="key", t_ms=150, key="w", down=False),
        ],
    )
    trace_path = tmp_path / "flick.trace.json"
    trace.save(str(trace_path))
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=400, dt_ns=5 * MS, interval_s=0.002)
    json_out = tmp_path / "report.json"

    code = cli.main(
        [
            "bench",
            str(trace_path),
            "--pacing",
            "time",
            "--look-split",
            "raw",
            "--settle-ms",
            "0",
            "--json",
            str(json_out),
            "--no-color",
        ]
    )
    assert code == 0
    assert wired["input"].kinds() == ["key_down", "look", "key_up"]
    assert wired["input"].payloads("look") == [(900.0, 0.0)]
    payload = json.loads(json_out.read_text())
    assert payload["metadata"]["look_split"] == "raw"
    assert any("recorded time" in note for note in payload["notes"])
    assert "look timing: raw" in capsys.readouterr().out


def test_look_split_is_rejected_when_the_input_is_frame_quantised(wired, capsys):
    """Frame-paced replay has no per-event look timing, so a strategy would be
    silently ignored - refuse instead."""
    code = cli.main(
        ["bench", "routes/example.trace.json", "--look-split", "raw", "--settle-ms", "0"]
    )
    assert code == 1
    assert "--look-split only applies to --pacing time" in capsys.readouterr().err


def test_look_split_is_rejected_for_a_simplified_route(wired, capsys):
    """A route's turns are interpolated by design, so the strategy cannot apply."""
    code = cli.main(
        ["bench", "routes/example.json", "--pacing", "time", "--look-split", "raw", "--settle-ms", "0"]
    )
    assert code == 1
    assert "applies to recorded traces" in capsys.readouterr().err


def test_bench_input_hz_sets_the_program_granularity(wired, tmp_path):
    trace = scripted_recording()
    trace_path = tmp_path / "t.trace.json"
    trace.save(str(trace_path))
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=400, dt_ns=5 * MS, interval_s=0.002)
    json_out = tmp_path / "report.json"

    assert (
        cli.main(
            [
                "bench",
                str(trace_path),
                "--frame-period-ms",
                "1",
                "--input-hz",
                "30",
                "--settle-ms",
                "0",
                "--json",
                str(json_out),
            ]
        )
        == 0
    )
    payload = json.loads(json_out.read_text())
    assert payload["metadata"]["input_rate_hz"] == 30.0
    assert payload["metadata"]["input_frames"] == 39  # 1300 ms at 30 Hz


def test_frame_pace_follows_the_measured_frame_intervals():
    """The default pace is not a constant: it is the device's own frame time."""
    from fpsbench.core.samples import FpsSample

    class _Collector:
        def __init__(self):
            self.samples = []

    collector = _Collector()
    pace = cli._FramePace(int(1e9 / 60), collector)
    pace.begin()
    base = time.perf_counter_ns()
    for index in range(60):
        collector.samples.append(
            FpsSample(source=PRESENTMON, t_ns=base + index * 33 * MS, dt_ns=33 * MS)
        )
    assert pace() == 33 * MS
    assert pace.source == "measured"

    fixed = cli._FramePace(int(1e9 / 60), collector, 8 * MS)
    assert fixed() == 8 * MS
    assert fixed.source == "fixed"


def test_frame_pacing_replays_the_same_input_stream_on_different_frame_rates(wired, tmp_path):
    """The point of frame pacing: the per-frame input sequence is the device's
    frame rate invariant, only the wall-clock duration changes."""
    trace = scripted_recording()
    trace_path = tmp_path / "t.trace.json"
    trace.save(str(trace_path))

    def injected_looks(period_ms):
        wired["input"].calls.clear()
        wired["fps"] = fakes.FakePresentMonFpsBackend(frames=600, dt_ns=5 * MS, interval_s=0.002)
        assert (
            cli.main(
                [
                    "bench",
                    str(trace_path),
                    "--frame-period-ms",
                    period_ms,
                    "--settle-ms",
                    "0",
                ]
            )
            == 0
        )
        return [(kind, payload) for kind, payload, _ in wired["input"].calls]

    fast = injected_looks("4")
    slow = injected_looks("33")
    assert fast == slow
    assert any(kind == "look" for kind, _payload in fast)


# --------------------------------------------------------------------------- #
# gamepad route
# --------------------------------------------------------------------------- #
def gamepad_recording(*, end_ms=600.0):
    trace = Trace(
        name="pad",
        events=[
            TraceEvent(kind="gamepad", t_ms=0, state=GamepadState()),
            TraceEvent(kind="gamepad", t_ms=100, state=GamepadState(lx=1.0, buttons=("a",))),
            TraceEvent(kind="gamepad", t_ms=300, state=GamepadState(l2=0.5)),
        ],
        origin_platform="macos",
        metadata={"gamepad": {"device": "Fake Pad", "source": "fake", "poll_hz": 250.0}},
        end_ms=end_ms,
    )
    return trace


def test_record_with_gamepad_writes_a_version_two_trace(wired, tmp_path, capsys):
    wired["recorder"] = fakes.FakeRecorderBackend(list(scripted_recording().events))
    wired["gamepad_recorder"] = fakes.FakeGamepadRecorder(
        [
            [(0.0, GamepadState())],
            [(120.0, GamepadState(rx=-1.0, buttons=("b",)))],
            [],
        ]
    )
    out = tmp_path / "pad.trace.json"
    code = cli.main(
        ["record", "--out", str(out), "--gamepad", "--gamepad-device", "Fake", "--max-duration", "0.3"]
    )
    assert code == 0
    trace = Trace.load(str(out))
    assert trace.schema_version == 2
    assert trace.counts()["gamepad"] == 2
    assert trace.metadata["gamepad"]["device"] == "Fake Pad"
    assert trace.duration_ms >= 120.0
    assert wired["gamepad_recorder"].prepared_with["device"] == "Fake"
    assert wired["gamepad_recorder"].started and wired["gamepad_recorder"].stopped
    output = capsys.readouterr().out
    assert "2 gamepad" in output


def test_bench_replays_gamepad_states_and_centres_them_again(wired, tmp_path):
    trace = gamepad_recording()
    path = tmp_path / "pad.trace.json"
    trace.save(str(path))
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=400, dt_ns=5 * MS, interval_s=0.002)
    json_out = tmp_path / "report.json"

    code = cli.main(
        [
            "bench",
            str(path),
            "--pacing",
            "time",
            "--settle-ms",
            "0",
            "--json",
            str(json_out),
            "--no-color",
        ]
    )
    assert code == 0
    gamepad = wired["gamepad"]
    assert gamepad.prepared and gamepad.torn_down
    assert gamepad.states[:-1] == [
        GamepadState(),
        GamepadState(lx=1.0, buttons=("a",)),
        GamepadState(l2=0.5),
    ]
    assert gamepad.states[-1] == NEUTRAL_STATE  # centred when the run ends
    payload = json.loads(json_out.read_text())
    assert payload["metadata"]["gamepad_backend"] == "fake-gamepad"
    assert payload["metadata"]["gamepad"]["device"] == "Fake Pad"
    assert any("gamepad state mirrors the recording" in note for note in payload["notes"])


def test_bench_never_falls_back_when_the_virtual_gamepad_is_unavailable(wired, tmp_path, capsys):
    trace = gamepad_recording()
    path = tmp_path / "pad.trace.json"
    trace.save(str(path))
    wired["gamepad"] = fakes.FakeGamepadBackend(fail_on_prepare=True)
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=100, dt_ns=5 * MS, interval_s=0.002)

    code = cli.main(["bench", str(path), "--pacing", "time", "--settle-ms", "0"])
    assert code == 1
    assert "no virtual gamepad here" in capsys.readouterr().err
    assert wired["input"].torn_down  # nothing is left prepared
    assert wired["input"].calls == []  # and no keyboard/mouse substitute was played


def test_simplify_refuses_a_gamepad_trace(wired, tmp_path, capsys):
    path = tmp_path / "pad.trace.json"
    gamepad_recording().save(str(path))
    code = cli.main(["bench", str(path), "--simplify-only", "--simplify", str(tmp_path / "r.json")])
    assert code == 1
    assert "gamepad events" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()


def test_probe_gamepad_reports_both_directions(wired, capsys):
    code = cli.main(["probe-gamepad"])
    assert code == 0
    output = capsys.readouterr().out
    assert "SDL gamepad reading" in output
    assert "gamepad injection" in output.lower()
    assert "reading: ok" in output
    assert "injection: blocked" in output


def test_doctor_reports_gamepad_capabilities(wired, capsys):
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "SDL gamepad reading" in output
    assert "gamepad injection" in output.lower()


def test_input_config_is_strict_and_applies_when_the_flag_is_absent(wired, tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("FPSBENCH_CONFIG", str(config_path))
    trace = gamepad_recording()
    path = tmp_path / "pad.trace.json"
    trace.save(str(path))
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=400, dt_ns=5 * MS, interval_s=0.002)
    json_out = tmp_path / "report.json"

    config_path.write_text(json.dumps({"input": {"look_split": "raw"}}))
    assert (
        cli.main(
            ["bench", str(path), "--pacing", "time", "--settle-ms", "0", "--json", str(json_out)]
        )
        == 0
    )
    assert json.loads(json_out.read_text())["metadata"]["look_split"] == "raw"

    config_path.write_text(json.dumps({"input": {"look_split": "smooth"}}))
    assert cli.main(["bench", str(path), "--pacing", "time", "--settle-ms", "0"]) == 1

    config_path.write_text(json.dumps({"input": {"look_split": "raw", "sens": 2}}))
    assert cli.main(["bench", str(path), "--pacing", "time", "--settle-ms", "0"]) == 1

def test_bench_frame_pacing_carries_gamepad_changes(wired, tmp_path):
    """Frame-paced replay sends the same controller states, one change per frame."""
    trace = gamepad_recording(end_ms=1000.0)
    path = tmp_path / "pad.trace.json"
    trace.save(str(path))
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=400, dt_ns=5 * MS, interval_s=0.002)
    json_out = tmp_path / "report.json"

    code = cli.main(
        [
            "bench",
            str(path),
            "--frame-period-ms",
            "1",
            "--settle-ms",
            "0",
            "--json",
            str(json_out),
            "--no-color",
        ]
    )
    assert code == 0
    gamepad = wired["gamepad"]
    assert gamepad.states[:2] == [GamepadState(), GamepadState(lx=1.0, buttons=("a",))]
    assert gamepad.states[-1] == NEUTRAL_STATE
    payload = json.loads(json_out.read_text())
    assert payload["metadata"]["input_pacing"] == "frames"
    # 1000 ms at the 60 Hz program: the explicit duration decides the frame count.
    assert payload["metadata"]["input_frames"] == 60


def test_gamepad_recording_starts_with_the_start_hotkey(wired, tmp_path):
    """The controller stream must not start before the recorder is armed."""
    recorder = _GatedRecorder(start_after_polls=2)
    wired["recorder"] = recorder
    wired["gamepad_recorder"] = fakes.FakeGamepadRecorder(
        [[(0.0, GamepadState())], [(400.0, GamepadState(rx=1.0))]]
    )
    out = tmp_path / "gated.trace.json"

    code = cli.main(["record", "--out", str(out), "--gamepad", "--max-duration", "0.5"])
    assert code == 0
    assert recorder.started_listening
    assert wired["gamepad_recorder"].started
    assert wired["gamepad_recorder"].polls_before_start == 0
    trace = Trace.load(str(out))
    assert trace.counts()["gamepad"] == 2


def test_probe_gamepad_can_stream_and_pulse(wired, capsys):
    wired["gamepad_recorder"] = fakes.FakeGamepadRecorder(
        [[(0.0, GamepadState()), (50.0, GamepadState(lx=1.0, buttons=("a",)))]]
    )
    code = cli.main(["probe-gamepad", "--seconds", "0.1", "--pulse"])
    assert code == 1  # the fake virtual device reports "unavailable"
    output = capsys.readouterr().out
    assert "streaming for 0.1 s" in output
    assert "lx=+1.00" in output
    assert "skipped: virtual gamepad injection is unavailable" in output
    assert wired["gamepad_recorder"].prepared_with["device"] is None


def test_a_mixed_trace_drives_both_input_paths(wired, tmp_path):
    """A recording can contain keyboard *and* controller input; both are replayed."""
    trace = Trace(
        name="mixed",
        events=[
            TraceEvent(kind="key", t_ms=0, key="w", down=True),
            TraceEvent(kind="gamepad", t_ms=50, state=GamepadState(rx=-1.0)),
            TraceEvent(kind="key", t_ms=150, key="w", down=False),
        ],
        end_ms=200.0,
    )
    path = tmp_path / "mixed.trace.json"
    trace.save(str(path))
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=400, dt_ns=5 * MS, interval_s=0.002)

    code = cli.main(["bench", str(path), "--pacing", "time", "--settle-ms", "0"])
    assert code == 0
    assert "key_down" in wired["input"].kinds()
    assert wired["gamepad"].states[0] == GamepadState(rx=-1.0)
    assert wired["gamepad"].states[-1] == NEUTRAL_STATE


# --------------------------------------------------------------------------- #
# measure
# --------------------------------------------------------------------------- #
class _InputThenHotkeyRecorder(fakes.FakeRecorderBackend):
    """Starts on the hotkey, emits scripted input, then honours the stop hotkey.

    ``recording()`` turns True once ``recording_after_polls`` polls have happened
    (the stand-in for the start hotkey); ``schedule`` is a list of
    ``(poll_index, events)`` delivered on that poll; ``stop_requested()`` turns
    True after ``stop_after_polls`` checks, which is the stop hotkey.
    """

    records_immediately = False
    finish_hotkey = "ctrl+alt+enter"

    def __init__(
        self,
        schedule=(),
        *,
        stop_after_polls: int = 25,
        recording_after_polls: int = 1,
    ) -> None:
        super().__init__([])
        self._schedule = [(int(after), list(events)) for after, events in schedule]
        self._stop_after = stop_after_polls
        self._recording_after = recording_after_polls
        self._polls = 0
        self._stop_checks = 0
        self.arm_called = False

    def arm(self) -> None:
        self.arm_called = True

    def recording(self) -> bool:
        return self._polls >= self._recording_after

    def poll(self):
        self._polls += 1
        due = [entry for entry in self._schedule if self._polls >= entry[0]]
        self._schedule = [entry for entry in self._schedule if self._polls < entry[0]]
        events = []
        for _after, batch in due:
            events.extend(batch)
        return events

    def stop_requested(self) -> bool:
        self._stop_checks += 1
        return self._stop_checks >= self._stop_after


def test_measure_starts_on_the_hotkey_and_stops_on_the_next_press(wired, tmp_path, capsys):
    recorder = _InputThenHotkeyRecorder(stop_after_polls=15)
    wired["recorder"] = recorder
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=400, dt_ns=2 * MS, interval_s=0.002)
    json_out = tmp_path / "measure.json"
    csv_out = tmp_path / "measure.csv"

    code = cli.main(
        [
            "measure",
            "--fps",
            "presentmon",
            "--process-name",
            "Game.exe",
            "--settle-seconds",
            "0",
            "--json",
            str(json_out),
            "--csv",
            str(csv_out),
        ]
    )
    assert code == 0
    assert recorder.arm_called is False  # the hotkey starts it, nothing is armed
    output = capsys.readouterr().out
    assert "ready: press ctrl+alt+enter (with the game focused) to start measuring" in output
    assert "measuring started" in output
    assert "measuring - press ctrl+alt+enter again to stop" in output
    assert "average" in output and "1% low" in output

    payload = json.loads(json_out.read_text())
    assert payload["source"] == PRESENTMON
    assert payload["duration_s"] > 0
    assert payload["samples"] > 0
    assert payload["summary"]["avg_fps"] > 0
    assert payload["summary"]["low_1pct_fps"] <= payload["summary"]["avg_fps"]
    assert payload["metadata"]["fps_source"] == PRESENTMON

    rows = list(csv.DictReader(io.StringIO(csv_out.read_text())))
    assert len(rows) == payload["samples"]
    assert recorder.stopped is True


def test_measure_settles_before_watching_for_the_hotkey(wired, tmp_path, capsys):
    """The start hotkey is only watched after the settle period."""
    recorder = _InputThenHotkeyRecorder([], stop_after_polls=60)
    wired["recorder"] = recorder
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=400, dt_ns=2 * MS, interval_s=0.002)
    json_out = tmp_path / "measure.json"

    code = cli.main(
        [
            "measure",
            "--fps",
            "presentmon",
            "--process-name",
            "Game.exe",
            "--settle-seconds",
            "0.2",
            "--json",
            str(json_out),
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "settling for 0.2 s" in output
    assert output.index("settling for 0.2 s") < output.index("ready: press")
    assert json.loads(json_out.read_text())["samples"] > 0


def test_measure_does_not_read_the_controller(wired, tmp_path, capsys):
    """The window starts on the hotkey, so no controller (or pygame) is needed."""
    wired["recorder"] = _InputThenHotkeyRecorder([], stop_after_polls=15)
    wired["gamepad_recorder"] = fakes.FakeGamepadRecorder(
        [[(0.0, GamepadState(lx=0.5))]]
    )
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=400, dt_ns=2 * MS, interval_s=0.002)

    code = cli.main(
        [
            "measure",
            "--fps",
            "presentmon",
            "--process-name",
            "Game.exe",
            "--settle-seconds",
            "0",
        ]
    )
    assert code == 0
    assert wired["gamepad_recorder"].started is False
    assert "controller" not in capsys.readouterr().out


def test_measure_reports_a_window_that_was_never_started(wired, capsys):
    wired["recorder"] = _InputThenHotkeyRecorder([], recording_after_polls=10**9)
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=50, dt_ns=5 * MS, interval_s=0.002)
    code = cli.main(
        [
            "measure",
            "--fps",
            "presentmon",
            "--process-name",
            "x",
            "--settle-seconds",
            "0",
            "--timeout",
            "0.2",
        ]
    )
    assert code == 1
    error = capsys.readouterr().err
    assert "was not started before the timeout" in error
    assert "ctrl+alt+enter" in error


def test_measure_timeout_covers_the_settle_period(wired, capsys):
    """``--timeout`` bounds the whole command, the settle included."""
    wired["recorder"] = _InputThenHotkeyRecorder([], stop_after_polls=10**9)
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=50, dt_ns=5 * MS, interval_s=0.002)
    began = time.monotonic()
    code = cli.main(
        [
            "measure",
            "--fps",
            "presentmon",
            "--process-name",
            "x",
            "--settle-seconds",
            "2",
            "--timeout",
            "0.2",
        ]
    )
    elapsed = time.monotonic() - began
    assert code == 1
    error = capsys.readouterr().err
    assert "ran out during the settle period" in error
    assert elapsed < 1.5


def test_measure_countdown_closes_the_window_and_reports(wired, tmp_path, capsys):
    """``--countdown S`` ends the window S seconds after the start hotkey."""
    wired["recorder"] = _InputThenHotkeyRecorder([], stop_after_polls=10**9)
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=400, dt_ns=2 * MS, interval_s=0.002)
    json_out = tmp_path / "countdown.json"
    began = time.monotonic()
    code = cli.main(
        [
            "measure",
            "--fps",
            "presentmon",
            "--process-name",
            "x",
            "--settle-seconds",
            "0",
            "--countdown",
            "0.3",
            "--json",
            str(json_out),
        ]
    )
    elapsed = time.monotonic() - began
    assert code == 0
    assert elapsed >= 0.25
    payload = json.loads(json_out.read_text())
    assert payload["countdown_s"] == 0.3
    assert 0.25 <= payload["duration_s"] <= 0.7
    assert payload["samples"] > 0
    assert "countdown" in capsys.readouterr().out


def test_measure_rejects_a_non_positive_countdown(wired, capsys):
    code = cli.main(
        ["measure", "--fps", "presentmon", "--process-name", "x", "--countdown", "0"]
    )
    assert code == 1
    assert "--countdown" in capsys.readouterr().err


def test_measure_reports_an_immediate_stop(wired, capsys):
    wired["recorder"] = _InputThenHotkeyRecorder([], stop_after_polls=0)
    wired["fps"] = fakes.FakePresentMonFpsBackend(frames=50, dt_ns=5 * MS, interval_s=0.002)
    code = cli.main(
        ["measure", "--fps", "presentmon", "--process-name", "x", "--settle-seconds", "0"]
    )
    assert code == 1
    error = capsys.readouterr().err
    assert "no FPS samples inside the measured window" in error


def test_measure_needs_a_region_for_ocr(wired, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FPSBENCH_CONFIG", str(tmp_path / "config.json"))
    assert cli.main(["measure", "--fps", "ocr"]) == 1
    assert "needs a region" in capsys.readouterr().err


def test_measure_has_an_fps_alias():
    args = cli.build_parser().parse_args(["fps", "--fps", "presentmon"])
    assert args.func is cli.cmd_measure
