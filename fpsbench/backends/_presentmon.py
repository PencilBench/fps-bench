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
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, List, Optional, Sequence

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

#: ``PresentMon 2.6.0`` as it appears in the usage text.
_VERSION_RE = re.compile(r"PresentMon\s+\d+(?:\.\d+)+")

#: PresentMon is started in its own process group on Windows so it can be stopped
#: with CTRL_BREAK (which makes it flush the CSV) instead of a hard kill.
_CREATION_FLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0

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

#: ETW session name fpsbench asks for.  PresentMon's default is ``"PresentMon"``,
#: which FPS overlays also use (Intel Graphics Software keeps it running), so a
#: capture with the default name fails with "a trace session named PresentMon is
#: already running" on a machine that merely has such an overlay installed.
_SESSION_NAME = "fpsbench"

#: Argument styles, tried in order (PresentMon 2.x uses ``--flag``, 1.x ``-flag``).
#: ``{target}`` is the target-selecting option name (``process_name`` or
#: ``process_id``).  Only the 2.x style carries the session-name flags; 1.x has no
#: equivalent, and adding an unknown option there would fail for a reason
#: unrelated to the capture.
_ARG_STYLES = (
    (
        "--{target}",
        "--output_file",
        "--terminate_on_proc_exit",
        "--session_name",
        "--stop_existing_session",
    ),
    ("-{target}", "-output_file", "-terminate_on_proc_exit"),
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
    """Best effort version string; ``"unknown"`` when the exe does not tell us.

    PresentMon has no ``--version``: an unsupported flag makes it print an error
    and its usage text, which happens to contain the version (``PresentMon 2.6.0``).
    Only that line is used - the error line itself says nothing about the build.
    """
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
        output = (result.stdout or b"").decode("utf-8", "replace")
        match = _VERSION_RE.search(output)
        if match:
            return match.group(0)
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
        startup_timeout_s: float = 2.0,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self._clock_ns = clock_ns
        self._poll_interval_s = poll_interval_s
        # ``startup_timeout_s`` is how long we wait for the first CSV *row* before
        # deciding that a still-running PresentMon simply has nothing to report
        # yet (2.x keeps the file empty until it has data).  A short wait is
        # enough: an argument error makes PresentMon exit almost immediately.
        self._startup_timeout_s = startup_timeout_s
        self._popen = popen
        self._stop = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._csv_path: Optional[str] = None
        self._log_path: Optional[str] = None
        self._log_handle: Optional[Any] = None
        self._log_all = ""
        self._notes: List[str] = []
        self._frames: List[FpsSample] = []
        self._attempts: List[str] = []
        self._version = "unknown"
        self._t0_ns: Optional[int] = None
        self._base_ms: Optional[float] = None
        self._accumulated_ns = 0
        self._process_name = ""
        self._process_id: Optional[int] = None
        self._target = ""
        self._config = FpsConfig()
        self._pending: List[_Frame] = []
        # Wall-clock anchor for the frame timeline.  PresentMon holds its CSV open
        # for the whole capture, so the frames are usually read *after* the run -
        # they are then placed relative to the moment PresentMon was started.
        self._t_start_ns: Optional[int] = None
        self._csv_locked = False

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
        pid = config.process_id
        if not name and pid is None:
            raise BackendUnavailable(
                "PresentMon needs the game's process name or pid",
                hint=(
                    "pass --process-name <game.exe> or --process-id <pid> "
                    "(or set it in the route's fps block)"
                ),
            )
        exe = find_presentmon(presentmon_path or config.presentmon_path)
        if not exe:
            raise BackendUnavailable("PresentMon was not found", hint=PRESENTMON_DOWNLOAD_HINT)
        self._process_name = name or ""
        self._process_id = pid
        # A pid wins: it is the more specific filter, and PresentMon would AND
        # the two - silently capturing nothing when they disagree.
        self._target = "pid %d" % pid if pid is not None else "process %r" % name
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
        self._log_path = os.path.join(os.path.dirname(self._csv_path), "presentmon.log")
        self._log_all = ""
        try:
            self._proc = self._launch(exe)
        except BaseException:
            # ``_cleanup`` only runs from samples(), so a failed launch would
            # otherwise leave the temporary directory behind (once per attempt).
            self._cleanup()
            raise
        self._t_start_ns = self._clock_ns()
        if self._version and self._version != "unknown":
            self._notes.append("%s (%s)" % (self._version, os.path.basename(exe)))
        else:
            self._notes.append("PresentMon version unknown (%s)" % os.path.basename(exe))
        if pid is not None and name:
            self._notes.append(
                "filtering by pid %d; process name %r was not used" % (pid, name)
            )

    def _launch(self, exe: str) -> subprocess.Popen:
        # ``--process_id`` filters the running instance; ``--process_name`` the
        # executable name.  Everything else about the command line is identical.
        if self._process_id is not None:
            target_flag, target_value = "process_id", str(self._process_id)
        else:
            target_flag, target_value = "process_name", self._process_name
        # Every style's failure is kept: the last one is often the least telling
        # (a 1.x-style argument style rejected by a 2.x build, say), while the
        # first is the one that explains the capture.
        errors: List[str] = []
        for style in _ARG_STYLES:
            flag = style[0].replace("{target}", target_flag)
            output, terminate = style[1], style[2]
            args = [exe, flag, target_value, output, self._csv_path, terminate]
            # 2.x: own session name (an overlay may hold the default one), plus
            # ``--stop_existing_session`` so a session a crashed run left behind
            # with *our* name is cleared instead of blocking the next run.
            for extra in style[3:]:
                args.append(extra)
                if extra in ("--session_name", "-session_name"):
                    args.append(_SESSION_NAME)
            self._attempts.append(" ".join(args))
            handle = None
            try:
                if self._log_path:
                    handle = open(self._log_path, "wb")
                proc = self._popen(
                    args,
                    stdout=handle if handle is not None else subprocess.DEVNULL,
                    stderr=subprocess.STDOUT if handle is not None else subprocess.DEVNULL,
                    # Its own process group, so ``stop()`` can send CTRL_BREAK and
                    # make PresentMon flush the CSV instead of killing it.
                    creationflags=_CREATION_FLAGS,
                )
            except OSError as exc:
                errors.append("%s: %s" % (type(exc).__name__, exc))
                if handle is not None:
                    handle.close()
                continue
            self._log_handle = handle
            deadline = time.time() + self._startup_timeout_s
            while time.time() < deadline:
                if os.path.exists(self._csv_path):
                    return proc
                if proc.poll() is not None:
                    self._close_log()
                    errors.append(
                        "PresentMon exited with code %s%s"
                        % (proc.returncode, self._because())
                    )
                    break
                time.sleep(0.05)
            else:
                if self._lost_events():
                    # PresentMon is alive but already dropping events: the capture
                    # cannot produce frames no matter how long we wait, and letting it
                    # run would only end in "no frames" much later.  Report it as
                    # unavailable so ``--fps auto`` can fall back to OCR with a reason.
                    reason = self._read_log()
                    self._close_log()
                    self._kill(proc)
                    raise BackendUnavailable(
                        "PresentMon started for %s but is dropping ETW events, so no "
                        "frames reach it (%s)"
                        % (self._target or "the game", reason or "events were lost"),
                        hint=(
                            "another tracing tool is competing for ETW buffers (an FPS "
                            "overlay, vendor telemetry such as Intel Graphics Software, "
                            "a leftover capture) - close it or reboot and retry; "
                            "`--fps auto` falls back to overlay OCR"
                        ),
                    )
                # Still running with no CSV: PresentMon 2.x keeps the output file
                # empty until it has frames or stops, and the target may simply not
                # be presenting yet - that is *not* a start failure, and treating it
                # as one hid the real reason ("is the game on screen?").  Hand the
                # live process to ``samples()`` instead of retrying a style that
                # cannot help.
                self._notes.append(
                    "PresentMon is running for %s but had written no frames after "
                    "%.1fs; frames are reported as they arrive"
                    % (self._target or "the game", self._startup_timeout_s)
                )
                return proc
            self._kill(proc)
        raise BackendUnavailable(
            "PresentMon did not start with any known argument style (%s)"
            % ("; ".join(errors) or "unknown error"),
            hint="check `%s` manually; tried:\n  %s"
            % (exe, "\n  ".join(self._attempts)),
        )

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            finally:
                self._log_handle = None

    def _read_log(self) -> str:
        """The tail of PresentMon's own output - the reason it gave up on us."""
        text = self._log_data()
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        # An argument error prints a short error line plus the long usage text; a
        # capture problem prints its own message.  Prefer the error lines, and drop
        # the lines that are about our own arguments (the console-stats warning and
        # the "session already running" note that ``--stop_existing_session``
        # answers) - they are expected and would hide the real reason.  A lost-event
        # warning is *not* noise: it is usually why no frames came out.
        benign = (
            "warning: --no_console_stats",
            "warning: a trace session named",
            "use --session_name",
            "usage:",
        )
        filtered = [line for line in lines if not line.lower().startswith(benign)]
        errors = [line for line in filtered if "error" in line.lower()]
        chosen = (errors or filtered)[-2:]
        return "; ".join(chosen)[:300]

    def _log_data(self) -> str:
        """Everything PresentMon printed (kept after the temp directory is gone)."""
        if self._log_all:
            return self._log_all
        if not self._log_path:
            return ""
        try:
            with open(self._log_path, "rb") as handle:
                data = handle.read()
        except OSError:
            return ""
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            # PresentMon 2.x writes UTF-16 to a redirected stdout.
            text = data.decode("utf-16", "replace")
        else:
            text = data.decode("utf-8", "replace")
        return text.replace("\x00", "")

    def _lost_events(self) -> bool:
        """Did PresentMon drop events?  Then the empty CSV is not the game's fault."""
        return "events were lost" in self._log_data().lower()

    def _because(self) -> str:
        reason = self._read_log()
        return " (%s)" % reason if reason else ""

    def samples(self) -> Iterator[FpsSample]:
        # ``_proc`` may already be gone when the CSV is read after a stop, so only
        # the path is required here.
        if self._csv_path is None:
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
        except PermissionError:
            # PresentMon 2.x keeps the output file open (no sharing) for the whole
            # capture, so it can only be read once PresentMon has exited.  Saying so
            # once is better than an empty report that looks like a broken game.
            if not self._csv_locked:
                self._csv_locked = True
                self._notes.append(
                    "PresentMon holds its CSV open while it captures, so the frames are "
                    "read when the run ends instead of live (the numbers are unaffected; "
                    "frame-paced replay falls back to the --input-hz cadence)"
                )
            return position, header_done, columns, partial
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
                # Anchor at the moment PresentMon was started: the frames may only be
                # read after the run (the CSV is locked while it captures), and
                # stamping them with the parse time would push them outside the
                # measured window.
                self._t0_ns = self._t_start_ns or self._clock_ns()
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
        # Keep PresentMon's own words: the temp directory (and its log) goes away
        # here, but the report may still want to explain why nothing was captured.
        if not self._log_all:
            self._log_all = self._log_data()
        self._close_log()
        if self._csv_path:
            shutil.rmtree(os.path.dirname(self._csv_path), ignore_errors=True)

    def stop(self) -> None:
        # Stop PresentMon *before* signalling the reader: it holds the CSV open, and
        # only a graceful exit writes the frames out.  ``_stop`` is set afterwards so
        # the reader's final drain sees the complete file.
        if self._proc is not None:
            self._stop_capture(self._proc)
            self._proc = None
        self._stop.set()

    def _stop_capture(self, proc: subprocess.Popen) -> None:
        """Ask PresentMon to exit so it flushes the CSV (a kill would lose it)."""
        if proc.poll() is not None:
            self._close_log()
            return
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except Exception:
            # No process group (or not Windows): fall back to a plain terminate,
            # which the caller will notice as "no frames" rather than a hang.
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._close_log()

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
                "PresentMon produced no frames for %s - is the game running and on "
                "screen?" % (self._target or "the game",)
            )
            reason = self._read_log()
            if reason:
                notes.append("PresentMon said: %s" % reason)
            if self._lost_events():
                notes.append(
                    "PresentMon's ETW buffers overflowed, so frames were dropped "
                    "before they could be read: another tracing tool is competing "
                    "for the same session resources (an FPS overlay, vendor telemetry "
                    "such as Intel Graphics Software, a leftover capture) - close it "
                    "or reboot and retry"
                )
        return notes

    @property
    def version(self) -> str:
        return self._version
