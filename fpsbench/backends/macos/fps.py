"""macOS FPS backends: ``metalperftrace`` first, overlay OCR as the fallback.

macOS 27 ships ``/usr/bin/metalperftrace``, which reads the OS's always-on Metal
frame timing.  ``auto`` (the default) uses it, which needs neither a visible FPS
counter nor Screen Recording permission, and falls back to OCR otherwise - the
fallback is always reported, never silent.  OCR needs a region; the per-frame
source does not, so a region is only required when macOS cannot use
``metalperftrace`` (macOS < 27, no Metal session for the process, ...).

Deliberately *not* implemented: screen-capture frame counting.  It is capped by
the display refresh rate and the capture itself competes with the game for GPU
time, which would corrupt the measurement.
"""

from __future__ import annotations

from .._dispatch import DispatchingFpsBackend
from .._metalperftrace import MetalPerfTraceFps
from .._ocr_fps import OcrSamplerFps
from ..base import FpsBackend

__all__ = ["MacFpsBackend", "make_ocr_backend", "make_metalperftrace_backend", "BACKEND"]


class MacFpsBackend(DispatchingFpsBackend):
    """``auto``/``metalperftrace`` -> metalperftrace, otherwise OCR."""

    def __init__(self) -> None:
        super().__init__(
            primary_mode="metalperftrace",
            primary_factory=make_metalperftrace_backend,
            ocr_factory=make_ocr_backend,
            primary_note="metalperftrace is not available on this macOS",
            primary_hint=(
                "metalperftrace needs macOS 27 or later and a running game process; "
                "use --fps ocr with a visible overlay counter instead"
            ),
            unsupported_hint=(
                "use --fps auto (metalperftrace) or --fps ocr on macOS - "
                "PresentMon is Windows-only"
            ),
        )


def make_metalperftrace_backend(presentmon_path=None) -> FpsBackend:
    return MetalPerfTraceFps()


def make_ocr_backend() -> FpsBackend:
    from .ocr import MacOcrBackend

    return OcrSamplerFps(MacOcrBackend())


BACKEND = MacFpsBackend
