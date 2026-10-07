"""Windows GDI region capture, packaged as a BMP byte string.

Kept free of any Windows-only import so the header construction can be unit
tested anywhere - a wrong BMP header would otherwise show up as "OCR reads
nothing" on a real machine.
"""

from __future__ import annotations

import struct
from typing import Tuple

__all__ = ["bmp_bytes", "bmp_info", "BMP_FILE_HEADER_SIZE", "BMP_INFO_HEADER_SIZE"]

BMP_FILE_HEADER_SIZE = 14
BMP_INFO_HEADER_SIZE = 40


def bmp_info(width: int, height: int) -> Tuple[int, int]:
    """``(stride, pixel_size)`` for a 32-bit top-down BMP of ``width``x``height``."""
    stride = int(width) * 4
    return stride, stride * int(height)


def bmp_bytes(width: int, height: int, pixels: bytes) -> bytes:
    """Wrap top-down BGRA pixels into a minimal 32-bit BMP (stdlib only).

    ``Windows.Media.Ocr`` needs a real image, and the GDI path already has raw
    BGRA rows, so a hand-built BMP avoids any encoder dependency.  A negative
    ``biHeight`` marks the rows as top-down, matching ``GetDIBits``.
    """
    width = int(width)
    height = int(height)
    stride, pixel_size = bmp_info(width, height)
    if len(pixels) < pixel_size:
        raise ValueError(
            "need %d bytes of pixel data for %dx%d, got %d" % (pixel_size, width, height, len(pixels))
        )
    file_header = struct.pack(
        "<2sIHHI",
        b"BM",
        BMP_FILE_HEADER_SIZE + BMP_INFO_HEADER_SIZE + pixel_size,
        0,
        0,
        BMP_FILE_HEADER_SIZE + BMP_INFO_HEADER_SIZE,
    )
    info_header = struct.pack(
        "<IiiHHIIiiII",
        BMP_INFO_HEADER_SIZE,
        width,
        -height,  # top-down
        1,
        32,
        0,  # BI_RGB
        pixel_size,
        2835,  # 72 dpi
        2835,
        0,
        0,
    )
    return file_header + info_header + pixels[:pixel_size]
