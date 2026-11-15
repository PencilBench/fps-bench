"""PresentMon CSV parsing and argument-style probing (no Windows needed).

``fpsbench.backends._presentmon`` only shells out and parses text, so it is
importable and testable everywhere - the parts that cannot be tested here are the
Windows-specific input and capture backends.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from fpsbench.backends._presentmon import (
    PresentMonFps,
    _CsvColumns,
    find_presentmon,
    presentmon_version,
)
from fpsbench.backends.base import BackendUnavailable
from fpsbench.core.route import FpsConfig

HEADER_1X = (
    "Application,ProcessID,SwapChainAddress,Runtime,SyncInterval,PresentFlags,"
    "MsBetweenPresents,MsBetweenDisplayChange,MsInPresentAPI\n"
)


class FakeProcess:
    """Stands in for ``PresentMon.exe``: writes a CSV, then waits to be killed."""

    def __init__(self, csv_path, rows, header=HEADER_1X, delay=0.0, write_header=True):
        self.csv_path = csv_path
        self.rows = rows
        self.header = header
        self.delay = delay
        self.write_header = write_header
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.terminated = True
        self.returncode = -9

    def write(self):
        with open(self.csv_path, "w", encoding="utf-8") as handle:
            if self.write_header:
                handle.write(self.header)
            for row in self.rows:
                handle.write(row + "\n")


def make_backend(rows, *, header=HEADER_1X, delay=0.0, write_header=True):
    """A PresentMonFps whose ``Popen`` writes a CSV file with the given rows.

    The explicit ``presentmon_path`` keeps the tests independent of whatever is
    installed on the machine (and of PATH lookups).
    """
    processes = []

    def popen(args, **kwargs):
        csv_path = args[args.index("--output_file") + 1] if "--output_file" in args else args[args.index("-output_file") + 1]
        process = FakeProcess(csv_path, rows, header, delay, write_header)
        process.write()  # PresentMon creates the output file as soon as it starts
        processes.append(process)
        return process

    backend = PresentMonFps(popen=popen, poll_interval_s=0.01, startup_timeout_s=2.0)
    backend._fake_exe = _fake_executable()
    return backend, processes


def _fake_executable():
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="fpsbench-test-pm-"), "PresentMon.exe")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("")
    return path


def collect(backend, config=None, *, settle_s=0.05):
    """Consume the stream the way the CLI does: in a thread, then stop()."""
    config = config or FpsConfig(mode="presentmon", process_name="game.exe")
    if config.presentmon_path is None:
        config = config.merged(presentmon_path=backend._fake_exe)
    backend.start(config)
    collected = []

    def consume():
        try:
            collected.extend(backend.samples())
        except BaseException as exc:  # pragma: no cover - surfaces as a test failure
            collected.append(exc)

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    time.sleep(settle_s)
    backend.stop()
    thread.join(timeout=3)
    assert not thread.is_alive(), "samples() did not stop when asked"
    return collected


# --------------------------------------------------------------------------- #
# locating the tool
# --------------------------------------------------------------------------- #
def test_find_presentmon_prefers_an_explicit_path(tmp_path):
    fake = tmp_path / "PresentMon.exe"
    fake.write_text("")
    assert find_presentmon(str(fake)) == str(fake)
    assert find_presentmon(str(tmp_path)) == str(fake)
    assert find_presentmon(str(tmp_path / "missing.exe")) is None


def test_version_is_unknown_when_the_exe_does_not_answer():
    assert presentmon_version(None) == "unknown"
    assert presentmon_version("/definitely/not/here") == "unknown"


# --------------------------------------------------------------------------- #
# CSV parsing
# --------------------------------------------------------------------------- #
def test_csv_columns_are_found_case_insensitively():
    columns = _CsvColumns.from_header(HEADER_1X.strip().split(","))
    assert columns is not None
    assert columns.between == 6
    assert columns.timestamp is None

    columns = _CsvColumns.from_header(["Application", "msbetweenpresents", "CPUStartTime"])
    assert columns is not None and columns.timestamp == 2 and columns.timestamp_scale == 1.0

    columns = _CsvColumns.from_header(["Application", "MsBetweenPresents", "TimeInSeconds"])
    assert columns is not None and columns.timestamp_scale == 1000.0

    assert _CsvColumns.from_header(["Application", "Nothing"]) is None


def test_samples_carry_true_frame_intervals():
    rows = [
        "game.exe,1234,0x1,DXGI,1,0,16.666,16.5,0.2",
        "game.exe,1234,0x1,DXGI,1,0,33.333,33.0,0.4",
        "game.exe,1234,0x1,DXGI,1,0,50.0,49.5,0.6",
    ]
    backend, _ = make_backend(rows)
    samples = collect(backend)
    assert [sample.dt_ns for sample in samples] == [16666000, 33333000, 50000000]
    assert all(sample.source == "presentmon" and sample.value is None for sample in samples)
    # Without a timestamp column the timeline is the accumulated frame time.
    assert [sample.t_ns - samples[0].t_ns for sample in samples] == [0, 16666000, 49999000]


def test_cpu_start_time_is_used_as_the_timeline():
    header = "Application,ProcessID,MsBetweenPresents,CPUStartTime\n"
    rows = [
        "game.exe,1,10.0,1000.0",
        "game.exe,1,10.0,1010.0",
        "game.exe,1,10.0,1040.0",
    ]
    backend, _ = make_backend(rows, header=header)
    samples = collect(backend)
    deltas_ms = [(sample.t_ns - samples[0].t_ns) / 1e6 for sample in samples]
    assert deltas_ms == [0.0, 10.0, 40.0]


def test_a_partial_last_line_is_buffered_until_it_is_complete(tmp_path):
    """PresentMon flushes in blocks; a torn line must not be parsed as a frame."""
    csv_path = tmp_path / "frames.csv"
    backend = PresentMonFps()
    backend._csv_path = str(csv_path)
    csv_path.write_text(
        HEADER_1X
        + "game.exe,1,0x1,DXGI,1,0,16.0,16.0,0.1\n"
        + "game.exe,1,0x1,DXGI,1,0,17."
    )
    position, header_done, columns, partial = backend._drain(0, False, None, "")
    assert [sample.dt_ns for sample in backend._read_pending()] == [16000000]
    assert partial.startswith("game.exe,1,0x1,DXGI,1,0,17.")

    with open(csv_path, "a", encoding="utf-8") as handle:
        handle.write("0,17.0,0.2\n")
    backend._drain(position, header_done, columns, partial)
    assert [sample.dt_ns for sample in backend._read_pending()] == [17000000]


def test_empty_or_broken_csv_does_not_explode():
    backend, _ = make_backend([], write_header=True)
    samples = collect(backend)
    assert samples == []
    assert any("no frames" in note for note in backend.notes())


def test_missing_column_is_reported_instead_of_silently_returning_nothing():
    backend, _ = make_backend(["game.exe,1,2"], header="Application,ProcessID,Other\n")
    samples = collect(backend)
    assert samples == []
    assert any("MsBetweenPresents" in note for note in backend.notes())


# --------------------------------------------------------------------------- #
# startup handling
# --------------------------------------------------------------------------- #
def test_process_name_is_required():
    backend, _ = make_backend([])
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(FpsConfig(mode="presentmon"))
    assert "process name" in str(excinfo.value)


def test_missing_executable_is_reported_with_a_download_hint():
    backend, _ = make_backend([])
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(
            FpsConfig(mode="presentmon", process_name="game.exe", presentmon_path="/nope/PresentMon.exe")
        )
    assert "PresentMon" in str(excinfo.value)
    assert "releases" in (excinfo.value.hint or "")


def test_argument_style_fallback_lists_every_attempt(tmp_path):
    fake = tmp_path / "PresentMon.exe"
    fake.write_text("")
    attempts = []

    def popen(args, **kwargs):
        attempts.append(args)
        raise OSError("cannot execute")

    backend = PresentMonFps(popen=popen, startup_timeout_s=0.2)
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(
            FpsConfig(mode="presentmon", process_name="game.exe"), presentmon_path=str(fake)
        )
    assert len(attempts) == 2
    assert attempts[0][1] == "--process_name"
    assert attempts[1][1] == "-process_name"
    assert "--process_name" in (excinfo.value.hint or "")


def test_process_id_is_used_instead_of_the_process_name(tmp_path):
    fake = tmp_path / "PresentMon.exe"
    fake.write_text("")
    attempts = []

    def popen(args, **kwargs):
        attempts.append(args)
        raise OSError("cannot execute")

    backend = PresentMonFps(popen=popen, startup_timeout_s=0.2)
    with pytest.raises(BackendUnavailable):
        backend.start(FpsConfig(mode="presentmon", process_id=4242), presentmon_path=str(fake))
    assert attempts[0][1:3] == ["--process_id", "4242"]
    assert attempts[1][1:3] == ["-process_id", "4242"]
    assert all("-process_name" not in arg for argv in attempts for arg in argv)


def test_a_pid_wins_over_a_process_name(tmp_path):
    """Both filters would be ANDed by PresentMon, so the pid has to win."""
    fake = tmp_path / "PresentMon.exe"
    fake.write_text("")
    attempts = []

    def popen(args, **kwargs):
        attempts.append(args)
        raise OSError("cannot execute")

    backend = PresentMonFps(popen=popen, startup_timeout_s=0.2)
    with pytest.raises(BackendUnavailable):
        backend.start(
            FpsConfig(mode="presentmon", process_name="game.exe", process_id=4242),
            presentmon_path=str(fake),
        )
    assert attempts[0][1:3] == ["--process_id", "4242"]


def test_a_pid_alone_satisfies_the_target_requirement(tmp_path):
    """No process name at all is fine as long as a pid is given."""
    fake = tmp_path / "PresentMon.exe"
    fake.write_text("")
    backend = PresentMonFps(popen=lambda args, **kw: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(FpsConfig(mode="presentmon", process_id=4242), presentmon_path=str(fake))
    assert "did not start" in str(excinfo.value)


def test_process_id_must_be_a_positive_integer():
    from fpsbench.core.route import RouteError

    for bad in (0, -1):
        with pytest.raises(RouteError):
            FpsConfig(mode="presentmon", process_id=bad)
    with pytest.raises(RouteError):
        FpsConfig.from_dict({"process_id": "4242"}, "fps")
    assert FpsConfig.from_dict({"process_id": 4242}, "fps").process_id == 4242


def test_lost_events_are_explained_in_the_notes(tmp_path):
    """An empty CSV because ETW dropped events must not read as "wrong process"."""
    fake = tmp_path / "PresentMon.exe"
    fake.write_text("")

    class SilentProcess:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    backend = PresentMonFps(
        popen=lambda args, **kw: SilentProcess(), startup_timeout_s=0.1
    )
    backend.start(FpsConfig(mode="presentmon", process_id=1), presentmon_path=str(fake))
    with open(backend._log_path, "w", encoding="utf-8") as handle:
        handle.write("warning: 1234 ETW events were lost.\nStarted recording.\n")
    notes = " | ".join(backend.notes())
    assert "events were lost" in notes
    assert "ETW buffers overflowed" in notes


def test_a_live_presentmon_with_no_rows_is_started_not_failed(tmp_path):
    """PresentMon 2.x keeps the CSV empty until it has frames: that is not an error."""
    fake = tmp_path / "PresentMon.exe"
    fake.write_text("")

    class SilentProcess:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    backend = PresentMonFps(
        popen=lambda args, **kw: SilentProcess(), startup_timeout_s=0.1
    )
    backend.start(FpsConfig(mode="presentmon", process_name="game.exe"), presentmon_path=str(fake))
    notes = " | ".join(backend.notes())
    assert "had written no frames" in notes
    assert "no frames for process 'game.exe'" in notes


def test_frames_are_read_after_a_graceful_stop(tmp_path):
    """PresentMon 2.x locks its CSV; the frames appear when it flushes on stop."""
    fake = tmp_path / "PresentMon.exe"
    fake.write_text("")
    rows = [
        "game.exe,1,0x1,DXGI,1,0,16.0,16.0,0.1",
        "game.exe,1,0x1,DXGI,1,0,17.0,17.0,0.1",
    ]

    class LockedProcess:
        """Writes the CSV only when it is asked to stop, like PresentMon does."""

        returncode = None
        signalled = False

        def __init__(self, csv_path):
            self.csv_path = csv_path

        def poll(self):
            return self.returncode

        def send_signal(self, sig):
            self.signalled = True
            with open(self.csv_path, "w", encoding="utf-8") as handle:
                handle.write(HEADER_1X + "\n".join(rows) + "\n")
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.returncode = 1

        def kill(self):
            self.returncode = -9

    def popen(args, **kwargs):
        csv_path = args[args.index("--output_file") + 1]
        process = LockedProcess(csv_path)
        popen.process = process
        return process

    backend = PresentMonFps(popen=popen, startup_timeout_s=0.1, poll_interval_s=0.01)
    backend.start(FpsConfig(mode="presentmon", process_name="game.exe"), presentmon_path=str(fake))
    backend.stop()
    assert popen.process.signalled is True
    samples = list(backend.samples())
    assert [sample.dt_ns for sample in samples] == [16000000, 17000000]


def test_dispatching_backend_falls_back_to_ocr_and_says_so():
    from fpsbench.backends._dispatch import DispatchingFpsBackend

    class FakeOcr:
        source = "ocr"

        def start(self, config, *, process_name=None, presentmon_path=None):
            self.started = True

        def samples(self):
            return iter(())

        def stop(self):
            pass

        def final_samples(self):
            return []

        def notes(self):
            return ["ocr used"]

    def failing_factory(path):
        raise BackendUnavailable("PresentMon was not found")

    ocr = FakeOcr()
    backend = DispatchingFpsBackend(
        primary_mode="presentmon",
        primary_factory=failing_factory,
        ocr_factory=lambda: ocr,
        primary_note="not found",
    )
    backend.start(FpsConfig(mode="auto", region=None))
    assert backend.source == "ocr"
    assert any("overlay OCR" in note for note in backend.notes())
    assert ocr.started


def test_dispatching_backend_refuses_a_missing_presentmon_when_asked_explicitly():
    from fpsbench.backends._dispatch import DispatchingFpsBackend

    def failing_factory(path):
        raise BackendUnavailable("PresentMon was not found")

    backend = DispatchingFpsBackend(
        primary_mode="presentmon",
        primary_factory=failing_factory,
        ocr_factory=lambda: None,
        primary_note="not found",
        primary_hint="install PresentMon, pass --presentmon <path>, or use --fps ocr",
    )
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(FpsConfig(mode="presentmon"))
    assert "--fps ocr" in (excinfo.value.hint or "")
