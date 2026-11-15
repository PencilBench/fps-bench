"""Backend interfaces and platform loading.

Every backend is created with no arguments and configured through ``prepare`` /
``start``.  Missing dependencies are reported as :class:`BackendUnavailable`
carrying the exact command to run - never as a raw ``ImportError``, and never as
a silent fallback.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..core.gamepad import GamepadState
from ..core.ocrparse import FpsParse
from ..core.route import FpsConfig, Region
from ..core.samples import OCR, PRESENTMON, FpsSample
from ..core.trace import TraceEvent

__all__ = [
    "BackendError",
    "BackendUnavailable",
    "InputBackend",
    "RecorderBackend",
    "GamepadBackend",
    "GamepadRecorderBackend",
    "FpsBackend",
    "OcrBackend",
    "ROLES",
    "current_platform",
    "install_hint",
    "load_backend",
    "load_backends",
    "load_module",
    "Check",
    "FpsSample",
    "PRESENTMON",
    "OCR",
]

ROLES = ("input", "record", "fps", "ocr", "gamepad", "gamepad_record")

_INSTALL_HINTS = {
    "macos": 'python3 -m pip install -e ".[macos]"',
    "windows": 'py -m pip install -e ".[windows]"',
}


class BackendError(RuntimeError):
    """Base class for backend failures."""


class BackendUnavailable(BackendError):
    """The backend cannot run here (missing dependency, wrong OS, no permission)."""

    def __init__(self, message: str, *, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Check:
    """One preflight result; ``doctor`` prints these verbatim."""

    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    fatal: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        mark = "OK  " if self.ok else ("FAIL" if self.fatal else "WARN")
        line = "[%s] %s" % (mark, self.name)
        if self.detail:
            line += " - %s" % self.detail
        if not self.ok and self.fix:
            line += "\n         fix: %s" % self.fix
        return line


class InputBackend:
    """Injects keyboard/mouse input into whatever window has focus."""

    name = "input"

    def prepare(self, *, window_title: Optional[str] = None, post_to_pid: Optional[int] = None) -> None:
        """Acquire what the implementation needs (DPI awareness, cursor state, ...)."""

    def key_down(self, key: str) -> None:
        raise NotImplementedError

    def key_up(self, key: str) -> None:
        raise NotImplementedError

    def look(self, dx: float, dy: float) -> None:
        """Relative mouse movement (no cursor push, no screen edge clamping)."""
        raise NotImplementedError

    def scroll(self, dy: float, dx: float = 0.0) -> None:
        raise NotImplementedError

    def button_down(self, button: str) -> None:
        raise NotImplementedError

    def button_up(self, button: str) -> None:
        raise NotImplementedError

    def teardown(self) -> None:
        """Undo whatever :meth:`prepare` changed."""

    def find_window(self, title_substring: str) -> Optional[str]:
        """Title of the first top-level window matching ``title_substring``."""
        return None

    def activate_window(self, title_substring: str) -> bool:
        """Bring the matching window to the foreground (``False`` = not found)."""
        return False

    def unsupported_keys(self) -> List[str]:
        """Canonical keys seen while playing that this platform cannot inject.

        A trace recorded on the other OS may use a key that does not exist here
        (``pause`` on macOS, for instance).  Those events are skipped and
        reported instead of aborting the run.
        """
        return []

    def capabilities(self) -> Dict[str, Any]:
        return {}


class RecorderBackend:
    """Records the user's own play-through as trace events on a monotonic clock."""

    name = "record"
    #: Human readable hotkey used to drop a ``label`` event.
    label_hotkey = "n/a"
    #: Human readable hotkey that *starts* and *stops* the recording ("" = none).
    finish_hotkey = ""
    #: True when capture begins on its own; False when the hotkey starts it.
    records_immediately = True
    supports_labels = False

    def prepare(self, *, window_title: Optional[str] = None) -> None:
        pass

    def start(self) -> None:
        raise NotImplementedError

    def recording(self) -> bool:
        """True once events are being captured.

        A recorder whose :attr:`records_immediately` is ``False`` returns ``False``
        until the start hotkey is pressed; before that its :meth:`poll` yields
        nothing, so the trace starts at the moment the user asked for.
        """
        return self.records_immediately

    def arm(self) -> None:
        """Start capturing **without** the start hotkey.

        This is what a recorder's own hotkey handler does on its first press; every
        command (`record`, `bench`, `measure`) now uses that explicit start gesture,
        so nothing arms the tap up front.  Recorders that cannot do it raise
        ``NotImplementedError``.
        """
        raise NotImplementedError("this recorder cannot be armed without its hotkey")

    def stop_requested(self) -> bool:
        """True once the in-game :attr:`finish_hotkey` has been pressed."""
        return False

    def poll(self) -> List[TraceEvent]:
        """Events recorded since the previous call (never blocks for long)."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def warnings(self) -> List[str]:
        return []


class GamepadBackend:
    """Sends complete gamepad state to a virtual device the game can enumerate.

    This is a **separate device** from :class:`InputBackend` on purpose: a
    keyboard/mouse trace and a controller trace are different input paths, and a
    game that only reads one of them must not be shown a fake success.
    """

    name = "gamepad"

    def prepare(self, *, window_title: Optional[str] = None) -> None:
        """Create/enable the virtual device and report what was enumerated."""

    def set_state(self, state: GamepadState) -> None:
        """Publish a full state (axes, triggers and buttons at once)."""
        raise NotImplementedError

    def neutral(self) -> None:
        """Centre every axis, rest both triggers and release every button."""
        raise NotImplementedError

    def teardown(self) -> None:
        """Release the virtual device (always called, also after a failure)."""

    def notes(self) -> List[str]:
        return []

    def capabilities(self) -> Dict[str, Any]:
        return {}


class GamepadRecorderBackend:
    """Records a real controller's state changes on a monotonic clock."""

    name = "gamepad-record"

    def prepare(self, *, device: Optional[str] = None, poll_hz: Optional[float] = None) -> None:
        """Open the device (or fail with an actionable message)."""

    def start(self) -> None:
        raise NotImplementedError

    def poll(self) -> List[TraceEvent]:
        """``gamepad`` events recorded since the previous call (never blocks)."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def end_ms(self) -> Optional[float]:
        """Recording length in ms, so a held final state keeps its duration."""
        return None

    def device_info(self) -> Dict[str, Any]:
        """Metadata describing the device and the mapping that was applied."""
        return {}

    def notes(self) -> List[str]:
        return []


class FpsBackend:
    """Produces a stream of :class:`FpsSample` (one source only)."""

    name = "fps"
    #: One of ``core.samples.SOURCES`` - set by ``start``.
    source = ""

    def start(
        self,
        config: FpsConfig,
        *,
        process_name: Optional[str] = None,
        presentmon_path: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    def samples(self) -> Iterator[FpsSample]:
        """Yield samples until :meth:`stop` is called (consumed in a thread)."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def final_samples(self) -> Optional[List[FpsSample]]:
        """Authoritative sample set after the run, or ``None`` to use the stream.

        OCR needs a final pass (an unconfirmed jump can only be detected once the
        *next* reading exists), so it returns the vetted list here.  The per-frame
        backends (PresentMon, metalperftrace) return ``None`` because their stream
        is already final.
        """
        return None

    def notes(self) -> List[str]:
        """Diagnostics to surface in the report (e.g. "fell back to OCR")."""
        return []


class OcrBackend:
    """Screen region capture plus number recognition (used by the OCR FPS path)."""

    name = "ocr"
    #: Coordinate space this platform's capture APIs use for regions.
    region_space = "points"

    def prepare(self) -> None:
        pass

    def grab_region(self, region: Region) -> Any:
        """Return an opaque image handle for ``region``."""
        raise NotImplementedError

    def read_fps(self, region: Region, *, min_fps: float = 0.5, max_fps: float = 2000.0) -> FpsParse:
        """Recognise and parse the FPS counter in ``region``."""
        raise NotImplementedError

    def trial_read(self, region: Region) -> FpsParse:
        """``read_fps`` with defaults, used by ``doctor`` and ``pick-region``."""
        return self.read_fps(region)


def current_platform() -> str:
    """``"macos"``, ``"windows"`` or ``"unsupported"``."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "unsupported"


def install_hint(platform: Optional[str] = None) -> str:
    return _INSTALL_HINTS.get(platform or current_platform(), "")


def load_backend(role: str, *, platform: Optional[str] = None, **args: Any):
    """Instantiate the ``role`` backend for ``platform`` (defaults to this one).

    Raises :class:`BackendUnavailable` with an actionable hint when the platform
    is unsupported or a dependency is missing.
    """
    if role not in ROLES:
        raise ValueError("unknown backend role %r (expected one of %s)" % (role, ROLES))
    module = load_module(role, platform=platform)
    factory = getattr(module, "BACKEND", None)
    if factory is None:
        raise BackendUnavailable("%s does not export BACKEND" % module.__name__)
    return factory(**args)


def load_module(name: str, *, platform: Optional[str] = None):
    """Import ``fpsbench.backends.<platform>.<name>`` (e.g. the ``preflight`` module)."""
    target = platform or current_platform()
    if target == "unsupported":
        raise BackendUnavailable(
            "%s is not supported: fpsbench only implements macOS and Windows 11 backends"
            % sys.platform,
            hint="run the tool on macOS or Windows 11",
        )
    module_name = "fpsbench.backends.%s.%s" % (target, name)
    try:
        return importlib.import_module(module_name)
    except BackendUnavailable:
        raise
    except ImportError as exc:
        raise BackendUnavailable(
            "cannot load %s for %s: %s" % (name, target, exc), hint=install_hint(target)
        ) from exc


def load_backends(
    roles: Sequence[str] = ROLES, *, platform: Optional[str] = None
) -> Dict[str, Any]:
    """Load several backends, letting the caller decide what to do per failure."""
    out: Dict[str, Any] = {}
    for role in roles:
        out[role] = load_backend(role, platform=platform)
    return out
