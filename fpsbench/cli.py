"""Command line interface: ``doctor``, ``record``, ``bench``, ``measure``, ``dry-run``,
``pick-region``.

The CLI is the only place that knows about both halves of the package (shared
core + platform backends).  Everything it does is deliberately explicit: a
missing permission, an unusable per-frame FPS source (metalperftrace /
PresentMon) and an unconfigured FPS region for the OCR fallback fail *before* the
run rather than producing a number nobody can trust.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .backends import base as backends_base
from .config import config_fps, config_input, update_config
from .core import quality, report, session, simplify as simplify_module
from .core.gamepad import GAMEPAD_BUTTONS, NEUTRAL_STATE, GamepadState
from .core.player import (
    LOOK_SPLIT_CHUNK,
    LOOK_SPLIT_RAW,
    Player,
    route_to_timeline,
    trace_to_timeline,
)
from .core.program import REFERENCE_HZ, route_to_frames, trace_to_frames
from .core.route import FpsConfig, Region, Route
from .core.samples import OCR, PER_FRAME_SOURCES, FpsSample, assign_labels
from .core.stats import percentile, summarize
from .core.trace import Trace

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INTERRUPTED = 130

#: FPS source modes accepted by ``--fps``.  Which of them work depends on the
#: platform: PresentMon is Windows-only, metalperftrace macOS-only, OCR both.
FPS_CHOICES = ("auto", "presentmon", "metalperftrace", "ocr")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _log(message: str = "") -> None:
    print(message, flush=True)


def _error(message: str, hint: Optional[str] = None) -> None:
    print("error: %s" % message, file=sys.stderr)
    if hint:
        for line in str(hint).splitlines():
            print("       %s" % line, file=sys.stderr)


def parse_region(text: str, *, space: str = "points") -> Region:
    """Parse ``x,y,w,h`` (also accepts ``x y w h`` and ``WxH+X+Y``-free input)."""
    cleaned = text.replace(",", " ").replace("x", " ") if "," in text else text.replace(",", " ")
    parts = [piece for piece in cleaned.replace(",", " ").split() if piece]
    if len(parts) != 4:
        # allow "w x h at x,y" style: 800x600+100+200
        import re

        match = re.match(r"^(\d+)[xX](\d+)\+(-?\d+)\+(-?\d+)$", text.strip())
        if match:
            w, h, x, y = (int(value) for value in match.groups())
            return Region(x=x, y=y, w=w, h=h, space=space)
        raise ValueError("expected x,y,w,h - got %r" % text)
    x, y, w, h = (float(piece) for piece in parts)
    return Region(x=x, y=y, w=w, h=h, space=space)


def _prepare_output(path: str) -> str:
    """Create the parent directory of an output path and return it unchanged.

    The quick start writes ``reports/baseline.json``, so a directory that does not
    exist yet is the first thing a new user hits; ``FileNotFoundError`` pointing at
    the file said nothing about the missing directory.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def load_playable(path: str) -> Tuple[str, Any]:
    """Load a route or a trace, deciding by the ``format`` field."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError("%s is not valid JSON: %s" % (path, exc)) from None
    fmt = payload.get("format") if isinstance(payload, dict) else None
    if fmt == "route":
        return "route", Route.from_dict(payload)
    if fmt == "trace":
        return "trace", Trace.from_dict(payload)
    raise ValueError(
        "%s has format=%r; expected 'route' or 'trace' (see routes/ for examples)" % (path, fmt)
    )


def resolve_fps_config(
    args,
    route: Optional[Route] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> FpsConfig:
    """Resolve the effective FPS config: CLI flags > route/trace block > config file.

    The config file's region, process name and PresentMon path fill in whatever
    the route/trace and the CLI left unset, so one ``pick-region`` (and/or a
    ``process_name`` in ``~/.fpsbench.json``) configures every route.
    """
    fallback = config_fps() or FpsConfig()
    if route is not None:
        base: FpsConfig = route.fps
    elif metadata:
        # A trace carries the region the recorder remembered in its metadata.
        base = simplify_module.fps_config_from_metadata(metadata)
    else:
        base = fallback
    region = None
    if getattr(args, "region", None):
        region = parse_region(args.region, space=fallback.region.space if fallback.region else "points")
    overrides: Dict[str, Any] = {
        "mode": getattr(args, "fps", None),
        "region": region,
        "presentmon_path": getattr(args, "presentmon", None),
        "process_name": getattr(args, "process_name", None),
    }
    config = FpsConfig.from_dict(base.to_dict(), "fps")
    for key, value in overrides.items():
        if value is None:
            continue
        if key == "region":
            config = config.merged(region=value)
        else:
            config = config.merged(**{key: value})
    # The config file fills in what the route/trace/CLI left unset.  Region,
    # process name and PresentMon path are per-machine facts, so a single setup
    # run configures every route - and only they are filled, never the mode
    # (an explicit route mode must win).
    for key, value in (
        ("region", fallback.region),
        ("process_name", fallback.process_name),
        ("presentmon_path", fallback.presentmon_path),
    ):
        if value is not None and getattr(config, key) is None:
            config = config.merged(**{key: value})
    return config


def _platform_module(name: str):
    return backends_base.load_module(name)


def _playback_clock() -> int:
    return time.perf_counter_ns()


def _boost_timing() -> Tuple[List[str], Optional[Any]]:
    """Ask the platform for the timing behaviour a precise player needs.

    Returns the notes to report and an optional restore callable (Windows claims
    a 1 ms timer resolution and has to give it back).
    """
    try:
        module = backends_base.load_module("timing")
    except backends_base.BackendUnavailable as exc:
        return ["timing boost unavailable: %s" % exc], None
    try:
        notes = list(module.boost_timing())
    except Exception as exc:  # best effort: never fail a benchmark over this
        return ["timing boost failed: %s: %s" % (type(exc).__name__, exc)], None
    return notes, getattr(module, "restore_timing", None)


def _sleep_until(clock_ns, target_ns: int) -> None:
    from .core.player import default_sleep_until

    default_sleep_until(clock_ns, target_ns, 1_500_000)


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def cmd_doctor(args) -> int:
    platform = backends_base.current_platform()
    if platform == "unsupported":
        _error("%s is not supported" % sys.platform, hint="run fpsbench on macOS or Windows 11")
        return EXIT_FAILURE

    region = None
    if args.region:
        region = parse_region(args.region)
    else:
        config = config_fps()
        region = config.region if config else None

    if args.json:
        _prepare_output(args.json)
    _log("fpsbench %s on %s (%s)" % (__version__, platform, sys.platform))
    _log("")
    fatal = 0
    try:
        preflight = _platform_module("preflight")
    except backends_base.BackendUnavailable as exc:
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE
    try:
        checks = preflight.preflight(window_title=args.window, region=region)
    except backends_base.BackendUnavailable as exc:
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE
    checks = list(checks) + _gamepad_checks()
    for check in checks:
        _log(check.render())
        if not check.ok and check.fatal:
            fatal += 1

    _log("")
    info = preflight.platform_info()
    if info:
        for key, value in sorted(info.items()):
            _log("  %s: %s" % (key, value))
    if region:
        _log("")
        _log("  FPS region from config: %gx%g at (%g,%g) %s" % (region.w, region.h, region.x, region.y, region.space))
    else:
        _log("")
        _log(
            "  no FPS region configured (only the overlay OCR fallback needs one) - "
            "run `fpsbench pick-region` to set it (config: %s)" % _config_path()
        )

    if args.list_windows:
        _log("")
        _log("  window titles:")
        lister = getattr(preflight, "list_window_titles", None)
        if lister is None:
            _log("    (this platform cannot enumerate window titles)")
        else:
            try:
                titles = lister()
            except Exception as exc:  # pragma: no cover - platform dependent
                titles = []
                _log("    (could not enumerate: %s)" % exc)
            for title in titles[:40]:
                _log("    %s" % title)
            if platform == "macos":
                _log(
                    "    (macOS only exposes titles of windows on the current Space, and needs "
                    "Screen Recording to show other apps' titles)"
                )

    if args.json:
        payload = {
            "platform": platform,
            "checks": [
                {
                    "name": check.name,
                    "ok": check.ok,
                    "detail": check.detail,
                    "fix": check.fix,
                    "fatal": check.fatal,
                }
                for check in checks
            ],
            "platform_info": info,
            "region": region.to_dict() if region else None,
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        _log("")
        _log("wrote %s" % args.json)

    if fatal:
        _log("")
        _error("%d blocking problem(s) - see the fix lines above" % fatal)
        return EXIT_FAILURE
    _log("")
    _log(
        "all checks passed for a %s run"
        % ("PresentMon/OCR" if platform == "windows" else "metalperftrace/OCR")
    )
    return EXIT_OK


def _config_path() -> str:
    from .config import config_path

    return config_path()


def _gamepad_checks() -> List[backends_base.Check]:
    """Gamepad checks for ``doctor`` - never fatal, the keyboard/mouse routes still work."""
    checks: List[backends_base.Check] = []
    try:
        recorder_module = backends_base.load_module("gamepad_record")
        probe = getattr(recorder_module, "probe", None)
        if probe is None:
            checks.append(
                backends_base.Check("SDL gamepad reading", False, "the recorder exposes no probe")
            )
        else:
            checks.extend(probe())
    except backends_base.BackendUnavailable as exc:
        checks.append(
            backends_base.Check("SDL gamepad reading", False, str(exc), fix=exc.hint or "")
        )
    try:
        gamepad_module = backends_base.load_module("gamepad")
        capability = getattr(gamepad_module, "capability", None)
        info = capability() if capability is not None else {"ok": False, "reason": "no probe"}
    except backends_base.BackendUnavailable as exc:
        checks.append(
            backends_base.Check("gamepad injection", False, str(exc), fix=exc.hint or "")
        )
        return checks
    if info.get("ok"):
        checks.append(
            backends_base.Check(
                "gamepad injection",
                True,
                info.get("backend") or info.get("reason") or "available",
            )
        )
    else:
        checks.append(
            backends_base.Check(
                "gamepad injection",
                False,
                "unavailable: %s" % (info.get("reason") or "unknown reason"),
                fix=info.get("hint") or "",
            )
        )
    return checks


# --------------------------------------------------------------------------- #
# probe-gamepad
# --------------------------------------------------------------------------- #
def cmd_probe_gamepad(args) -> int:
    """Report what this machine can *read* and *emit* as a gamepad.

    Reading and injection are probed separately on purpose: a setup that can
    record but not replay (or the other way round) must say so instead of
    pretending a route is comparable.
    """
    platform = backends_base.current_platform()
    if platform == "unsupported":
        _error("%s is not supported" % sys.platform, hint="run fpsbench on macOS or Windows 11")
        return EXIT_FAILURE
    _log("fpsbench %s - gamepad probe (%s)" % (__version__, platform))
    _log("")

    reading_ok = True
    recorder = None
    _log("reading a real controller (SDL):")
    try:
        recorder_module = backends_base.load_module("gamepad_record")
    except backends_base.BackendUnavailable as exc:
        reading_ok = False
        _log("  [FAIL] %s" % exc)
        if exc.hint:
            _log("         fix: %s" % exc.hint)
    else:
        for check in recorder_module.probe():
            _log("  " + check.render().replace("\n", "\n  "))
            reading_ok = reading_ok and check.ok
        if args.seconds:
            if not reading_ok:
                _log("  skipping --seconds: no controller is readable")
            else:
                recorder = _stream_gamepad(args)
                reading_ok = reading_ok and recorder is not None

    _log("")
    _log("virtual gamepad injection:")
    injection_ok = True
    injection_backend = None
    try:
        gamepad_module = backends_base.load_module("gamepad")
        info = gamepad_module.capability()
    except backends_base.BackendUnavailable as exc:
        injection_ok = False
        _log("  [FAIL] %s" % exc)
        if exc.hint:
            _log("         fix: %s" % exc.hint)
        info = None
    if info is not None:
        if info.get("ok"):
            _log("  [OK  ] %s" % (info.get("backend") or info.get("reason") or "available"))
        else:
            injection_ok = False
            _log("  [FAIL] unavailable: %s" % (info.get("reason") or "unknown reason"))
            if info.get("hint"):
                _log("         fix: %s" % info["hint"])
        if info.get("driver"):
            _log("         driver: %s" % info["driver"])

    if args.pulse:
        _log("")
        _log("sending a test pattern (this injects real input into the focused app):")
        if not injection_ok:
            _log("  skipped: virtual gamepad injection is unavailable")
        else:
            injection_backend = _pulse_gamepad()
            injection_ok = injection_backend

    _log("")
    verdict = []
    verdict.append("reading: %s" % ("ok" if reading_ok else "blocked"))
    verdict.append("injection: %s" % ("ok (unverified mapping)" if injection_ok else "blocked"))
    _log("; ".join(verdict))
    if not reading_ok or (args.pulse and not injection_ok):
        return EXIT_FAILURE
    if not injection_ok:
        _log(
            "note: this combination can record gamepad traces but not replay them; the "
            "keyboard/mouse route is the supported path on this machine"
        )
    return EXIT_OK


def _stream_gamepad(args):
    """Open the controller and print state changes for ``--seconds``."""
    try:
        recorder = backends_base.load_backend("gamepad_record")
        recorder.prepare(device=args.device, poll_hz=args.poll_hz)
    except backends_base.BackendUnavailable as exc:
        _log("  [FAIL] %s" % exc)
        return None
    except Exception as exc:
        _log("  [FAIL] %s: %s" % (type(exc).__name__, exc))
        return None
    _log("  streaming for %g s (move sticks/triggers now):" % args.seconds)
    recorder.start()
    deadline = time.monotonic() + args.seconds
    changes = 0
    try:
        while time.monotonic() < deadline:
            for event in recorder.poll():
                changes += 1
                _log("    %8.1f ms  %s" % (event.t_ms, event.state.describe() if event.state else "?"))
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        recorder.stop()
    info = recorder.device_info()
    _log(
        "  %d change(s); device: %s (%s)"
        % (changes, info.get("device"), info.get("source"))
    )
    for note in recorder.notes():
        _log("  note: %s" % note)
    if not changes:
        _log("  warning: no state change was seen - is the controller awake and not grabbed by another app?")
    return recorder


def _pulse_gamepad() -> bool:
    """Send a short, deterministic pattern so a game/second process can be checked."""
    try:
        backend = backends_base.load_backend("gamepad")
        backend.prepare()
    except backends_base.BackendUnavailable as exc:
        _log("  [FAIL] %s" % exc)
        return False
    except Exception as exc:
        _log("  [FAIL] %s: %s" % (type(exc).__name__, exc))
        return False
    pattern = [
        GamepadState(lx=0.6),
        GamepadState(ly=-0.6),
        GamepadState(rx=0.4, buttons=("a",)),
        GamepadState(l2=0.7),
        GamepadState(r2=0.7, buttons=("dpup", "leftshoulder")),
        GamepadState(buttons=GAMEPAD_BUTTONS),
        NEUTRAL_STATE,
    ]
    try:
        for state in pattern:
            _log("    %s" % state.describe())
            backend.set_state(state)
            time.sleep(0.25)
    except Exception as exc:
        _log("  [FAIL] %s: %s" % (type(exc).__name__, exc))
        return False
    finally:
        try:
            backend.neutral()
        finally:
            backend.teardown()
    _log("  [OK  ] pattern sent and centred again")
    for note in backend.notes():
        _log("  note: %s" % note)
    return True


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #
def cmd_record(args) -> int:
    platform = backends_base.current_platform()
    try:
        recorder = backends_base.load_backend("record")
    except backends_base.BackendUnavailable as exc:
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE

    input_config = config_input()
    gamepad_recorder = None
    if getattr(args, "gamepad", False):
        device = getattr(args, "gamepad_device", None) or input_config.get("gamepad_device")
        poll_hz = getattr(args, "gamepad_poll_hz", None) or input_config.get("gamepad_poll_hz")
        try:
            gamepad_recorder = backends_base.load_backend("gamepad_record")
            gamepad_recorder.prepare(device=device, poll_hz=poll_hz)
        except backends_base.BackendUnavailable as exc:
            _stop_quietly(recorder)
            _error(str(exc), hint=exc.hint)
            return EXIT_FAILURE
        except Exception as exc:
            _stop_quietly(recorder)
            _error("%s: %s" % (type(exc).__name__, exc))
            return EXIT_FAILURE

    fps_config = resolve_fps_config(args)
    if fps_config.region is None:
        _log("note: no FPS region configured; run `fpsbench pick-region` before benchmarking")
    _prepare_output(args.out)  # fail on a bad path now, not after the recording

    try:
        recorder.prepare(window_title=args.window)
        recorder.start()
    except backends_base.BackendUnavailable as exc:
        _stop_quietly(recorder)
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE
    except Exception as exc:
        _stop_quietly(recorder)
        _error("%s: %s" % (type(exc).__name__, exc))
        return EXIT_FAILURE

    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if recorder.records_immediately:
        _log("recording started - play the route now.")
    else:
        _log(
            "waiting: press %s (with the game focused) to start recording"
            % recorder.finish_hotkey
        )
    _log("  * switch to the game and move like the benchmark should")
    _log("  * press %s to drop a segment label (e.g. at the boss arena)" % recorder.label_hotkey)
    if recorder.finish_hotkey:
        _log(
            "  * press it again to stop recording"
            if not recorder.records_immediately
            else "  * press %s to stop recording" % recorder.finish_hotkey
        )
    if interactive:
        _log("  * press Enter here to stop recording (Ctrl-C also stops)")
    else:
        _log("  * stdin is not a terminal: stop with Ctrl-C, or use --max-duration")
    if args.max_duration:
        _log(
            "  * the whole run stops automatically after %g s (waiting included)"
            % args.max_duration
        )

    events = []
    pad_events = []
    stop_reason = "Enter"
    started = recorder.records_immediately
    pad_started = False
    if started and gamepad_recorder is not None:
        gamepad_recorder.start()
        pad_started = True
    # ``--max-duration`` caps the whole run, waiting for the start press included, so
    # a scripted run can never hang on a hotkey nobody presses.
    deadline = time.monotonic() + args.max_duration if args.max_duration else None

    stop_flag = threading.Event()

    def reader():
        try:
            input()
        except (EOFError, KeyboardInterrupt, OSError):
            pass  # closed stdin, Ctrl-C or a captured stdin all mean "stop"
        stop_flag.set()

    if interactive:
        thread = threading.Thread(target=reader, name="fpsbench-stdin", daemon=True)
        thread.start()
    try:
        last_line = 0.0
        while not stop_flag.is_set():
            events.extend(recorder.poll())
            if pad_started and gamepad_recorder is not None:
                pad_events.extend(gamepad_recorder.poll())
            if not started and recorder.recording():
                started = True
                _log("recording started - play the route now.")
                if gamepad_recorder is not None:
                    gamepad_recorder.start()
                    pad_started = True
            if recorder.stop_requested():
                stop_reason = recorder.finish_hotkey
                break
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "time limit"
                break
            if time.monotonic() - last_line > 1.0:
                last_line = time.monotonic()
                if started:
                    counts = _event_counts(events + pad_events)
                    print(
                        "\r  %5.1fs  keys=%d looks=%d labels=%d gamepad=%d"
                        % (
                            _last_time(events + pad_events) / 1000.0,
                            counts["key"],
                            counts["look"],
                            counts["label"],
                            counts["gamepad"],
                        ),
                        end="",
                        flush=True,
                    )
                else:
                    # Staring at an all-zero counter wondering whether it is armed is
                    # the one confusing part of an explicit start gesture.
                    print(
                        "\r  waiting for %s to start recording..." % recorder.finish_hotkey,
                        end="",
                        flush=True,
                    )
            time.sleep(0.05)
    except KeyboardInterrupt:
        stop_reason = "Ctrl-C"
    finally:
        events.extend(recorder.poll())
        recorder.stop()
        if gamepad_recorder is not None:
            if pad_started:
                pad_events.extend(gamepad_recorder.poll())
            gamepad_recorder.stop()
        print("\r" + " " * 60 + "\r", end="")
    _log("recording stopped (%s)" % stop_reason)

    all_events = sorted(events + pad_events, key=lambda event: event.t_ms)
    if not all_events:
        if not started:
            _error(
                "recording was never started",
                hint="press %s (with the game focused) to start and stop the recording"
                % recorder.finish_hotkey,
            )
        else:
            _error("nothing was recorded", hint=recorder_warnings(recorder) or "check the permissions")
        return EXIT_FAILURE

    metadata: Dict[str, Any] = {"recorded_from": "fpsbench record"}
    metadata["fps_mode"] = fps_config.mode
    if fps_config.region is not None:
        metadata["fps_region"] = fps_config.region.to_dict()
    if fps_config.process_name:
        metadata["process_name"] = fps_config.process_name
    if fps_config.presentmon_path:
        metadata["presentmon_path"] = fps_config.presentmon_path
    pad_end_ms = None
    if gamepad_recorder is not None:
        metadata["gamepad"] = gamepad_recorder.device_info()
        pad_notes = list(gamepad_recorder.notes())
        if pad_notes:
            metadata["gamepad"]["notes"] = pad_notes
        pad_end_ms = gamepad_recorder.end_ms()
    last_event_ms = float(all_events[-1].t_ms) if all_events else 0.0
    end_ms = None
    if pad_end_ms is not None:
        # A held stick after the last change still has to keep its duration: the
        # trace's explicit end is what stops the replay from cutting it short.
        end_ms = max(float(pad_end_ms), last_event_ms)
    trace = Trace(
        name=args.name or os.path.splitext(os.path.basename(args.out))[0],
        events=all_events,
        created=session.utc_now(),
        origin_platform=platform,
        metadata=metadata,
        end_ms=end_ms,
    )

    if args.trim_lead_in:
        result = quality.trim_idle(trace)
        trace = result.trace
        _log(
            "trimmed %.0f ms of lead-in%s"
            % (result.removed_lead_ms, " and %.0f ms of tail idle" % result.removed_tail_ms if result.removed_tail_ms else "")
        )

    warnings = recorder_warnings(recorder) + quality.analyse_trace(trace)
    if gamepad_recorder is not None:
        warnings.extend(gamepad_recorder.notes() if hasattr(gamepad_recorder, "notes") else [])
    trace.save(args.out)
    _log("")
    _log("wrote %s" % args.out)
    counts = trace.counts()
    _log(
        "  %.1f s, %d events (%d key, %d look, %d scroll, %d button, %d label, %d gamepad)"
        % (
            trace.duration_ms / 1000.0,
            len(trace.events),
            counts["key"],
            counts["look"],
            counts["scroll"],
            counts["button"],
            counts["label"],
            counts["gamepad"],
        )
    )
    for _, text in trace.labels():
        _log("  label %r" % text)
    for warning in warnings:
        _log("  warning: %s" % warning)
    _log("")
    _log("next: fpsbench bench %s" % args.out)
    return EXIT_OK


def recorder_warnings(recorder) -> List[str]:
    try:
        return list(recorder.warnings())
    except Exception:
        return []


def _stop_quietly(recorder) -> None:
    """Best-effort teardown after a failed ``start()``.

    A partially started recorder can still own a thread, a hidden window or a
    CoreFoundation run loop source; releasing it here keeps the failure from
    leaking resources for the rest of the process.
    """
    try:
        recorder.stop()
    except Exception:
        pass


def _event_counts(events: Sequence[Any]) -> Dict[str, int]:
    counts = {"key": 0, "look": 0, "scroll": 0, "button": 0, "label": 0, "gamepad": 0}
    for event in events:
        counts[event.kind] = counts.get(event.kind, 0) + 1
    return counts


def _wait_for_start_gesture(
    window: Optional[str], *, deadline: Optional[float]
) -> Tuple[bool, str]:
    """Block until the user presses the start hotkey (``ctrl+alt+enter``).

    Reusing the recorder is deliberate: it is the same gesture that starts a
    recording, its listen-only tap never consumes the input, and the plumbing
    already exists on both platforms.  Its own event stream is ignored - only the
    key that arms the recording is of interest here.

    Returns ``(started, note)``; ``started`` is False when ``deadline`` ran out
    first, and platforms that cannot watch for a hotkey start immediately.
    """
    try:
        recorder = backends_base.load_backend("record")
    except backends_base.BackendUnavailable as exc:
        _log("  no start hotkey here (%s) - benchmarking immediately" % exc)
        return True, ""
    if recorder.records_immediately:
        return True, ""
    try:
        recorder.prepare(window_title=window)
        recorder.start()
    except Exception as exc:
        _log("  could not watch for the start hotkey (%s) - benchmarking immediately" % exc)
        _stop_quietly(recorder)
        return True, ""

    hotkey = recorder.finish_hotkey or "ctrl+alt+enter"
    _log("press %s (with the game focused) to start the benchmark" % hotkey)
    try:
        while not recorder.recording():
            if deadline is not None and time.monotonic() >= deadline:
                return False, ""
            time.sleep(0.05)
        _log("  benchmark started")
        return True, "benchmark started by the %s hotkey" % hotkey
    finally:
        _stop_quietly(recorder)


def _last_time(events: Sequence[Any]) -> float:
    """Latest timestamp in a possibly unsorted batch (the two recorders are merged)."""
    return max((float(event.t_ms) for event in events), default=0.0)


def _frames_for(trace: Optional[Trace], route: Optional[Route], repeat_index: int, rate_hz: float):
    """Quantise the playable into one repeat of frame-paced input."""
    if trace is not None:
        return trace_to_frames(trace, rate_hz=rate_hz)
    assert route is not None
    return route_to_frames(route, rate_hz=rate_hz, repeat_index=repeat_index)


# --------------------------------------------------------------------------- #
# bench
# --------------------------------------------------------------------------- #
class _FramePace:
    """Frame period for frame-paced replay, refined from what the game presented.

    The input program is quantised to reference frames (``--input-hz``); replay
    delivers exactly one input frame per *rendered* frame, so the amount of input
    a frame sees does not depend on the rate.  The pace therefore has to follow
    the device: it starts at the reference period and switches to the median of
    the frame intervals the FPS source is reporting (PresentMon / metalperftrace
    write per-frame ``dt_ns``; an overlay counter cannot, so the reference period
    stays in place and the report says so).

    This is an *estimate* of the rendered frame period.  It is not synchronised
    to the game's own input sampling, its simulation step or its present phase,
    and the report labels it as an estimate for that reason.
    """

    #: How often the estimate is refreshed from newly arrived samples.
    REFRESH_S = 0.15
    #: Samples in the rolling window (about a second at 120 FPS).
    WINDOW = 120

    def __init__(self, nominal_ns: int, collector, fixed_ns: Optional[int] = None) -> None:
        self.nominal_ns = int(nominal_ns)
        self._collector = collector
        self._fixed_ns = int(fixed_ns) if fixed_ns else None
        self._period_ns: Optional[int] = None
        self._measured = False
        self._last_refresh: Optional[float] = None
        self._since_ns = 0

    @property
    def period_ns(self) -> int:
        if self._fixed_ns is not None:
            return self._fixed_ns
        return self._period_ns if self._period_ns is not None else self.nominal_ns

    @property
    def period_ms(self) -> float:
        return self.period_ns / 1e6

    @property
    def measured(self) -> bool:
        return self._measured

    @property
    def source(self) -> str:
        """``"fixed"``, ``"measured"`` or ``"reference"`` (nothing to follow yet)."""
        if self._fixed_ns is not None:
            return "fixed"
        return "measured" if self._measured else "reference"

    def begin(self) -> None:
        """Start following a new repeat: ignore frames from the settle/idle gap."""
        self._since_ns = time.perf_counter_ns()
        self._last_refresh = None

    def _median_dt(self, samples: Sequence[FpsSample], t_from_ns: Optional[int] = None) -> Optional[int]:
        floor_ns = self._since_ns if t_from_ns is None else t_from_ns
        intervals = [
            int(sample.dt_ns)
            for sample in samples
            if sample.dt_ns and sample.source in PER_FRAME_SOURCES and sample.t_ns >= floor_ns
        ]
        if not intervals:
            return None
        window = intervals[-self.WINDOW :]
        median = percentile([float(value) for value in sorted(window)], 50)
        # A single unusable frame must not send the pace to microseconds or minutes.
        return int(max(500_000, min(500_000_000, median)))

    def __call__(self) -> int:
        if self._fixed_ns is not None:
            return self._fixed_ns
        now = time.monotonic()
        if self._last_refresh is None or now - self._last_refresh >= self.REFRESH_S:
            self._last_refresh = now
            median = self._median_dt(self._collector.samples)
            if median is not None:
                self._period_ns = median
                self._measured = True
        return self.period_ns


class _SampleCollector:
    """Consumes an FPS backend's stream on a worker thread."""

    def __init__(self, fps_backend) -> None:
        self._backend = fps_backend
        self._samples: List[FpsSample] = []
        self._error: Optional[BaseException] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="fpsbench-fps", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for sample in self._backend.samples():
                self._samples.append(sample)
        except BaseException as exc:  # keep the failure for the report
            self._error = exc

    def join(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def samples(self) -> List[FpsSample]:
        return list(self._samples)

    @property
    def error(self) -> Optional[BaseException]:
        return self._error


def cmd_bench(args) -> int:
    try:
        kind, playable = load_playable(args.path)
    except ValueError as exc:
        _error(str(exc))
        return EXIT_FAILURE
    except OSError as exc:
        _error("cannot read %s: %s" % (args.path, exc))
        return EXIT_FAILURE

    route = playable if kind == "route" else None
    trace = playable if kind == "trace" else None
    name = playable.name
    route_source = kind
    route_hash = playable.hash()

    for output in (args.simplify, args.json, args.csv):
        if output:  # a bench run can take minutes: fail on a bad path before it starts
            _prepare_output(output)

    if args.simplify:
        if trace is None:
            _error("--simplify needs a trace input (a route is already simplified)")
            return EXIT_FAILURE
        result = simplify_module.simplify(
            trace,
            repeat=max(1, args.repeat or 1),
            warmup_ms=args.warmup_ms if args.warmup_ms is not None else None,
        )
        result.route.save(args.simplify)
        _log("wrote %s (%d steps)" % (args.simplify, len(result.route.steps)))
        for warning in result.warnings:
            _log("  warning: %s" % warning)
        if args.simplify_only:
            return EXIT_OK

    fps_config = resolve_fps_config(args, route, trace.metadata if trace is not None else None)
    repeats = int(args.repeat or (route.repeat if route else 1) or 1)
    pacing = getattr(args, "pacing", None) or "frames"
    input_hz = float(getattr(args, "input_hz", None) or REFERENCE_HZ)
    input_config = config_input()
    look_split_arg = getattr(args, "look_split", None)
    if look_split_arg is not None and pacing != "time":
        # Frame-paced replay quantises movement *and* look into input frames, so
        # neither strategy would run; refusing beats a silently ignored strategy.
        _error(
            "--look-split only applies to --pacing time",
            hint=(
                "frame-paced replay quantises look into input frames, so it cannot keep or "
                "chunk the recorded deltas; compare trajectories with "
                "`--pacing time --look-split raw`"
            ),
        )
        return EXIT_FAILURE
    if look_split_arg is not None and route is not None:
        _error(
            "--look-split applies to recorded traces, not routes",
            hint=(
                "a route's turn steps are interpolated by design; bench the trace it was "
                "simplified from when the recorded look timing matters"
            ),
        )
        return EXIT_FAILURE
    config_look_split = input_config.get("look_split")
    if look_split_arg is not None:
        look_split = look_split_arg
    elif pacing == "time" and trace is not None and config_look_split:
        # A config default is a preference, not a command: it only applies where
        # the strategy can actually run.
        look_split = config_look_split
    else:
        look_split = LOOK_SPLIT_CHUNK
    if input_hz <= 0:
        _error("--input-hz must be > 0")
        return EXIT_FAILURE
    warmup_ms = args.warmup_ms if args.warmup_ms is not None else (route.warmup_ms if route else 0.0)
    if pacing == "frames" and args.warmup_ms is None:
        # Frame-paced replay is about the whole path, so the headline statistics
        # cover the whole replay unless a warmup window was asked for explicitly.
        warmup_ms = 0.0
    settle_ms = args.settle_ms if args.settle_ms is not None else (route.settle_ms if route else 3000.0)
    nominal_period_ns = int(round(1e9 / input_hz))

    try:
        input_backend = backends_base.load_backend("input")
        fps_backend = backends_base.load_backend("fps")
    except backends_base.BackendUnavailable as exc:
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE

    try:
        preflight = _platform_module("preflight")
        platform_info = preflight.platform_info()
    except Exception as exc:
        platform_info = {"platform_info_error": "%s: %s" % (type(exc).__name__, exc)}

    # Fail before playing anything if the FPS source cannot possibly work.  In
    # `auto` the platform's per-frame source (PresentMon / metalperftrace) may
    # still succeed without a region, so the region is only mandatory when OCR
    # is explicitly requested - the dispatcher reports both failures otherwise.
    if fps_config.mode == "ocr" and fps_config.region is None:
        _error(
            "no FPS region configured for the overlay OCR source",
            hint="run `fpsbench pick-region` to frame the overlay FPS counter",
        )
        return EXIT_FAILURE

    warnings: List[str] = []
    found_window: Optional[str] = None
    if args.window:
        try:
            found_window = input_backend.find_window(args.window)
        except Exception as exc:
            warnings.append("could not enumerate windows: %s" % exc)
        if not found_window:
            _error(
                "no window title contains %r" % args.window,
                hint=(
                    "switch to the game window and retry, or list the titles with "
                    "`fpsbench doctor --list-windows`; on macOS that list only covers windows on "
                    "the current Space (a fullscreen game has its own) and needs Screen Recording "
                    "to show other apps' titles"
                ),
            )
            return EXIT_FAILURE

    try:
        input_backend.prepare(window_title=args.window, post_to_pid=args.post_to_pid)
    except backends_base.BackendUnavailable as exc:
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE

    gamepad_backend = None
    trace_counts = trace.counts() if trace is not None else {}
    gamepad_events = trace_counts.get("gamepad", 0)
    look_events = trace_counts.get("look", 0)
    if gamepad_events:
        # A gamepad trace needs the controller path; falling back to keyboard/mouse
        # would silently replay something else, so a missing backend is fatal.
        try:
            gamepad_backend = backends_base.load_backend("gamepad")
            gamepad_backend.prepare(window_title=args.window)
        except backends_base.BackendUnavailable as exc:
            input_backend.teardown()
            _error(str(exc), hint=exc.hint)
            return EXIT_FAILURE
        except Exception as exc:
            input_backend.teardown()
            _error("%s: %s" % (type(exc).__name__, exc))
            return EXIT_FAILURE

    try:
        fps_backend.start(fps_config, process_name=fps_config.process_name, presentmon_path=fps_config.presentmon_path)
    except backends_base.BackendUnavailable as exc:
        input_backend.teardown()
        if gamepad_backend is not None:
            gamepad_backend.teardown()
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE

    collector = _SampleCollector(fps_backend)
    collector.start()
    _log("FPS source: %s" % (fps_backend.source or "?"))
    _log("route: %s (%s, hash %s), %d repeat(s), settle %.0f ms, warmup %.0f ms"
         % (name, route_source, route_hash, repeats, settle_ms, warmup_ms))
    if gamepad_backend is not None:
        _log("gamepad: %s, %d recorded state change(s)" % (gamepad_backend.name, gamepad_events))
    if pacing == "frames":
        _log(
            "pacing: frame-quantised input program, one sample per estimated rendered frame "
            "(%g Hz program)" % input_hz
        )
    elif trace is not None and look_events:
        _log(
            "pacing: recorded timeline; look deltas %s"
            % (
                "posted once at their recorded time (--look-split raw)"
                if look_split == LOOK_SPLIT_RAW
                else "split into evenly spaced chunks when large (--look-split raw keeps "
                "the recorded times)"
            )
        )
    elif trace is None:
        _log("pacing: absolute timeline; route turn steps keep their interpolation")
    else:
        _log("pacing: recorded timeline")

    clock = _playback_clock
    player = Player(
        input_backend,
        gamepad=gamepad_backend,
        clock_ns=clock,
        sleep_until=lambda target: _sleep_until(clock, target),
        look_scale=args.look_scale if args.look_scale is not None else 1.0,
    )
    # The timeout covers the whole run, waiting for the start press included, so a
    # scripted bench can never hang on a hotkey nobody presses.
    deadline = time.monotonic() + args.timeout if args.timeout else None
    fixed_period_ns = None
    if getattr(args, "frame_period_ms", None):
        fixed_period_ns = int(round(float(args.frame_period_ms) * 1e6))
    pace = _FramePace(nominal_period_ns, collector, fixed_period_ns)
    runs: List[report.BenchRun] = []
    aborted = False
    interrupted = False
    restore_timing = None
    timing_notes: List[str] = []
    gesture_notes: List[str] = []
    try:
        started, gesture_note = _wait_for_start_gesture(args.window, deadline=deadline)
        if not started:
            _error(
                "the benchmark was never started",
                hint=(
                    "press ctrl+alt+enter (with the game focused) to start the run, or raise "
                    "--timeout"
                ),
            )
            return EXIT_FAILURE
        if gesture_note:
            gesture_notes.append(gesture_note)
        timing_notes, restore_timing = _boost_timing()
        for note in timing_notes:
            _log("  %s" % note)
        if fixed_period_ns is not None:
            _log("  fixed frame pace: %.2f ms/frame" % (fixed_period_ns / 1e6))
        for index in range(1, repeats + 1):
            if settle_ms > 0:
                _log("  repeat %d/%d: settling %.0f ms" % (index, repeats, settle_ms))
                time.sleep(settle_ms / 1000.0)
            markers: List[Tuple[int, str]] = []
            if pacing == "frames":
                frames = _frames_for(trace, route, index, input_hz)
                pace.begin()
                _log("  repeat %d/%d: playing %d input frames" % (index, repeats, len(frames)))
                stats = player.play_frames(
                    frames,
                    period_ns=pace,
                    on_label=lambda text, t_ns: markers.append((t_ns, text)),
                    abort=(lambda: deadline is not None and time.monotonic() > deadline),
                )
            else:
                timeline = (
                    trace_to_timeline(trace, look_split=look_split)
                    if trace is not None
                    else route_to_timeline(route, repeat_index=index)
                )
                _log("  repeat %d/%d: playing %d events" % (index, repeats, len(timeline)))
                stats = player.play(
                    timeline,
                    on_label=lambda text, t_ns: markers.append((t_ns, text)),
                    abort=(lambda: deadline is not None and time.monotonic() > deadline),
                )
            if stats.aborted:
                aborted = True
                warnings.append("run aborted at the timeout (%s s)" % args.timeout)
            runs.append(
                report.BenchRun(
                    index=index,
                    samples=[],
                    started_ns=stats.started_ns,
                    ended_ns=stats.ended_ns,
                    markers=markers,
                    max_lateness_ns=stats.max_lateness_ns,
                    input_frames=stats.frames,
                )
            )
            if aborted:
                break
    except KeyboardInterrupt:
        interrupted = True
        warnings.append("interrupted by the user")
    finally:
        try:
            fps_backend.stop()
        finally:
            collector.join()
            input_backend.teardown()
            if gamepad_backend is not None:
                gamepad_backend.teardown()
            if restore_timing is not None:
                try:
                    restore_timing()
                except Exception:
                    pass

    if collector.error is not None:
        warnings.append("FPS sampling stopped early: %s" % collector.error)

    streamed = collector.samples
    final = fps_backend.final_samples()
    if final is not None:
        authoritative = final
        if len(authoritative) != len(streamed):
            # OCR vets unconfirmed jumps after the run; metalperftrace re-reads
            # the tool's output file once it has exited.
            _log(
                "  %d samples streamed live -> %d in the final set"
                % (len(streamed), len(authoritative))
            )
    else:
        authoritative = streamed
    notes = gesture_notes + list(timing_notes) + fps_backend.notes()
    if gamepad_backend is not None:
        notes.extend(gamepad_backend.notes())
        notes.append(
            "gamepad state mirrors the recording: one full snapshot per recorded change, held "
            "until the next one, then centred at the end (no interpolation, no dead zone)"
        )
    if pacing == "frames":
        pace_note = {
            "fixed": " (fixed with --frame-period-ms)",
            "measured": " (follows the frame intervals the FPS source reports)",
            "reference": " (reference cadence: no per-frame intervals to follow)",
        }[pace.source]
        notes.append(
            "frame-paced replay: %d input frame(s) per repeat, one quantised sample per rendered "
            "frame, pace %.2f ms/frame%s"
            % (len(_frames_for(trace, route, 1, input_hz)), pace.period_ms, pace_note)
        )
        notes.append("FPS statistics cover the whole replay window (no warmup trim)")
        notes.append(
            "the frame pace is estimated from the presented frame intervals; it is not "
            "synchronised to the game's own input sampling or simulation step"
        )
        if args.warmup_ms is None and (route is not None and route.warmup_ms):
            notes.append("the route's warmup window was ignored; pass --warmup-ms to restore it")
    elif trace is not None and look_events:
        if look_split == LOOK_SPLIT_RAW:
            notes.append(
                "look deltas were posted once each at their recorded time (--look-split raw): "
                "the turn keeps the recording's own event distribution"
            )
        else:
            notes.append(
                "large recorded look deltas were split into evenly spaced chunks for injection "
                "(--look-split raw keeps their recorded times); the injected turn finishes after "
                "the recorded time"
            )
    elif trace is None:
        notes.append(
            "route turn steps replay as evenly interpolated events, not as recorded deltas; "
            "bench the trace when the trajectory has to match the recording"
        )
    worst_lateness_ns = max([run.max_lateness_ns for run in runs] + [0])
    # Half a frame of slip is where input starts landing in the wrong frame; below
    # that it is just timer granularity.
    lateness_budget_ns = max(2_000_000, pace.period_ns // 2 if pacing == "frames" else 4_000_000)
    if worst_lateness_ns > lateness_budget_ns:
        notes.append(
            "worst scheduling lateness was %.1f ms - the input timeline ran behind the game"
            % (worst_lateness_ns / 1e6)
        )
    if repeats > 1:
        notes.append(
            "repeats continue from where the previous run ended (the tool never resets the game); "
            "use --repeat 1 when the route does not return to its start"
        )
    if args.look_scale is not None and args.look_scale != 1.0:
        notes.append("look deltas were scaled by %g before injection" % args.look_scale)
    unsupported = input_backend.unsupported_keys()
    if unsupported:
        notes.append(
            "these keys do not exist on this platform and were skipped: %s"
            % ", ".join(sorted(unsupported))
        )

    for run in runs:
        start, end = run.started_ns, run.ended_ns
        # The frame the last input sample triggers is presented up to one frame
        # period after the dispatch, so the whole replay window includes it.
        window_end = end + (pace.period_ns if pacing == "frames" else 0)
        window_samples = [
            sample for sample in authoritative if start - 1 <= sample.t_ns <= window_end + 1
        ]
        # Kept untrimmed: the headline statistics describe the whole replay.
        run.window_samples = window_samples
        if warmup_ms > 0:
            window_samples = [sample for sample in window_samples if sample.t_ns >= start + warmup_ms * 1e6]
        run.samples = (
            assign_labels(window_samples, run.markers) if run.markers else window_samples
        )
        if not window_samples:
            warnings.append("repeat %d produced no FPS samples" % run.index)

    usable_runs = [run for run in runs if run.samples]
    if not usable_runs:
        _error(
            "no FPS samples were collected",
            hint="is the overlay counter visible? was the game running? (notes: %s)" % ("; ".join(notes) or "none"),
        )
        return EXIT_FAILURE if not interrupted else EXIT_INTERRUPTED

    metadata = session.collect_session(
        platform_info=platform_info,
        route_name=name,
        route_hash=route_hash,
        route_source=route_source,
        origin_platform=getattr(playable, "origin_platform", None),
        fps_source=fps_backend.source,
        fps_config=fps_config.to_dict(),
        warmup_ms=warmup_ms,
        settle_ms=settle_ms,
        repeats=repeats,
        backend_notes=notes,
    )
    if found_window:
        metadata["window"] = found_window
    metadata["input_pacing"] = pacing
    metadata["input_rate_hz"] = input_hz
    metadata["look_split"] = (
        "frame-quantised"
        if pacing == "frames"
        else (look_split if trace is not None else "interpolated")
    )
    if gamepad_backend is not None:
        metadata["gamepad_backend"] = gamepad_backend.name
        recorded = (trace.metadata or {}).get("gamepad") if trace is not None else None
        if isinstance(recorded, dict):
            metadata["gamepad"] = dict(recorded)
    metadata["worst_lateness_ms"] = round(
        max([run.max_lateness_ns for run in runs] + [0]) / 1e6, 3
    )
    if pacing == "frames":
        metadata["fps_window"] = "whole trace"
        metadata["frame_period_ms"] = round(pace.period_ms, 4)
        metadata["frame_pace_source"] = pace.source
        metadata["input_frames"] = max([run.input_frames for run in runs] + [0])

    result = report.build_result(
        name,
        usable_runs,
        route_hash=route_hash,
        route_source=route_source,
        metadata=metadata,
        warnings=warnings,
        notes=notes,
    )

    _log("")
    print(report.render_terminal(result, color=False if args.no_color else None), end="")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(report.result_to_json(result))
            handle.write("\n")
        _log("wrote %s" % args.json)
    if args.csv:
        with open(args.csv, "w", encoding="utf-8") as handle:
            handle.write(report.samples_to_csv(result.all_samples))
        _log("wrote %s" % args.csv)

    if interrupted:
        return EXIT_INTERRUPTED
    return EXIT_OK


# --------------------------------------------------------------------------- #
# pick-region
# --------------------------------------------------------------------------- #
def cmd_pick_region(args) -> int:
    platform = backends_base.current_platform()
    if platform == "unsupported":
        _error("%s is not supported" % sys.platform)
        return EXIT_FAILURE
    try:
        preflight = _platform_module("preflight")
    except backends_base.BackendUnavailable as exc:
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE

    region: Optional[Region] = None
    if args.region:
        region = parse_region(args.region, space="pixels" if platform == "windows" else "points")
    elif args.manual:
        prompt = "region x,y,w,h (points): " if platform == "macos" else "region x,y,w,h (pixels): "
        try:
            region = preflight.manual_region(prompt)
        except ValueError as exc:
            _error(str(exc))
            return EXIT_FAILURE
    else:
        _log("starting the selection window - drag a box around the FPS digits, Esc cancels")
        try:
            region = preflight.pick_region()
        except backends_base.BackendUnavailable as exc:
            _error(str(exc), hint="use `fpsbench pick-region --manual` instead")
            return EXIT_FAILURE

    if region is None:
        _log("cancelled - nothing written")
        return EXIT_FAILURE

    result, error = preflight.trial_read(region)
    if error:
        _log("warning: could not read the region now (%s)" % error)
    elif result is not None:
        text = result.raw_text.replace("\n", " ")[:60]
        if result.value is None:
            _log("warning: read %r but found no FPS value (%s)" % (text, result.reason))
            _log("         make sure the overlay counter is visible, then pick the region again")
        else:
            _log("region reads %r -> %.1f FPS" % (text, result.value))

    payload = {
        "region": region.to_dict(),
        "fps": {"mode": args.fps or "auto", "sample_hz": 6.0, "min_fps": 0.5, "max_fps": 2000.0},
    }
    if args.route:
        try:
            kind, playable = load_playable(args.route)
        except ValueError as exc:
            _error(str(exc))
            return EXIT_FAILURE
        if kind != "route":
            _error("--route expects a route file (traces stay lossless; the region belongs in a route)")
            return EXIT_FAILURE
        from .core.route import save_route

        playable.fps = playable.fps.merged(region=region, mode=args.fps or playable.fps.mode)
        save_route(playable, args.route)
        _log("wrote the region into %s" % args.route)
    if not args.no_config:
        path = update_config(region=region.to_dict(), fps=payload["fps"])
        _log("wrote %s" % path)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# measure
# --------------------------------------------------------------------------- #
#: Recorded event kinds that count as the player actually doing something.
_INPUT_KINDS = ("key", "look", "scroll", "button", "gamepad")


class _InputWatch:
    """Listen-only keyboard/mouse recorder kept open for the whole measurement.

    ``record``/``bench`` wait for the start hotkey and close the listener right
    after it.  ``measure`` instead starts on the first piece of input, so the tap
    has to be *armed* up front (:meth:`RecorderBackend.arm`) and stays open until
    the stop hotkey arrives.
    """

    def __init__(self) -> None:
        self.recorder = None
        self.hotkey = "ctrl+alt+enter"

    def open(self) -> bool:
        try:
            recorder = backends_base.load_backend("record")
        except backends_base.BackendUnavailable as exc:
            _error("cannot watch for input: %s" % exc, hint=exc.hint)
            return False
        self.hotkey = recorder.finish_hotkey or self.hotkey
        try:
            recorder.prepare()
            recorder.start()
            recorder.arm()
        except NotImplementedError:
            _error(
                "this platform's recorder cannot be armed without its hotkey",
                hint="use `record`/`bench` (explicit start gesture) instead",
            )
            _stop_quietly(recorder)
            return False
        except Exception as exc:
            _error(
                "could not watch for input: %s: %s" % (type(exc).__name__, exc),
                hint=getattr(exc, "hint", None),
            )
            _stop_quietly(recorder)
            return False
        self.recorder = recorder
        return True

    def close(self) -> None:
        if self.recorder is not None:
            _stop_quietly(self.recorder)
            self.recorder = None


def _open_gamepad_watch(args):
    """Open the controller reader, or return ``None`` when it is not available."""
    if getattr(args, "no_gamepad", False):
        return None
    try:
        recorder = backends_base.load_backend("gamepad_record")
        recorder.prepare()
        return recorder
    except backends_base.BackendUnavailable as exc:
        _log("note: controller input is not monitored (%s)" % exc)
    except Exception as exc:
        _log("note: controller input is not monitored (%s: %s)" % (type(exc).__name__, exc))
    return None


def _is_input_event(event: Any) -> bool:
    """Real gameplay input; a label marker is our own hotkey, not input."""
    return getattr(event, "kind", None) in _INPUT_KINDS


def cmd_measure(args) -> int:
    """Measure FPS from the first input until ``ctrl+alt+enter``, without replay.

    The command settles first (``--settle-seconds``), then opens the measurement
    window on the first keyboard, mouse or controller input and closes it on the
    stop hotkey.  It injects nothing and changes no game state; the headline
    numbers are the average and the 1% low FPS over exactly that window.
    """
    fps_config = resolve_fps_config(args)
    if fps_config.mode == "ocr" and fps_config.region is None:
        _error("measure with --fps ocr needs a region", hint="run `fpsbench pick-region` first")
        return EXIT_FAILURE

    for output in (args.json, args.csv):
        if output:
            _prepare_output(output)

    try:
        fps_backend = backends_base.load_backend("fps")
    except backends_base.BackendUnavailable as exc:
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE

    try:
        fps_backend.start(
            fps_config, process_name=fps_config.process_name, presentmon_path=fps_config.presentmon_path
        )
    except backends_base.BackendUnavailable as exc:
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE

    try:
        platform_info = _platform_module("preflight").platform_info()
    except Exception:
        platform_info = None

    _log("FPS source: %s" % (fps_backend.source or "?"))
    collector = _SampleCollector(fps_backend)
    collector.start()

    watch = _InputWatch()
    if not watch.open():
        fps_backend.stop()
        collector.join()
        return EXIT_FAILURE
    recorder = watch.recorder
    assert recorder is not None
    gamepad = _open_gamepad_watch(args)

    deadline = time.monotonic() + args.timeout if args.timeout else None
    settle_s = max(0.0, float(getattr(args, "settle_seconds", 0.0) or 0.0))
    start_ns = end_ns = 0
    stop_note = ""
    interrupted = False
    try:
        if settle_s > 0:
            _log("settling for %g s - get the game to the scene you want to measure" % settle_s)
            settle_deadline = time.monotonic() + settle_s
            while time.monotonic() < settle_deadline:
                if recorder.stop_requested():
                    _error("stopped before any input was seen")
                    return EXIT_FAILURE
                if deadline is not None and time.monotonic() >= deadline:
                    _error("--timeout %g s ran out during the settle period" % args.timeout)
                    return EXIT_FAILURE
                recorder.poll()  # whatever happens while settling does not count
                time.sleep(0.05)

        pad_snapshot = True
        if gamepad is not None:
            try:
                gamepad.start()
            except Exception as exc:
                _log("note: controller input is not monitored (%s: %s)" % (type(exc).__name__, exc))
                gamepad = None

        _log("ready: measuring starts on the first input (keyboard, mouse or controller)")
        while start_ns == 0:
            if recorder.stop_requested():
                _error("the measurement was stopped before any input was seen")
                return EXIT_FAILURE
            if deadline is not None and time.monotonic() >= deadline:
                _error(
                    "no input was seen before the timeout (%g s)" % args.timeout,
                    hint="wait for the settle period to end, then touch the controls",
                )
                return EXIT_FAILURE
            if any(_is_input_event(event) for event in recorder.poll()):
                start_ns = _playback_clock()
                break
            if gamepad is not None:
                events = gamepad.poll()
                if pad_snapshot:
                    # The first poll repeats the state the controller is already in.
                    events = events[1:]
                    pad_snapshot = False
                if events:
                    start_ns = _playback_clock()
                    break
            time.sleep(0.01)

        _log("measuring - press %s to stop (Ctrl-C also stops)" % watch.hotkey)
        while not recorder.stop_requested():
            if deadline is not None and time.monotonic() >= deadline:
                stop_note = "stopped at the timeout (%g s)" % args.timeout
                break
            recorder.poll()  # drain: a long measurement must not grow the buffer
            if gamepad is not None:
                gamepad.poll()
            time.sleep(0.01)
        end_ns = _playback_clock()
    except KeyboardInterrupt:
        interrupted = True
        end_ns = _playback_clock()
    finally:
        watch.close()
        if gamepad is not None:
            try:
                gamepad.stop()
            finally:
                gamepad = None
        try:
            fps_backend.stop()
        finally:
            collector.join()

    if stop_note:
        _log(stop_note)
    if end_ns <= start_ns:
        _error(
            "nothing was measured",
            hint="the window opens on the first input; %s stops it" % watch.hotkey,
        )
        return EXIT_FAILURE if not interrupted else EXIT_INTERRUPTED

    samples = fps_backend.final_samples()
    if samples is None:
        samples = collector.samples
    window = [sample for sample in samples if start_ns <= sample.t_ns <= end_ns]
    if not window:
        _error(
            "no FPS samples inside the measured window",
            hint="; ".join(fps_backend.notes()) or "is the game running and presenting?",
        )
        return EXIT_FAILURE if not interrupted else EXIT_INTERRUPTED

    summary = summarize(window)
    duration_ns = end_ns - start_ns
    approximate = summary.source == OCR
    _log("")
    _log("FPS source: %s" % report.describe_source(summary.source))
    if approximate:
        _log("            NOT per-frame data - percentiles are approximations")
    window_label = "frames" if summary.is_per_frame else "readings"
    _log(
        "window: %s, %d %s"
        % (report.format_duration(duration_ns), summary.samples, window_label)
    )
    _log("")
    suffix = " (approximate)" if approximate else ""
    for label, value in (
        ("average", "%.1f FPS" % summary.avg_fps),
        ("1% low", "%.1f FPS%s" % (summary.low_1pct, suffix)),
        ("0.1% low", "%.1f FPS%s" % (summary.low_01pct, suffix)),
        ("median", "%.1f FPS" % summary.median_fps),
        ("min/max", "%.1f / %.1f FPS" % (summary.min_fps, summary.max_fps)),
    ):
        _log("  %-9s %s" % (label, value))
    if summary.frame is not None:
        _log(
            "  %-9s %.2f ms p99, %d stutter(s)"
            % ("frame", summary.frame.frame_time_ms_p99, summary.frame.stutters)
        )

    notes = list(summary.notes) + fps_backend.notes()
    if collector.error is not None:
        notes.append("FPS sampling stopped early: %s" % collector.error)
    _log("")
    for note in notes:
        _log("note: %s" % note)

    if args.json:
        payload = {
            "source": summary.source,
            "duration_s": duration_ns / 1e9,
            "samples": summary.samples,
            "summary": summary.as_dict(),
            "notes": notes,
            "metadata": session.collect_session(
                platform_info=platform_info,
                fps_source=fps_backend.source,
                fps_config=fps_config.to_dict(),
            ),
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=False))
            handle.write("\n")
        _log("wrote %s" % args.json)
    if args.csv:
        with open(args.csv, "w", encoding="utf-8") as handle:
            handle.write(report.samples_to_csv(window))
        _log("wrote %s" % args.csv)

    if interrupted:
        return EXIT_INTERRUPTED
    return EXIT_OK


# --------------------------------------------------------------------------- #
# dry-run
# --------------------------------------------------------------------------- #
def cmd_dry_run(args) -> int:
    fps_config = resolve_fps_config(args)
    if fps_config.mode == "ocr" and fps_config.region is None:
        _error("dry-run with --fps ocr needs a region", hint="run `fpsbench pick-region` first")
        return EXIT_FAILURE

    try:
        input_backend = backends_base.load_backend("input")
        fps_backend = backends_base.load_backend("fps")
    except backends_base.BackendUnavailable as exc:
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE

    _log("platform: %s" % backends_base.current_platform())
    _log("input backend: %s" % input_backend.name)
    try:
        input_backend.prepare(window_title=args.window)
        _log("  prepare/teardown: ok (%s)" % (input_backend.capabilities() or "no state"))
    except Exception as exc:
        _error("input backend prepare failed: %s: %s" % (type(exc).__name__, exc))
        return EXIT_FAILURE
    finally:
        input_backend.teardown()

    try:
        fps_backend.start(fps_config, process_name=fps_config.process_name, presentmon_path=fps_config.presentmon_path)
    except backends_base.BackendUnavailable as exc:
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE

    _log("FPS source: %s" % (fps_backend.source or "?"))
    collector = _SampleCollector(fps_backend)
    collector.start()
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        fps_backend.stop()
        collector.join()

    samples = fps_backend.final_samples()
    if samples is None:
        samples = collector.samples
    if not samples:
        _error("no samples were collected in %g s" % args.seconds, hint="; ".join(fps_backend.notes()) or None)
        return EXIT_FAILURE
    summary = summarize(samples)
    _log("")
    _log(
        "%d usable samples in %g s window (drained %d raw)"
        % (len(samples), args.seconds, len(collector.samples))
    )
    for key, value in summary.as_dict().items():
        if isinstance(value, float):
            _log("  %s: %.3f" % (key, value))
        else:
            _log("  %s: %s" % (key, value))
    _log("")
    _log(report.ascii_timeline(samples))
    for note in fps_backend.notes() + ([str(collector.error)] if collector.error else []):
        _log("note: %s" % note)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fpsbench",
        description="External black-box 'walk the map' benchmark: replay a recorded route and "
        "measure FPS from metalperftrace (macOS 27+) or PresentMon (Windows), "
        "falling back to an overlay counter read through OCR.",
    )
    parser.add_argument("--version", action="version", version="fpsbench %s" % __version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check permissions, dependencies and configuration")
    doctor.add_argument("--region", help="FPS region as x,y,w,h (defaults to the config file)")
    doctor.add_argument("--window", help="game window title substring to verify")
    doctor.add_argument(
        "--list-windows",
        action="store_true",
        help="list window titles (macOS: current Space only, needs Screen Recording)",
    )
    doctor.add_argument("--json", help="write the check results as JSON")
    doctor.set_defaults(func=cmd_doctor)

    record = subparsers.add_parser("record", help="record a manual play-through into a trace")
    record.add_argument("--out", required=True, help="output trace path (e.g. routes/mine.trace.json)")
    record.add_argument("--name", help="route name stored in the trace")
    record.add_argument("--window", help="game window title substring to activate first")
    record.add_argument("--trim-lead-in", action="store_true", help="drop the silent lead-in/tail")
    record.add_argument(
        "--max-duration",
        type=float,
        help="stop automatically after N seconds (counted from the command, waiting included)",
    )
    record.add_argument(
        "--gamepad",
        action="store_true",
        help=(
            "also record controller state (SDL GameController via the [gamepad] extra); "
            "keyboard/mouse are recorded as usual"
        ),
    )
    record.add_argument(
        "--gamepad-device",
        help="record only the controller whose name contains this text (see `fpsbench probe-gamepad`)",
    )
    record.add_argument(
        "--gamepad-poll-hz",
        type=float,
        help="controller sampling rate in Hz (default 250; a change is recorded when it is seen)",
    )
    record.add_argument("--region", help="FPS region to store in the trace metadata")
    record.add_argument("--fps", choices=FPS_CHOICES, help="FPS source to remember")
    record.add_argument("--presentmon", help="path to PresentMon.exe (Windows)")
    record.add_argument(
        "--process-name",
        help=(
            "game process name (PresentMon / metalperftrace); on macOS an app or window "
            "name is resolved to the running process"
        ),
    )
    record.set_defaults(func=cmd_record)

    bench = subparsers.add_parser("bench", help="replay a route/trace and report FPS")
    bench.add_argument("path", help="route or trace JSON file")
    bench.add_argument("--repeat", type=int, help="number of repeats (default: route value or 1)")
    bench.add_argument(
        "--pacing",
        choices=("frames", "time"),
        default="frames",
        help=(
            "input pacing: 'frames' (default) quantises the route into per-frame samples and "
            "replays one sample per rendered frame, so the per-frame input sequence does not "
            "depend on the frame rate; 'time' replays the recorded timeline as-is (the "
            "reference for trajectory comparison)"
        ),
    )
    bench.add_argument(
        "--look-split",
        choices=("chunk", "raw"),
        help=(
            "how a recorded look delta is timed ('--pacing time' only): 'chunk' (default) "
            "splits a large delta into chunks spaced ~8 ms apart so a per-frame clamp cannot "
            "swallow it, which delays the turn past its recorded time; 'raw' posts every "
            "recorded delta once at its recorded time"
        ),
    )
    bench.add_argument(
        "--input-hz",
        type=float,
        default=REFERENCE_HZ,
        help="reference frames per second of the quantised input program (default 60)",
    )
    bench.add_argument(
        "--frame-period-ms",
        type=float,
        help=(
            "fix the frame pace instead of following the frame intervals the FPS source reports "
            "(use the recording's frame time to reproduce its per-frame input amounts)"
        ),
    )
    bench.add_argument("--warmup-ms", type=float, help="discard this much at the start of each repeat")
    bench.add_argument(
        "--look-scale",
        type=float,
        help=(
            "multiply every injected look delta (default 1.0); use 0.5 when the replay turns "
            "twice as far as the recording did"
        ),
    )
    bench.add_argument("--settle-ms", type=float, help="idle time before each repeat (default 3000)")
    bench.add_argument("--fps", choices=FPS_CHOICES, help="FPS source")
    bench.add_argument("--presentmon", help="path to PresentMon.exe (Windows)")
    bench.add_argument(
        "--process-name",
        help=(
            "game process name (PresentMon / metalperftrace); on macOS an app or window "
            "name is resolved to the running process"
        ),
    )
    bench.add_argument("--region", help="FPS region as x,y,w,h")
    bench.add_argument("--window", help="game window title substring to activate first")
    bench.add_argument(
        "--post-to-pid",
        type=int,
        help="post events to this pid (macOS); on Windows, activate that process's window",
    )
    bench.add_argument(
        "--timeout",
        type=float,
        help="abort after N seconds (waiting for the start hotkey included)",
    )
    bench.add_argument("--simplify", help="also write a declarative route here")
    bench.add_argument("--simplify-only", action="store_true", help="write the route and stop")
    bench.add_argument("--json", help="write the report as JSON")
    bench.add_argument("--csv", help="write the per-sample time series as CSV")
    bench.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    bench.set_defaults(func=cmd_bench)

    probe = subparsers.add_parser(
        "probe-gamepad",
        help="check gamepad reading and virtual-device injection (no benchmark)",
    )
    probe.add_argument("--device", help="only the controller whose name contains this text")
    probe.add_argument(
        "--seconds",
        type=float,
        help="stream recorded state changes for N seconds (move the sticks to see them)",
    )
    probe.add_argument("--poll-hz", type=float, help="sampling rate while streaming (default 250)")
    probe.add_argument(
        "--pulse",
        action="store_true",
        help="also send a short test pattern to the virtual device (injects real input!)",
    )
    probe.set_defaults(func=cmd_probe_gamepad)

    pick = subparsers.add_parser("pick-region", help="select the overlay FPS region")
    pick.add_argument("--region", help="set the region directly (x,y,w,h)")
    pick.add_argument("--manual", action="store_true", help="type the region instead of dragging")
    pick.add_argument("--route", help="write the region into this route file as well")
    pick.add_argument("--fps", choices=FPS_CHOICES, help="FPS source to remember")
    pick.add_argument("--no-config", action="store_true", help="do not touch the config file")
    pick.set_defaults(func=cmd_pick_region)

    measure = subparsers.add_parser(
        "measure",
        aliases=["fps"],
        help=(
            "measure FPS from the first input until the stop hotkey "
            "(no route, no input injection)"
        ),
    )
    measure.add_argument("--fps", choices=FPS_CHOICES, help="FPS source")
    measure.add_argument("--region", help="FPS region as x,y,w,h (OCR source only)")
    measure.add_argument("--presentmon", help="path to PresentMon.exe (Windows)")
    measure.add_argument(
        "--process-name",
        help=(
            "game process name (PresentMon / metalperftrace); on macOS an app or window "
            "name is resolved to the running process"
        ),
    )
    measure.add_argument(
        "--settle-seconds",
        type=float,
        default=3.0,
        help="wait this long before watching for the first input (default 3)",
    )
    measure.add_argument(
        "--no-gamepad",
        action="store_true",
        help="do not monitor controller input (keyboard and mouse still start the window)",
    )
    measure.add_argument(
        "--timeout",
        type=float,
        help="abort after N seconds (settle and waiting for the first input included)",
    )
    measure.add_argument("--json", help="write the measurement as JSON")
    measure.add_argument("--csv", help="write the per-sample time series as CSV")
    measure.set_defaults(func=cmd_measure)

    dry = subparsers.add_parser("dry-run", help="exercise the capture chain without injecting input")
    dry.add_argument("--seconds", type=float, default=5.0, help="sampling window (default 5)")
    dry.add_argument("--fps", choices=FPS_CHOICES, help="FPS source")
    dry.add_argument("--region", help="FPS region as x,y,w,h")
    dry.add_argument("--presentmon", help="path to PresentMon.exe (Windows)")
    dry.add_argument(
        "--process-name",
        help=(
            "game process name (PresentMon / metalperftrace); on macOS an app or window "
            "name is resolved to the running process"
        ),
    )
    dry.add_argument("--window", help="game window title substring")
    dry.set_defaults(func=cmd_dry_run)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or EXIT_OK)
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        _error("interrupted")
        return EXIT_INTERRUPTED
    except backends_base.BackendUnavailable as exc:
        _error(str(exc), hint=exc.hint)
        return EXIT_FAILURE
    except (ValueError, OSError) as exc:
        # Route/trace decoding, region parsing and file access all raise these;
        # report them as user errors instead of a traceback.
        _error("%s: %s" % (type(exc).__name__, exc))
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
