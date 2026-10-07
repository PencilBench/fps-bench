"""Overlay-OCR text handling: text -> FPS value, step-hold, outlier rejection.

Everything here is a pure function of its inputs so it can be unit tested
without a screen capture stack.  Both platform OCR backends funnel their
recognised text through this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from .samples import OCR, FpsSample

__all__ = [
    "FpsParse",
    "OcrReading",
    "parse_fps",
    "parse_fps_detail",
    "parse_readings",
    "vet_readings",
    "make_readings",
    "readings_to_samples",
    "hold_spans",
    "DEFAULT_MAX_FPS",
    "KEYWORD_RE",
]

#: Upper bound for a plausible FPS reading.  Anything above this is treated as a
#: misread (frame counter, temperature, "9999" artefacts, ...).
DEFAULT_MAX_FPS = 2000.0

#: Lower bound; a counter can legitimately report a very low value while loading.
DEFAULT_MIN_FPS = 0.5

KEYWORD_RE = re.compile(r"\bfps\b|\bframes?\s*per\s*second\b|帧", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_MS_RE = re.compile(r"milliseconds|\bms\b", re.IGNORECASE)
_MS_AFTER_RE = re.compile(r"^\s*(?:mi?lliseconds|ms)\b", re.IGNORECASE)
_MS_BEFORE_RE = re.compile(r"(?:mi?lliseconds|ms)\s*[:=]?\s*$", re.IGNORECASE)
#: ``1920x1080`` / ``1920 x 1080`` / ``2560×1440`` - not an FPS counter.
_RESOLUTION_RE = re.compile(r"\d+\s*[x×*]\s*\d+")


@dataclass(frozen=True)
class FpsParse:
    """Result of parsing one OCR text block (kept for diagnostics / doctor)."""

    value: Optional[float]
    candidates: Tuple[float, ...]
    raw_text: str
    reason: str

    @property
    def ok(self) -> bool:
        return self.value is not None


def _to_float(token: str) -> Optional[float]:
    """Interpret one OCR number token, tolerating ',' as decimal separator."""
    text = token.strip()
    if "," in text and "." not in text:
        head, _, tail = text.partition(",")
        # "1,234" -> thousands separator, "59,9" -> decimal comma.
        text = head + tail if len(tail) == 3 and head else head + "." + tail
    text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_fps_detail(
    text: str,
    *,
    min_fps: float = DEFAULT_MIN_FPS,
    max_fps: float = DEFAULT_MAX_FPS,
) -> FpsParse:
    """Parse ``text`` and explain the choice (used by ``doctor`` and tests)."""
    raw = text or ""
    matches = []
    for match in _NUMBER_RE.finditer(raw):
        value = _to_float(match.group(0))
        if value is not None:
            matches.append((match, value))
    if not matches:
        return FpsParse(None, (), raw, "no numeric token found")

    keyword_lines = _keyword_lines(raw)
    resolutions = [(match.start(), match.end()) for match in _RESOLUTION_RE.finditer(raw)]

    best: Optional[float] = None
    best_score: Optional[float] = None
    best_reason = ""
    best_match = None
    for match, value in matches:
        score, reason = _score(raw, match, value, keyword_lines, resolutions, min_fps, max_fps)
        if best_score is None or score > best_score or (score == best_score and value > (best or 0)):
            best, best_score, best_reason = value, score, reason
            best_match = match
    assert best is not None and best_match is not None
    if _is_resolution_member(best_match, resolutions):
        return FpsParse(
            None,
            tuple(value for _, value in matches),
            raw,
            "best candidate %.0f is part of a resolution string" % best,
        )
    if not (min_fps <= best <= max_fps):
        return FpsParse(
            None,
            tuple(value for _, value in matches),
            raw,
            "best candidate %.3f outside plausible range %.1f-%.1f" % (best, min_fps, max_fps),
        )
    return FpsParse(best, tuple(value for _, value in matches), raw, best_reason)


def _is_resolution_member(match: "re.Match", resolutions: Sequence[Tuple[int, int]]) -> bool:
    return any(start <= match.start() and match.end() <= end for start, end in resolutions)


def parse_fps(
    text: str,
    *,
    min_fps: float = DEFAULT_MIN_FPS,
    max_fps: float = DEFAULT_MAX_FPS,
) -> Optional[float]:
    """Return the most likely FPS reading in ``text`` (``None`` if implausible).

    Handles the common overlay shapes: ``"143 FPS"``, ``"FPS: 143.2"``,
    ``"143"``.  When a block contains several numbers (for example
    ``"FPS 143\\n12.4 ms"`` or ``"FPS 143\\n1920x1080"``) the number closest to
    the FPS keyword wins, frame times and resolution strings are down-weighted.
    """
    return parse_fps_detail(text, min_fps=min_fps, max_fps=max_fps).value


def _keyword_lines(text: str) -> List[Tuple[int, int]]:
    spans = []
    for match in KEYWORD_RE.finditer(text):
        start = text.rfind("\n", 0, match.start()) + 1
        end = text.find("\n", match.end())
        spans.append((start, len(text) if end < 0 else end))
    return spans


def _score(
    text: str,
    match: "re.Match",
    value: float,
    keyword_lines: Sequence[Tuple[int, int]],
    resolutions: Sequence[Tuple[int, int]],
    min_fps: float,
    max_fps: float,
) -> Tuple[float, str]:
    """Heuristic score for one candidate number inside ``text``."""
    score = 0.0
    reasons: List[str] = []
    start, end = match.start(), match.end()

    if any(line_start <= start and end <= line_end for line_start, line_end in keyword_lines):
        score += 3.0
        reasons.append("on the same line as an FPS keyword")
    elif keyword_lines:
        score += 0.5
        reasons.append("text mentions FPS")
    else:
        score -= 1.0
        reasons.append("no FPS keyword")

    if any(span_start <= start and end <= span_end for span_start, span_end in resolutions):
        score -= 4.0
        reasons.append("part of a resolution string")

    if _MS_AFTER_RE.match(text[end:]) or _MS_BEFORE_RE.search(text[:start]):
        score -= 2.0
        reasons.append("frame time (ms), not a frame rate")

    if min_fps <= value <= max_fps:
        score += 2.0
        reasons.append("plausible FPS range")
    else:
        score -= 4.0
        reasons.append("outside plausible FPS range")

    if value >= 10.0:
        score += 0.5
    if float(value).is_integer():
        score += 0.5
        reasons.append("integer reading")

    return score, "; ".join(reasons)


@dataclass(frozen=True)
class OcrReading:
    """One OCR poll, before step-holding."""

    t_ns: int
    value: Optional[float]
    raw: str = ""
    suspicious: bool = False
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.value is not None and not self.suspicious


def parse_readings(
    raw_readings: Iterable[Tuple[int, str]],
    *,
    min_fps: float = DEFAULT_MIN_FPS,
    max_fps: float = DEFAULT_MAX_FPS,
) -> List[OcrReading]:
    """Parse every poll; only the single-reading checks are applied here."""
    readings: List[OcrReading] = []
    for t_ns, text in raw_readings:
        result = parse_fps_detail(text or "", min_fps=min_fps, max_fps=max_fps)
        if result.value is None:
            readings.append(OcrReading(t_ns, None, text or "", True, result.reason))
        else:
            readings.append(OcrReading(t_ns, result.value, text or "", False, result.reason))
    return readings


def vet_readings(
    readings: Sequence[OcrReading],
    *,
    jump_ratio: float = 3.0,
    confirm_jumps: bool = True,
) -> List[OcrReading]:
    """Flag readings that jump by more than ``jump_ratio`` without confirmation.

    A jump is kept when the following reading is far from the *previous* level
    too (``confirm_jumps``): that is a real settings change rather than a misread
    of an unrelated number on screen.  An isolated spike is marked suspicious.
    """
    out: List[OcrReading] = []
    for index, reading in enumerate(readings):
        if reading.suspicious:
            out.append(reading)
            continue
        previous = _previous_accepted(out)
        if previous is None or previous.value is None:
            out.append(reading)
            continue
        ratio = max(reading.value, previous.value) / max(min(reading.value, previous.value), 1e-9)
        if ratio <= jump_ratio:
            out.append(reading)
            continue
        following = _next_usable(readings, index + 1)
        sustained = following is not None and _is_far(following.value, previous.value, jump_ratio)
        if confirm_jumps and sustained:
            out.append(reading)
            continue
        out.append(
            OcrReading(
                reading.t_ns,
                reading.value,
                reading.raw,
                True,
                "jump %.3f -> %.3f (>%.1fx) not confirmed by the next reading"
                % (previous.value, reading.value, jump_ratio),
            )
        )
    return out


def make_readings(
    raw_readings: Iterable[Tuple[int, str]],
    *,
    min_fps: float = DEFAULT_MIN_FPS,
    max_fps: float = DEFAULT_MAX_FPS,
    jump_ratio: float = 3.0,
    confirm_jumps: bool = True,
) -> List[OcrReading]:
    """Turn ``(t_ns, recognised_text)`` pairs into vetted readings.

    Rejection rules (all of them are surfaced, never silent):

    * the text did not yield a plausible FPS value;
    * the value is outside ``[min_fps, max_fps]`` (a misread frame counter,
      a resolution, a temperature, ...);
    * the value jumps by more than ``jump_ratio`` relative to the previous
      accepted reading **and** the next reading falls back to the previous
      level (an isolated spike = misread).  With ``confirm_jumps`` a sustained
      change is kept, because a real settings change may move FPS a lot.
    """
    return vet_readings(
        parse_readings(raw_readings, min_fps=min_fps, max_fps=max_fps),
        jump_ratio=jump_ratio,
        confirm_jumps=confirm_jumps,
    )


def _previous_accepted(readings: Sequence[OcrReading]) -> Optional[OcrReading]:
    for reading in reversed(readings):
        if reading.usable:
            return reading
    return None


def _next_usable(readings: Sequence[OcrReading], start: int) -> Optional[OcrReading]:
    for reading in readings[start:]:
        if reading.usable:
            return reading
    return None


def _is_far(value: Optional[float], reference: Optional[float], jump_ratio: float) -> bool:
    if value is None or reference is None:
        return False
    ratio = max(value, reference) / max(min(value, reference), 1e-9)
    return ratio > jump_ratio


def readings_to_samples(
    readings: Sequence[OcrReading],
    *,
    label: Optional[str] = None,
) -> List[FpsSample]:
    """Map accepted readings onto the timeline as ``source="ocr"`` samples.

    Exactly one sample per accepted reading: the statistics layer must not see a
    value twice just because the counter held it for a while.  Step-holding is a
    presentation concern - use :func:`hold_spans` for charts and CSV.
    """
    samples: List[FpsSample] = []
    for reading in readings:
        if not reading.usable:
            continue
        assert reading.value is not None
        samples.append(FpsSample(source=OCR, t_ns=reading.t_ns, value=reading.value, label=label))
    return samples


def hold_spans(
    readings: Sequence[OcrReading],
    *,
    end_ns: Optional[int] = None,
    hold_last_ms: float = 1000.0,
) -> List[Tuple[int, int, float]]:
    """``(start_ns, end_ns, value)`` spans for step-holding accepted readings.

    A reading is held until the next *any* reading (accepted or rejected) so a
    rejected spike visibly interrupts the hold.  The last span ends at
    ``end_ns`` when known, otherwise ``hold_last_ms`` after its reading.
    """
    ordered = sorted(readings, key=lambda r: r.t_ns)
    spans: List[Tuple[int, int, float]] = []
    for index, reading in enumerate(ordered):
        if not reading.usable:
            continue
        assert reading.value is not None
        if index + 1 < len(ordered):
            end = ordered[index + 1].t_ns
        elif end_ns is not None and end_ns > reading.t_ns:
            end = end_ns
        else:
            end = reading.t_ns + int(hold_last_ms * 1e6)
        if end > reading.t_ns:
            spans.append((reading.t_ns, end, reading.value))
    return spans
