"""Small user config file (``~/.fpsbench.json``) for things that outlive one run.

The FPS settings are the main ones: ``pick-region`` writes the OCR region here and
``--process-name`` can be stored here too, and ``record`` / ``bench`` read them
when the route (or the CLI) does not carry their own.  Keeping them out of the
route file means a single setup run configures every route on that machine.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from .core.route import FpsConfig

__all__ = [
    "config_path",
    "load_config",
    "save_config",
    "config_fps",
    "config_input",
    "update_config",
]

_ENV_OVERRIDE = "FPSBENCH_CONFIG"


def config_path() -> str:
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser("~"), ".fpsbench.json")


def load_config() -> Dict[str, Any]:
    """Config as a dict; a missing or unreadable file is simply empty."""
    path = config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_config(config: Dict[str, Any]) -> str:
    path = config_path()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def update_config(**values: Any) -> str:
    """Merge ``values`` (``None`` values are dropped) into the config file."""
    config = load_config()
    for key, value in values.items():
        if value is not None:
            config[key] = value
    return save_config(config)


def config_fps(*, mode: Optional[str] = None) -> Optional[FpsConfig]:
    """FPS configuration from the config file, or ``None`` when it has none."""
    config = load_config()
    fps_block = config.get("fps") if isinstance(config.get("fps"), dict) else {}
    # A region is only needed for the OCR fallback, so a process name (or an
    # explicit mode) on its own is a valid FPS configuration.
    top_level_keys = {"region", "process_name", "presentmon_path", "fps_mode"}
    if not fps_block and not (top_level_keys & set(config)):
        return None
    data: Dict[str, Any] = {
        "mode": mode or fps_block.get("mode") or config.get("fps_mode") or "auto",
        "sample_hz": fps_block.get("sample_hz", 6.0),
        "min_fps": fps_block.get("min_fps", 0.5),
        "max_fps": fps_block.get("max_fps", 2000.0),
        "presentmon_path": fps_block.get("presentmon_path") or config.get("presentmon_path"),
        "process_name": fps_block.get("process_name") or config.get("process_name"),
        "region": fps_block.get("region") or config.get("region"),
    }
    try:
        return FpsConfig.from_dict(data, "config")
    except Exception:
        return None


#: Optional input settings (they describe *this machine*, not a game).
_INPUT_KEYS = {"look_split", "gamepad_device", "gamepad_poll_hz"}


def config_input() -> Dict[str, Any]:
    """Optional input settings from the config file (``input`` block).

    Unlike the FPS block - which silently ignores a broken value because it is
    only a convenience - a bad input setting is a hard error: silently falling
    back would change how input is replayed without telling anybody.
    """
    config = load_config()
    block = config.get("input")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ValueError("config: 'input' must be an object")
    unknown = sorted(set(block) - _INPUT_KEYS)
    if unknown:
        raise ValueError(
            "config: unknown input setting %r (expected %s)"
            % (unknown[0], ", ".join(sorted(_INPUT_KEYS)))
        )
    out: Dict[str, Any] = {}
    if "look_split" in block:
        value = block["look_split"]
        if value not in ("chunk", "raw"):
            raise ValueError("config: input.look_split must be 'chunk' or 'raw', got %r" % (value,))
        out["look_split"] = value
    if "gamepad_device" in block:
        value = block["gamepad_device"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("config: input.gamepad_device must be a non-empty string")
        out["gamepad_device"] = value.strip()
    if "gamepad_poll_hz" in block:
        value = block["gamepad_poll_hz"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("config: input.gamepad_poll_hz must be a number")
        if not 1.0 <= float(value) <= 2000.0:
            raise ValueError("config: input.gamepad_poll_hz must be within 1..2000")
        out["gamepad_poll_hz"] = float(value)
    return out
