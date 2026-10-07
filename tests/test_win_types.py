"""Win32 structure layouts and the BMP wrapper - verified without Windows.

A wrong ``sizeof`` or field offset is the classic reason ``SendInput`` fails with
``ERROR_INVALID_PARAMETER``, the Raw Input parser reads garbage, or
``EnumDisplaySettingsW`` fills the wrong fields.  Because the structures use
explicit fixed-width types (not ``ctypes.wintypes``, whose ``DWORD`` is 8 bytes
outside Windows) their layout can be checked here, against the documented values.
"""

from __future__ import annotations

import ctypes
import struct

import pytest

from fpsbench.backends import _capture_bmp, _win_types as wt

IS_64_BIT = ctypes.sizeof(ctypes.c_void_p) == 8
X64 = pytest.mark.skipif(not IS_64_BIT, reason="documented sizes are the x64 ones")


# --------------------------------------------------------------------------- #
# SendInput
# --------------------------------------------------------------------------- #
@X64
def test_sendinput_structure_sizes_match_the_sdk():
    assert ctypes.sizeof(wt.MOUSEINPUT) == 32
    assert ctypes.sizeof(wt.KEYBDINPUT) == 24
    assert ctypes.sizeof(wt.HARDWAREINPUT) == 8
    assert ctypes.sizeof(wt.INPUT) == 40


@X64
def test_input_union_starts_after_the_type_field():
    offsets = {name: getattr(wt.INPUT, name).offset for name, _ in wt.INPUT._fields_}
    assert offsets == {"type": 0, "u": 8}
    # Any of the three members must fit inside the union.
    assert ctypes.sizeof(wt.INPUT) == 8 + ctypes.sizeof(wt.MOUSEINPUT)


def test_key_input_uses_scancodes():
    down = wt.key_input(0x11, extended=False, down=True)
    assert down.type == wt.INPUT_KEYBOARD
    assert down.u.ki.wScan == 0x11
    assert down.u.ki.dwFlags == wt.KEYEVENTF_SCANCODE
    assert down.u.ki.wVk == 0  # virtual key codes are never used

    up = wt.key_input(0x48, extended=True, down=False)
    assert up.u.ki.dwFlags == wt.KEYEVENTF_SCANCODE | wt.KEYEVENTF_EXTENDEDKEY | wt.KEYEVENTF_KEYUP


def test_mouse_inputs_carry_the_right_flags():
    move = wt.mouse_move_input(12, -7)
    assert move.type == wt.INPUT_MOUSE
    assert (move.u.mi.dx, move.u.mi.dy) == (12, -7)
    assert move.u.mi.dwFlags & wt.MOUSEEVENTF_MOVE
    assert move.u.mi.dwFlags & wt.MOUSEEVENTF_MOVE_NOCOALESCE

    wheel = wt.mouse_wheel_input(-120)
    # mouseData is a DWORD carrying a signed value in its low word.
    assert ctypes.c_int32(wheel.u.mi.mouseData).value == -120
    assert wheel.u.mi.mouseData == 0xFFFFFF88
    assert wheel.u.mi.dwFlags == wt.MOUSEEVENTF_WHEEL
    assert wt.mouse_wheel_input(120, horizontal=True).u.mi.dwFlags == wt.MOUSEEVENTF_HWHEEL

    click = wt.mouse_button_input(
        wt.MOUSEEVENTF_LEFTDOWN, wt.MOUSEEVENTF_LEFTUP, 0, down=True
    )
    assert click.u.mi.dwFlags == wt.MOUSEEVENTF_LEFTDOWN


# --------------------------------------------------------------------------- #
# Raw Input
# --------------------------------------------------------------------------- #
@X64
def test_raw_input_structure_sizes_and_offsets():
    assert ctypes.sizeof(wt.RAWINPUTDEVICE) == 16
    assert ctypes.sizeof(wt.RAWINPUTHEADER) == 24
    assert ctypes.sizeof(wt.RAWMOUSE) == 24
    assert ctypes.sizeof(wt.RAWKEYBOARD) == 16
    assert ctypes.sizeof(wt.RAWINPUT) == 48

    offsets = {name: getattr(wt.RAWMOUSE, name).offset for name, _ in wt.RAWMOUSE._fields_}
    assert offsets == {
        "usFlags": 0,
        "button_union": 4,
        "ulRawButtons": 8,
        "lLastX": 12,
        "lLastY": 16,
        "ulExtraInformation": 20,
    }


def test_raw_mouse_button_union_overlaps():
    mouse = wt.RAWMOUSE()
    mouse.button_union.buttons.usButtonFlags = 0x0004
    mouse.button_union.buttons.usButtonData = -120
    # ulButtons shares storage with the two USHORTs.
    assert mouse.button_union.ulButtons == (0x0004 | (0xFF88 << 16))


def test_button_flag_table_covers_every_button_on_both_edges():
    flags = {bit: (button, down) for bit, button, down in wt.MOUSE_BUTTON_FLAGS}
    assert flags[0x0001] == ("left", True)
    assert flags[0x0002] == ("left", False)
    assert flags[0x0004] == ("right", True)
    assert flags[0x0010] == ("middle", True)
    assert flags[0x0040] == ("x1", True)
    assert flags[0x0100] == ("x2", True)
    # Every bit is distinct and every button has both edges.
    assert len(flags) == len(wt.MOUSE_BUTTON_FLAGS)
    buttons = {}
    for _bit, button, down in wt.MOUSE_BUTTON_FLAGS:
        buttons.setdefault(button, set()).add(down)
    assert buttons == {
        "left": {True, False},
        "right": {True, False},
        "middle": {True, False},
        "x1": {True, False},
        "x2": {True, False},
    }


def test_raw_input_union_holds_the_largest_member():
    assert ctypes.sizeof(wt.RAWINPUT) == ctypes.sizeof(wt.RAWINPUTHEADER) + ctypes.sizeof(wt.RAWMOUSE)


# --------------------------------------------------------------------------- #
# display mode
# --------------------------------------------------------------------------- #
def test_devmode_layout_matches_wingdi():
    assert ctypes.sizeof(wt.DEVMODEW) == 220
    offsets = {name: getattr(wt.DEVMODEW, name).offset for name, _ in wt.DEVMODEW._fields_}
    assert offsets["dmSize"] == 68
    assert offsets["dmFields"] == 72
    assert offsets["dmFormName"] == 102
    assert offsets["dmBitsPerPel"] == 168
    assert offsets["dmPelsWidth"] == 172
    assert offsets["dmPelsHeight"] == 176
    assert offsets["dmDisplayFrequency"] == 184


def test_devmode_fields_are_readable_after_writing_raw_bytes():
    mode = wt.DEVMODEW()
    mode.dmSize = ctypes.sizeof(wt.DEVMODEW)
    mode.dmPelsWidth = 2560
    mode.dmPelsHeight = 1440
    mode.dmDisplayFrequency = 144
    raw = bytes(mode)
    assert struct.unpack_from("<I", raw, 172)[0] == 2560
    assert struct.unpack_from("<I", raw, 176)[0] == 1440
    assert struct.unpack_from("<I", raw, 184)[0] == 144


# --------------------------------------------------------------------------- #
# BMP wrapper
# --------------------------------------------------------------------------- #
def test_bmp_bytes_header_is_valid():
    pixels = bytes(4 * 3 * 2)  # 3x2 BGRA
    data = _capture_bmp.bmp_bytes(3, 2, pixels)
    assert data[:2] == b"BM"
    file_size, reserved1, reserved2, offset = struct.unpack_from("<IHHI", data, 2)
    assert file_size == len(data)
    assert (reserved1, reserved2) == (0, 0)
    assert offset == 14 + 40

    header_size, width, height, planes, bpp, compression, image_size = struct.unpack_from(
        "<IiiHHII", data, 14
    )
    assert header_size == 40
    assert (width, height) == (3, -2)  # negative height = top-down
    assert (planes, bpp, compression) == (1, 32, 0)
    assert image_size == len(pixels)
    assert data[54:] == pixels


def test_bmp_bytes_rejects_a_short_buffer():
    with pytest.raises(ValueError) as excinfo:
        _capture_bmp.bmp_bytes(10, 10, b"\x00" * 10)
    assert "need 400 bytes" in str(excinfo.value)


def test_bmp_stride_is_four_bytes_per_pixel():
    assert _capture_bmp.bmp_info(7, 3) == (28, 84)


# --------------------------------------------------------------------------- #
# function prototypes
# --------------------------------------------------------------------------- #
DLL_NAMES = ("user32", "gdi32", "kernel32", "advapi32", "shell32")


def _win32_calls_in_the_backends():
    """``{(dll, function)}`` for every ``user32.Foo``-style call in the sources.

    The Windows backends cannot be imported off Windows, so the call sites are
    found by parsing the source text instead.
    """
    import ast
    import pathlib

    found = set()
    windows_dir = pathlib.Path(wt.__file__).parent / "windows"
    for path in sorted(windows_dir.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.attr, str):
                continue
            value = node.value
            # ``user32.Foo`` and ``ctypes.windll.user32.Foo``
            if isinstance(value, ast.Name) and value.id in DLL_NAMES:
                found.add((value.id, node.attr))
            elif (
                isinstance(value, ast.Attribute)
                and value.attr in DLL_NAMES
                and node.attr
                and node.attr[0].isupper()
            ):
                found.add((value.attr, node.attr))
    return found


def test_every_win32_call_site_has_a_prototype():
    """A missing entry means ctypes would use its 32-bit default conversion.

    That truncates ``HWND``/``LPARAM``/``HRAWINPUT`` values silently, which is how
    Raw Input recording can return nothing at all with no error on the machine.
    """
    calls = _win32_calls_in_the_backends()
    assert calls, "the source scan found no Win32 calls - the scan is broken"
    missing = sorted(
        "%s.%s" % (dll, function)
        for dll, function in calls
        if function not in wt.WIN_PROTOTYPES.get(dll, {})
    )
    assert missing == []
    for dll, table in wt.WIN_PROTOTYPES.items():
        assert dll in DLL_NAMES
        for function, (argtypes, restype) in table.items():
            assert isinstance(argtypes, tuple), function
            for argtype in argtypes:
                assert hasattr(argtype, "from_param"), (function, argtype)
                # A declared POINTER(T) only accepts an exact type match, so the
                # byref(wintypes.DWORD()) / array / None call sites would raise.
                assert not isinstance(argtype, type(ctypes.POINTER(ctypes.c_int))), (
                    function,
                    argtype,
                )
            assert restype is None or hasattr(restype, "_type_"), (function, restype)


def test_pointer_sized_arguments_are_not_left_to_the_default_conversion():
    """Guard the specific truncations that break the Windows backends."""
    void = ctypes.c_void_p
    assert wt.WIN_PROTOTYPES["user32"]["GetRawInputData"][0][0] is void  # HRAWINPUT
    assert wt.WIN_PROTOTYPES["user32"]["DefWindowProcW"][0][2] is wt.ULONG_PTR  # WPARAM
    assert wt.WIN_PROTOTYPES["user32"]["DefWindowProcW"][0][3] is wt.LRESULT  # LPARAM
    assert wt.WIN_PROTOTYPES["user32"]["SendInput"][0][1] is void  # LPINPUT
    assert wt.WIN_PROTOTYPES["user32"]["EnumDisplaySettingsW"][0][2] is void
    assert wt.WIN_PROTOTYPES["advapi32"]["GetSidSubAuthorityCount"][1] is ctypes.POINTER(
        ctypes.c_ubyte
    )
    assert wt.WIN_PROTOTYPES["advapi32"]["GetSidSubAuthority"][1] is ctypes.POINTER(wt.DWORD)


class _DummyDll:
    """A stand-in for a ``WinDLL``: caches one callable per requested name."""

    def __init__(self, name: str, present) -> None:
        self._name = name
        self._present = set(present)

    def __getattr__(self, name):
        if name.startswith("_") or name in self._present:
            function = lambda *args: 0  # noqa: E731 - attribute target
            object.__setattr__(self, name, function)
            return function
        raise AttributeError(name)


def test_install_prototypes_declares_argtypes_and_restype():
    dll = _DummyDll("user32", present=("GetRawInputData", "SendInput"))
    declared = wt.install_prototypes(dll)
    assert "GetRawInputData" in declared and "SendInput" in declared
    assert dll.GetRawInputData.argtypes[0] is ctypes.c_void_p
    assert dll.GetRawInputData.restype is wt.UINT
    assert dll.SendInput.argtypes[1] is ctypes.c_void_p


def test_install_prototypes_skips_functions_the_dll_does_not_export():
    """Older Windows builds must not turn a working call into a crash."""
    dll = _DummyDll("user32", present=("GetSystemMetrics",))
    declared = wt.install_prototypes(dll)
    assert declared == ("GetSystemMetrics",)


def test_install_prototypes_accepts_an_explicit_name_list():
    dll = _DummyDll("gdi32", present=("SelectObject", "DeleteObject"))
    declared = wt.install_prototypes(dll, ["SelectObject", "NotAFunction"])
    assert declared == ("SelectObject",)
    assert dll.SelectObject.restype is ctypes.c_void_p
    assert dll.SelectObject.argtypes == [ctypes.c_void_p, ctypes.c_void_p]


def test_declared_prototypes_match_the_actual_call_arities():
    """A prototype with the wrong number of parameters is as bad as none.

    The call sites are read from the sources (the modules cannot be imported off
    Windows), so this catches a typo in the table that would otherwise surface as
    an ``ArgumentError`` on a real machine.
    """
    import ast
    import pathlib

    windows_dir = pathlib.Path(wt.__file__).parent / "windows"
    checked = 0
    for path in sorted(windows_dir.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            value = node.func.value
            dll = None
            if isinstance(value, ast.Name) and value.id in DLL_NAMES:
                dll = value.id
            elif isinstance(value, ast.Attribute) and value.attr in DLL_NAMES:
                dll = value.attr
            if dll is None:
                continue
            prototype = wt.WIN_PROTOTYPES.get(dll, {}).get(node.func.attr)
            assert prototype is not None, "%s.%s has no prototype" % (dll, node.func.attr)
            argtypes = prototype[0]
            assert node.keywords == [], (path.name, node.func.attr, "keyword argument at a call site")
            assert not any(
                isinstance(argument, ast.Starred) for argument in node.args
            ), (path.name, node.func.attr)
            assert len(node.args) == len(argtypes), (
                "%s.%s is called with %d argument(s) but declares %d"
                % (dll, node.func.attr, len(node.args), len(argtypes))
            )
            checked += 1
    assert checked >= 40, "only %d call sites were checked" % checked
