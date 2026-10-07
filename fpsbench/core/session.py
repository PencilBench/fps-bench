"""Run metadata: everything needed to decide whether two results are comparable.

The metadata block is printed with every report and embedded in the JSON/CSV
exports.  Without it an FPS number is meaningless - the same route on the same
machine at a different resolution or refresh rate is a different measurement.
"""

from __future__ import annotations

import getpass
import os
import platform
import sys
import time
from typing import Any, Dict, List, Optional

__all__ = ["collect_base", "collect_session", "utc_now", "format_metadata"]


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def collect_base() -> Dict[str, Any]:
    """OS / interpreter facts available without any platform backend."""
    import fpsbench

    return {
        "tool": "fpsbench",
        "tool_version": fpsbench.__version__,
        "os": platform.system(),
        "os_version": platform.mac_ver()[0] if sys.platform == "darwin" else platform.version(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "cpu": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "user": getpass.getuser(),
        "cwd": os.getcwd(),
        "argv": list(sys.argv),
        "timestamp": utc_now(),
    }


def collect_session(
    *,
    platform_info: Optional[Dict[str, Any]] = None,
    route_name: Optional[str] = None,
    route_hash: Optional[str] = None,
    route_source: Optional[str] = None,
    origin_platform: Optional[str] = None,
    fps_source: Optional[str] = None,
    fps_config: Optional[Dict[str, Any]] = None,
    warmup_ms: Optional[float] = None,
    settle_ms: Optional[float] = None,
    repeats: Optional[int] = None,
    backend_notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Assemble the metadata block for a bench run."""
    metadata = collect_base()
    metadata.update({"platform": sys.platform})
    if platform_info:
        metadata["platform_info"] = platform_info
    if route_name is not None:
        metadata["route_name"] = route_name
    if route_hash is not None:
        metadata["route_hash"] = route_hash
    if route_source is not None:
        metadata["route_source"] = route_source
    if origin_platform is not None:
        # Where the route was *recorded*: replaying a macOS trace on Windows is
        # supported, so this is not the same as the platform of the measurement.
        metadata["origin_platform"] = origin_platform
    if fps_source is not None:
        metadata["fps_source"] = fps_source
    if fps_config is not None:
        metadata["fps_config"] = fps_config
    if warmup_ms is not None:
        metadata["warmup_ms"] = warmup_ms
    if settle_ms is not None:
        metadata["settle_ms"] = settle_ms
    if repeats is not None:
        metadata["repeats"] = repeats
    if backend_notes:
        metadata["backend_notes"] = list(backend_notes)
    return metadata


#: Order and labels of the metadata lines shown by the report.
_DISPLAY_ORDER = (
    ("os", "OS"),
    ("os_version", "OS version"),
    ("machine", "machine"),
    ("cpu", "CPU"),
    ("cpu_count", "cores"),
    ("python", "Python"),
    ("tool_version", "fpsbench"),
    ("platform", "platform"),
    ("fps_source", "FPS source"),
    ("fps_window", "FPS window"),
    ("input_pacing", "input pacing"),
    ("input_rate_hz", "input program"),
    ("look_split", "look timing"),
    ("gamepad_backend", "gamepad backend"),
    ("frame_period_ms", "frame pace"),
    ("frame_pace_source", "pace source"),
    ("input_frames", "input frames"),
    ("worst_lateness_ms", "worst lateness"),
    ("route_name", "route"),
    ("route_hash", "route hash"),
    ("route_source", "route source"),
    ("origin_platform", "recorded on"),
    ("warmup_ms", "warmup"),
    ("settle_ms", "settle"),
    ("repeats", "repeats"),
    ("timestamp", "measured"),
)


def format_metadata(metadata: Dict[str, Any]) -> List[str]:
    """Human readable ``key: value`` lines for the report header."""
    lines: List[str] = []
    for key, label in _DISPLAY_ORDER:
        if key in metadata and metadata[key] is not None:
            lines.append("%s: %s" % (label, metadata[key]))
    info = metadata.get("platform_info") or {}
    for key in ("gpu", "displays", "refresh_hz", "resolution", "scale", "presentmon", "metalperftrace"):
        if info.get(key):
            lines.append("%s: %s" % (key.replace("_", " "), info[key]))
    region = (metadata.get("fps_config") or {}).get("region")
    if isinstance(region, dict):
        lines.append(
            "FPS region: %sx%s at (%s,%s) %s"
            % (region.get("w"), region.get("h"), region.get("x"), region.get("y"), region.get("space"))
        )
    recorded_gamepad = metadata.get("gamepad")
    if isinstance(recorded_gamepad, dict) and recorded_gamepad.get("device"):
        lines.append(
            "gamepad recorded: %s (%s, poll %s Hz)"
            % (
                recorded_gamepad.get("device"),
                recorded_gamepad.get("source") or "?",
                recorded_gamepad.get("poll_hz") or "?",
            )
        )
    for note in metadata.get("backend_notes") or []:
        lines.append("note: %s" % note)
    return lines
