"""The unified sample contract shared by every FPS backend and the report layer.

Two kinds of samples exist and they must never be mixed:

``source="presentmon"`` / ``source="metalperftrace"``
    A per-frame interval: ``dt_ns`` is set, ``value`` is ``None``.  PresentMon
    reports every present, ``metalperftrace`` reconstructs the interval from the
    OS's 1 s Metal interval statistics (see the backend for what is exact).

``source="ocr"``
    An overlay counter reading: ``value`` is set, ``dt_ns`` is ``None``.

Both carry a monotonic ``t_ns`` timestamp and an optional ``label`` (the map
segment the sample belongs to, produced by the recording hotkeys).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "FpsSample",
    "PRESENTMON",
    "METALPERFTRACE",
    "OCR",
    "SOURCES",
    "PER_FRAME_SOURCES",
    "assign_labels",
    "relabel",
    "sample_label_at",
]

PRESENTMON = "presentmon"
METALPERFTRACE = "metalperftrace"
OCR = "ocr"

#: Sources whose samples are frame intervals (``dt_ns``) rather than counter
#: readings (``value``).  They share the statistics semantics.
PER_FRAME_SOURCES = (PRESENTMON, METALPERFTRACE)
SOURCES = PER_FRAME_SOURCES + (OCR,)


@dataclass(frozen=True)
class FpsSample:
    """One measurement sample on a monotonic timeline."""

    source: str
    t_ns: int
    dt_ns: Optional[int] = None
    value: Optional[float] = None
    label: Optional[str] = None

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError("unknown sample source %r (expected one of %s)" % (self.source, SOURCES))
        if self.source in PER_FRAME_SOURCES and self.dt_ns is None:
            raise ValueError("a per-frame sample requires dt_ns")
        if self.source == OCR and self.value is None:
            raise ValueError("ocr sample requires value")
        if self.dt_ns is not None and self.dt_ns < 0:
            raise ValueError("dt_ns must be non negative")

    @property
    def fps(self) -> float:
        """Instantaneous FPS of this sample (frame interval or counter reading)."""
        if self.source in PER_FRAME_SOURCES:
            assert self.dt_ns is not None
            return 1e9 / self.dt_ns if self.dt_ns > 0 else float("inf")
        assert self.value is not None
        return float(self.value)


def assign_labels(
    samples: Iterable[FpsSample], markers: Sequence[Tuple[int, str]]
) -> List[FpsSample]:
    """Stamp every sample with the most recent label marker at or before it.

    ``markers`` is a sorted sequence of ``(t_ns, text)`` pairs.  Samples before
    the first marker keep ``label=None`` (they are reported as the "start"
    segment).  ``relabel`` markers (``text`` empty) clear the label.
    """
    ordered = sorted(markers, key=lambda m: m[0])
    out: List[FpsSample] = []
    current: Optional[str] = None
    index = 0
    for sample in samples:
        while index < len(ordered) and ordered[index][0] <= sample.t_ns:
            current = ordered[index][1] or None
            index += 1
        out.append(replace(sample, label=current))
    return out


def relabel(samples: Iterable[FpsSample], label: Optional[str]) -> List[FpsSample]:
    """Force every sample into one label (used for warmup / repeat segments)."""
    return [replace(s, label=label) for s in samples]


def sample_label_at(markers: Sequence[Tuple[int, str]], t_ns: int) -> Optional[str]:
    """Label in effect at ``t_ns`` (helper for tests and the analysis layer)."""
    current: Optional[str] = None
    for marker_t, text in sorted(markers, key=lambda m: m[0]):
        if marker_t > t_ns:
            break
        current = text or None
    return current
