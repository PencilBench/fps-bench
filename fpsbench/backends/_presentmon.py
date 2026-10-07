"""PresentMon (ETW) front end - shared by the Windows backends.

PresentMon gives *true per-frame present intervals*, which is the only way to get
meaningful 1%/0.1% lows.  It is an external tool (not shipped with fpsbench):

    https://github.com/GameTechDev/PresentMon/releases

The CSV column layout differs between PresentMon 1.x and 2.x, so the parser is
case-insensitive and only depends on columns that both versions emit
(``MsBetweenPresents`` plus one of ``CPUStartTime`` / ``TimeInSeconds``).  If
PresentMon rejects our arguments the file simply never appears, and the next
argument style is tried - the failure message lists every command that was
attempted.
"""

from __future__ import annotations

import csv
import glob
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Sequence

from ..core.route import FpsConfig
from ..core.samples import PRESENTMON, FpsSample
from .base import BackendUnavailable, FpsBackend

__all__ = [
    "PresentMonFps",
    "find_presentmon",
    "presentmon_version",
    "PRESENTMON_DOWNLOAD_HINT",
]

PRESENTMON_DOWNLOAD_HINT = (
    "download PresentMon from https://github.com/GameTechDev/PresentMon/releases "
    'and pass --presentmon "<path to PresentMon.exe>" (or put it on PATH)'
)

_EXE_NAMES = ("PresentMon.exe", "PresentMon", "PresentMon-Console.exe")

#: Only system-wide / per-user *application* locations are searched.  Browsing
#: the whole home directory (Downloads, Dropbox, ...) would be slow, can trip
#: macOS privacy prompts (which hang the process) and is none of our business.
_INSTALL_GLOBS = (
    r"C:\Program Files\PresentMon*\PresentMon.exe",
    r"C:\Program Files (x86)\PresentMon*\PresentMon.exe",
    r"C:\Program Files\Intel\PresentMon*\PresentMon.exe",
    r"C:\Program Files\CapFrameX\PresentMon*.exe",
    r"C:\ProgramData\PresentMon*\PresentMon.exe",
    "%LOCALAPPDATA%/PresentMon*/PresentMon.exe",
    "%LOCALAPPDATA%/Programs/PresentMon*/PresentMon.exe",
)

#: Argument styles, tried in order (PresentMon 2.x uses ``--flag``, 1.x ``-flag``).
_ARG_STYLES = (
    ("--process_name", "--output_file", "--terminate_on_proc_exit"),
    ("-process_name", "-output_file", "-terminate_on_proc_exit"),
)


def find_presentmon(explicit: Optional[str] = None) -> Optional[str]:
    """Locate ``PresentMon`` (explicit path, PATH, then common install dirs)."""
    if explicit:
        candidate = os.path.expanduser(explicit)
        if os.path.isfile(candidate):
            return candidate
        if os.path.isdir(candidate):
            for name in _EXE_NAMES:
                nested = os.path.join(candidate, name)
                if os.path.isfile(nested):
                    return nested
        return None
    for name in _EXE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for pattern in _INSTALL_GLOBS:
        try:
            matches = sorted(glob.glob(os.path.expandvars(pattern)))
        except OSError:  # an unreadable location must not break the search
            continue
        if matches:
            return matches[-1]
    return None


def presentmon_version(exe: Optional[str], *, timeout: float = 3.0) -> str:
    """Best effort version string; ``"unknown"`` when the exe does not tell us."""
    if not exe:
        return "unknown"
    for flag in ("--version", "-version", "-v"):
        try:
            result = subprocess.run(
                [exe, flag],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        except Exception:
            continue
        output = (result.stdout or b"").decode("utf-8", "replace").strip()
        if output:
            return output.splitlines()[0][:120]
    return "unknown"


@dataclass
class _CsvColumns:
    between: int
    timestamp: Optional[int]
    timestamp_scale: float = 1.0  # to milliseconds

    @classmethod
    def from_header(cls, header: Sequence[str]) -> Optional["_CsvColumns"]:
        lowered = [name.strip().strip('"').lower() for name in header]
        between = _index_of(lowered, "msbetweenpresents")
        if between is None:
            return None
        timestamp = _index_of(lowered, "cpustarttime")
        scale = 1.0
        if timestamp is None:
            timestamp = _index_of(lowered, "timeinseconds")
            scale = 1000.0
        return cls(between=between, timestamp=timestamp, timestamp_scale=scale)


def _index_of(names: Sequence[str], wanted: str) -> Optional[int]:
    for index, name in enumerate(names):
        if name == wanted:
            return index
    return None


@dataclass
class _Frame:
    dt_ns: int
    timestamp_ms: Optional[float]


class PresentMonFps(FpsBackend):
    """Runs ``PresentMon.exe`` against the game and streams frame intervals."""

    source = PRESENTMON

    def __init__(
        self,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        poll_interval_s: float = 0.25,
        startup_timeout_s: float = 6.0,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self._clock_ns = clock_ns
        self._poll_interval_s = poll_interval_s
        self._startup_timeout_s = startup_timeout_s
        self._popen = popen
        self._stop = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._csv_path: Optional[str] = None
        self._notes: List[str] = []
        self._frames: List[FpsSample] = []
        self._attempts: List[str] = []
        self._version = "unknown"
        self._t0_ns: Optional[int] = None
        self._base_ms: Optional[float] = None
        self._accumulated_ns = 0
        self._process_name = ""
        self._config = FpsConfig()
        self._pending: List[_Frame] = []

    # -- lifecycle --------------------------------------------------------- #
    def start(
        self,
        config: FpsConfig,
        *,
        process_name: Optional[str] = None,
        presentmon_path: Optional[str] = None,
    ) -> None:
        self._config = config
        name = process_name or config.process_name
        if not name:
            raise BackendUnavailable(
                "PresentMon needs the game's process name",
                hint="pass --process-name <game.exe> (or set it in the route's fps block)",
            )
        exe = find_presentmon(presentmon_path or config.presentmon_path)
        if not exe:
            raise BackendUnavailable("PresentMon was not found", hint=PRESENTMON_DOWNLOAD_HINT)
        self._process_name = name
        self._version = presentmon_version(exe)
        self._csv_path = os.path.join(
            tempfile.mkdtemp(prefix="fpsbench-presentmon-"), "frames.csv"
        )
        self._stop.clear()
        self._frames = []
        self._attempts = []
        self._pending = []
        self._t0_ns = None
        self._base_ms = None
        self._accumulated_ns = 0
        try:
            self._proc = self._launch(exe, name)
        except BaseException:
            # ``_cleanup`` only runs from samples(), so a failed launch would
            # otherwise leave the temporary directory behind (once per attempt).
            self._cleanup()
            raise
        self._notes.append("PresentMon %s: %s" % (self._version, os.path.basename(exe)))

    def _launch(self, exe: str, process_name: str) -> subprocess.Popen:
        last_error: Optional[str] = None
        for between, output, terminate in _ARG_STYLES:
            args = [exe, between, process_name, output, self._csv_path, terminate]
            self._attempts.append(" ".join(args))
            try:
                proc = self._popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except OSError as exc:
                last_error = "%s: %s" % (type(exc).__name__, exc)
                continue
            deadline = time.time() + self._startup_timeout_s
            while time.time() < deadline:
                if os.path.exists(self._csv_path):
                    return proc
                if proc.poll() is not None:
                    last_error = "PresentMon exited with code %s" % proc.returncode
                    break
                time.sleep(0.05)
            else:
                last_error = "no CSV output after %.0fs" % self._startup_timeout_s
            self._kill(proc)
        raise BackendUnavailable(
            "PresentMon did not start with any known argument style (%s)"
            % (last_error or "unknown error"),
            hint="check `%s` manually; tried:\n  %s"
            % (exe, "\n  ".join(self._attempts)),
        )

    def samples(self) -> Iterator[FpsSample]:
        if self._proc is None or self._csv_path is None:
            raise BackendUnavailable("PresentMonFps.start() was not called")
        position = 0
        header_done = False
        columns: Optional[_CsvColumns] = None
        partial = ""
        try:
            while True:
                finished = self._stop.is_set()
                position, header_done, columns, partial = self._drain(
                    position, header_done, columns, partial
                )
                for sample in self._read_pending():
                    yield sample
                if finished:
                    break
                if self._proc is not None and self._proc.poll() is not None:
                    # PresentMon exited on its own (the game closed, or it gave up
                    # on our arguments): finish what is in the file and stop.
                    self._drain(position, header_done, columns, partial, final=True)
                    for sample in self._read_pending():
                        yield sample
                    return
                time.sleep(self._poll_interval_s)
            self._drain(position, header_done, columns, partial, final=True)
            for sample in self._read_pending():
                yield sample
        finally:
            self._cleanup()

    def _drain(
        self,
        position: int,
        header_done: bool,
        columns: Optional[_CsvColumns],
        partial: str,
        *,
        final: bool = False,
    ):
        """Read whatever complete lines the CSV has (PresentMon flushes lazily)."""
        if self._csv_path is None:
            return position, header_done, columns, partial
        try:
            with open(self._csv_path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                chunk = handle.read()
                position = handle.tell()
        except OSError:
            return position, header_done, columns, partial
        text = partial + chunk
        lines = text.splitlines()
        if lines and not text.endswith("\n") and not final:
            partial = lines.pop()
        else:
            partial = ""
        for line in lines:
            if not line.strip():
                continue
            fields = next(csv.reader([line]))
            if not header_done:
                columns = _CsvColumns.from_header(fields)
                header_done = True
                if columns is None:
                    self._notes.append(
                        "PresentMon CSV has no MsBetweenPresents column; header was: %s"
                        % (", ".join(fields[:12]) or "(empty)",)
                    )
                    self._stop.set()
                continue
            if columns is None:
                continue
            self._ingest(fields, columns)
        return position, header_done, columns, partial

    def _ingest(self, fields: Sequence[str], columns: _CsvColumns) -> None:
        if len(fields) <= columns.between:
            return
        try:
            between_ms = float(fields[columns.between])
        except ValueError:
            return
        timestamp_ms: Optional[float] = None
        if columns.timestamp is not None and len(fields) > columns.timestamp:
            try:
                timestamp_ms = float(fields[columns.timestamp]) * columns.timestamp_scale
            except ValueError:
                timestamp_ms = None
        self._pending.append(_Frame(max(0, int(round(between_ms * 1e6))), timestamp_ms))

    def _read_pending(self) -> List[FpsSample]:
        out: List[FpsSample] = []
        for frame in self._pending:
            if self._t0_ns is None:
                self._t0_ns = self._clock_ns()
                self._base_ms = frame.timestamp_ms
            if frame.timestamp_ms is not None and self._base_ms is not None:
                t_ns = self._t0_ns + int((frame.timestamp_ms - self._base_ms) * 1e6)
            else:
                t_ns = self._t0_ns + self._accumulated_ns
            self._accumulated_ns += frame.dt_ns
            sample = FpsSample(source=PRESENTMON, t_ns=t_ns, dt_ns=frame.dt_ns)
            self._frames.append(sample)
            out.append(sample)
        self._pending = []
        return out

    def _cleanup(self) -> None:
        if self._csv_path:
            shutil.rmtree(os.path.dirname(self._csv_path), ignore_errors=True)

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            self._kill(self._proc)
            self._proc = None

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
        except Exception:
            pass

    # -- results ----------------------------------------------------------- #
    def final_samples(self) -> Optional[List[FpsSample]]:
        return None  # the stream is already the authoritative frame list

    def captured_samples(self) -> List[FpsSample]:
        return list(self._frames)

    def notes(self) -> List[str]:
        notes = list(self._notes)
        if not self._frames:
            notes.append(
                "PresentMon produced no frames for process %r - is the game running?"
                % (self._process_name,)
            )
        return notes

    @property
    def version(self) -> str:
        return self._version
