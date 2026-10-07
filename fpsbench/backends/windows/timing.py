"""Windows timing boost: ``timeBeginPeriod(1)`` while a run is in progress.

Without it the smallest sleep the OS honours is ~15.6 ms, which would turn the
scheduler's coarse sleep into a visible staircase.  ``timeBeginPeriod(1)`` lowers
the timer resolution to 1 ms for the process (it is reference counted, so the
matching ``timeEndPeriod`` call matters and is done by :func:`restore_timing`).

Best effort: failures are reported as notes, never raised.
"""

from __future__ import annotations

import ctypes
import sys
from typing import List

__all__ = ["boost_timing", "restore_timing"]

_PERIOD_MS = 1


def _winmm():
    if not sys.platform.startswith("win"):
        raise OSError("winmm is only available on Windows")
    return ctypes.WinDLL("winmm")


def boost_timing() -> List[str]:
    """Raise the timer resolution for this process (idempotent per call)."""
    try:
        result = _winmm().timeBeginPeriod(_PERIOD_MS)
    except Exception as exc:
        return ["timeBeginPeriod(%d) unavailable (%s); sleeps may be quantised to 15.6 ms" % (_PERIOD_MS, type(exc).__name__)]
    if result != 0:
        return ["timeBeginPeriod(%d) failed with code %d" % (_PERIOD_MS, result)]
    return ["Windows timer resolution raised to %d ms for the run" % _PERIOD_MS]


def restore_timing() -> List[str]:
    """Release the timer resolution claim made by :func:`boost_timing`."""
    try:
        result = _winmm().timeEndPeriod(_PERIOD_MS)
    except Exception as exc:
        return ["timeEndPeriod(%d) unavailable (%s)" % (_PERIOD_MS, type(exc).__name__)]
    if result != 0:
        return ["timeEndPeriod(%d) returned %d" % (_PERIOD_MS, result)]
    return []
