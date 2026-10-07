"""ctypes definitions for the Win32 structures used by the Windows backends.

These live in their own module - with no ``WinDLL`` calls and no platform guard -
so that the *layout* can be unit tested anywhere.  A wrong offset or size is the
classic reason ``SendInput`` fails with ``ERROR_INVALID_PARAMETER`` (or the Raw
Input parser reads garbage), and that would otherwise only be discovered on a
Windows machine.

The fields use explicit fixed-width types (``c_uint32``, ``c_uint16``, ...) rather
than ``ctypes.wintypes``: ``wintypes`` maps ``DWORD`` to the *host* ``c_ulong``,
which is 8 bytes outside Windows, so the structure would only have the right
layout on Windows.  Pointer-sized fields use ``c_void_p`` / ``c_size_t``, which
are correct on both LLP64 and LP64.
"""

from __future__ import annotations

import ctypes
from typing import Tuple

__all__ = [
    "ULONG_PTR",
    "WORD",
    "DWORD",
    "LONG",
    "UINT",
    "USHORT",
    "WCHAR",
    "BYTE",
    "INPUT",
    "MOUSEINPUT",
    "KEYBDINPUT",
    "HARDWAREINPUT",
    "RAWINPUT",
    "RAWINPUTHEADER",
    "RAWINPUTDEVICE",
    "RAWMOUSE",
    "RAWKEYBOARD",
    "RAWHID",
    "WNDCLASSW",
    "DEVMODEW",
    "WNDPROC",
    "WNDPROC_TYPE",
    "key_input",
    "mouse_move_input",
    "mouse_wheel_input",
    "mouse_button_input",
    "LRESULT",
    "INPUT_MOUSE",
    "INPUT_KEYBOARD",
    "KEYEVENTF_EXTENDEDKEY",
    "KEYEVENTF_KEYUP",
    "KEYEVENTF_SCANCODE",
    "MOUSEEVENTF_MOVE",
    "MOUSEEVENTF_LEFTDOWN",
    "MOUSEEVENTF_LEFTUP",
    "MOUSEEVENTF_RIGHTDOWN",
    "MOUSEEVENTF_RIGHTUP",
    "MOUSEEVENTF_MIDDLEDOWN",
    "MOUSEEVENTF_MIDDLEUP",
    "MOUSEEVENTF_XDOWN",
    "MOUSEEVENTF_XUP",
    "MOUSEEVENTF_WHEEL",
    "MOUSEEVENTF_HWHEEL",
    "MOUSEEVENTF_MOVE_NOCOALESCE",
    "WHEEL_DELTA",
    "XBUTTON1",
    "XBUTTON2",
    "WM_INPUT",
    "WM_CLOSE",
    "WM_DESTROY",
    "WM_QUIT",
    "RIDEV_INPUTSINK",
    "RID_INPUT",
    "RIM_TYPEMOUSE",
    "RIM_TYPEKEYBOARD",
    "RI_KEY_BREAK",
    "RI_KEY_E0",
    "RI_KEY_E1",
    "MOUSE_MOVE_RELATIVE",
    "MOUSE_MOVE_ABSOLUTE",
    "MOUSE_BUTTON_FLAGS",
    "RI_MOUSE_WHEEL",
    "RI_MOUSE_HWHEEL",
    "button_flag_table",
    "WIN_PROTOTYPES",
    "install_prototypes",
]

# --------------------------------------------------------------------------- #
# input flags
# --------------------------------------------------------------------------- #
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000
WHEEL_DELTA = 120
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

# --------------------------------------------------------------------------- #
# raw input
# --------------------------------------------------------------------------- #
WM_INPUT = 0x00FF
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
HWND_MESSAGE = ctypes.c_void_p(-3)

RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1

RI_KEY_BREAK = 0x01
RI_KEY_E0 = 0x02
RI_KEY_E1 = 0x04

MOUSE_MOVE_RELATIVE = 0x00
MOUSE_MOVE_ABSOLUTE = 0x01

#: ``RAWMOUSE.usButtonFlags`` bits -> ``(button, down)``.
MOUSE_BUTTON_FLAGS: Tuple[Tuple[int, str, bool], ...] = (
    (0x0001, "left", True),
    (0x0002, "left", False),
    (0x0004, "right", True),
    (0x0008, "right", False),
    (0x0010, "middle", True),
    (0x0020, "middle", False),
    (0x0040, "x1", True),
    (0x0080, "x1", False),
    (0x0100, "x2", True),
    (0x0200, "x2", False),
)
RI_MOUSE_WHEEL = 0x0400
RI_MOUSE_HWHEEL = 0x0800

BYTE = ctypes.c_uint8
WORD = ctypes.c_uint16
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
ULONG = ctypes.c_uint32
UINT = ctypes.c_uint32
USHORT = ctypes.c_uint16
WCHAR = ctypes.c_uint16
WCHAR32 = ctypes.c_uint16 * 32
PVOID = ctypes.c_void_p
#: ``ULONG_PTR`` / ``WPARAM``: pointer sized, unsigned.
ULONG_PTR = ctypes.c_size_t
#: ``LPARAM``: pointer sized, signed.
LRESULT = ctypes.c_ssize_t


def button_flag_table() -> Tuple[Tuple[int, str, bool], ...]:
    return MOUSE_BUTTON_FLAGS


# --------------------------------------------------------------------------- #
# SendInput structures
# --------------------------------------------------------------------------- #
class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", DWORD),
        ("wParamL", WORD),
        ("wParamH", WORD),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", DWORD), ("u", _INPUTUNION))


def key_input(scan_code: int, *, extended: bool, down: bool) -> INPUT:
    """A ``KEYEVENTF_SCANCODE`` keyboard INPUT for one key transition."""
    flags = KEYEVENTF_SCANCODE
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if not down:
        flags |= KEYEVENTF_KEYUP
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(0, scan_code, flags, 0, 0)))


def mouse_move_input(dx: int, dy: int) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(
            mi=MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_MOVE_NOCOALESCE, 0, 0)
        ),
    )


def mouse_wheel_input(delta: int, *, horizontal: bool = False) -> INPUT:
    """A wheel INPUT; ``delta`` is the wheel rotation (negative = towards the user).

    ``mouseData`` is a DWORD that carries a *signed* value in its low word, so a
    negative delta is stored as its two's complement bit pattern.
    """
    return INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(
            mi=MOUSEINPUT(
                0,
                0,
                int(delta) & 0xFFFFFFFF,
                MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL,
                0,
                0,
            )
        ),
    )


def mouse_button_input(down_flag: int, up_flag: int, data: int, *, down: bool) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(mi=MOUSEINPUT(0, 0, data, down_flag if down else up_flag, 0, 0)),
    )


# --------------------------------------------------------------------------- #
# Raw Input structures
# --------------------------------------------------------------------------- #
def _proc_types():
    """``(field_type, callback_factory)`` for the window procedure.

    ``WINFUNCTYPE`` only exists on Windows; elsewhere the field degrades to a
    plain pointer so the structure can still be inspected (and size checked).
    """
    factory = getattr(ctypes, "WINFUNCTYPE", None)
    if factory is None:
        return PVOID, None
    proc = factory(LRESULT, PVOID, UINT, ULONG_PTR, LRESULT)
    return proc, proc


WNDPROC_TYPE, WNDPROC = _proc_types()


class WNDCLASSW(ctypes.Structure):
    _fields_ = (
        ("style", UINT),
        ("lpfnWndProc", WNDPROC_TYPE),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", PVOID),
        ("hIcon", PVOID),
        ("hCursor", PVOID),
        ("hbrBackground", PVOID),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    )


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = (
        ("usUsagePage", USHORT),
        ("usUsage", USHORT),
        ("dwFlags", DWORD),
        ("hwndTarget", PVOID),
    )


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = (
        ("dwType", DWORD),
        ("dwSize", DWORD),
        ("hDevice", PVOID),
        ("wParam", ULONG_PTR),
    )


class _RAWMOUSE_BUTTONS(ctypes.Structure):
    _fields_ = (("usButtonFlags", USHORT), ("usButtonData", USHORT))


class _RAWMOUSE_BUTTONUNION(ctypes.Union):
    _fields_ = (("ulButtons", ULONG), ("buttons", _RAWMOUSE_BUTTONS))


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("button_union",)
    _fields_ = (
        ("usFlags", USHORT),
        ("button_union", _RAWMOUSE_BUTTONUNION),
        ("ulRawButtons", ULONG),
        ("lLastX", LONG),
        ("lLastY", LONG),
        ("ulExtraInformation", ULONG),
    )


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = (
        ("MakeCode", USHORT),
        ("Flags", USHORT),
        ("Reserved", USHORT),
        ("VKey", USHORT),
        ("Message", UINT),
        ("ExtraInformation", ULONG),
    )


class RAWHID(ctypes.Structure):
    _fields_ = (("dwSize", DWORD), ("dwCount", DWORD), ("bRawData", BYTE * 1))


class _RAWINPUTUNION(ctypes.Union):
    _fields_ = (("mouse", RAWMOUSE), ("keyboard", RAWKEYBOARD), ("hid", RAWHID))


class RAWINPUT(ctypes.Structure):
    _fields_ = (("header", RAWINPUTHEADER), ("data", _RAWINPUTUNION))


# --------------------------------------------------------------------------- #
# display mode
# --------------------------------------------------------------------------- #
class DEVMODEW(ctypes.Structure):
    """``DEVMODEW`` (220 bytes) - enough to read the current display mode."""

    _fields_ = (
        ("dmDeviceName", WCHAR32),
        ("dmSpecVersion", WORD),
        ("dmDriverVersion", WORD),
        ("dmSize", WORD),
        ("dmDriverExtra", WORD),
        ("dmFields", DWORD),
        ("dmOrientation", ctypes.c_short),
        ("dmPaperSize", ctypes.c_short),
        ("dmPaperLength", ctypes.c_short),
        ("dmPaperWidth", ctypes.c_short),
        ("dmScale", ctypes.c_short),
        ("dmCopies", ctypes.c_short),
        ("dmDefaultSource", ctypes.c_short),
        ("dmPrintQuality", ctypes.c_short),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", WCHAR32),
        ("dmLogPixels", WORD),
        ("dmBitsPerPel", DWORD),
        ("dmPelsWidth", DWORD),
        ("dmPelsHeight", DWORD),
        ("dmDisplayFlags", DWORD),
        ("dmDisplayFrequency", DWORD),
        ("dmICMMethod", DWORD),
        ("dmICMIntent", DWORD),
        ("dmMediaType", DWORD),
        ("dmDitherType", DWORD),
        ("dmReserved1", DWORD),
        ("dmReserved2", DWORD),
        ("dmPanningWidth", DWORD),
        ("dmPanningHeight", DWORD),
    )


# --------------------------------------------------------------------------- #
# function prototypes
# --------------------------------------------------------------------------- #
#: ``{dll: {function: (argtypes, restype)}}`` for every Win32 entry point the
#: Windows backends call.
#:
#: Declaring them is not cosmetic.  Without ``argtypes`` ctypes converts an
#: argument with the *default* C rules, so a 64-bit value (``LPARAM``, an
#: ``HRAWINPUT``, an ``HWND``, an ``HBITMAP``) is truncated to 32 bits - the call
#: then fails or, worse, silently returns nothing.  That is precisely the kind of
#: breakage that cannot be seen from a non-Windows machine, hence the explicit
#: table and ``tests/test_win_types.py`` checking that every ``user32.Foo`` in the
#: backend sources appears here.
WIN_PROTOTYPES = {
    # Pointer parameters are declared as ``c_void_p`` rather than ``POINTER(T)``:
    # ctypes only accepts an exact type match for a declared pointer, and the call
    # sites legitimately pass ``byref(wintypes.DWORD())``, arrays and ``None``.
    # ``c_void_p`` accepts all of them and still passes the full 64-bit value,
    # which is what actually matters here.
    "user32": {
        # messages and windows
        "CreateWindowExW": (
            (DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD, ctypes.c_int, ctypes.c_int,
             ctypes.c_int, ctypes.c_int, PVOID, PVOID, PVOID, PVOID),
            PVOID,
        ),
        "DefWindowProcW": ((PVOID, UINT, ULONG_PTR, LRESULT), LRESULT),
        "DestroyWindow": ((PVOID,), ctypes.c_bool),
        "RegisterClassW": ((PVOID,), ctypes.c_uint16),  # ATOM
        "UnregisterClassW": ((ctypes.c_wchar_p, PVOID), ctypes.c_bool),
        "GetMessageW": ((PVOID, PVOID, UINT, UINT), ctypes.c_int),
        "TranslateMessage": ((PVOID,), ctypes.c_bool),
        "DispatchMessageW": ((PVOID,), LRESULT),
        "PostQuitMessage": ((ctypes.c_int,), None),
        "PostMessageW": ((PVOID, UINT, ULONG_PTR, LRESULT), ctypes.c_bool),
        "PostThreadMessageW": ((DWORD, UINT, ULONG_PTR, LRESULT), ctypes.c_bool),
        # input
        "SendInput": ((UINT, PVOID, ctypes.c_int), UINT),
        "RegisterRawInputDevices": ((PVOID, UINT, UINT), ctypes.c_bool),
        "GetRawInputData": ((PVOID, UINT, PVOID, PVOID, UINT), UINT),
        # window enumeration / focus
        "EnumWindows": ((PVOID, LRESULT), ctypes.c_bool),
        "GetWindowTextLengthW": ((PVOID,), ctypes.c_int),
        "GetWindowTextW": ((PVOID, ctypes.c_wchar_p, ctypes.c_int), ctypes.c_int),
        "GetWindowThreadProcessId": ((PVOID, PVOID), DWORD),
        "IsWindowVisible": ((PVOID,), ctypes.c_bool),
        "ShowWindow": ((PVOID, ctypes.c_int), ctypes.c_bool),
        "SetForegroundWindow": ((PVOID,), ctypes.c_bool),
        # device context / display
        "GetDC": ((PVOID,), PVOID),
        "ReleaseDC": ((PVOID, PVOID), ctypes.c_int),
        "EnumDisplaySettingsW": ((ctypes.c_wchar_p, DWORD, PVOID), ctypes.c_bool),
        "GetSystemMetrics": ((ctypes.c_int,), ctypes.c_int),
        "GetDpiForSystem": ((), UINT),
        "SetProcessDPIAware": ((), ctypes.c_bool),
    },
    "gdi32": {
        "BitBlt": (
            (PVOID, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, PVOID,
             ctypes.c_int, ctypes.c_int, DWORD),
            ctypes.c_bool,
        ),
        "CreateCompatibleBitmap": ((PVOID, ctypes.c_int, ctypes.c_int), PVOID),
        "CreateCompatibleDC": ((PVOID,), PVOID),
        "DeleteDC": ((PVOID,), ctypes.c_bool),
        "DeleteObject": ((PVOID,), ctypes.c_bool),
        "SelectObject": ((PVOID, PVOID), PVOID),
        "GetDIBits": ((PVOID, PVOID, UINT, UINT, PVOID, PVOID, UINT), ctypes.c_int),
    },
    "kernel32": {
        "CloseHandle": ((PVOID,), ctypes.c_bool),
        "GetCurrentProcess": ((), PVOID),
        "GetCurrentThreadId": ((), DWORD),
        "GetModuleHandleW": ((ctypes.c_wchar_p,), PVOID),
    },
    "advapi32": {
        "OpenProcessToken": ((PVOID, DWORD, PVOID), ctypes.c_bool),
        "GetTokenInformation": ((PVOID, ctypes.c_int, PVOID, DWORD, PVOID), ctypes.c_bool),
        # Both return *pointers*: leaving restype implicit would hand back a Python
        # int and make ``count.contents`` an AttributeError, so the integrity level
        # would silently stay unknown (see windows/preflight.py).
        "GetSidSubAuthorityCount": ((PVOID,), ctypes.POINTER(ctypes.c_ubyte)),
        "GetSidSubAuthority": ((PVOID, DWORD), ctypes.POINTER(DWORD)),
    },
    "shell32": {
        "IsUserAnAdmin": ((), ctypes.c_bool),
    },
}


def install_prototypes(dll, names=None) -> Tuple[str, ...]:
    """Declare ``argtypes``/``restype`` for ``names`` on a loaded ``WinDLL``.

    Functions the DLL does not export (older Windows builds, or a different DLL)
    are skipped, so this never makes a working call fail.  Returns the names that
    were actually declared.
    """
    declared = []
    for name in (names if names is not None else _prototypes_for(dll)):
        prototype = _prototype_for(dll, name)
        if prototype is None:
            continue
        function = getattr(dll, name, None)
        if function is None:
            continue
        argtypes, restype = prototype
        try:
            function.argtypes = list(argtypes)
            function.restype = restype
        except (AttributeError, TypeError):  # pragma: no cover - not a ctypes function
            continue
        declared.append(name)
    return tuple(declared)


def _dll_key(dll) -> str:
    """The :data:`WIN_PROTOTYPES` key for a loaded ``WinDLL`` (``"user32"``, ...)."""
    name = (getattr(dll, "_name", "") or "").lower()
    for dll_name in WIN_PROTOTYPES:
        if dll_name in name:
            return dll_name
    return ""


def _prototypes_for(dll) -> Tuple[str, ...]:
    """Every function this DLL is expected to export, per :data:`WIN_PROTOTYPES`."""
    return tuple(WIN_PROTOTYPES.get(_dll_key(dll), {}))


def _prototype_for(dll, name):
    """The prototype for ``name``, preferring this DLL's own table."""
    own = WIN_PROTOTYPES.get(_dll_key(dll), {})
    if name in own:
        return own[name]
    for table in WIN_PROTOTYPES.values():
        if name in table:
            return table[name]
    return None
