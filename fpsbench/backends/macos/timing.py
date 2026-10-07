"""macOS timing boost: ask for the user-interactive QoS class.

The player schedules events on an absolute timeline with a coarse sleep plus a
busy-wait tail.  On macOS the scheduler can still deprioritise that thread (timer
coalescing, App Nap), which shows up as millisecond-scale lateness on a long
route.  Requesting ``QOS_CLASS_USER_INTERACTIVE`` for the calling thread removes
most of it.

Everything here is best effort: a failure is reported as a note and never raises,
because a slightly less precise timeline still produces a valid measurement.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from typing import List

__all__ = ["boost_timing", "current_qos_class", "QOS_CLASS_USER_INTERACTIVE"]

QOS_CLASS_USER_INTERACTIVE = 0x21
QOS_CLASS_USER_INITIATED = 0x19

_QOS_NAMES = {
    0x21: "user-interactive",
    0x19: "user-initiated",
    0x15: "default",
    0x11: "utility",
    0x09: "background",
    0x00: "unspecified",
}


def _libsystem():
    path = ctypes.util.find_library("System") or "/usr/lib/libSystem.dylib"
    lib = ctypes.CDLL(path, use_errno=True)
    # Signatures must be declared: passing an int where a pthread_t pointer is
    # expected (or the reverse) segfaults inside libSystem.
    lib.pthread_set_qos_class_self_np.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.pthread_set_qos_class_self_np.restype = ctypes.c_int
    lib.pthread_get_qos_class_np.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.pthread_get_qos_class_np.restype = ctypes.c_int
    lib.pthread_self.restype = ctypes.c_void_p
    return lib


def current_qos_class() -> int:
    """QoS class of the calling thread (``-1`` when it cannot be read)."""
    try:
        lib = _libsystem()
        qos_class = ctypes.c_int(0)
        relative = ctypes.c_int(0)
        if lib.pthread_get_qos_class_np(
            lib.pthread_self(), ctypes.byref(qos_class), ctypes.byref(relative)
        ) != 0:
            return -1
        return int(qos_class.value)
    except Exception:
        return -1


def boost_timing() -> List[str]:
    """Request the user-interactive QoS class for this thread (best effort)."""
    try:
        lib = _libsystem()
        if lib.pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0) != 0:
            return ["could not request the user-interactive QoS class; playback timing may jitter"]
    except Exception as exc:
        return ["QoS request unavailable (%s); playback timing may jitter" % type(exc).__name__]
    observed = current_qos_class()
    name = _QOS_NAMES.get(observed, hex(observed) if observed >= 0 else "unknown")
    if observed == QOS_CLASS_USER_INTERACTIVE:
        return ["playback thread QoS: user-interactive (sub-millisecond scheduling)"]
    return ["playback thread QoS is %s: the system may coalesce our timers" % name]
