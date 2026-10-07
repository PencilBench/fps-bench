"""Windows FPS backends: PresentMon first, overlay OCR as the fallback.

Overlay OCR is the fallback when PresentMon is missing; a configured region is
only needed in that case.  Only PresentMon yields true per-frame present
intervals, so it is the mode to use when 1%/0.1% lows matter.

``metalperftrace`` is macOS-only; asking for it here fails with a hint instead of
silently degrading.
"""

from __future__ import annotations

from .._dispatch import DispatchingFpsBackend
from .._ocr_fps import OcrSamplerFps
from .._presentmon import PresentMonFps
from ..base import FpsBackend

__all__ = ["WindowsFpsBackend", "make_ocr_backend", "make_presentmon_backend", "BACKEND"]


class WindowsFpsBackend(DispatchingFpsBackend):
    def __init__(self) -> None:
        super().__init__(
            primary_mode="presentmon",
            primary_factory=make_presentmon_backend,
            ocr_factory=make_ocr_backend,
            primary_note="PresentMon was not found",
            unsupported_hint=(
                "use --fps auto (PresentMon) or --fps ocr on Windows - "
                "metalperftrace is macOS-only"
            ),
        )


def make_presentmon_backend(presentmon_path=None) -> FpsBackend:
    return PresentMonFps()


def make_ocr_backend() -> FpsBackend:
    from .ocr import WindowsOcrBackend

    return OcrSamplerFps(WindowsOcrBackend())


BACKEND = WindowsFpsBackend
