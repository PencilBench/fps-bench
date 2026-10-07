"""Windows gamepad recording: SDL2 (pygame) GameController polling.

The recorder is shared code (``fpsbench.backends._sdl_gamepad``); this module only
binds it to the Windows backend role so ``load_backend("gamepad_record")`` works.
"""

from __future__ import annotations

from .._sdl_gamepad import BACKEND, SdlGamepadRecorder, probe  # noqa: F401

__all__ = ["BACKEND", "SdlGamepadRecorder", "probe"]
