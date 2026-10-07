"""Platform module smoke tests.

The real platform backends cannot be *exercised* in a sandbox (no screen recording
permission, no GUI session), but they can be imported, instantiated and asked for
their metadata.  That catches typos in the pyobjc/ctypes plumbing, which is the
most likely way these modules break.

The Windows backends are checked from the other direction: on a non-Windows host
they must refuse to load with an actionable message instead of a bare
``ImportError``.
"""

from __future__ import annotations

import pytest

from fpsbench.backends import base as backends_base
from fpsbench.backends.base import BackendUnavailable

PLATFORM = backends_base.current_platform()


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_backends_load_and_construct():
    for role in ("input", "record", "fps", "ocr"):
        backend = backends_base.load_backend(role)
        assert backend is not None
        assert backend.name


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_preflight_module_imports():
    module = backends_base.load_module("preflight")
    assert hasattr(module, "preflight")
    assert hasattr(module, "platform_info")
    assert hasattr(module, "pick_region")
    info = module.platform_info()
    assert isinstance(info, dict)


@pytest.mark.skipif(PLATFORM != "windows", reason="Windows only")
def test_windows_backends_load_and_construct():
    for role in ("input", "record", "fps", "ocr"):
        assert backends_base.load_backend(role) is not None


@pytest.mark.skipif(PLATFORM == "windows", reason="only meaningful off Windows")
def test_windows_input_backend_refuses_to_load_off_windows():
    with pytest.raises(BackendUnavailable) as excinfo:
        backends_base.load_module("input", platform="windows")
    assert "Windows" in str(excinfo.value)


@pytest.mark.skipif(PLATFORM == "windows", reason="only meaningful off Windows")
def test_windows_recorder_refuses_to_load_off_windows():
    with pytest.raises(BackendUnavailable):
        backends_base.load_module("record", platform="windows")


@pytest.mark.skipif(PLATFORM == "windows", reason="only meaningful off Windows")
def test_windows_ocr_and_preflight_refuse_to_load_off_windows():
    for name in ("ocr", "preflight"):
        with pytest.raises(BackendUnavailable) as excinfo:
            backends_base.load_module(name, platform="windows")
        assert "Windows" in str(excinfo.value)


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_fps_backend_rejects_presentmon_before_playing(monkeypatch):
    from fpsbench.core.route import FpsConfig

    backend = backends_base.load_backend("fps")
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(FpsConfig(mode="presentmon", process_name="game.exe"))
    assert "presentmon" in str(excinfo.value).lower()
    hint = excinfo.value.hint or ""
    assert "PresentMon" in hint
    assert "ocr" in hint


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_auto_needs_no_region_but_metalperftrace_needs_a_process():
    """The default source does not need a region; it needs the process name."""
    from fpsbench.core.route import FpsConfig

    backend = backends_base.load_backend("fps")
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(FpsConfig(mode="auto", region=None))
    message = str(excinfo.value)
    assert "process name" in message
    # metalperftrace is unavailable *and* OCR has no region: both are named.
    assert "metalperftrace" in message
    assert "pick-region" in (excinfo.value.hint or "")


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_ocr_backend_needs_a_region():
    from fpsbench.core.route import FpsConfig

    backend = backends_base.load_backend("fps")
    with pytest.raises(BackendUnavailable) as excinfo:
        backend.start(FpsConfig(mode="ocr"))
    assert "pick-region" in (excinfo.value.hint or "")


def test_unsupported_platforms_are_reported_clearly():
    with pytest.raises(BackendUnavailable) as excinfo:
        backends_base.load_backend("input", platform="unsupported")
    assert "macOS and Windows 11" in str(excinfo.value)
    with pytest.raises(BackendUnavailable) as excinfo:
        backends_base.load_module("preflight", platform="unsupported")
    assert "macOS and Windows 11" in str(excinfo.value)


def test_unknown_role_is_a_programming_error():
    with pytest.raises(ValueError):
        backends_base.load_backend("not-a-role")


def test_missing_dependency_is_reported_with_the_install_command(monkeypatch):
    """A backend whose platform module raises ImportError must not leak it."""
    import importlib

    def exploding(name):
        if name.endswith(".ocr"):
            raise ImportError("pyobjc (Vision) is missing")
        return importlib.import_module(name)

    monkeypatch.setattr(backends_base.importlib, "import_module", exploding)
    with pytest.raises(BackendUnavailable) as excinfo:
        backends_base.load_backend("ocr", platform="macos")
    assert "pyobjc (Vision) is missing" in str(excinfo.value)
    assert "pip install" in (excinfo.value.hint or "")


def test_install_hint_points_at_the_platform_extra():
    assert "macos" in backends_base.install_hint("macos")
    assert "windows" in backends_base.install_hint("windows")
    assert backends_base.install_hint("linux") == ""


def test_check_rendering_shows_the_fix_line():
    ok = backends_base.Check("thing", True, "fine").render()
    assert "[OK" in ok and "fix:" not in ok
    bad = backends_base.Check("thing", False, "broken", fix="do X", fatal=True).render()
    assert "[FAIL]" in bad
    assert "fix: do X" in bad
    warn = backends_base.Check("thing", False, "hmm").render()
    assert "[WARN]" in warn


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_doctor_reports_permissions_instead_of_crashing():
    """In a sandbox this must produce FAIL/WARN lines, not a traceback."""
    from fpsbench import cli

    checks = backends_base.load_module("preflight").preflight()
    assert checks
    text = "\n".join(check.render() for check in checks)
    assert "Accessibility permission" in text
    assert "Screen Recording permission" in text
    assert cli.main(["doctor", "--list-windows"]) in (0, 1)


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_can_enumerate_window_titles():
    """``doctor --list-windows`` is what the "Target window" fix line points at, so
    the macOS preflight module must expose the enumerator (it used to live only in
    ``input.py``, which made the hint print an empty list)."""
    module = backends_base.load_module("preflight")
    assert hasattr(module, "list_window_titles")
    titles = module.list_window_titles()
    assert isinstance(titles, list)
    assert all(isinstance(title, str) for title in titles)


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_missing_window_explains_the_current_space_limitation():
    module = backends_base.load_module("preflight")
    check = module._check_window("fpsbench-no-such-window-title")
    assert not check.ok and check.fatal
    assert "no on-screen window title contains" in check.detail
    rendered = check.render()
    assert "doctor --list-windows" in rendered
    assert "Space" in rendered


# --------------------------------------------------------------------------- #
# recorder diagnostics
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_recorder_diagnostics_survive_draining():
    """The CLI drains the recorder every 50 ms, so end-of-run warnings must not
    read the (empty) pending event list - that reported every good recording as
    "no events were recorded at all"."""
    from fpsbench.core.trace import TraceEvent

    recorder = backends_base.load_backend("record")
    recorder._toggle()  # the recording only starts on the user's start press
    # ``_emit`` is the tap callback's sink; using it avoids needing permissions.
    recorder._emit(TraceEvent(kind="key", t_ms=0, key="w", down=True))
    recorder._emit(TraceEvent(kind="look", t_ms=10, dx=5, dy=0))
    assert len(recorder.poll()) == 2  # what cmd_record does while playing

    recorder.stop()
    warnings = " | ".join(recorder.warnings())
    assert "no events were recorded" not in warnings
    assert "keys were recorded but no mouse movement" not in warnings


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_recorder_still_reports_a_truly_empty_recording():
    recorder = backends_base.load_backend("record")
    recorder.stop()
    assert any("no events were recorded" in w for w in recorder.warnings())

    keys_only = backends_base.load_backend("record")
    from fpsbench.core.trace import TraceEvent

    keys_only._emit(TraceEvent(kind="key", t_ms=0, key="w", down=True))
    keys_only.poll()
    keys_only.stop()
    assert any("no mouse movement" in w for w in keys_only.warnings())


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_recorder_hotkeys_label_and_toggle(monkeypatch):
    """ctrl+alt+enter starts the capture and a second press ends it;
    ctrl+alt+1..9 drops a label while recording.  Every chord is swallowed, and
    what was played while waiting for the start press is not in the trace."""
    from fpsbench.core import keycodes

    module = backends_base.load_module("record")
    quartz = module.Quartz

    class _Key:
        def __init__(self, vk, flags=0):
            self.vk = vk
            self.flags = flags

    monkeypatch.setattr(
        quartz,
        "CGEventGetIntegerValueField",
        lambda event, field: 0 if field == quartz.kCGKeyboardEventAutorepeat else event.vk,
    )
    monkeypatch.setattr(quartz, "CGEventGetFlags", lambda event: event.flags)

    chord = module._FLAG_CTRL | module._FLAG_ALT
    recorder = module.MacRecorderBackend()
    assert recorder.label_hotkey == "ctrl+alt+1..9"
    assert recorder.finish_hotkey == "ctrl+alt+enter"
    assert recorder.records_immediately is False
    assert recorder.recording() is False

    # Getting ready: a key and a label before the start press are not recorded.
    recorder._on_key(_Key(keycodes.MAC_VK["w"]), down=True)
    recorder._on_key(_Key(keycodes.MAC_VK["1"], chord), down=True)
    assert recorder.poll() == []
    assert not recorder.stop_requested()

    recorder._on_key(_Key(keycodes.MAC_VK["enter"], chord), down=True)
    recorder._on_key(_Key(keycodes.MAC_VK["enter"], chord), down=False)
    assert recorder.recording() is True
    assert recorder.poll() == []  # the chord itself is not part of the route
    assert not recorder.stop_requested()

    recorder._on_key(_Key(keycodes.MAC_VK["w"]), down=True)
    events = recorder.poll()
    assert [event.kind for event in events] == ["key"]
    assert events[0].t_ms < 100  # the trace clock starts at the press, not at start()

    recorder._on_key(_Key(keycodes.MAC_VK["1"], chord), down=True)
    assert [(event.kind, event.text) for event in recorder.poll()] == [("label", "1")]
    recorder._on_key(_Key(keycodes.MAC_VK["1"], chord), down=False)
    assert recorder.poll() == []

    recorder._on_key(_Key(keycodes.MAC_VK["enter"], chord), down=True)
    recorder._on_key(_Key(keycodes.MAC_VK["enter"], chord), down=False)
    assert recorder.poll() == []
    assert recorder.stop_requested()
    assert recorder.recording() is False


def test_cocoa_to_quartz_conversion_uses_the_primary_screen():
    """Region selection is only correct on multi-display setups if the y mirror
    uses the primary screen's height and subtracts the screen's global origin."""
    from fpsbench.backends.macos.selection import quartz_point

    class _Origin:
        def __init__(self, x, y):
            self.x, self.y = x, y

    class _Size:
        def __init__(self, width, height):
            self.width, self.height = width, height

    class _Rect:
        def __init__(self, x, y, width, height):
            self.origin = _Origin(x, y)
            self.size = _Size(width, height)

    primary = _Rect(0, 0, 1920, 1080)
    # A 100x40 box 20 points above the bottom-left of the primary screen: its top
    # edge is 60 points up in Cocoa, i.e. y = 1080 - 60 = 1020 in Quartz space.
    assert quartz_point(_Rect(10, 20, 100, 40), primary, 1080) == (10.0, 1020.0, 100.0, 40.0)

    # Secondary screen above the primary: its frame starts at y=1080 (Cocoa global).
    above = _Rect(0, 1080, 2560, 1440)
    x, y, w, h = quartz_point(_Rect(0, 1400, 200, 40), above, 1080)
    # Top edge in global Cocoa coordinates is 1080 + 1400 + 40 = 2520, and Quartz
    # measures from the top of the primary: 1080 - 2520 = -1440.
    assert (x, y, w, h) == (0.0, -1440.0, 200.0, 40.0)


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_gamepad_modules_load_and_report_capability():
    module = backends_base.load_module("gamepad")
    info = module.capability()
    assert "ok" in info and "reason" in info
    if not info["ok"]:
        # The unverifiable path must refuse loudly instead of degrading.
        with pytest.raises(BackendUnavailable):
            backends_base.load_backend("gamepad").prepare()

    recorder_module = backends_base.load_module("gamepad_record")
    checks = recorder_module.probe()
    assert checks and checks[0].name


def test_gamepad_recorder_constructs_without_opening_a_device():
    """The SDL recorder must import and construct even when pygame is absent."""
    recorder = backends_base.load_backend("gamepad_record", platform="macos")
    assert recorder.name


@pytest.mark.skipif(PLATFORM == "windows", reason="only meaningful off Windows")
def test_windows_gamepad_backend_refuses_to_load_off_windows():
    with pytest.raises(BackendUnavailable) as excinfo:
        backends_base.load_module("gamepad", platform="windows")
    assert "Windows" in str(excinfo.value)
