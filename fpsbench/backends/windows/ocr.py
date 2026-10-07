"""Windows OCR fallback: GDI region capture + ``Windows.Media.Ocr``.

Used when PresentMon is not installed (or was explicitly not requested).  The
capture is a plain ``BitBlt`` of the configured region - no screen-capture frame
counting, for the same reason as on macOS.

The captured pixels are wrapped into a minimal BMP in memory (stdlib only) and
handed to ``BitmapDecoder``; ``winsdk`` supplies the WinRT projection, which is
why it is the single Windows dependency.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from ctypes import wintypes
from typing import Any

from ...core.ocrparse import FpsParse, parse_fps_detail
from ...core.route import Region
from .._capture_bmp import bmp_bytes
from .._win_types import install_prototypes
from ..base import BackendUnavailable, OcrBackend

if not sys.platform.startswith("win"):
    raise BackendUnavailable(
        "the Windows OCR backend can only run on Windows (this is %s)" % sys.platform,
        hint="run fpsbench on Windows 11",
    )

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

install_prototypes(user32)
install_prototypes(gdi32)

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0

_WINSDK_HINT = 'py -m pip install -e ".[windows]"'


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    )


class BITMAPINFO(ctypes.Structure):
    _fields_ = (("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3))


class WindowsOcrBackend(OcrBackend):
    name = "GDI+Windows.Media.Ocr"
    region_space = "pixels"

    def __init__(self) -> None:
        self._engine = None
        self._winsdk = None

    def prepare(self) -> None:
        self._load_engine()

    def _load_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        try:
            import winsdk.windows.globalization as globalization
            import winsdk.windows.media.ocr as ocr
            import winsdk.windows.graphics.imaging as imaging
            import winsdk.windows.storage.streams as streams
        except ImportError as exc:
            raise BackendUnavailable(
                "the Windows OCR path needs the winsdk package: %s" % exc, hint=_WINSDK_HINT
            ) from exc
        engine = ocr.OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            language = globalization.Language("en-US")
            if not ocr.OcrEngine.is_language_supported(language):
                raise BackendUnavailable(
                    "no Windows OCR language pack is installed",
                    hint="Settings > Time & language > Language & region > add a language with "
                    "the optional OCR feature (en-US works out of the box on most systems)",
                )
            engine = ocr.OcrEngine.try_create_from_language(language)
        if engine is None:
            raise BackendUnavailable("Windows.Media.Ocr could not create an engine")
        self._winsdk = (ocr, imaging, streams)
        self._engine = engine
        return engine

    # -- capture ----------------------------------------------------------- #
    def grab_region(self, region: Region) -> bytes:
        """Return a BMP byte string of ``region`` (physical pixels)."""
        return self.capture_bmp(region)

    @staticmethod
    def capture_bmp(region: Region) -> bytes:
        width = int(max(1, round(region.w)))
        height = int(max(1, round(region.h)))
        left = int(round(region.x))
        top = int(round(region.y))

        window_dc = user32.GetDC(None)
        if not window_dc:
            raise BackendUnavailable("GetDC failed: %s" % ctypes.WinError(ctypes.get_last_error()))
        memory_dc = None
        bitmap = None
        previous = None
        try:
            memory_dc = gdi32.CreateCompatibleDC(window_dc)
            bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
            if not memory_dc or not bitmap:
                raise BackendUnavailable(
                    "could not create a capture bitmap: %s" % ctypes.WinError(ctypes.get_last_error())
                )
            previous = gdi32.SelectObject(memory_dc, bitmap)
            if not gdi32.BitBlt(
                memory_dc, 0, 0, width, height, window_dc, left, top, SRCCOPY | CAPTUREBLT
            ):
                raise BackendUnavailable(
                    "BitBlt failed: %s" % ctypes.WinError(ctypes.get_last_error())
                )
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down rows
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB
            buffer = ctypes.create_string_buffer(width * 4 * height)
            copied = gdi32.GetDIBits(
                memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS
            )
            if not copied:
                raise BackendUnavailable(
                    "GetDIBits failed: %s" % ctypes.WinError(ctypes.get_last_error())
                )
            return bmp_bytes(width, height, buffer.raw)
        finally:
            if bitmap:
                # A bitmap that is still selected into a DC cannot be deleted, and
                # DeleteDC does not free it - so put the old object back first or
                # every OCR poll leaks a GDI bitmap until the quota runs out.
                if memory_dc and previous:
                    gdi32.SelectObject(memory_dc, previous)
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            if window_dc:
                user32.ReleaseDC(None, window_dc)

    # -- recognition ------------------------------------------------------- #
    def recognise(self, bmp: bytes) -> str:
        self._load_engine()
        _, imaging, streams = self._winsdk

        async def run() -> str:
            stream = streams.InMemoryRandomAccessStream()
            writer = streams.DataWriter(stream)
            writer.write_bytes(bmp)
            await writer.store_async()
            await writer.flush_async()
            stream.seek(0)
            decoder = await imaging.BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            result = await self._engine.recognize_async(bitmap)
            return result.text or ""

        return asyncio.run(run())

    def read_fps(
        self, region: Region, *, min_fps: float = 0.5, max_fps: float = 2000.0
    ) -> FpsParse:
        text = self.recognise(self.grab_region(region))
        return parse_fps_detail(text, min_fps=min_fps, max_fps=max_fps)

    def capture_text(self, region: Region):
        bmp = self.grab_region(region)
        return self.recognise(bmp), bmp


def capture_text(region: Region):
    backend = WindowsOcrBackend()
    backend.prepare()
    return backend.capture_text(region)


BACKEND = WindowsOcrBackend
