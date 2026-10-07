"""macOS preflight: permissions, displays and FPS region handling.

``doctor`` output is meant to be the first thing a user runs after installing, so
every item reports the precise System Settings path instead of raising.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from ...core.route import Region
from ..base import BackendUnavailable, Check

__all__ = ["preflight", "platform_info", "pick_region", "trial_read", "list_window_titles"]

_SETTINGS = "System Settings > Privacy & Security"

_ACCESSIBILITY_FIX = (
    "enable your terminal/IDE under %s > Accessibility (macOS only lets a trusted "
    "process read/inject events), then restart it" % _SETTINGS
)
_SCREEN_FIX = (
    "enable your terminal/IDE under %s > Screen Recording - it is needed by the "
    "overlay OCR FPS source (the fallback when metalperftrace cannot be used)" % _SETTINGS
)


def _quartz():
    try:
        import Quartz

        return Quartz
    except ImportError as exc:  # pragma: no cover
        raise BackendUnavailable(
            "pyobjc (Quartz) is not installed: %s" % exc, hint='python3 -m pip install -e ".[macos]"'
        ) from exc


def preflight(*, window_title: Optional[str] = None, region: Optional[Region] = None) -> List[Check]:
    checks: List[Check] = []
    checks.append(_check_python())
    checks.extend(_check_pyobjc())
    checks.append(_check_accessibility())
    checks.append(_check_screen_recording())
    checks.append(_check_secure_input())
    checks.extend(_check_displays())
    checks.append(_check_metalperftrace())
    checks.append(_check_ocr_backend())
    if region is not None:
        checks.append(_check_region(region))
    if window_title:
        checks.append(_check_window(window_title))
    return checks


def _check_python() -> Check:
    version = "%d.%d.%d" % sys.version_info[:3]
    if sys.version_info >= (3, 10):
        return Check("Python version", True, version)
    return Check(
        "Python version",
        True,
        "%s (3.9 works but pins pyobjc to 10.3.x; 3.10+ is recommended)" % version,
    )


def _check_pyobjc() -> List[Check]:
    from .._metalperftrace import find_metalperftrace

    ocr_optional = find_metalperftrace() is not None
    checks: List[Check] = []
    try:
        __import__("Quartz")
        checks.append(Check("pyobjc Quartz", True))
    except ImportError as exc:
        checks.append(
            Check("pyobjc Quartz", False, str(exc), fix='python3 -m pip install -e ".[macos]"', fatal=True)
        )
    for module, label in (("Vision", "Vision (OCR)"), ("ScreenCaptureKit", "ScreenCaptureKit")):
        try:
            __import__(module)
            checks.append(Check(label, True))
        except ImportError as exc:
            checks.append(
                Check(
                    label,
                    False,
                    str(exc),
                    fix='python3 -m pip install -e ".[macos]"',
                    fatal=module == "Vision" and not ocr_optional,
                )
            )
    return checks


def _check_accessibility() -> Check:
    try:
        import ApplicationServices

        options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        trusted = bool(ApplicationServices.AXIsProcessTrustedWithOptions(options))
    except ImportError:
        return Check("Accessibility permission", False, "ApplicationServices unavailable",
                     fix='python3 -m pip install -e ".[macos]"', fatal=True)
    except Exception as exc:
        return Check("Accessibility permission", False, "%s: %s" % (type(exc).__name__, exc), fix=_ACCESSIBILITY_FIX)
    if trusted:
        return Check("Accessibility permission", True, "granted (keyboard/mouse injection and recording)")
    return Check(
        "Accessibility permission",
        False,
        "not granted: input injection and CGEventTap recording will not work",
        fix=_ACCESSIBILITY_FIX,
        fatal=True,
    )


def _check_screen_recording() -> Check:
    from .._metalperftrace import find_metalperftrace

    per_frame = find_metalperftrace()
    try:
        quartz = _quartz()
        preflight_fn = getattr(quartz, "CGPreflightScreenCaptureAccess", None)
        if preflight_fn is None:
            return Check("Screen Recording permission", False, "CGPreflightScreenCaptureAccess unavailable",
                         fix=_SCREEN_FIX)
        granted = bool(preflight_fn())
    except BackendUnavailable as exc:
        return Check("Screen Recording permission", False, str(exc), fix=_SCREEN_FIX, fatal=True)
    if granted:
        return Check("Screen Recording permission", True, "granted (required for the OCR FPS fallback)")
    return Check(
        "Screen Recording permission",
        False,
        "not granted: only the overlay OCR fallback needs it"
        + ("" if per_frame else " and metalperftrace is not installed either, so there is no FPS source"),
        fix=_SCREEN_FIX,
        fatal=per_frame is None,
    )


def _check_metalperftrace() -> Check:
    from .._metalperftrace import (
        METALPERFTRACE_NOT_FOUND_HINT,
        find_metalperftrace,
        metalperftrace_version,
    )

    if sys.platform != "darwin":  # pragma: no cover - this module is macOS only
        return Check("metalperftrace", False, "not macOS")
    exe = find_metalperftrace()
    if exe is None:
        return Check(
            "metalperftrace",
            False,
            "not found: the default FPS source on macOS 27+ (FPS will come from overlay OCR)",
            fix=METALPERFTRACE_NOT_FOUND_HINT,
        )
    return Check(
        "metalperftrace",
        True,
        "%s (%s) - the default FPS source; no FPS region or Screen Recording needed"
        % (exe, metalperftrace_version(exe)),
    )


def _check_secure_input() -> Check:
    try:
        from .record import is_secure_event_input_enabled

        enabled = is_secure_event_input_enabled()
    except Exception as exc:
        return Check("Secure Input", False, "%s: %s" % (type(exc).__name__, exc))
    if enabled:
        return Check(
            "Secure Input",
            False,
            "Secure Input is enabled - keystrokes are withheld from recorders",
            fix="switch to another app (usually a password field or a terminal app enables it) and re-run",
        )
    return Check("Secure Input", True, "off")


def _check_displays() -> List[Check]:
    info = display_info()
    if not info:
        return [Check("Displays", False, "could not query displays (no GUI session?)")]
    detail = "%s points" % info.get("resolution", "?")
    if info.get("pixels"):
        detail += " (%s px)" % info["pixels"]
    detail += " @ %sHz" % info.get("refresh_hz", "?")
    if info.get("scale"):
        detail += ", scale %s" % info["scale"]
    return [Check("Displays", True, detail)]


def _check_ocr_backend() -> Check:
    from .._metalperftrace import find_metalperftrace

    optional = find_metalperftrace() is not None
    try:
        from .ocr import MacOcrBackend

        backend = MacOcrBackend()
        backend.prepare()
    except BackendUnavailable as exc:
        return Check("OCR backend", False, str(exc), fix=exc.hint or _SCREEN_FIX, fatal=not optional)
    except Exception as exc:
        return Check("OCR backend", False, "%s: %s" % (type(exc).__name__, exc), fix=_SCREEN_FIX,
                     fatal=not optional)
    return Check("OCR backend", True, "ScreenCaptureKit + Vision (the FPS fallback)")


def _check_region(region: Region) -> Check:
    result, error = trial_read(region)
    if error:
        return Check(
            "FPS region",
            False,
            "%s: %s" % (error, _describe(region)),
            fix="re-run `fpsbench pick-region` for a tighter box around the FPS digits",
            fatal=True,
        )
    assert result is not None
    if result.value is None:
        return Check(
            "FPS region",
            False,
            "read %r from %s -> no FPS value found (%s)" % (result.raw_text[:60], _describe(region), result.reason),
            fix=(
                "make sure the overlay counter is visible while the game runs, then re-run "
                "`fpsbench pick-region`"
            ),
            fatal=True,
        )
    return Check(
        "FPS region",
        True,
        "read %r -> %.1f FPS from %s" % (result.raw_text.replace("\n", " ")[:40], result.value, _describe(region)),
    )


def _check_window(title: str) -> Check:
    try:
        from .input import MacInputBackend

        backend = MacInputBackend()
        found = backend.find_window(title)
    except Exception as exc:
        return Check("Target window", False, "%s: %s" % (type(exc).__name__, exc))
    if not found:
        return Check(
            "Target window",
            False,
            "no on-screen window title contains %r" % (title,),
            fix=(
                "switch to the game window and retry, then check the exact title with "
                "`fpsbench doctor --list-windows`; that list only covers windows on the current "
                "Space (a fullscreen game has its own Space, and a minimised window is missing "
                "too), and without Screen Recording other apps' titles are hidden"
            ),
            fatal=True,
        )
    return Check("Target window", True, repr(found))


def list_window_titles() -> List[str]:
    """Titles ``doctor --list-windows`` prints (macOS: current Space, needs Screen Recording)."""
    from .input import list_window_titles as _titles

    return _titles()


def _describe(region: Region) -> str:
    return "%gx%g at (%g,%g) %s" % (region.w, region.h, region.x, region.y, region.space)


def trial_read(region: Region):
    """``(FpsParse, error)`` - one OCR attempt, used by doctor and pick-region."""
    try:
        from .ocr import MacOcrBackend

        backend = MacOcrBackend()
        backend.prepare()
        return backend.read_fps(region), None
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def display_info() -> Dict[str, Any]:
    """Primary display facts for the report metadata (never raises)."""
    info: Dict[str, Any] = {}
    try:
        from AppKit import NSScreen

        screen = NSScreen.mainScreen()
        if screen is not None:
            frame = screen.frame()
            info["resolution"] = "%dx%d" % (frame.size.width, frame.size.height)
            info["scale"] = screen.backingScaleFactor()
    except Exception:
        pass
    try:
        quartz = _quartz()
        display = quartz.CGMainDisplayID()
        pixels = (quartz.CGDisplayPixelsWide(display), quartz.CGDisplayPixelsHigh(display))
        info["pixels"] = "%dx%d" % pixels
        mode = quartz.CGDisplayCopyDisplayMode(display)
        if mode is not None:
            refresh = quartz.CGDisplayModeGetRefreshRate(mode)
            if refresh:
                info["refresh_hz"] = round(float(refresh), 2)
        info["refresh_hz"] = info.get("refresh_hz", "unknown")
    except Exception:
        pass
    try:
        import platform as _platform

        info["arch"] = _platform.machine()
    except Exception:
        pass
    return info


def platform_info() -> Dict[str, Any]:
    """Metadata block for a bench run (display + GPU + FPS source when cheaply available)."""
    info = display_info()
    gpu = _gpu_name()
    if gpu:
        info["gpu"] = gpu
    from .._metalperftrace import find_metalperftrace, metalperftrace_version

    exe = find_metalperftrace()
    if exe:
        info["metalperftrace"] = metalperftrace_version(exe)
    return info


def _gpu_name() -> Optional[str]:
    """``system_profiler`` is slow; cache-free but bounded and never fatal."""
    import subprocess

    try:
        result = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except Exception:
        return None
    for line in (result.stdout or b"").decode("utf-8", "replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("Chipset Model:"):
            return stripped.split(":", 1)[1].strip()
    return None


# --------------------------------------------------------------------------- #
# pick-region
# --------------------------------------------------------------------------- #
def pick_region(*, message: str = "Drag a box around the FPS counter") -> Optional[Region]:
    """Interactive rubber-band selection; ``None`` when the user cancels."""
    from .selection import select_region_appkit

    return select_region_appkit(message)


def manual_region(prompt: str = "region x,y,w,h: ") -> Optional[Region]:
    """Type the region instead of dragging (also useful over SSH)."""
    try:
        raw = input(prompt).strip()
    except EOFError:
        return None
    if not raw:
        return None
    parts = [piece for piece in raw.replace(",", " ").replace("x", " ").split() if piece]
    if len(parts) != 4:
        raise ValueError("expected x,y,w,h - got %r" % raw)
    x, y, w, h = (float(piece) for piece in parts)
    return Region(x=x, y=y, w=w, h=h, space="points")
