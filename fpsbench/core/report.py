"""Reporting: terminal tables, JSON, CSV and a dependency-free ASCII timeline.

Everything a report contains is derived from a :class:`BenchResult`, which the
CLI assembles from the recorded samples plus the session metadata.  Two rules
are enforced here:

* the FPS source (``presentmon`` vs ``ocr``) is always visible, next to the
  numbers it produced, and the OCR caveat is printed verbatim;
* colour is optional - the output degrades to plain text when the terminal (or
  the user) does not want ANSI escapes.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .samples import METALPERFTRACE, OCR, PER_FRAME_SOURCES, PRESENTMON, FpsSample
from .session import format_metadata
from .stats import Summary, segments_by_label, summarize

__all__ = [
    "BenchRun",
    "BenchResult",
    "build_result",
    "render_terminal",
    "result_to_dict",
    "result_to_json",
    "samples_to_csv",
    "ascii_timeline",
    "supports_color",
    "describe_source",
    "format_duration",
    "RESET",
    "BOLD",
    "DIM",
    "GREEN",
    "YELLOW",
    "RED",
    "CYAN",
]

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


def describe_source(source: str) -> str:
    """Human readable name of an FPS source (shared by the report and ``measure``)."""
    return {
        PRESENTMON: "presentmon (true per-frame present times)",
        METALPERFTRACE: "metalperftrace (Metal frame intervals, 1 s statistics)",
        OCR: "overlay OCR counter readings",
    }.get(source, source or "unknown")


def supports_color(stream=None) -> bool:
    """Honour ``NO_COLOR`` and only colour a real terminal."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def format_duration(ns: float) -> str:
    seconds = ns / 1e9
    if seconds < 60:
        return "%.1fs" % seconds
    minutes, rest = divmod(seconds, 60)
    return "%dm%04.1fs" % (int(minutes), rest)


@dataclass
class BenchRun:
    """One playback of the route (one repeat)."""

    index: int
    samples: List[FpsSample]
    started_ns: int = 0
    ended_ns: int = 0
    warnings: List[str] = field(default_factory=list)
    #: ``(t_ns, text)`` label markers emitted during this repeat.
    markers: List[Tuple[int, str]] = field(default_factory=list)
    #: Every sample inside the replay window, before the warmup trim.  The
    #: headline statistics are computed from these (whole-trace FPS).
    window_samples: List[FpsSample] = field(default_factory=list)
    #: Worst scheduling lateness of the input timeline in this repeat.
    max_lateness_ns: int = 0
    #: Input frames delivered (frame-paced playback only).
    input_frames: int = 0

    @property
    def duration_ns(self) -> int:
        return max(0, self.ended_ns - self.started_ns)

    def summary(self) -> Summary:
        return summarize(self.samples)

    def segments(self) -> "OrderedDict[Optional[str], List[FpsSample]]":
        return segments_by_label(self.samples)


@dataclass
class BenchResult:
    """Everything a report needs."""

    name: str
    route_hash: str
    route_source: str
    runs: List[BenchRun]
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    source: str = ""

    @property
    def all_samples(self) -> List[FpsSample]:
        out: List[FpsSample] = []
        for run in self.runs:
            out.extend(run.samples)
        return out

    def overall(self) -> Summary:
        return summarize(self.all_samples)

    def whole_trace(self) -> Optional[Summary]:
        """Summary of the **whole replay window** (no warmup trim), or ``None``.

        Frame-paced replays are judged on the complete path: one input frame per
        rendered frame, so the number of frames and the wall-clock duration of the
        entire replay are the measurement.
        """
        samples = [sample for run in self.runs for sample in run.window_samples]
        try:
            return summarize(samples)
        except ValueError:
            return None

    def pooled_by_label(self) -> "OrderedDict[Optional[str], List[FpsSample]]":
        pooled: "OrderedDict[Optional[str], List[FpsSample]]" = OrderedDict()
        for run in self.runs:
            for label, samples in run.segments().items():
                pooled.setdefault(label, []).extend(samples)
        return pooled

    def median_of_runs(self) -> Optional[float]:
        """Median of per-run averages - the headline number for repeated runs."""
        values = []
        for run in self.runs:
            try:
                values.append(run.summary().avg_fps)
            except ValueError:
                continue
        if not values:
            return None
        values.sort()
        middle = len(values) // 2
        if len(values) % 2:
            return values[middle]
        return (values[middle - 1] + values[middle]) / 2.0


def build_result(
    name: str,
    runs: Sequence[BenchRun],
    *,
    route_hash: str = "",
    route_source: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    warnings: Optional[Sequence[str]] = None,
    notes: Optional[Sequence[str]] = None,
) -> BenchResult:
    sources = {sample.source for run in runs for sample in run.samples}
    if len(sources) > 1:
        raise ValueError("a result must not mix sample sources: %s" % sorted(sources))
    return BenchResult(
        name=name,
        route_hash=route_hash,
        route_source=route_source,
        runs=list(runs),
        metadata=dict(metadata or {}),
        warnings=list(warnings or []),
        notes=list(notes or []),
        source=sources.pop() if sources else "",
    )


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _paint(text: str, color: str, enabled: bool) -> str:
    return "%s%s%s" % (color, text, RESET) if enabled else text


def _fps_cell(value: float, enabled: bool) -> str:
    """Green >= 60, yellow >= 30, red below - purely a reading aid."""
    text = "%.1f" % value
    if not enabled:
        return text
    if value >= 60:
        return _paint(text, GREEN, enabled)
    if value >= 30:
        return _paint(text, YELLOW, enabled)
    return _paint(text, RED, enabled)


def _kv_table(rows: Sequence[Sequence[str]], headers: Sequence[str], indent: str = "  ") -> List[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [indent + "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)).rstrip()]
    lines.append(indent + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append(indent + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return lines


def render_terminal(result: BenchResult, *, color: Optional[bool] = None) -> str:
    """Full report: metadata, overall stats, per label, per repeat, timeline."""
    use_color = supports_color() if color is None else color
    lines: List[str] = []
    title = "fpsbench - %s" % result.name
    lines.append(_paint(title, BOLD, use_color))
    lines.append("=" * len(title))

    lines.append("FPS source: %s" % _paint(describe_source(result.source), CYAN, use_color))
    if result.source == OCR:
        lines.append("            NOT per-frame data - percentiles are approximations")
    if result.route_source:
        lines.append("route source: %s (hash %s)" % (result.route_source, result.route_hash))
    lines.append("")

    lines.append(_paint("metadata", BOLD, use_color))
    for line in format_metadata(result.metadata):
        lines.append("  %s" % line)
    lines.append("")

    try:
        overall = result.overall()
    except ValueError as exc:
        lines.append("no usable samples: %s" % exc)
        lines.extend(_notes_block(result, use_color))
        return "\n".join(lines) + "\n"

    lines.append(_paint("overall", BOLD, use_color))
    whole = result.whole_trace()
    entries = [
        ("whole trace" if result.metadata.get("fps_window") == "whole trace" else "all runs", overall)
    ]
    if whole is not None and whole.samples != overall.samples:
        # A warmup window was requested: show the untrimmed replay next to it.
        entries.insert(0, ("whole trace", whole))
    lines.extend(_summary_table(entries, use_color))
    median = result.median_of_runs()
    if median is not None and len(result.runs) > 1:
        lines.append("")
        lines.append(
            "  %d repeats - per-run average %s, median of runs %.1f FPS"
            % (
                len(result.runs),
                ", ".join("%.1f" % run.summary().avg_fps for run in result.runs),
                median,
            )
        )

    if len(result.runs) > 1:
        lines.append("")
        lines.append(_paint("per repeat", BOLD, use_color))
        rows = []
        for run in result.runs:
            summary = run.summary()
            rows.append(
                (
                    "run %d" % run.index,
                    "%.1f" % summary.avg_fps,
                    "%.1f" % summary.median_fps,
                    "%.1f" % summary.min_fps,
                    "%.1f" % summary.max_fps,
                    "%.1f" % summary.low_1pct,
                    format_duration(summary.duration_ns),
                    "%d" % summary.samples,
                )
            )
        lines.extend(
            _kv_table(
                rows,
                ("repeat", "avg", "median", "min", "max", "1% low", "duration", "samples"),
            )
        )

    labelled = result.pooled_by_label()
    if any(label for label in labelled):
        lines.append("")
        lines.append(_paint("per segment (all runs)", BOLD, use_color))
        entries = []
        for label, samples in labelled.items():
            try:
                entries.append((label or "(before first label)", summarize(samples)))
            except ValueError:
                continue
        lines.extend(_summary_table(entries, use_color))

    lines.append("")
    lines.append(_paint("frame time timeline", BOLD, use_color))
    lines.append(ascii_timeline(result.all_samples))

    lines.extend(_notes_block(result, use_color))
    return "\n".join(lines) + "\n"


def _notes_block(result: BenchResult, use_color: bool) -> List[str]:
    """Notes and warnings - printed for every report, including empty ones."""
    lines: List[str] = []
    if result.notes:
        lines.append("")
        for note in result.notes:
            lines.append("note: %s" % note)
    if result.warnings:
        lines.append("")
        lines.append(_paint("warnings", BOLD, use_color))
        for warning in result.warnings:
            lines.append("  - %s" % warning)
    return lines


def _summary_table(entries: Sequence[tuple], use_color: bool) -> List[str]:
    """Overall / per-segment numbers; the columns depend on the sample source."""
    per_frame = bool(entries) and entries[0][1].is_per_frame
    headers = ["segment", "avg FPS", "median", "min", "max", "1% low", "0.1% low"]
    headers += ["p99 frame", "stutters"] if per_frame else ["p1", "readings"]
    rows: List[List[str]] = []
    for label, summary in entries:
        row = [
            str(label),
            _fps_cell(summary.avg_fps, use_color),
            "%.1f" % summary.median_fps,
            "%.1f" % summary.min_fps,
            "%.1f" % summary.max_fps,
            "%.1f" % summary.low_1pct,
            "%.1f" % summary.low_01pct,
        ]
        if summary.is_per_frame and summary.frame is not None:
            row += ["%.2f ms" % summary.frame.frame_time_ms_p99, "%d" % summary.frame.stutters]
        else:
            row += ["%.1f" % summary.low_1pct, "%d" % summary.samples]
        rows.append(row)
    return _kv_table(rows, headers)


# --------------------------------------------------------------------------- #
# machine readable exports
# --------------------------------------------------------------------------- #
def result_to_dict(result: BenchResult) -> Dict[str, Any]:
    runs: List[Dict[str, Any]] = []
    for run in result.runs:
        entry: Dict[str, Any] = {
            "repeat": run.index,
            "duration_s": run.duration_ns / 1e9,
            "samples": len(run.samples),
            "input_frames": run.input_frames,
            "max_lateness_ms": run.max_lateness_ns / 1e6,
        }
        try:
            entry["summary"] = run.summary().as_dict()
        except ValueError:
            entry["summary"] = None
            entry["error"] = "no usable samples"
        segments = []
        for label, samples in run.segments().items():
            try:
                segments.append({"label": label, "summary": summarize(samples).as_dict()})
            except ValueError:
                continue
        entry["segments"] = segments
        runs.append(entry)

    payload: Dict[str, Any] = {
        "name": result.name,
        "source": result.source,
        "route_hash": result.route_hash,
        "route_source": result.route_source,
        "repeats": len(result.runs),
        "metadata": result.metadata,
        "warnings": result.warnings,
        "notes": result.notes,
        "runs": runs,
    }
    try:
        payload["overall"] = result.overall().as_dict()
    except ValueError:
        payload["overall"] = None
    whole = result.whole_trace()
    payload["whole_trace"] = whole.as_dict() if whole is not None else None
    payload["median_of_runs"] = result.median_of_runs()
    return payload


def result_to_json(result: BenchResult, *, indent: int = 2) -> str:
    return json.dumps(result_to_dict(result), indent=indent, sort_keys=False)


def samples_to_csv(samples: Iterable[FpsSample], *, t0_ns: Optional[int] = None) -> str:
    """Per-sample time series (``source`` is never implicit here)."""
    materialised = list(samples)
    if t0_ns is None:
        t0_ns = min((sample.t_ns for sample in materialised), default=0)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["source", "t_ns", "t_s", "frame_ms", "fps", "label"])
    for sample in materialised:
        frame_ms = "" if sample.dt_ns is None else "%.4f" % (sample.dt_ns / 1e6)
        writer.writerow(
            [
                sample.source,
                sample.t_ns,
                "%.6f" % ((sample.t_ns - t0_ns) / 1e9),
                frame_ms,
                "%.3f" % sample.fps,
                sample.label or "",
            ]
        )
    return buffer.getvalue()


def ascii_timeline(
    samples: Sequence[FpsSample],
    *,
    width: int = 78,
    height: int = 8,
) -> str:
    """Zero-dependency bar chart of FPS over time (bucketed, so N is irrelevant)."""
    materialised = [sample for sample in samples if sample.fps != float("inf")]
    if not materialised:
        return "  (no samples)"
    if len(materialised) == 1:
        return "  single sample: %.1f FPS" % materialised[0].fps

    t0 = materialised[0].t_ns
    t1 = materialised[-1].t_ns
    span = max(1, t1 - t0)
    width = max(20, min(width, 200))
    height = max(3, min(height, 20))
    per_frame = materialised[0].source in PER_FRAME_SOURCES

    bucket_index = [
        min(width - 1, int((sample.t_ns - t0) * (width - 1) / span)) for sample in materialised
    ]
    values: List[Optional[float]] = [None] * width
    if per_frame:
        # Frame weighted: frames per bucket divided by the bucket's wall time.
        totals = [0] * width
        counts = [0] * width
        for index, sample in zip(bucket_index, materialised):
            totals[index] += int(sample.dt_ns or 0)
            counts[index] += 1
        for index in range(width):
            if counts[index] and totals[index] > 0:
                values[index] = counts[index] * 1e9 / totals[index]
    else:
        sums = [0.0] * width
        counts = [0] * width
        for index, sample in zip(bucket_index, materialised):
            sums[index] += sample.fps
            counts[index] += 1
        for index in range(width):
            if counts[index]:
                values[index] = sums[index] / counts[index]

    present = [value for value in values if value is not None]
    if not present:
        return "  (no samples)"
    low = min(present)
    high = max(present)
    if high - low < 1e-9:
        high = low + 1.0

    grid = [[" "] * width for _ in range(height)]
    for x, value in enumerate(values):
        if value is None:
            continue
        level = int(round((value - low) * (height - 1) / (high - low)))
        level = max(0, min(height - 1, level))
        for y in range(level + 1):
            grid[height - 1 - y][x] = "."

    lines: List[str] = []
    for row_index, row in enumerate(grid):
        value_at_row = high - (high - low) * row_index / (height - 1)
        lines.append("  %7.1f |%s" % (value_at_row, "".join(row)))
    lines.append("  %7s +%s" % ("", "-" * width))

    label_line = [" "] * width
    last_drawn: Optional[str] = None
    for sample in materialised:
        if not sample.label or sample.label == last_drawn:
            continue  # consecutive samples share a label; draw it once
        position = min(width - 1, int((sample.t_ns - t0) * (width - 1) / span))
        text = sample.label
        if position + len(text) < width and all(
            character == " " for character in label_line[position : position + len(text)]
        ):
            label_line[position : position + len(text)] = list(text)
            last_drawn = sample.label
    if any(character != " " for character in label_line):
        lines.append("  %7s  %s" % ("labels", "".join(label_line).rstrip()))
    lines.append(
        "  %7s  duration %s, %d samples, %.1f-%.1f FPS"
        % ("", format_duration(span), len(materialised), low, high)
    )
    return "\n".join(lines)
