"""Canonical key names and their mapping to macOS virtual key codes / Windows scan codes.

Design rules
------------
* A trace stores the *canonical* name as the primary key identity, plus the
  originating platform's native code in the ``native`` field.
* Replay prefers the local platform's native code found in the event, and falls
  back to a canonical-name lookup.  That is what makes one trace file playable
  on both platforms.
* Modifier state is canonical too, so ``["shift"]`` means the same thing on both
  platforms.

Canonical names are lower case: letters ``a``-``z``, digits ``0``-``9``, and the
symbolic names listed in :data:`NAMED_KEYS`.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

__all__ = [
    "NAMED_KEYS",
    "MODIFIER_KEYS",
    "MAC_VK",
    "WIN_SCANCODE",
    "WIN_EXTENDED",
    "KeycodeError",
    "normalize",
    "is_canonical",
    "mac_vk",
    "win_scancode",
    "canonical_for_mac_vk",
    "canonical_for_win_scancode",
    "native_code",
]


class KeycodeError(ValueError):
    """Raised for an unknown key name or an unmappable native code."""


#: Symbolic (non letter/digit) canonical names.
NAMED_KEYS = (
    "esc",
    "enter",
    "tab",
    "space",
    "backspace",
    "delete",
    "insert",
    "home",
    "end",
    "pageup",
    "pagedown",
    "up",
    "down",
    "left",
    "right",
    "capslock",
    "numlock",
    "scrolllock",
    "printscreen",
    "pause",
    "menu",
    "minus",
    "equal",
    "lbracket",
    "rbracket",
    "backslash",
    "semicolon",
    "quote",
    "comma",
    "period",
    "slash",
    "grave",
    "kp0",
    "kp1",
    "kp2",
    "kp3",
    "kp4",
    "kp5",
    "kp6",
    "kp7",
    "kp8",
    "kp9",
    "kpdecimal",
    "kpdivide",
    "kpmultiply",
    "kpminus",
    "kpplus",
    "kpenter",
) + tuple("f%d" % i for i in range(1, 25))

#: Canonical modifier names.
MODIFIER_KEYS = ("shift", "ctrl", "alt", "cmd")

#: Accepted spellings -> canonical name.
_ALIASES: Dict[str, str] = {
    "escape": "esc",
    "return": "enter",
    "cr": "enter",
    "del": "delete",
    "forwarddelete": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "pgdown": "pagedown",
    "uparrow": "up",
    "downarrow": "down",
    "leftarrow": "left",
    "rightarrow": "right",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "control": "ctrl",
    "ctl": "ctrl",
    "option": "alt",
    "opt": "alt",
    "altgr": "alt",
    "meta": "cmd",
    "super": "cmd",
    "win": "cmd",
    "windows": "cmd",
    "command": "cmd",
    "lshift": "shift",
    "rshift": "shift",
    "lctrl": "ctrl",
    "rctrl": "ctrl",
    "lalt": "alt",
    "ralt": "alt",
    "lcmd": "cmd",
    "rcmd": "cmd",
    "caps": "capslock",
    "spacebar": "space",
    "-": "minus",
    "=": "equal",
    "[": "lbracket",
    "]": "rbracket",
    "\\": "backslash",
    ";": "semicolon",
    "'": "quote",
    ",": "comma",
    ".": "period",
    "/": "slash",
    "`": "grave",
    "num0": "kp0",
    "num1": "kp1",
    "num2": "kp2",
    "num3": "kp3",
    "num4": "kp4",
    "num5": "kp5",
    "num6": "kp6",
    "num7": "kp7",
    "num8": "kp8",
    "num9": "kp9",
    "numdecimal": "kpdecimal",
    "numdivide": "kpdivide",
    "nummultiply": "kpmultiply",
    "numminus": "kpminus",
    "numplus": "kpplus",
    "numenter": "kpenter",
    "keypad0": "kp0",
    "keypad1": "kp1",
    "keypad2": "kp2",
    "keypad3": "kp3",
    "keypad4": "kp4",
    "keypad5": "kp5",
    "keypad6": "kp6",
    "keypad7": "kp7",
    "keypad8": "kp8",
    "keypad9": "kp9",
    "keypaddecimal": "kpdecimal",
    "keypaddivide": "kpdivide",
    "keypadmultiply": "kpmultiply",
    "keypadminus": "kpminus",
    "keypadplus": "kpplus",
    "keypadenter": "kpenter",
}

_CANONICAL = frozenset(
    list(NAMED_KEYS)
    + list(MODIFIER_KEYS)
    + list("abcdefghijklmnopqrstuvwxyz")
    + list("0123456789")
)


def normalize(name: str) -> str:
    """Return the canonical spelling of ``name``.

    Raises :class:`KeycodeError` for names that are not part of the vocabulary -
    silently ignoring an unknown key would corrupt a trace.
    """
    if not isinstance(name, str):
        raise KeycodeError("key name must be a string, got %r" % (name,))
    key = name.strip().lower()
    if not key:
        raise KeycodeError("empty key name")
    if key in _CANONICAL:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    # "shift_l" / "shift-r" style spellings.
    side_stripped = re.sub(r"[_\- ]*(l|r|left|right)$", "", key)
    if side_stripped != key and side_stripped in _CANONICAL:
        return side_stripped
    raise KeycodeError("unknown key name: %r" % (name,))


def is_canonical(name: str) -> bool:
    return isinstance(name, str) and name in _CANONICAL


# --------------------------------------------------------------------------- #
# macOS virtual key codes (ANSI layout, HIToolbox Events.h)
# --------------------------------------------------------------------------- #
_MAC_LETTER_VK = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45,
    "m": 46,
}

_MAC_DIGIT_VK = {
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28,
    "9": 25, "0": 29,
}

_MAC_NAMED_VK = {
    "equal": 24, "minus": 27, "rbracket": 30, "lbracket": 33, "enter": 36,
    "quote": 39, "semicolon": 41, "backslash": 42, "comma": 43, "slash": 44,
    "period": 47, "tab": 48, "space": 49, "grave": 50, "backspace": 51,
    "esc": 53, "cmd": 55, "shift": 56, "capslock": 57, "alt": 58, "ctrl": 59,
    "kpdecimal": 65, "kpmultiply": 67, "kpplus": 69, "kpdivide": 75,
    "kpenter": 76, "kpminus": 78,
    "home": 115, "pageup": 116, "delete": 117, "end": 119, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111, "f13": 105,
    "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80,
    "f20": 90,
    "kp0": 82, "kp1": 83, "kp2": 84, "kp3": 85, "kp4": 86, "kp5": 87,
    "kp6": 88, "kp7": 89, "kp8": 91, "kp9": 92,
    "printscreen": 105,  # f13 doubles as print screen on compact keyboards
}

#: Canonical name -> macOS virtual key code.
#:
#: Absent on purpose (no such key on Apple keyboards, so a macOS trace can never
#: contain them; a Windows trace that does keeps working through the
#: "unsupported key" path in the input backends): numlock, scrolllock, pause,
#: menu, insert (Apple's Help key is not an Insert key) and f21-f24.
MAC_VK: Dict[str, int] = {}
MAC_VK.update(_MAC_LETTER_VK)
MAC_VK.update(_MAC_DIGIT_VK)
MAC_VK.update(_MAC_NAMED_VK)

# --------------------------------------------------------------------------- #
# Windows scan codes (set 1 make codes)
# --------------------------------------------------------------------------- #
_WIN_NAMED_SC = {
    "esc": 0x01, "backspace": 0x0E, "tab": 0x0F, "enter": 0x1C,
    "minus": 0x0C, "equal": 0x0D, "lbracket": 0x1A, "rbracket": 0x1B,
    "backslash": 0x2B, "semicolon": 0x27, "quote": 0x28, "grave": 0x29,
    "comma": 0x33, "period": 0x34, "slash": 0x35,
    "shift": 0x2A, "ctrl": 0x1D, "alt": 0x38, "space": 0x39,
    "capslock": 0x3A, "numlock": 0x45, "scrolllock": 0x46,
    "kpmultiply": 0x37, "kpminus": 0x4A, "kpplus": 0x4E, "kpdecimal": 0x53,
    "kp0": 0x52, "kp1": 0x4F, "kp2": 0x50, "kp3": 0x51, "kp4": 0x4B,
    "kp5": 0x4C, "kp6": 0x4D, "kp7": 0x47, "kp8": 0x48, "kp9": 0x49,
    # Extended (prefixed with 0xE0 on the wire) - see WIN_EXTENDED.
    "kpenter": 0x1C, "kpdivide": 0x35, "printscreen": 0x37, "pause": 0x45,
    "home": 0x47, "up": 0x48, "pageup": 0x49, "left": 0x4B, "right": 0x4D,
    "end": 0x4F, "down": 0x50, "pagedown": 0x51, "insert": 0x52,
    "delete": 0x53, "cmd": 0x5B, "menu": 0x5D, "numlock": 0x45,
}

_WIN_LETTER_SC = {
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15,
    "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19, "a": 0x1E, "s": 0x1F,
    "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23, "j": 0x24, "k": 0x25,
    "l": 0x26, "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30,
    "n": 0x31, "m": 0x32,
}

_WIN_DIGIT_SC = {
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06, "6": 0x07,
    "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
}

_WIN_FUNCTION_SC = dict(("f%d" % i, 0x3A + i) for i in range(1, 11))  # f1=0x3B
_WIN_FUNCTION_SC.update({"f11": 0x57, "f12": 0x58})
_WIN_FUNCTION_SC.update(dict(("f%d" % i, 0x63 + i) for i in range(13, 25)))

#: Canonical name -> Windows scan code (set 1).
WIN_SCANCODE: Dict[str, int] = {}
WIN_SCANCODE.update(_WIN_NAMED_SC)
WIN_SCANCODE.update(_WIN_LETTER_SC)
WIN_SCANCODE.update(_WIN_DIGIT_SC)
WIN_SCANCODE.update(_WIN_FUNCTION_SC)

#: Canonical names whose scan code must be sent with KEYEVENTF_EXTENDEDKEY (0xE0).
WIN_EXTENDED = frozenset(
    {
        "kpenter", "kpdivide", "printscreen", "home", "up", "pageup", "left",
        "right", "end", "down", "pagedown", "insert", "delete", "cmd", "menu",
    }
)

_MAC_VK_REVERSE: Dict[int, str] = {}
for _name in sorted(MAC_VK):  # sorted -> deterministic when several names share a code
    _MAC_VK_REVERSE.setdefault(MAC_VK[_name], _name)

#: Key codes that have no forward mapping but must still be recognised when
#: recording: the right hand side of the modifier keys (and fn).
_MAC_REVERSE_EXTRA = {54: "cmd", 60: "shift", 61: "alt", 62: "ctrl"}
for _vk, _name in _MAC_REVERSE_EXTRA.items():
    _MAC_VK_REVERSE.setdefault(_vk, _name)

# Num-lock-off navigation aliases: both produce the same scan code.
_WIN_SC_REVERSE: Dict[int, str] = {}
for _name in sorted(WIN_SCANCODE):
    _WIN_SC_REVERSE.setdefault(WIN_SCANCODE[_name], _name)


def mac_vk(name: str) -> int:
    """Virtual key code for ``name`` (raises :class:`KeycodeError` if unmapped)."""
    key = normalize(name)
    try:
        return MAC_VK[key]
    except KeyError:
        raise KeycodeError("no macOS key code for %r" % (name,)) from None


def win_scancode(name: str) -> Tuple[int, bool]:
    """``(scan_code, extended)`` for ``name``."""
    key = normalize(name)
    try:
        return WIN_SCANCODE[key], key in WIN_EXTENDED
    except KeyError:
        raise KeycodeError("no Windows scan code for %r" % (name,)) from None


def canonical_for_mac_vk(vk: int) -> Optional[str]:
    return _MAC_VK_REVERSE.get(int(vk))


def canonical_for_win_scancode(scan: int, extended: bool = False) -> Optional[str]:
    """Best effort reverse lookup; ``extended`` disambiguates the 0xE0 block."""
    if extended:
        for name in sorted(WIN_EXTENDED):
            if WIN_SCANCODE.get(name) == int(scan):
                return name
    return _WIN_SC_REVERSE.get(int(scan))


def native_code(name: str, platform: str) -> Dict[str, object]:
    """Build the ``native`` payload for a trace event recorded on ``platform``."""
    if platform == "macos":
        return {"vk": mac_vk(name)}
    if platform == "windows":
        code, extended = win_scancode(name)
        return {"scancode": code, "ext": extended}
    raise KeycodeError("unknown platform %r" % (platform,))


def native_names() -> List[str]:
    """Canonical names that have a mapping on at least one platform."""
    return sorted(set(MAC_VK) | set(WIN_SCANCODE))
