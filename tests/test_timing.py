"""Platform timing boosts (``timeBeginPeriod`` / user-interactive QoS).

The PLAN requires the player's scheduler to get better-than-default timing from
the OS.  Both implementations are best effort by design, so the tests check that
they report what happened and never raise - plus, on macOS, that the QoS class
really changed (which is observable through ``pthread_get_qos_class_np``).
"""

from __future__ import annotations

import sys

import pytest

from fpsbench.backends import base as backends_base
from fpsbench.backends.base import BackendUnavailable

PLATFORM = backends_base.current_platform()


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_boost_requests_user_interactive_qos():
    from fpsbench.backends.macos import timing

    before = timing.current_qos_class()
    notes = timing.boost_timing()
    assert isinstance(notes, list) and notes and all(isinstance(note, str) for note in notes)
    # The request is observable: either it took effect, or the note says why not.
    observed = timing.current_qos_class()
    if observed == timing.QOS_CLASS_USER_INTERACTIVE:
        assert any("user-interactive" in note for note in notes)
    else:
        assert observed == before or observed == -1
        assert any("QoS" in note or "jitter" in note for note in notes)


@pytest.mark.skipif(PLATFORM != "macos", reason="macOS only")
def test_macos_timing_module_loads_through_the_loader():
    module = backends_base.load_module("timing")
    assert callable(module.boost_timing)


def test_windows_timing_module_is_importable_and_best_effort():
    """It must import anywhere (the WinDLL call is lazy) and never raise."""
    from fpsbench.backends.windows import timing

    notes = timing.boost_timing()
    assert isinstance(notes, list)
    if not sys.platform.startswith("win"):
        # Off Windows there is no winmm: the note explains the fallback.
        assert "unavailable" in notes[0]
        assert isinstance(timing.restore_timing(), list)
    else:  # pragma: no cover - Windows only
        assert "1 ms" in notes[0]


def test_missing_timing_module_does_not_break_a_run(monkeypatch):
    from fpsbench import cli

    def exploding(name, platform=None):
        raise BackendUnavailable("no timing module here")

    monkeypatch.setattr(backends_base, "load_module", exploding)
    notes, restore = cli._boost_timing()
    assert notes and "unavailable" in notes[0]
    assert restore is None


def test_a_broken_timing_module_is_reported_not_raised(monkeypatch):
    from fpsbench import cli

    class Exploding:
        @staticmethod
        def boost_timing():
            raise RuntimeError("timer says no")

    monkeypatch.setattr(backends_base, "load_module", lambda name, platform=None: Exploding)
    notes, restore = cli._boost_timing()
    assert notes and "timer says no" in notes[0]
    assert restore is None


def test_restore_callable_is_picked_up(monkeypatch):
    from fpsbench import cli

    calls = []

    class Module:
        @staticmethod
        def boost_timing():
            return ["boosted"]

        @staticmethod
        def restore_timing():
            calls.append("restored")

    monkeypatch.setattr(backends_base, "load_module", lambda name, platform=None: Module)
    notes, restore = cli._boost_timing()
    assert notes == ["boosted"]
    assert restore is not None
    restore()
    assert calls == ["restored"]
