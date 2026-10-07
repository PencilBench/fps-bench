"""macOS virtual gamepad injection: CoreHID ``HIDVirtualDevice`` (helper process).

Apple's CoreHID can create a **system-wide** virtual HID device (macOS 15+) and
that is the only driver-free way to show a game a controller that does not
exist.  Three things have to hold before this backend can honestly claim it
works, and none of them can be checked from this repository:

1. macOS 15+ - the project still supports macOS 12.3+, and the minimum version is
   not raised to hide a gamepad limitation;
2. ``com.apple.developer.hid.virtual.device`` on a **signed** helper process - a
   plain ``python3`` cannot obtain that entitlement;
3. a game that actually consumes the device: SDL's in-process virtual joystick
   is not visible to another process, and a device that enumerates is not a
   device a game reads.

So the backend refuses with the exact missing piece instead of degrading to
keyboard/mouse.  A user who has validated a signed helper can point
``FPSBENCH_GAMEPAD_HELPER`` at it: the Python side is then a minimal, documented
JSON-lines client (one full state per line, no HID report parsing and no device
descriptors), which keeps the unverified native part as small as possible.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from ...core.gamepad import NEUTRAL_STATE, GamepadState
from ..base import BackendUnavailable, GamepadBackend

__all__ = ["MacGamepadBackend", "BACKEND", "capability", "HELPER_ENV"]

HELPER_ENV = "FPSBENCH_GAMEPAD_HELPER"

#: First macOS release with CoreHID ``HIDVirtualDevice``.
MIN_MACOS_MAJOR = 15


def _macos_major() -> int:
    version = platform.mac_ver()[0] or "0"
    try:
        return int(version.split(".")[0])
    except ValueError:
        return 0


def _corehid_available() -> bool:
    """True when PyObjC's CoreHID framework is importable (macOS 15+ builds)."""
    try:
        return importlib.util.find_spec("CoreHID") is not None
    except Exception:  # pragma: no cover - a broken framework install
        return False


def capability() -> Dict[str, Any]:
    """What ``doctor`` and ``probe-gamepad`` print on this machine."""
    major = _macos_major()
    helper = os.environ.get(HELPER_ENV) or ""
    resolved = shutil.which(helper) or (helper if helper and os.path.isfile(helper) else "")
    corehid = _corehid_available()
    info: Dict[str, Any] = {
        "platform": "macos",
        "os_version": platform.mac_ver()[0] or "unknown",
        "macos_major": major,
        "corehid": corehid,
        "helper": resolved or None,
    }
    missing = []
    if major and major < MIN_MACOS_MAJOR:
        missing.append("macOS %d+ is required (this is %s)" % (MIN_MACOS_MAJOR, info["os_version"]))
    if not resolved:
        # The helper owns the HID device, so PyObjC CoreHID is only needed as a
        # hint about what a helper would be built against.
        if not corehid:
            missing.append(
                "PyObjC CoreHID is not installed (needs macOS 15+ and a matching "
                "pyobjc-framework-CoreHID)"
            )
        missing.append(
            "no signed helper is configured (%s): HIDVirtualDevice needs the "
            "com.apple.developer.hid.virtual.device entitlement on a signed binary "
            "(see helper/README.md)" % HELPER_ENV
        )
    info["ok"] = not missing
    info["reason"] = "; ".join(missing) or "helper configured (unverified)"
    info["hint"] = (
        "validate a signed CoreHID helper by replaying a short route and checking that a second "
        "process and the game both see the device; until then use the keyboard/mouse route"
    )
    return info


class MacGamepadBackend(GamepadBackend):
    """JSON-lines client for a signed CoreHID helper (opt-in through an env var)."""

    name = "CoreHID HIDVirtualDevice (helper)"

    def __init__(self, *, helper: Optional[str] = None) -> None:
        self._helper = helper or os.environ.get(HELPER_ENV) or ""
        self._proc: Optional[subprocess.Popen] = None
        self._notes: List[str] = []
        self._device = ""

    def prepare(self, *, window_title: Optional[str] = None) -> None:
        info = capability()
        if not info.get("ok"):
            raise BackendUnavailable(
                "macOS gamepad injection is blocked: %s" % info["reason"], hint=info["hint"]
            )
        path = self._helper or info.get("helper")
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - explicit user-configured helper
                [path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise BackendUnavailable("cannot start the gamepad helper %s: %s" % (path, exc)) from exc
        reply = self._call({"cmd": "hello"})
        if not reply.get("ok"):
            self.teardown()
            raise BackendUnavailable(
                "the gamepad helper refused to start: %s" % reply.get("error", "no reason given")
            )
        self._device = str(reply.get("device") or "CoreHID virtual gamepad")
        self._notes.append(
            "CoreHID helper %s reports %r: this path is unverified - check that the game and a "
            "second process both see the device" % (path, self._device)
        )
        self.set_state(NEUTRAL_STATE)

    def _call(self, message: Dict[str, Any]) -> Dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("the gamepad helper is not running")
        try:
            self._proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
        except OSError as exc:
            raise RuntimeError("gamepad helper I/O failed: %s" % exc) from exc
        if not line:
            raise RuntimeError("the gamepad helper exited unexpectedly")
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise RuntimeError("gamepad helper sent invalid JSON: %r" % line) from exc
        if not isinstance(reply, dict):
            raise RuntimeError("gamepad helper reply is not an object")
        return reply

    def set_state(self, state: GamepadState) -> None:
        payload: Dict[str, Any] = {"cmd": "state"}
        payload.update(state.to_dict())
        reply = self._call(payload)
        if not reply.get("ok"):
            raise RuntimeError("gamepad helper rejected a state: %s" % reply.get("error", "?"))

    def neutral(self) -> None:
        if self._proc is None:
            return
        try:
            self._call({"cmd": "neutral"})
        except Exception:  # teardown must never raise
            pass

    def teardown(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # pragma: no cover - process already gone
            pass

    def notes(self) -> List[str]:
        return list(self._notes)

    def capabilities(self) -> Dict[str, Any]:
        return capability()


BACKEND = MacGamepadBackend
