"""FPS statistics - two strictly separate semantics, selected by ``sample.source``.

``presentmon`` / ``metalperftrace``
    Frame intervals.  Average FPS, instantaneous min/max/median, 1% and 0.1% low
    (average of the slowest frames), frame-time percentiles, stutter count.
    Percentiles are meaningful here, but see the per-source caveat: PresentMon
    reports every present, ``metalperftrace`` reconstructs the interval from the
    OS's 1 s statistics.

``ocr``
    A sequence of counter readings (~1 Hz, usually rounded).  Reading mean /
    median / min / max and *approximate* low quantiles.  These numbers describe
    the counter, not the frame stream; the report says so.

Nothing in this module mixes the two: :func:`summarize` refuses a sample list
that contains more than one source.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .samples import METALPERFTRACE, OCR, PER_FRAME_SOURCES, PRESENTMON, FpsSample

__all__ = [
    "StatsError",
    "percentile",
    "FrameStats",
    "ReadingStats",
    "Summary",
    "summarize",
    "summarize_many",
    "segments_by_label",
    "trim",
    "low_fps",
    "OCR_CAVEAT",
]


class StatsError(ValueError):
    """Raised when a sample set cannot be summarised (empty or mixed sources)."""


OCR_CAVEAT = (
    "overlay-counter readings, not per-frame data: percentiles are approximations "
    "bounded by the counter refresh rate and rounding"
)

_PER_FRAME_CAVEATS = {
    PRESENTMON: "per-frame present intervals from PresentMon (ETW)",
    METALPERFTRACE: (
        "per-frame intervals reconstructed from metalperftrace's 1 s Metal statistics: "
        "average, total and instantaneous min/max are exact, percentiles are approximated "
        "by the interval's mean frame time"
    ),
}


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (numpy's default, "type 7").

    ``sorted_values`` must be ascending and non empty.  ``q`` is in percent.
    """
    if not sorted_values:
        raise StatsError("percentile of an empty sequence")
    if q < 0 or q > 100:
        raise StatsError("percentile q must be within 0..100, got %r" % (q,))
    n = len(sorted_values)
    if n == 1:
        return float(sorted_values[0])
    position = (n - 1) * (q / 100.0)
    lower = int(math.floor(position))
    upper = min(lower + 1, n - 1)
    fraction = position - lower
    return float(sorted_values[lower]) + (float(sorted_values[upper]) - float(sorted_values[lower])) * fraction


def low_fps(frame_times_ns: Sequence[int], fraction: float) -> float:
    """"N% low" FPS: average of the slowest ``fraction`` of frames, inverted.

    This is the CapFrameX / PresentMon convention: FPS derived from the *mean*
    of the slowest frame times, not from a single percentile.
    """
    if not frame_times_ns:
        raise StatsError("low_fps of an empty frame list")
    if not 0 < fraction <= 1:
        raise StatsError("fraction must be within (0, 1]")
    count = max(1, int(math.ceil(len(frame_times_ns) * fraction)))
    slowest = sorted(frame_times_ns, reverse=True)[:count]
    mean_dt = sum(slowest) / float(len(slowest))
    return 1e9 / mean_dt if mean_dt > 0 else float("inf")


@dataclass(frozen=True)
class FrameStats:
    """Statistics over per-frame intervals (``source`` is a per-frame source)."""

    frames: int
    duration_ns: int
    avg_fps: float
    min_fps: float
    max_fps: float
    median_fps: float
    fps_1pct_low: float
    fps_01pct_low: float
    frame_time_ms_p50: float
    frame_time_ms_p95: float
    frame_time_ms_p99: float
    frame_time_ms_p999: float
    frame_time_ms_stdev: float
    stutters: int

    @property
    def stutter_ratio(self) -> float:
        return self.stutters / float(self.frames) if self.frames else 0.0

    def as_dict(self) -> Dict[str, float]:
        return {
            "frames": self.frames,
            "duration_s": self.duration_ns / 1e9,
            "avg_fps": self.avg_fps,
            "min_fps": self.min_fps,
            "max_fps": self.max_fps,
            "median_fps": self.median_fps,
            "fps_1pct_low": self.fps_1pct_low,
            "fps_01pct_low": self.fps_01pct_low,
            "frame_time_ms_p50": self.frame_time_ms_p50,
            "frame_time_ms_p95": self.frame_time_ms_p95,
            "frame_time_ms_p99": self.frame_time_ms_p99,
            "frame_time_ms_p999": self.frame_time_ms_p999,
            "frame_time_ms_stdev": self.frame_time_ms_stdev,
            "stutters": self.stutters,
            "stutter_ratio": self.stutter_ratio,
        }


@dataclass(frozen=True)
class ReadingStats:
    """Statistics over counter readings (``source="ocr"``)."""

    readings: int
    duration_ns: int
    mean_fps: float
    median_fps: float
    min_fps: float
    max_fps: float
    p1_fps: float
    p01_fps: float

    def as_dict(self) -> Dict[str, float]:
        return {
            "readings": self.readings,
            "duration_s": self.duration_ns / 1e9,
            "mean_fps": self.mean_fps,
            "median_fps": self.median_fps,
            "min_fps": self.min_fps,
            "max_fps": self.max_fps,
            "p1_fps": self.p1_fps,
            "p01_fps": self.p01_fps,
        }


@dataclass(frozen=True)
class Summary:
    """Source-agnostic view used by the report layer."""

    source: str
    samples: int
    duration_ns: int
    avg_fps: float
    median_fps: float
    min_fps: float
    max_fps: float
    low_1pct: float
    low_01pct: float
    notes: List[str] = field(default_factory=list)
    frame: Optional[FrameStats] = None
    readings: Optional[ReadingStats] = None

    @property
    def is_per_frame(self) -> bool:
        return self.source in PER_FRAME_SOURCES

    def as_dict(self) -> Dict[str, Any]:
        """Source-agnostic payload; the source specific block is nested."""
        payload: Dict[str, Any] = {
            "source": self.source,
            "samples": self.samples,
            "duration_s": self.duration_ns / 1e9,
            "avg_fps": self.avg_fps,
            "median_fps": self.median_fps,
            "min_fps": self.min_fps,
            "max_fps": self.max_fps,
            "low_1pct_fps": self.low_1pct,
            "low_01pct_fps": self.low_01pct,
            "notes": list(self.notes),
        }
        if self.frame is not None:
            payload["frame"] = self.frame.as_dict()
        if self.readings is not None:
            payload["readings"] = self.readings.as_dict()
        return payload


def summarize(samples: Iterable[FpsSample]) -> Summary:
    """Summarise one homogeneous sample set (raises on empty or mixed input)."""
    materialised = list(samples)
    if not materialised:
        raise StatsError("no samples to summarise")
    sources = {sample.source for sample in materialised}
    if len(sources) > 1:
        raise StatsError(
            "refusing to mix sample sources %s: ocr readings and frame intervals "
            "must be summarised separately" % (sorted(sources),)
        )
    source = sources.pop()
    if source in PER_FRAME_SOURCES:
        return _summarize_per_frame(source, materialised)
    return _summarize_ocr(materialised)


def _summarize_per_frame(source: str, samples: List[FpsSample]) -> Summary:
    frame_times = [int(sample.dt_ns) for sample in samples if sample.dt_ns is not None]
    if not frame_times:
        raise StatsError("%s samples carry no dt_ns" % source)
    total_ns = sum(frame_times)
    median_dt = percentile(sorted(frame_times), 50)
    stutter_threshold = 2.0 * median_dt
    stutters = sum(1 for dt in frame_times if dt > stutter_threshold)
    mean_dt = total_ns / float(len(frame_times))
    variance = sum((dt - mean_dt) ** 2 for dt in frame_times) / float(len(frame_times))
    stats = FrameStats(
        frames=len(frame_times),
        duration_ns=total_ns,
        avg_fps=(len(frame_times) * 1e9 / total_ns) if total_ns else float("inf"),
        min_fps=1e9 / max(frame_times),
        max_fps=1e9 / min(frame_times),
        median_fps=1e9 / median_dt if median_dt else float("inf"),
        fps_1pct_low=low_fps(frame_times, 0.01),
        fps_01pct_low=low_fps(frame_times, 0.001),
        frame_time_ms_p50=median_dt / 1e6,
        frame_time_ms_p95=percentile(sorted(frame_times), 95) / 1e6,
        frame_time_ms_p99=percentile(sorted(frame_times), 99) / 1e6,
        frame_time_ms_p999=percentile(sorted(frame_times), 99.9) / 1e6,
        frame_time_ms_stdev=math.sqrt(variance) / 1e6,
        stutters=stutters,
    )
    return Summary(
        source=source,
        samples=len(frame_times),
        duration_ns=total_ns,
        avg_fps=stats.avg_fps,
        median_fps=stats.median_fps,
        min_fps=stats.min_fps,
        max_fps=stats.max_fps,
        low_1pct=stats.fps_1pct_low,
        low_01pct=stats.fps_01pct_low,
        notes=[_PER_FRAME_CAVEATS.get(source, "per-frame intervals")],
        frame=stats,
    )


def _summarize_ocr(samples: List[FpsSample]) -> Summary:
    values = [float(sample.value) for sample in samples if sample.value is not None]
    if not values:
        raise StatsError("ocr samples carry no value")
    ordered = sorted(values)
    timestamps = [sample.t_ns for sample in samples]
    duration = max(timestamps) - min(timestamps) if len(timestamps) > 1 else 0
    stats = ReadingStats(
        readings=len(values),
        duration_ns=duration,
        mean_fps=sum(values) / float(len(values)),
        median_fps=percentile(ordered, 50),
        min_fps=ordered[0],
        max_fps=ordered[-1],
        p1_fps=percentile(ordered, 1),
        p01_fps=percentile(ordered, 0.1),
    )
    return Summary(
        source=OCR,
        samples=len(values),
        duration_ns=duration,
        avg_fps=stats.mean_fps,
        median_fps=stats.median_fps,
        min_fps=stats.min_fps,
        max_fps=stats.max_fps,
        low_1pct=stats.p1_fps,
        low_01pct=stats.p01_fps,
        notes=[OCR_CAVEAT],
        readings=stats,
    )


def summarize_many(
    sample_sets: Sequence[Sequence[FpsSample]],
) -> List[Summary]:
    """Summarise a sequence of runs (each run homogeneous; sources may differ)."""
    return [summarize(samples) for samples in sample_sets]


def segments_by_label(samples: Iterable[FpsSample]) -> "OrderedDict[Optional[str], List[FpsSample]]":
    """Group samples by ``label``, preserving first-appearance order."""
    out: "OrderedDict[Optional[str], List[FpsSample]]" = OrderedDict()
    for sample in samples:
        out.setdefault(sample.label, []).append(sample)
    return out


def trim(samples: Sequence[FpsSample], t0_ns: int, skip_ns: int) -> List[FpsSample]:
    """Drop everything before ``t0_ns + skip_ns`` (the warmup window)."""
    if skip_ns <= 0:
        return list(samples)
    cutoff = t0_ns + skip_ns
    return [sample for sample in samples if sample.t_ns >= cutoff]
