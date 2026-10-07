"""``metalperftrace`` stream parsing and interval reconstruction (no macOS needed).

``fpsbench.backends._metalperftrace`` shells out and parses JSON, so all of it is
importable and testable everywhere.  The fixtures below mirror the real
``metalperftrace listen --json`` payload (pretty-printed objects, concatenated),
captured from macOS 27 against a ``CAMetalLayer`` window.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import pytest

from fpsbench.backends import _metalperftrace as mpt
from fpsbench.backends._metalperftrace import (
    MetalPerfTraceFps,
    _pick_interval,
    extract_updates,
    find_metalperftrace,
    interval_frame_times_ms,
    parse_utc_ns,
)
from fpsbench.backends.base import BackendUnavailable
from fpsbench.core.route import FpsConfig
from fpsbench.core.samples import METALPERFTRACE

#: Fixed monotonic/wall-clock reference used by the backend under test.
CLOCK_REF_NS = 1_000_000_000_000
WALL_REF_NS = parse_utc_ns("2026-10-06T16:00:30.000Z")
END_DATE = "2026-10-06T16:00:31.058Z"

# --------------------------------------------------------------------------- #
# fixtures modelled on the real payload
# --------------------------------------------------------------------------- #
def frame_interval_stats(count, total_ms, min_ms, max_ms, average_ms=None, stddev_ms=0.1):
    if average_ms is None:
        average_ms = total_ms / count if count else 0.0
    return {
        "Count": count,
        "Total (ms)": total_ms,
        "Min (ms)": min_ms,
        "Max (ms)": max_ms,
        "Average (ms)": average_ms,
        "StdDev (ms)": stddev_ms,
    }


def update(
    start_mct,
    end_mct,
    layers,
    *,
    process="Game",
    pid=4242,
    date="2026-10-06T16:00:47.553Z",
):
    return {
        "States": {},
        "PID": pid,
        "Process": process,
        "Date": date,
        "Timestamp": 1791302447.553,
        "Layers": layers,
    }


def layer(count, total_ms, min_ms, max_ms, *, end_date="2026-10-06T16:00:31.058Z"):
    fps = (count * 1000.0 / total_ms) if total_ms else 0.0
    return {
        "Configuration": [{"Name": "", "Width (pixels)": 1600, "Height (pixels)": 1200}],
        "Performance Stats": {
            "Start MCT Seconds": 0.0,  # overwritten by the caller
            "End MCT Seconds": 0.0,
            "Start Date": "2026-10-06T16:00:30.058Z",
            "End Date": end_date,
            "Total Duration (sec)": 1.0,
            "Time Active (sec)": 1.0,
            "Frame-On-Glass Interval Stats": frame_interval_stats(count, total_ms, min_ms, max_ms),
            "Presented Frame Stats": {"Frame Count": count, "FPS": fps},
        },
    }


def timed_update(start_mct, end_mct, specs, *, end_date="2026-10-06T16:00:31.058Z"):
    """``specs`` is a list of ``(count, total_ms, min_ms, max_ms)``, one per layer."""
    layers = []
    for count, total_ms, min_ms, max_ms in specs:
        entry = layer(count, total_ms, min_ms, max_ms, end_date=end_date)
        entry["Performance Stats"]["Start MCT Seconds"] = start_mct
        entry["Performance Stats"]["End MCT Seconds"] = end_mct
        layers.append(entry)
    return update(start_mct, end_mct, layers)


class FakeProcess:
    """Stands in for ``metalperftrace listen --json --output FILE``.

    Like the real tool it writes NDJSON (one compact object per line) to the
    ``--output`` path as soon as it starts, and rewrites the file as one
    pretty-printed JSON array when it is terminated.
    """

    def __init__(self, payload: str = "", *, exit_code=None, array_on_exit=True) -> None:
        self.payload = payload
        self._exit_code = exit_code
        self._array_on_exit = array_on_exit
        self.returncode = None
        self.terminated = False
        self.args = None
        self.output_path = None

    # -- what Popen would do ------------------------------------------------ #
    def write_output(self, path: str) -> None:
        self.output_path = path
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.payload)

    def _rewrite_as_array(self) -> None:
        if not self.output_path:
            return
        objects = []
        for line in self.payload.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                objects.append(json.loads(line))
            except ValueError:
                continue
        with open(self.output_path, "w", encoding="utf-8") as handle:
            json.dump(objects, handle, indent=2)

    # -- the subprocess interface ------------------------------------------ #
    def poll(self):
        return self._exit_code

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        if self._array_on_exit:
            self._rewrite_as_array()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


def make_backend(monkeypatch, payload="", *, exit_code=None, array_on_exit=True, **kwargs):
    proc = FakeProcess(payload, exit_code=exit_code, array_on_exit=array_on_exit)

    def popen(args, **popen_kwargs):
        proc.args = args
        path = args[args.index("--output") + 1]
        proc.write_output(path)
        return proc

    monkeypatch.setattr(mpt, "find_metalperftrace", lambda explicit=None: "/usr/bin/metalperftrace")
    monkeypatch.setattr(mpt, "metalperftrace_version", lambda exe=None, **kw: "macOS 27.0")
    # Process-name resolution reads the live app list; keep the tests hermetic.
    monkeypatch.setattr(mpt, "_running_applications", lambda: [])
    options = {
        "poll_interval_s": 0.01,
        "startup_timeout_s": 1.0,
        "clock_ns": lambda: CLOCK_REF_NS,
        "wall_clock_ns": lambda: WALL_REF_NS,
    }
    options.update(kwargs)
    backend = MetalPerfTraceFps(popen=popen, **options)
    return backend, proc


def ndjson(updates):
    """The tool's running layout: one compact object per line."""
    return "".join(json.dumps(entry) + "\n" for entry in updates)


def collect(backend, config, *, settle_s=0.05):
    """Consume the stream the way the CLI does: live, then the final re-read."""
    backend.start(config)
    streamed = []

    def consume():
        try:
            streamed.extend(backend.samples())
        except BaseException as exc:  # pragma: no cover - surfaces as a test failure
            streamed.append(exc)

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    time.sleep(settle_s)
    backend.stop()
    thread.join(timeout=3)
    assert not thread.is_alive(), "samples() did not stop when asked"
    final = backend.final_samples()
    return streamed if final is None else final


# --------------------------------------------------------------------------- #
# locating the tool
# --------------------------------------------------------------------------- #
def test_find_metalperftrace_prefers_an_explicit_path(tmp_path):
    fake = tmp_path / "metalperftrace"
    fake.write_text("")
    assert find_metalperftrace(str(fake)) == str(fake)
    assert find_metalperftrace(str(tmp_path / "missing")) is None


def test_metalperftrace_exists_on_this_machine_but_the_helper_is_importable_everywhere():
    # The module must be importable on any OS; discovery simply returns None.
    assert mpt.metalperftrace_version(None)  # never empty


# --------------------------------------------------------------------------- #
# interval -> frame time reconstruction
# --------------------------------------------------------------------------- #
def test_reconstruction_preserves_count_total_min_and_max():
    frames = interval_frame_times_ms(frame_interval_stats(4, 64.0, 8.0, 24.0))
    assert len(frames) == 4
    assert frames[0] == 8.0 and frames[-1] == 24.0
    assert sum(frames) == pytest.approx(64.0)
    assert min(frames) == 8.0 and max(frames) == 24.0


def test_reconstruction_of_a_single_frame_is_the_whole_interval():
    assert interval_frame_times_ms(frame_interval_stats(1, 33.3, 33.3, 33.3)) == [33.3]


def test_reconstruction_of_two_frames_keeps_both_extremes():
    assert interval_frame_times_ms(frame_interval_stats(2, 40.0, 8.0, 32.0)) == [8.0, 32.0]


def test_reconstruction_falls_back_to_the_mean_without_a_total():
    frames = interval_frame_times_ms({"Count": 3, "Average (ms)": 10.0})
    assert frames == [10.0, 10.0, 10.0]


def test_reconstruction_ignores_empty_or_broken_intervals():
    assert interval_frame_times_ms({}) == []
    assert interval_frame_times_ms({"Count": 0, "Total (ms)": 0}) == []
    # min + max larger than the total cannot happen; never emit a negative frame
    frames = interval_frame_times_ms(frame_interval_stats(4, 10.0, 8.0, 9.0))
    assert all(frame >= 0 for frame in frames)


# --------------------------------------------------------------------------- #
# stream parsing
# --------------------------------------------------------------------------- #
def test_extract_updates_reads_pretty_printed_objects_back_to_back():
    first = json.dumps(update(0.0, 1.0, [layer(2, 33.0, 16.0, 17.0)]), indent=2)
    second = json.dumps(update(1.0, 2.0, [layer(2, 33.0, 16.0, 17.0)]), indent=2)
    updates, remainder = extract_updates(first + "\n" + second + "\n")
    assert len(updates) == 2
    assert remainder == ""
    assert updates[1]["PID"] == 4242


def test_extract_updates_keeps_a_torn_tail_for_the_next_read():
    complete = json.dumps(update(0.0, 1.0, [layer(2, 33.0, 16.0, 17.0)]))
    torn = complete[:40]
    updates, remainder = extract_updates(complete + "\n" + torn)
    assert len(updates) == 1
    assert remainder == torn


def test_extract_updates_handles_compact_ndjson_too():
    line = json.dumps(update(0.0, 1.0, [layer(2, 33.0, 16.0, 17.0)]))
    updates, remainder = extract_updates(line + "\n" + line + "\n")
    assert [len(u["Layers"]) for u in updates] == [1, 1]
    assert remainder == ""


def test_extract_updates_handles_the_pretty_array_the_tool_rewrites_on_exit():
    first = update(0.0, 1.0, [layer(2, 33.0, 16.0, 17.0)])
    second = update(1.0, 2.0, [layer(2, 33.0, 16.0, 17.0)])
    as_array = json.dumps([first, second], indent=2)
    updates, remainder = extract_updates(as_array)
    assert len(updates) == 2
    assert remainder == ""
    assert updates[1]["Process"] == "Game"


def test_the_busiest_layer_wins():
    update_payload = update(
        0.0,
        1.0,
        [layer(2, 33.0, 16.0, 17.0), layer(60, 1000.0, 12.0, 40.0)],
    )
    interval = _pick_interval(update_payload)
    assert interval is not None
    assert len(interval.frames_ms) == 60


def test_parse_utc_ns_is_timezone_independent():
    assert parse_utc_ns("1970-01-01T00:00:00Z") == 0
    assert parse_utc_ns("2026-10-06T16:00:31.058Z") == parse_utc_ns("2026-10-06T16:00:31.058")
    assert parse_utc_ns("2026-10-06T16:00:31.058Z") - parse_utc_ns("2026-10-06T16:00:30.0Z") == 1_058_000_000
    assert parse_utc_ns("not a date") is None
    assert parse_utc_ns(None) is None


def test_a_layer_without_frame_statistics_is_skipped():
    payload = update(0.0, 1.0, [{"Configuration": []}, {"Performance Stats": {}}])
    assert _pick_interval(payload) is None


# --------------------------------------------------------------------------- #
# the backend
# --------------------------------------------------------------------------- #
def test_process_name_is_required(monkeypatch):
    backend, proc = make_backend(monkeypatch, "")
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(FpsConfig(mode="metalperftrace"))
    assert "process name" in str(excinfo.value)
    assert proc.args is None


def test_listen_is_asked_for_json_and_the_process(monkeypatch):
    payload = ndjson([timed_update(100.0, 100.064, [(4, 64.0, 8.0, 24.0)])])
    backend, proc = make_backend(monkeypatch, payload)
    collect(backend, FpsConfig(mode="metalperftrace", process_name="Game"), settle_s=0.02)
    assert proc.args[:3] == ["/usr/bin/metalperftrace", "listen", "--json"]
    assert "--process" in proc.args and proc.args[proc.args.index("--process") + 1] == "Game"


def test_frames_carry_the_interval_statistics_as_intervals(monkeypatch):
    payload = ndjson([timed_update(100.0, 100.064, [(4, 64.0, 8.0, 24.0)])])
    backend, _ = make_backend(monkeypatch, payload)
    samples = collect(backend, FpsConfig(mode="metalperftrace", process_name="Game"), settle_s=0.02)
    assert [sample.source for sample in samples] == [METALPERFTRACE] * 4
    assert [sample.dt_ns for sample in samples] == [8_000_000, 16_000_000, 16_000_000, 24_000_000]
    # the timeline is the accumulated interval and never goes backwards
    assert samples == sorted(samples, key=lambda sample: sample.t_ns)
    assert samples[-1].t_ns - samples[0].t_ns == 56_000_000


def test_timestamps_come_from_the_interval_date_not_from_the_read_time(monkeypatch):
    """A block-buffered update must still land where it was recorded."""
    payload = ndjson([timed_update(100.0, 100.033, [(2, 33.0, 16.0, 17.0)])])
    backend, _ = make_backend(monkeypatch, payload)
    samples = collect(backend, FpsConfig(mode="metalperftrace", process_name="Game"), settle_s=0.02)
    end_ns = CLOCK_REF_NS + (parse_utc_ns(END_DATE) - WALL_REF_NS)
    start_ns = end_ns - 33_000_000
    assert samples[0].t_ns == start_ns + 16_000_000
    assert samples[-1].t_ns == start_ns + 33_000_000


def test_the_read_time_is_only_a_fallback_when_the_date_is_missing(monkeypatch):
    payload = ndjson([timed_update(100.0, 100.033, [(2, 33.0, 16.0, 17.0)])])
    backend, _ = make_backend(monkeypatch, payload)
    # Remove the absolute date: the backend falls back to "the interval ended now".
    backend._wall_clock_ns = lambda: 0
    original = mpt.parse_utc_ns

    def no_dates(text):
        return None

    monkeypatch.setattr(mpt, "parse_utc_ns", no_dates)
    try:
        samples = collect(backend, FpsConfig(mode="metalperftrace", process_name="Game"), settle_s=0.02)
    finally:
        monkeypatch.setattr(mpt, "parse_utc_ns", original)
    assert len(samples) == 2
    assert samples[-1].t_ns <= CLOCK_REF_NS


def test_several_updates_accumulate_on_one_timeline(monkeypatch):
    payload = ndjson(
        [
            timed_update(100.0, 100.033, [(2, 33.0, 16.0, 17.0)]),
            timed_update(
                101.0,
                101.033,
                [(2, 33.0, 16.0, 17.0)],
                end_date="2026-10-06T16:00:32.058Z",
            ),
        ]
    )
    backend, _ = make_backend(monkeypatch, payload)
    samples = collect(backend, FpsConfig(mode="metalperftrace", process_name="Game"), settle_s=0.05)
    assert len(samples) == 4
    assert (samples[-1].t_ns - samples[0].t_ns) == pytest.approx(1_017_000_000, abs=2_000_000)


def test_no_metal_session_is_reported_instead_of_hanging(monkeypatch):
    backend, _ = make_backend(monkeypatch, "", exit_code=0, startup_timeout_s=0.2)
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(FpsConfig(mode="metalperftrace", process_name="Game"))
    assert "no Metal layer session" in str(excinfo.value)
    assert "Game" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# process-name resolution (a localized app name is not a process name)
# --------------------------------------------------------------------------- #
APPS = [
    ("红色沙漠", "CrimsonDesert_Steam", "Crimson Desert"),
    ("蒸汽", "Steam", "Steam"),
]


def test_a_localized_app_name_resolves_to_the_executable(monkeypatch):
    monkeypatch.setattr(mpt, "_running_applications", lambda: list(APPS))
    assert mpt.resolve_process_name("红色沙漠")[0] == "CrimsonDesert_Steam"
    assert mpt.resolve_process_name("Crimson Desert")[0] == "CrimsonDesert_Steam"
    assert mpt.resolve_process_name("crimsondesert_steam")[0] == "CrimsonDesert_Steam"
    assert mpt.resolve_process_name("Crimson")[0] == "CrimsonDesert_Steam"


def test_the_executable_name_wins_over_a_matching_localized_name(monkeypatch):
    apps = [("Game", "GameExe", "Game Bundle"), ("Other", "Game", "Other")]
    monkeypatch.setattr(mpt, "_running_applications", lambda: apps)
    assert mpt.resolve_process_name("Game")[0] == "Game"


def test_an_unknown_or_ambiguous_name_is_kept_with_candidates(monkeypatch):
    apps = [("Foo Bar", "FooBar", "FooBar"), ("Foo Baz", "FooBaz", "FooBaz")]
    monkeypatch.setattr(mpt, "_running_applications", lambda: apps)
    assert mpt.resolve_process_name("Foo") == ("Foo", ["FooBar", "FooBaz"])
    assert mpt.resolve_process_name("Nothing Here") == ("Nothing Here", ["FooBar", "FooBaz"])
    assert mpt.resolve_process_name("") == ("", ["FooBar", "FooBaz"])


def test_the_localized_name_is_what_metalperftrace_is_given(monkeypatch):
    """Observed in the field: `--process-name 红色沙漠` for CrimsonDesert_Steam."""
    backend, proc = make_backend(monkeypatch, "", exit_code=0, startup_timeout_s=0.2)
    monkeypatch.setattr(mpt, "_running_applications", lambda: list(APPS))
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(FpsConfig(mode="metalperftrace", process_name="红色沙漠"))
    assert proc.args[proc.args.index("--process") + 1] == "CrimsonDesert_Steam"
    hint = excinfo.value.hint or ""
    assert "CrimsonDesert_Steam" in hint
    assert "resolved" in hint


def test_a_working_session_notes_the_resolution(monkeypatch):
    payload = ndjson([timed_update(100.0, 100.064, [(4, 64.0, 8.0, 24.0)])])
    backend, _ = make_backend(monkeypatch, payload)
    monkeypatch.setattr(mpt, "_running_applications", lambda: list(APPS))
    collect(backend, FpsConfig(mode="metalperftrace", process_name="红色沙漠"), settle_s=0.02)
    assert any("resolved to the running process" in note for note in backend.notes())


def test_notes_explain_the_reconstruction(monkeypatch):
    payload = ndjson([timed_update(100.0, 100.064, [(4, 64.0, 8.0, 24.0)])])
    backend, _ = make_backend(monkeypatch, payload)
    collect(backend, FpsConfig(mode="metalperftrace", process_name="Game"), settle_s=0.02)
    notes = " ".join(backend.notes())
    assert "reconstructs" in notes
    assert "approximation" in notes


def test_a_zero_frame_interval_mid_run_adds_nothing(monkeypatch):
    payload = ndjson(
        [
            timed_update(100.0, 100.033, [(2, 33.0, 16.0, 17.0)]),
            timed_update(
                101.0,
                101.5,
                [(0, 0.0, 0.0, 0.0)],
                end_date="2026-10-06T16:00:32.058Z",
            ),
        ]
    )
    backend, _ = make_backend(monkeypatch, payload)
    samples = collect(backend, FpsConfig(mode="metalperftrace", process_name="Game"), settle_s=0.05)
    assert len(samples) == 2
    assert all(sample.dt_ns is not None for sample in samples)
    notes = " ".join(backend.notes())
    assert "no rendered frames" in notes
    assert "occluded" in notes


def test_a_session_with_no_frames_at_all_is_not_usable(monkeypatch):
    """A live process that presents nothing cannot be benchmarked by this source.

    Its layer exists (metalperftrace reports it every second), so this must not be
    reported as a wrong process name - an occluded window presents no frames.
    """
    payload = ndjson([timed_update(100.0, 100.033, [(0, 0.0, 0.0, 0.0)])])
    backend, _ = make_backend(monkeypatch, payload, startup_timeout_s=0.2)
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(FpsConfig(mode="metalperftrace", process_name="Game"))
    message = str(excinfo.value)
    assert "has a Metal layer for process 'Game'" in message
    assert "no frame reached the display" in message
    assert "no Metal layer session" not in message
    assert "occluded" in (excinfo.value.hint or "")


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the macOS running-app list")
def test_running_applications_only_lists_user_facing_apps():
    """Background agents outnumber Dock apps ~12:1 and are never the game, so the
    suggestion list stays short enough to read."""
    apps = mpt._running_applications()
    assert apps
    for localized, executable, bundle in apps:
        assert isinstance(localized, str) and isinstance(bundle, str)
        assert executable and "/" not in executable
    names = {executable for _localized, executable, _bundle in apps}
    assert not names & {"loginwindow", "universalaccessd", "WallpaperAgent"}
