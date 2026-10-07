"""Windows preflight: permissions, UIPI, PresentMon and display geometry.

UIPI is the failure mode worth checking explicitly: when the game runs at a
higher integrity level than this process, ``SendInput`` succeeds but Windows
silently discards the events, which looks exactly like "the game ignores me".
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Any, Dict, List, Optional

from ...core.route import Region
from .._win_types import DEVMODEW, install_prototypes
from ..base import BackendUnavailable, Check

__all__ = ["preflight", "platform_info", "pick_region", "trial_read", "is_user_admin"]

if not sys.platform.startswith("win"):
    raise BackendUnavailable(
        "the Windows preflight can only run on Windows (this is %s)" % sys.platform,
        hint="run fpsbench on Windows 11",
    )

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

for _dll in (user32, kernel32, shell32, advapi32):
    install_prototypes(_dll)

_SESSION_FIX = (
    "run your terminal/IDE as Administrator (and, for anti-cheat games with a kernel driver, "
    "keep the game at the same integrity level)"
)


def is_user_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def _integrity_level() -> Optional[str]:
    """Current process integrity level name (Low/Medium/High/System)."""
    TOKEN_QUERY = 0x0008
    TokenIntegrityLevel = 25
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, TokenIntegrityLevel, buffer, size.value, ctypes.byref(size)
        ):
            return None
        # TOKEN_MANDATORY_LABEL: SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; }
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
        if not sid:
            return None
        count = advapi32.GetSidSubAuthorityCount(sid)
        if not count:
            return None
        sub_authority = advapi32.GetSidSubAuthority(sid, count.contents.value - 1)
        if not sub_authority:
            return None
        level = int(sub_authority.contents.value)
        return {0x1000: "Low", 0x2000: "Medium", 0x3000: "High", 0x4000: "System"}.get(
            level, "0x%X" % level
        )
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(token)


def preflight(*, window_title: Optional[str] = None, region: Optional[Region] = None) -> List[Check]:
    checks: List[Check] = []
    checks.append(_check_admin())
    checks.append(_check_dpi())
    checks.append(_check_ocr_engine())
    checks.append(_check_presentmon())
    checks.extend(_check_display())
    if region is not None:
        checks.append(_check_region(region))
    if window_title:
        checks.append(_check_window(window_title))
    return checks


def _check_admin() -> Check:
    level = _integrity_level()
    if is_user_admin():
        return Check("Integrity level", True, "%s and elevated - synthetic input will not be blocked"
                     % (level or "High"))
    return Check(
        "Integrity level",
        False,
        "%s (not elevated): an elevated game will silently ignore injected input (UIPI)" % (level or "Medium"),
        fix=_SESSION_FIX,
    )


def _check_dpi() -> Check:
    try:
        user32.SetProcessDPIAware()
    except Exception as exc:
        return Check("DPI awareness", False, str(exc), fix=_SESSION_FIX)
    return Check("DPI awareness", True, "SetProcessDPIAware() so region pixels are physical")


def _check_ocr_engine() -> Check:
    try:
        from .ocr import WindowsOcrBackend

        backend = WindowsOcrBackend()
        backend.prepare()
    except Exception as exc:
        hint = getattr(exc, "hint", None)
        return Check("Windows OCR engine", False, str(exc), fix=hint or 'py -m pip install -e ".[windows]"')
    return Check("Windows OCR engine", True, "Windows.Media.Ocr ready (fallback path)")


def _check_presentmon() -> Check:
    from .._presentmon import PRESENTMON_DOWNLOAD_HINT, find_presentmon, presentmon_version

    exe = find_presentmon()
    if not exe:
        return Check(
            "PresentMon",
            False,
            "not found - FPS will come from overlay OCR, so 1%/0.1% lows become approximations",
            fix=PRESENTMON_DOWNLOAD_HINT,
        )
    return Check("PresentMon", True, "%s (%s)" % (exe, presentmon_version(exe)))


def _check_display() -> List[Check]:
    info = display_info()
    if not info.get("resolution"):
        return [Check("Display", False, "could not read the display mode")]
    detail = "%s @ %sHz" % (info["resolution"], info.get("refresh_hz", "?"))
    if info.get("scale"):
        detail += ", scale %s" % info["scale"]
    return [Check("Display", True, detail)]


def _check_region(region: Region) -> Check:
    result, error = trial_read(region)
    if error:
        return Check(
            "FPS region",
            False,
            "%s: %gx%g at (%g,%g)" % (error, region.w, region.h, region.x, region.y),
            fix="re-run `fpsbench pick-region`",
            fatal=True,
        )
    assert result is not None
    if result.value is None:
        return Check(
            "FPS region",
            False,
            "read %r -> no FPS value (%s)" % (result.raw_text[:60], result.reason),
            fix="make the overlay counter visible and re-run `fpsbench pick-region`",
            fatal=True,
        )
    return Check(
        "FPS region",
        True,
        "read %r -> %.1f FPS" % (result.raw_text.replace("\n", " ")[:40], result.value),
    )


def _check_window(title: str) -> Check:
    from .input import WindowsInputBackend

    try:
        backend = WindowsInputBackend()
        found = backend.find_window(title)
    except Exception as exc:
        return Check("Target window", False, "%s: %s" % (type(exc).__name__, exc))
    if not found:
        return Check(
            "Target window",
            False,
            "no visible window title contains %r" % (title,),
            fix=(
                "start the game and switch to its window, then check the exact title with "
                "`fpsbench doctor --list-windows`"
            ),
            fatal=True,
        )
    return Check("Target window", True, repr(found))


def trial_read(region: Region):
    """``(FpsParse, error)`` - one OCR attempt for doctor and pick-region."""
    try:
        from .ocr import WindowsOcrBackend

        backend = WindowsOcrBackend()
        backend.prepare()
        return backend.read_fps(region), None
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def display_info() -> Dict[str, Any]:
    from .._presentmon import find_presentmon, presentmon_version

    info: Dict[str, Any] = {}
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    try:
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)
        info["resolution"] = "%dx%d" % (width, height)
    except Exception:
        pass
    try:
        mode = DEVMODEW()
        mode.dmSize = ctypes.sizeof(DEVMODEW)
        ENUM_CURRENT_SETTINGS = -1  # 0xFFFFFFFF
        if user32.EnumDisplaySettingsW(None, ctypes.c_uint32(ENUM_CURRENT_SETTINGS), ctypes.byref(mode)):
            if mode.dmPelsWidth and mode.dmPelsHeight:
                info["pixels"] = "%dx%d" % (mode.dmPelsWidth, mode.dmPelsHeight)
                info["resolution"] = info["pixels"]
            if mode.dmDisplayFrequency:
                info["refresh_hz"] = int(mode.dmDisplayFrequency)
    except Exception:
        pass
    try:
        dpi = user32.GetDpiForSystem()
        info["scale"] = round(dpi / 96.0, 2)
    except Exception:
        pass
    exe = find_presentmon()
    if exe:
        info["presentmon"] = "%s (%s)" % (os.path.basename(exe), presentmon_version(exe))
    info["integrity_level"] = _integrity_level() or "unknown"
    info["elevated"] = is_user_admin()
    return info


def platform_info() -> Dict[str, Any]:
    return display_info()


def pick_region(*, message: str = "Drag a box around the FPS counter") -> Optional[Region]:
    from .selection import select_region_tkinter

    return select_region_tkinter(message)


def list_window_titles() -> List[str]:
    from .input import list_windows

    return [title for _, title in list_windows()]
