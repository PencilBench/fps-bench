"""``metalperftrace`` (macOS 27+) front end - the macOS per-frame FPS source.

Starting with macOS 27 the OS records Metal frame timing continuously for every
``CAMetalLayer``, and ``/usr/bin/metalperftrace`` exposes it:

    metalperftrace listen --json --output FILE --interval 1 --process NAME

``--process`` filters on the *executable* name, which is not what the user sees:
the app is called 红色沙漠 in the Dock and in its window title while the
executable is ``CrimsonDesert_Steam``.  :func:`resolve_process_name` bridges that,
so ``--process-name 红色沙漠`` works; a name that matches nothing is passed
through unchanged and the running apps are offered as suggestions.

Every update is one JSON object covering the last interval (about a second) and
carries, per layer, the OS's *interval statistics* for the frame-on-glass
interval: ``Count``, ``Total (ms)``, ``Min (ms)``, ``Max (ms)``, ``Average (ms)``
and ``StdDev (ms)``.  It reads always-on recorded data, so it needs **no screen
recording permission**, no visible FPS counter, and does not compete with the
game for GPU time.

What that buys, and what it does not:

* ``Count`` frames and ``Total (ms)`` are exact sums over the interval, so the
  average FPS, the instantaneous min/max frame time and the frame count are
  exact;
* individual frame times are *not* exposed, only the interval's statistics.
  :func:`interval_frame_times_ms` therefore reconstructs a frame list that
  preserves count, total, min and max, and places the remaining frames at the
  interval's mean frame time.  Percentiles computed from it are approximations
  bounded by the interval width - the report says so.

Why ``--output FILE`` and not stdout: the binary block-buffers stdout (16 KiB, so
updates arrive in multi-second batches), while the ``--output`` file is appended
per update as NDJSON.  That file is tailed like PresentMon's CSV.  It is also the
authoritative source: :meth:`MetalPerfTraceFps.final_samples` re-reads it from
the start once the tool has exited (it rewrites it as one JSON array on exit),
which no torn read or buffer boundary can disturb.

Timestamps never depend on when we read an update: each interval carries
``Start Date``/``End Date`` plus Mach-continuous seconds, so :func:`parse_utc_ns`
anchors the frames on the player's clock however late the update arrives.

The binary is an OS component (nothing is vendored).
"""

from __future__ import annotations

import calendar
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from ..core.route import FpsConfig
from ..core.samples import METALPERFTRACE, FpsSample
from .base import BackendUnavailable, FpsBackend

__all__ = [
    "MetalPerfTraceFps",
    "find_metalperftrace",
    "metalperftrace_version",
    "extract_updates",
    "interval_frame_times_ms",
    "parse_utc_ns",
    "resolve_process_name",
    "METALPERFTRACE_NOT_FOUND_HINT",
    "METALPERFTRACE_PROCESS_HINT",
]

METALPERFTRACE_NOT_FOUND_HINT = (
    "metalperftrace ships with macOS 27 or later as /usr/bin/metalperftrace; "
    "on an older macOS use --fps ocr with a visible overlay counter"
)
METALPERFTRACE_PROCESS_HINT = (
    "pass --process-name <game> (the process name metalperftrace reports, e.g. "
    "from Activity Monitor), or set fps.process_name in the route/config"
)

_BINARY_NAME = "metalperftrace"
_INSTALL_PATHS = ("/usr/bin/metalperftrace",)

#: The per-frame (as opposed to GPU/CPU workload) statistic block.
_FRAME_INTERVAL_KEY = "Frame-On-Glass Interval Stats"


# --------------------------------------------------------------------------- #
# locating the tool
# --------------------------------------------------------------------------- #
def find_metalperftrace(explicit: Optional[str] = None) -> Optional[str]:
    """Locate ``metalperftrace`` (explicit path, ``PATH``, then ``/usr/bin``)."""
    if explicit:
        candidate = os.path.expanduser(explicit)
        return candidate if os.path.isfile(candidate) else None
    found = shutil.which(_BINARY_NAME)
    if found:
        return found
    for path in _INSTALL_PATHS:
        if os.path.isfile(path):
            return path
    return None


def metalperftrace_version(exe: Optional[str] = None, *, timeout: float = 3.0) -> str:
    """Best effort version string.

    ``metalperftrace`` has no ``--version`` flag (it answers with its usage text),
    so the OS version is reported instead - the tool ships with the OS.
    """
    if exe:
        try:
            result = subprocess.run(
                [exe, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        except Exception:
            result = None
        if result is not None:
            output = (result.stdout or b"").decode("utf-8", "replace").strip()
            if (
                output
                and "usage" not in output.lower()
                and any(character.isdigit() for character in output)
                and len(output) < 120
            ):
                return output.splitlines()[0][:120]
    mac_version = platform.mac_ver()[0]
    return "macOS %s" % mac_version if mac_version else "unknown"


# --------------------------------------------------------------------------- #
# process-name resolution
# --------------------------------------------------------------------------- #
def _running_applications() -> List[Tuple[str, str, str]]:
    """``[(localizedName, executable name, bundle name), ...]`` for user apps.

    Only *regular* applications (Dock apps) are returned - the same call also lists
    dozens of background agents, which are never the game and would drown out the
    suggestions.  macOS only: anything else (or a missing pyobjc) yields an empty
    list, which simply leaves the user's name untouched.
    """
    try:
        from AppKit import NSWorkspace
    except ImportError:
        return []
    try:
        applications = NSWorkspace.sharedWorkspace().runningApplications() or []
    except Exception:
        return []
    found: List[Tuple[str, str, str]] = []
    for application in applications:
        try:
            if application.activationPolicy() != 0:  # NSApplicationActivationPolicyRegular
                continue
        except Exception:
            continue
        executable = ""
        url = application.executableURL()
        if url is not None:
            executable = os.path.basename(str(url.path()))
        if not executable:
            continue
        bundle = ""
        bundle_url = application.bundleURL()
        if bundle_url is not None:
            bundle = os.path.basename(str(bundle_url.path()))
            if bundle.endswith(".app"):
                bundle = bundle[: -len(".app")]
        found.append((str(application.localizedName() or ""), executable, bundle))
    return found


def resolve_process_name(name: str) -> Tuple[str, List[str]]:
    """Map a user supplied name onto the name ``metalperftrace --process`` matches.

    ``--process`` filters on the **executable** name, but what a user sees is the
    app's *localized* name - on a non-English macOS that is usually the window
    title (``红色沙漠``) while the executable is called something else entirely
    (``CrimsonDesert_Steam``).  Passing the title through verbatim therefore finds
    no Metal layer session even though the game renders.

    Returns ``(name to use, candidates)``: the localized/bundle name is resolved
    to the executable name, and when nothing matches at all the running
    applications are returned so the caller can list them as suggestions.
    """
    needle = name.strip().lower()
    applications = _running_applications()
    candidates: List[str] = []
    for _localized, executable, _bundle in applications:
        if executable not in candidates:
            candidates.append(executable)
    if not needle or not applications:
        return name, candidates

    def names(entry: Tuple[str, str, str]) -> Tuple[str, ...]:
        localized, executable, bundle = entry
        return (localized.lower(), executable.lower(), bundle.lower())

    # Exact match first: the executable's own name wins, then the localized and
    # bundle names, so a localized name never shadows a real executable.
    for index in (1, 0, 2):
        for application in applications:
            if needle == names(application)[index]:
                return application[1], candidates
    partial = [
        application
        for application in applications
        if any(needle in value for value in names(application))
    ]
    if len(partial) == 1:
        return partial[0][1], candidates
    return name, candidates


def _no_session_hint(requested: str, used: str, candidates: List[str]) -> str:
    notes: List[str] = []
    if used != requested:
        notes.append(
            "--process-name %r is not a process name; it was resolved to the running "
            "process %r" % (requested, used)
        )
    if candidates:
        notes.append(
            "--process-name matches the *executable* name, not the window or app title; "
            "processes running now: %s" % ", ".join(candidates[:12])
        )
    if not notes:
        notes.append(METALPERFTRACE_PROCESS_HINT)
    notes.append("make sure the game is running and rendering with its window visible")
    return "; ".join(notes)


# --------------------------------------------------------------------------- #
# pure parsing helpers (unit tested without macOS)
# --------------------------------------------------------------------------- #
#: Characters that separate the JSON values the tool emits (NDJSON newlines, and
#: the ``[``/``,``/``]`` of the array it rewrites the file into on exit).
_SEPARATORS = " \t\r\n,[]"


def extract_updates(buffer: str) -> Tuple[List[Any], str]:
    """Decode every complete JSON value at the start of ``buffer``.

    Tolerates both shapes the tool writes: one compact object per line while
    running, and a pretty-printed array after it rewrites the file on exit.  The
    unparsed remainder (a torn tail, or a value still being written) is returned
    so it can be prepended to the next read.
    """
    decoder = json.JSONDecoder()
    values: List[Any] = []
    index = 0
    length = len(buffer)
    while True:
        while index < length and buffer[index] in _SEPARATORS:
            index += 1
        if index >= length:
            return values, ""
        try:
            value, end = decoder.raw_decode(buffer, index)
        except ValueError:
            return values, buffer[index:]
        values.append(value)
        index = end


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z?$")


def parse_utc_ns(text: Any) -> Optional[int]:
    """``"2026-10-06T16:00:31.058Z"`` -> nanoseconds since the Unix epoch.

    ``calendar.timegm`` is used instead of ``datetime`` so the result does not
    depend on the host time zone.
    """
    if not isinstance(text, str):
        return None
    match = _DATE_RE.match(text.strip())
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(value) for value in match.groups()[:6])
    fraction = match.group(7) or ""
    fraction_ns = int(fraction.ljust(9, "0")[:9])
    try:
        epoch = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    except (ValueError, OverflowError):
        return None
    return epoch * 1_000_000_000 + fraction_ns


def interval_frame_times_ms(stats: Dict[str, Any]) -> List[float]:
    """Reconstruct frame times (ms) from one interval's statistics.

    Count, sum (``Total (ms)``), min and max are preserved exactly; the frames in
    between are placed at their mean, which is the only value the OS does not
    report.  A degenerate or inconsistent interval falls back to the mean.
    """
    count = _as_float(stats.get("Count"))
    if count is None or count < 1:
        return []
    frames = int(count)
    total = _as_float(stats.get("Total (ms)"))
    average = _as_float(stats.get("Average (ms)"))
    if total is None or total <= 0:
        total = (average or 0.0) * frames
    if total <= 0:
        return []
    minimum = _as_float(stats.get("Min (ms)"))
    maximum = _as_float(stats.get("Max (ms)"))
    if frames == 1 or minimum is None or maximum is None or minimum + maximum > total:
        return [total / frames] * frames
    if frames == 2:
        return [minimum, maximum]
    middle = (total - minimum - maximum) / (frames - 2)
    return [minimum] + [middle] * (frames - 2) + [maximum]


@dataclass
class _Interval:
    """One decoded interval of one Metal layer."""

    frames_ms: List[float]
    end_epoch_ns: Optional[int]
    span_ns: Optional[int]


def _layer_interval(layer: Dict[str, Any]) -> Optional[_Interval]:
    perf = layer.get("Performance Stats")
    if not isinstance(perf, dict):
        return None
    stats = perf.get(_FRAME_INTERVAL_KEY)
    if not isinstance(stats, dict):
        return None
    frames = interval_frame_times_ms(stats)
    if not frames:
        return None
    start_mct = _as_float(perf.get("Start MCT Seconds"))
    end_mct = _as_float(perf.get("End MCT Seconds"))
    span_ns = (
        int((end_mct - start_mct) * 1e9)
        if start_mct is not None and end_mct is not None and end_mct > start_mct
        else None
    )
    return _Interval(frames_ms=frames, end_epoch_ns=parse_utc_ns(perf.get("End Date")), span_ns=span_ns)


def _pick_interval(update: Dict[str, Any]) -> Optional[_Interval]:
    """The interval of the busiest layer in one update (the game's window)."""
    layers = update.get("Layers")
    if not isinstance(layers, list):
        return None
    best: Optional[_Interval] = None
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        interval = _layer_interval(layer)
        if interval is None:
            continue
        if best is None or len(interval.frames_ms) > len(best.frames_ms):
            best = interval
    return best


def _intervals_from(updates: List[Any]) -> List[_Interval]:
    intervals: List[_Interval] = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        interval = _pick_interval(update)
        if interval is not None:
            intervals.append(interval)
    return intervals


class MetalPerfTraceFps(FpsBackend):
    """Runs ``metalperftrace listen --json`` and streams the Metal frame intervals."""

    source = METALPERFTRACE

    def __init__(
        self,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        wall_clock_ns: Callable[[], int] = time.time_ns,
        interval_s: float = 1.0,
        poll_interval_s: float = 0.1,
        startup_timeout_s: float = 8.0,
        shutdown_timeout_s: float = 3.0,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self._clock_ns = clock_ns
        self._wall_clock_ns = wall_clock_ns
        self._interval_s = max(1.0, float(interval_s))
        self._poll_interval_s = poll_interval_s
        self._startup_timeout_s = startup_timeout_s
        self._shutdown_timeout_s = shutdown_timeout_s
        self._popen = popen
        self._stop = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._dir: Optional[str] = None
        self._output_path: Optional[str] = None
        self._position = 0
        self._partial = b""
        self._notes: List[str] = []
        self._pending: List[_Interval] = []
        self._frames: List[FpsSample] = []
        self._version = "unknown"
        self._exe = ""
        self._process_name = ""
        self._requested_name = ""
        self._candidates: List[str] = []
        # (wall clock, monotonic clock) read together, so an interval's absolute
        # date maps onto our clock no matter when the update reaches us.
        self._wall_ref_ns = 0
        self._clock_ref_ns = 0
        self._intervals_seen = 0
        self._updates_seen = 0
        self._layers_seen = 0

    # -- lifecycle --------------------------------------------------------- #
    def start(
        self,
        config: FpsConfig,
        *,
        process_name: Optional[str] = None,
        presentmon_path: Optional[str] = None,
    ) -> None:
        name = process_name or config.process_name
        if not name:
            raise BackendUnavailable(
                "metalperftrace needs the game's process name to know which Metal layer to read",
                hint=METALPERFTRACE_PROCESS_HINT,
            )
        exe = find_metalperftrace()
        if not exe:
            raise BackendUnavailable(
                "metalperftrace was not found", hint=METALPERFTRACE_NOT_FOUND_HINT
            )
        self._version = metalperftrace_version(exe)
        self._exe = exe
        self._stop.clear()
        self._notes = []
        # ``--process`` matches the executable name; users naturally pass the name
        # they see (the localized app/window title), so resolve it first.
        self._requested_name = name
        self._process_name, self._candidates = resolve_process_name(name)
        if self._process_name != name:
            self._notes.append(
                "--process-name %r resolved to the running process %r"
                % (name, self._process_name)
            )
        self._pending = []
        self._frames = []
        self._position = 0
        self._partial = b""
        self._intervals_seen = 0
        self._updates_seen = 0
        self._layers_seen = 0
        self._wall_ref_ns = self._wall_clock_ns()
        self._clock_ref_ns = self._clock_ns()
        self._dir = tempfile.mkdtemp(prefix="fpsbench-metalperftrace-")
        self._output_path = os.path.join(self._dir, "listen.json")
        args = [
            exe,
            "listen",
            "--json",
            "--output",
            self._output_path,
            "--interval",
            "%g" % self._interval_s,
            "--process",
            self._process_name,
        ]
        try:
            self._proc = self._popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            self._cleanup()
            raise BackendUnavailable(
                "metalperftrace could not be started: %s" % exc, hint=METALPERFTRACE_NOT_FOUND_HINT
            ) from None

        if not self._await_first_interval():
            reason = self._early_exit_reason()
            layer_without_frames = self._layers_seen > 0
            self._cleanup()
            if layer_without_frames:
                # The process name was right and metalperftrace knows its layer, but
                # nothing reached the display - an occluded window presents no frames.
                raise BackendUnavailable(
                    "metalperftrace has a Metal layer for process %r but no frame reached the "
                    "display%s" % (self._process_name, reason),
                    hint=(
                        "the layer exists and presents nothing: bring the game's window to the "
                        "front (an occluded, minimised or other-Space window is not presented) or "
                        "pass --window <title> so fpsbench activates it, and retry once the game "
                        "is actually rendering"
                    ),
                )
            raise BackendUnavailable(
                "metalperftrace reported no Metal layer session for process %r%s"
                % (self._process_name, reason),
                hint=_no_session_hint(self._requested_name, self._process_name, self._candidates),
            )
        self._notes.append("metalperftrace %s: %s" % (self._version, os.path.basename(exe)))

    def _await_first_interval(self) -> bool:
        deadline = time.monotonic() + self._startup_timeout_s
        while time.monotonic() < deadline:
            self._ingest(self._poll_updates())
            if self._intervals_seen:
                return True
            if self._proc is not None and self._proc.poll() is not None:
                break
            time.sleep(self._poll_interval_s)
        return self._intervals_seen > 0

    def _early_exit_reason(self) -> str:
        proc = self._proc
        if proc is None:
            return ""
        code = proc.poll()
        if code is not None:
            return " (metalperftrace exited with code %s)" % code
        if self._layers_seen:
            # Updates *did* arrive - saying "no update" here would send the user
            # looking for the wrong problem.
            return " (%d update(s) over %.0fs, every one with 0 frames)" % (
                self._updates_seen,
                self._startup_timeout_s,
            )
        return " (no update within %.0fs)" % self._startup_timeout_s

    def samples(self) -> Iterator[FpsSample]:
        if self._proc is None:
            raise BackendUnavailable("MetalPerfTraceFps.start() was not called")
        while True:
            for sample in self._drain_pending():
                yield sample
            if self._stop.is_set():
                break
            self._ingest(self._poll_updates())
            if self._proc is not None and self._proc.poll() is not None:
                break
            time.sleep(self._poll_interval_s)
        # the tool rewrites the file as one JSON array on exit; a final read picks
        # up whatever was still being written when it was signalled
        self._ingest(self._poll_updates())
        for sample in self._drain_pending():
            yield sample

    def stop(self) -> None:
        proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=self._shutdown_timeout_s)
                    except Exception:
                        proc.kill()
                        try:
                            proc.wait(timeout=1)
                        except Exception:
                            pass
            except Exception:
                pass
            self._proc = None
        self._stop.set()

    def _cleanup(self) -> None:
        self.stop()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    # -- reading the tool's output file ------------------------------------ #
    def _read_new_bytes(self) -> bytes:
        """Bytes appended since the last read (``b""`` when not readable yet)."""
        if self._output_path is None:
            return b""
        try:
            with open(self._output_path, "rb") as handle:
                handle.seek(self._position)
                raw = handle.read()
                self._position = handle.tell()
        except OSError:
            return b""
        return raw

    def _poll_updates(self) -> List[Any]:
        """Parse the NDJSON the tool appends while it runs.

        Complete lines only: a line boundary is a safe place to split UTF-8, so a
        torn tail simply waits for the next read.
        """
        raw = self._read_new_bytes()
        if not raw:
            return []
        lines = (self._partial + raw).split(b"\n")
        self._partial = lines.pop()
        updates: List[Any] = []
        for line in lines:
            line = line.strip()
            if not line or line in (b"[", b"]"):
                continue
            try:
                updates.append(json.loads(line.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                continue
        return updates

    def _read_all_updates(self) -> List[Any]:
        """Every update in the file, however the tool left it (NDJSON or array)."""
        if self._output_path is None:
            return []
        try:
            with open(self._output_path, "rb") as handle:
                text = handle.read().decode("utf-8", "replace")
        except OSError:
            return []
        updates, _remainder = extract_updates(text)
        return updates

    # -- parsing ----------------------------------------------------------- #
    def _ingest(self, updates: List[Any]) -> None:
        for update in updates:
            self._updates_seen += 1
            if not isinstance(update, dict):
                continue
            layers = update.get("Layers")
            if isinstance(layers, list):
                self._layers_seen += len([layer for layer in layers if isinstance(layer, dict)])
            interval = _pick_interval(update)
            if interval is None:
                continue
            self._intervals_seen += 1
            self._pending.append(interval)

    def _timeline_ns(self, interval: _Interval) -> List[int]:
        """Frame timestamps for one interval, on our monotonic clock.

        The interval's own ``End Date`` is preferred, so a late (buffered) update
        still lands at the moment it was recorded.
        """
        frames = interval.frames_ms
        total_ns = int(round(sum(frames) * 1e6))
        if interval.end_epoch_ns is not None:
            end_ns = self._clock_ref_ns + (interval.end_epoch_ns - self._wall_ref_ns)
        else:
            end_ns = self._clock_ns()
        position_ns = end_ns - (interval.span_ns or total_ns)
        out: List[int] = []
        for frame_ms in frames:
            position_ns += int(round(frame_ms * 1e6))
            out.append(position_ns)
        return out

    def _samples_from(self, intervals: List[_Interval]) -> List[FpsSample]:
        out: List[FpsSample] = []
        for interval in intervals:
            for t_ns, frame_ms in zip(self._timeline_ns(interval), interval.frames_ms):
                out.append(
                    FpsSample(source=METALPERFTRACE, t_ns=t_ns, dt_ns=int(round(frame_ms * 1e6)))
                )
        return out

    def _drain_pending(self) -> List[FpsSample]:
        out = self._samples_from(self._pending)
        self._pending = []
        self._frames.extend(out)
        return out

    # -- results ----------------------------------------------------------- #
    def final_samples(self) -> Optional[List[FpsSample]]:
        """Re-read the whole file after the tool exited (authoritative).

        The incremental stream can miss the tail that the tool was still writing;
        this pass sees the finished file, so it is what the report should use.
        """
        updates = self._read_all_updates()
        intervals = _intervals_from(updates)
        if intervals:
            self._updates_seen = len(updates)
            self._intervals_seen = len(intervals)
            self._frames = self._samples_from(intervals)
        self._cleanup()
        return list(self._frames)

    def captured_samples(self) -> List[FpsSample]:
        return list(self._frames)

    def notes(self) -> List[str]:
        notes = list(self._notes)
        idle = self._updates_seen - self._intervals_seen
        if idle > 0 and self._intervals_seen:
            notes.append(
                "%d of %d metalperftrace updates reported no rendered frames (the game window "
                "was occluded, minimised or not presenting)" % (idle, self._updates_seen)
            )
        if not self._frames:
            notes.append(
                "metalperftrace produced no frames for process %r - is the game running?"
                % (self._process_name,)
            )
        else:
            notes.append(
                "metalperftrace reconstructs frame times from per-interval Metal statistics "
                "(%d frames over %d interval(s) for %r); percentiles are approximations bounded "
                "by the %.0fs interval"
                % (len(self._frames), self._intervals_seen, self._process_name, self._interval_s)
            )
        return notes

    @property
    def version(self) -> str:
        return self._version
