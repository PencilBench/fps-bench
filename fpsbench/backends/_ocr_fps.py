"""Shared overlay-OCR FPS sampling loop.

The per-platform part is only *capture + recognise* (:class:`OcrBackend`); the
polling loop, the reading vetting and the sample stream live here so macOS and
Windows behave identically.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterator, List, Optional

from ..core.ocrparse import OcrReading, readings_to_samples, vet_readings
from ..core.route import FpsConfig, Region
from ..core.samples import OCR, FpsSample
from .base import BackendUnavailable, FpsBackend, OcrBackend

__all__ = ["OcrSamplerFps"]


class OcrSamplerFps(FpsBackend):
    """Polls an :class:`OcrBackend` at ``sample_hz`` and yields OCR samples.

    ``samples()`` streams a sample per accepted poll (live progress).  Because
    rejecting an unconfirmed jump needs to see the following reading, the
    authoritative set is produced by :meth:`final_samples` after the run - the
    CLI uses that one for statistics.
    """

    source = OCR

    #: Stop after this many consecutive unusable polls: a hidden counter or a
    #: broken capture must be reported, not sampled silently for ten minutes.
    MAX_CONSECUTIVE_FAILURES = 12

    def __init__(
        self,
        ocr: OcrBackend,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        sleep: Callable[[float], None] = time.sleep,
        max_consecutive_failures: Optional[int] = None,
    ) -> None:
        self._ocr = ocr
        self._clock_ns = clock_ns
        self._sleep = sleep
        self._stop = threading.Event()
        self._readings: List[OcrReading] = []
        self._config = FpsConfig()
        self._region: Optional[Region] = None
        self._notes: List[str] = []
        self._error: Optional[str] = None
        self._max_consecutive_failures = (
            self.MAX_CONSECUTIVE_FAILURES
            if max_consecutive_failures is None
            else max_consecutive_failures
        )

    # -- lifecycle --------------------------------------------------------- #
    def start(
        self,
        config: FpsConfig,
        *,
        process_name: Optional[str] = None,
        presentmon_path: Optional[str] = None,
    ) -> None:
        if config.region is None:
            raise BackendUnavailable(
                "no FPS region is configured for the overlay OCR source",
                hint="run `fpsbench pick-region` to frame the overlay FPS counter",
            )
        self._config = config
        self._region = config.region
        self._stop.clear()
        self._readings = []
        self._notes = []
        self._error = None
        self._ocr.prepare()

    def samples(self) -> Iterator[FpsSample]:
        if self._region is None:
            raise BackendUnavailable("OcrSamplerFps.start() was not called")
        period_ns = int(1e9 / self._config.sample_hz)
        next_tick = self._clock_ns()
        consecutive_failures = 0
        while not self._stop.is_set():
            next_tick += period_ns
            t_ns = self._clock_ns()
            try:
                parsed = self._ocr.read_fps(
                    self._region, min_fps=self._config.min_fps, max_fps=self._config.max_fps
                )
            except Exception as exc:  # a capture failure must not kill the run silently
                self._error = str(exc)
                self._notes.append("OCR capture failed: %s" % exc)
                break
            reading = OcrReading(
                t_ns, parsed.value, parsed.raw_text, suspicious=parsed.value is None, reason=parsed.reason
            )
            self._readings.append(reading)
            if reading.usable:
                consecutive_failures = 0
                assert reading.value is not None
                yield FpsSample(source=OCR, t_ns=t_ns, value=reading.value)
            else:
                consecutive_failures += 1
                if (
                    self._max_consecutive_failures
                    and consecutive_failures >= self._max_consecutive_failures
                ):
                    self._notes.append(
                        "stopped sampling: %d polls in a row read no usable FPS value (last text: %r) "
                        "- is the overlay counter visible inside the configured region?"
                        % (consecutive_failures, (parsed.raw_text or "").strip()[:60])
                    )
                    break
            remaining = (next_tick - self._clock_ns()) / 1e9
            if remaining > 0:
                self._sleep(remaining)

    def stop(self) -> None:
        self._stop.set()

    # -- results ----------------------------------------------------------- #
    def readings(self) -> List[OcrReading]:
        return list(self._readings)

    def rejected(self) -> List[OcrReading]:
        return [r for r in self._readings if r.suspicious]

    def final_samples(self) -> List[FpsSample]:
        """Vetted samples: range-checked at poll time, jump-checked at the end."""
        return readings_to_samples(vet_readings(self._readings))

    def notes(self) -> List[str]:
        notes = list(self._notes)
        total = len(self._readings)
        poll_rejected = sum(1 for reading in self._readings if reading.suspicious)
        vetted = vet_readings(self._readings)
        jump_rejected = sum(1 for reading in vetted if reading.suspicious) - poll_rejected
        if poll_rejected:
            notes.append(
                "%d of %d OCR polls were rejected while sampling (unreadable, 0 or out of range)"
                % (poll_rejected, total)
            )
        if jump_rejected > 0:
            notes.append(
                "%d further reading(s) were dropped after the run as unconfirmed jumps"
                % jump_rejected
            )
        accepted = [reading for reading in vetted if reading.usable]
        if accepted:
            # The recognised text is part of the measurement: a counter that says
            # "143 FPS" and one that says "143" are read differently.  The count is
            # of readings that actually feed the statistics, after vetting.
            notes.append(
                "OCR read %r in the last accepted poll (%d of %d polls usable)"
                % (
                    accepted[-1].raw.replace("\n", " ").strip()[:40],
                    len(accepted),
                    total,
                )
            )
        if self._error:
            notes.append("last capture error: %s" % self._error)
        return notes
