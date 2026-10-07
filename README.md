# fpsbench

External, black-box **"walk the map" benchmark** for single-player games that have
no built-in benchmark mode.

You play a route once, fpsbench records your keyboard/mouse, and from then on it
replays that route by injecting input while sampling FPS from the OS's own
frame-timing data (`metalperftrace` on macOS 27+, PresentMon on Windows) or, where
neither is available, from an on-screen counter read through OCR. That lets you
measure the same piece of gameplay before and after a graphics setting,
resolution, overclock or driver change.

What it deliberately does **not** do:

* it does not launch or stop the game (you get into the map yourself);
* it does not read game memory, inject a DLL or modify the game in any way;
* it does not count captured screen frames (that is capped by the refresh rate and
  the capture competes with the game for GPU time).

## How it works

```
record:  platform recorder  ->  trace JSON (lossless, the reference artefact)
         (+ SDL gamepad)
                                      |
                                 --simplify
                                      v
                                route JSON (editable steps: key / turn / wait / scroll / button)
bench:   route or trace  ->  shared absolute-timeline player  ->  platform input backend
                                              |                          (+ virtual gamepad)
                              platform FPS backend  ->  FpsSample stream
                                              |
                                  stats  ->  terminal table / JSON / CSV / ASCII timeline
```

The shared half (`fpsbench/core`) is pure logic and is unit tested on any OS.
Everything OS specific lives behind four small interfaces in
`fpsbench/backends/base.py`:

| interface | macOS | Windows 11 |
|---|---|---|
| `InputBackend` | `CGEvent` (cursor decoupled for relative look) | `SendInput` + **scancodes** (Unity/Unreal ignore virtual key codes) |
| `RecorderBackend` | `CGEventTap`, listen-only (raw `kCGMouseEventDeltaX/Y`) | **Raw Input** (`RAWMOUSE.lLastX/Y`; a low-level hook cannot see unclipped deltas) |
| `GamepadRecorderBackend` | SDL2 GameController (pygame, optional extra) | SDL2 GameController (pygame, optional extra) |
| `GamepadBackend` | CoreHID `HIDVirtualDevice` through a signed helper (unverified) | `vgamepad` + ViGEmBus, virtual Xbox 360 |
| `FpsBackend` | **`metalperftrace`** (macOS 27+, true frame intervals) → fallback overlay OCR | **PresentMon** (ETW, true frame intervals) → fallback overlay OCR |
| `OcrBackend` | ScreenCaptureKit + Vision | GDI `BitBlt` + `Windows.Media.Ocr` |

Frame-time based statistics (1% / 0.1% lows, frame-time percentiles) come from
`metalperftrace` on macOS and PresentMon on Windows; both are wired in by default
and need neither a visible counter nor Screen Recording. Overlay OCR is the
fallback when neither is available, and then the numbers come from a counter that
usually updates about once per second, so the report says so explicitly and the
low percentiles are labelled as approximations.

## Requirements

* **macOS 12.3+** with Python 3.10+ (3.9 also works, but pins `pyobjc` to 10.3.x)
  and the `.[macos]` extra.
* **Windows 11** with Python 3.10+ and the `.[windows]` extra (`winsdk`, only
  needed for the OCR fallback).
* **Optional, controller route only:** the `.[gamepad]` extra (`pygame`, i.e.
  SDL2) to *record* a controller, plus a virtual device to replay it — ViGEmBus
  with `vgamepad` on Windows (archived upstream), a signed CoreHID helper on
  macOS. The keyboard/mouse route needs neither.
* An FPS source. Both platforms have one built in — `metalperftrace` on macOS 27+
  and [PresentMon](https://github.com/GameTechDev/PresentMon/releases) on Windows
  — and neither needs a visible counter or a screen region. A **visible FPS
  counter** (plus `fpsbench pick-region`) is only needed for the fallback:
  on Windows when PresentMon is missing, on macOS before 27 or when the game's
  Metal layer cannot be read. Recommended counters: Steam overlay (both
  platforms), RTSS (Windows), `MTL_HUD_ENABLED=1` (macOS Metal HUD), or the
  game's own. Note that the Metal HUD's FPS readout is a 120-frame *rolling*
  average, which under-reports stutter — see
  [the HUD section of the related-work notes](docs/RELATED-WORK.md#macos-using-the-metal-performance-hud-to-measure-fps).
* Either `metalperftrace`/PresentMon working for your game, or `--process-name`
  plus `fpsbench pick-region`.

### Install

```bash
# macOS
python3 -m venv .venv && .venv/bin/pip install -e ".[macos]"
scripts/bench.sh doctor

# Windows 11 (PowerShell)
py -m venv .venv; .venv\Scripts\pip install -e ".[windows]"
scripts\bench.ps1 doctor
```

`scripts/bench.sh` / `scripts/bench.ps1` are thin wrappers that find the
interpreter and forward every argument, so `scripts/bench.sh bench route.json`
always works even if `fpsbench` is not on `PATH`.

### Permissions

| platform | what to grant | where |
|---|---|---|
| macOS | **Accessibility** (input injection, recording); **Screen Recording** only for the OCR fallback (macOS < 27, or when the game's Metal layer cannot be read) | System Settings > Privacy & Security |
| Windows | nothing normally; run your terminal **as Administrator** if the game is elevated (otherwise UIPI silently drops synthetic input) | — |

`fpsbench doctor` checks all of this and prints the exact fix for anything that
is missing. Note that macOS **Secure Input** (some password fields and terminal
apps enable it) blocks recording and injection until you switch away.

## Quick start

```bash
# 1. is everything ready? (on macOS 27+ this already reports metalperftrace)
scripts/bench.sh doctor --window "Elden Ring"

# 2. (optional for the OCR fallback) frame the overlay FPS counter
scripts/bench.sh pick-region

# 3. make sure the capture chain actually reads frames
scripts/bench.sh dry-run --seconds 10 --process-name eldenring

# 4. record your route: run the command, then press ctrl+alt+enter to start
#    recording, play normally, and press ctrl+alt+enter again to stop
#    (ctrl+alt+1..9 label segments; --process-name is stored for the FPS source)
scripts/bench.sh record --out routes/liurnia.trace.json --process-name eldenring --trim-lead-in

# 5. benchmark it: press ctrl+alt+enter (with the game focused) when it says so,
#    to start the run; JSON + CSV for further analysis
scripts/bench.sh bench routes/liurnia.trace.json \
    --json reports/baseline.json --csv reports/baseline.csv
```

Then change one graphics setting, run step 5 again, and compare `median of runs`
(and the per-segment numbers) between the two JSON reports.

Existing recordings can be turned into an editable script:

```bash
scripts/bench.sh bench routes/liurnia.trace.json --simplify routes/liurnia.json --simplify-only
```

## Command reference

| command | purpose |
|---|---|
| `doctor [--region x,y,w,h] [--window SUBSTR] [--list-windows] [--json FILE]` | permissions, dependencies, displays, metalperftrace/PresentMon, FPS region trial read, gamepad reading/injection capability; `--list-windows` prints the window titles `--window` matches against (on macOS: current Space only, needs Screen Recording) |
| `record --out FILE [--name N] [--window SUBSTR] [--trim-lead-in] [--max-duration S] [--gamepad [--gamepad-device SUBSTR] [--gamepad-poll-hz H]] [--region x,y,w,h] [--fps ...] [--process-name NAME]` | record a play-through into a trace (hotkeys below); `--gamepad` also records controller state into a schema-2 trace |
| `bench PATH [--pacing frames\|time] [--look-split chunk\|raw] [--input-hz H] [--frame-period-ms MS] [--repeat N] [--warmup-ms MS] [--settle-ms MS] [--look-scale F] [--fps auto\|presentmon\|metalperftrace\|ocr] [--presentmon PATH] [--process-name NAME] [--region x,y,w,h] [--window SUBSTR] [--post-to-pid PID] [--timeout S] [--simplify FILE] [--simplify-only] [--json FILE] [--csv FILE] [--no-color]` | replay and report; `PATH` may be a trace or a route (`--post-to-pid` posts events to that pid on macOS, and activates its window on Windows); waits for the start hotkey. `--pacing frames` (default) replays one quantised sample per estimated rendered frame, `--pacing time` replays the recorded timeline as-is and is the reference for trajectory comparison (`--look-split raw` keeps the recorded look timing; see [Frame-paced replay](#frame-paced-replay-frame-rate-independent-input)) |
| `measure [--settle-seconds S] [--no-gamepad] [--fps auto\|presentmon\|metalperftrace\|ocr] [--process-name NAME] [--region x,y,w,h] [--timeout S] [--json FILE] [--csv FILE]` (alias `fps`) | settle, then measure FPS from the **first keyboard/mouse/controller input** until `ctrl+alt+enter` — no route, no replay, no input injection; reports **average** and **1% low** (plus 0.1% low, median, min/max); see [Measuring FPS without a route](#measuring-fps-without-a-route) |
| `probe-gamepad [--device SUBSTR] [--seconds S] [--poll-hz H] [--pulse]` | report controller **reading** and virtual-device **injection** separately; `--seconds` streams recorded state changes, `--pulse` sends a short test pattern to the virtual pad |
| `pick-region [--region x,y,w,h] [--manual] [--route FILE] [--fps ...] [--no-config]` | select the FPS counter region and store it |
| `dry-run [--seconds S] [--fps ...] [--region x,y,w,h] [--window SUBSTR]` | exercise the capture chain without injecting any input |

Hotkeys (same on macOS and Windows): **`ctrl+alt+enter` is the start gesture** —
for `bench` it begins the benchmark, for `record` it starts the capture and a
second press stops it, and for `measure` it only **stops** (measuring starts on
the first input; Ctrl-C also stops and still reports); **`ctrl+alt+1..9` drops a
segment label**.

Both commands come up waiting for that press, which is how you get the game into
position first: nothing played before it is measured, and nothing played before it
ends up in a trace (whose clock starts at the press). Enter in the terminal (or
Ctrl-C) also stops a recording, and `--max-duration` / `--timeout` bound the whole
run — the wait included — so a scripted run cannot hang on a hotkey nobody presses.

Timings: `--settle-ms` idles before each repeat (default 3000 ms) so the game
settles after the window is activated; `--warmup-ms` discards the beginning of
each repeat (shader compilation, loading) before statistics are computed.

## File formats

Both formats are strict: unknown fields, unknown step/event kinds, wrong types and
non-monotonic timestamps are **errors**. A silently ignored field would make two
runs incomparable, which defeats the purpose.

### `trace` — recorded, lossless (`--simplify` input)

```json
{
  "format": "trace", "schema_version": 2,
  "name": "Liurnia ride", "created": "2026-10-06T20:30:00Z", "origin_platform": "macos",
  "metadata": { "display_hz": 120, "fps_region": {"x": 10, "y": 20, "w": 120, "h": 40, "space": "points"} },
  "events": [
    {"t_ms": 0, "kind": "key", "key": "w", "down": true, "native": {"vk": 13}, "mods": []},
    {"t_ms": 1234, "kind": "look", "dx": 37, "dy": -4},
    {"t_ms": 1234, "kind": "label", "text": "cliff"}
  ]
}
```

Keys are stored under their **canonical name** (`w`, `shift`, `space`) *plus* the
recording platform's native code (`vk` on macOS, `scancode`/`ext` on Windows).
Replay prefers the local native code and falls back to the canonical name, so one
trace file works on both platforms. A key that does not exist on the replay
platform (`pause` on macOS) is skipped and reported, never fatal.

**Schema 2** adds a controller path and an explicit duration:

```json
{
  "format": "trace", "schema_version": 2, "name": "Boss run", "duration_ms": 42000,
  "metadata": {
    "gamepad": {"device": "DualSense Edge Wireless Controller", "source": "SDL2 gamepad",
                "axes": {"raw_range": [-32768, 32767], "range": [-1, 1], "y": "down",
                         "normalisation": "value / 32767"},
                "triggers": {"raw_range": [0, 32767], "range": [0, 1], "rest": 0,
                             "normalisation": "value / 32767"},
                "mapping": {"leftx": "a0", "lefttrigger": "a4", "a": "b0", "dpup": "b11"},
                "poll_hz": 250.0, "recorded_changes": 2}
  },
  "events": [
    {"t_ms": 0, "kind": "gamepad", "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "l2": 0.0, "r2": 0.0},
    {"t_ms": 1200, "kind": "gamepad", "lx": 1.0, "ly": -0.25, "rx": 0.0, "ry": 0.0,
     "l2": 0.6, "r2": 0.0, "buttons": ["a"]}
  ]
}
```

A `gamepad` event is a **complete state**, written only when something changed:
sticks are `-1..1` (y down, SDL's convention), triggers `0..1`, `buttons` is a
canonical subset of `a b x y leftshoulder rightshoulder dpup dpdown dpleft dpright
back start guide leftstick rightstick`. Out-of-range or non-finite values, unknown
buttons and partial states are rejected. `duration_ms` is the real end of the
recording, so a state that is still held at the end keeps its length (a change-only
trace would otherwise end at the last change). Version 1 traces keep loading
unchanged; version 1 cannot carry gamepad events.

Controller traces cannot be simplified: the route format has no gamepad steps, and
dropping them would produce a route that plays something else, so `--simplify`
refuses instead.

### `route` — declarative, editable (`bench` input, `--simplify` output)

```json
{
  "format": "route", "schema_version": 1, "name": "Liurnia ride",
  "warmup_ms": 8000, "settle_ms": 3000, "repeat": 3,
  "fps": {"mode": "auto", "region": {"x": 10, "y": 20, "w": 120, "h": 40, "space": "points"}},
  "steps": [
    {"type": "key",  "keys": ["w"], "hold_ms": 4000, "label": "straight"},
    {"type": "turn", "dx": 600, "dy": 0, "duration_ms": 300, "keys": ["w"]},
    {"type": "wait", "ms": 500}
  ]
}
```

Step types: `key` (`keys`, `hold_ms`), `turn` (`dx`, `dy`, `duration_ms`), `wait`
(`ms`), `scroll` (`dy`, `duration_ms`), `button` (`button`, `hold_ms`) and `label`
(`text`, a zero-length segment marker). Any step may carry `keys` to hold those
keys for its duration — that is how "run forward while turning" is expressed, and
adjacent steps with the same key set replay as one continuous hold.

`--simplify` compresses a trace into these steps: a hold becomes `key`, a burst of
`look` events becomes `turn` (deltas summed), idle gaps become `wait`, and the
compression never moves the end of the run (splitting a hold around a turn keeps
the exact timing). Warnings are printed for anything it could not express, for
example two non-key inputs overlapping in the same slice.

See [routes/example.json](routes/example.json) and
[routes/example.trace.json](routes/example.trace.json).

## FPS sources and methodology

* **`--fps auto`** (default) — the platform's own frame-timing source:
  `metalperftrace` on macOS 27+ / PresentMon on Windows. Only if that cannot be
  used does it fall back to overlay OCR, which then needs a region; a fallback is
  always reported in the metadata and the notes.
* **`--fps metalperftrace`** — macOS only. Reads the OS's always-on Metal frame
  timing (`/usr/bin/metalperftrace listen --json`): no screen region, no Screen
  Recording permission, no GPU contention. Needs `--process-name` so it knows
  which Metal layer to read; `metalperftrace` filters on the **executable** name,
  so fpsbench resolves the name you see (the app/window title, e.g. `红色沙漠`)
  to the running process (`CrimsonDesert_Steam`) and reports the substitution.
  It counts frames that reached the display, so the game has to be *on screen*:
  an occluded, minimised or other-Space window presents nothing and cannot be
  measured — fpsbench says so instead of blaming `--process-name`.
* **`--fps presentmon`** — Windows only. True per-frame present intervals from
  ETW. Requires `--process-name`; PresentMon must be on `PATH`, next to a known
  install location, or given with `--presentmon <path>`. Production arguments are
  probed in both `--flag` and `-flag` spellings (1.x vs 2.x); failures list every
  command that was tried.
* **`--fps ocr`** — reads the counter through OCR at `fps.sample_hz` (default
  6 Hz). Needs `--region`/`pick-region`. Polls that do not parse, are out of the
  plausible range, or are an unconfirmed jump (an isolated spike that the next
  poll does not corroborate) are rejected and reported. Sampling stops with an
  explanation after 12 consecutive unusable polls, so a hidden counter cannot
  masquerade as a run.

Statistics are computed per source and never mixed:

| | `metalperftrace` / `presentmon` | `ocr` |
|---|---|---|
| average FPS | frames / duration | mean of the readings |
| spread | instantaneous min/max/median, frame-time p50/p95/p99/p99.9, stdev | reading min/max/median |
| low percentiles | **1% / 0.1% low** = mean of the slowest frames | 1st / 0.1th percentile of readings (approximate) |
| stutters | frames longer than 2× the median frame time | — |

`metalperftrace` reports one *interval* of Metal statistics per second (frame
count, total, min/max and standard deviation of the frame-on-glass interval), not
every present. Frame times are reconstructed from that: the average, the frame
count and the instantaneous min/max are exact, and the frames in between sit at
the interval's mean, so the percentiles are approximations bounded by the 1 s
interval. The report says so in its notes. Windows/PresentMon is the only path
that sees every present.

Everything is also segmented by the `label` hotkeys and reported per repeat
(per-run averages plus median of runs).

### Measuring FPS without a route

`measure` is the plain "how fast is it running right now" command: it injects
**nothing** and changes no game state, so it works in a menu, in the middle of a
manual play-through, or when the route does not exist yet.

```bash
# macOS (metalperftrace needs the process name)
scripts/bench.sh measure --fps metalperftrace --process-name Game

# Windows
scripts/bench.sh measure --fps presentmon --process-name Game.exe

# overlay counter instead (needs a configured region)
scripts/bench.sh measure --fps ocr --region 20,20,140,40
```

1. Start it — it settles for `--settle-seconds` (default 3 s) without counting
   anything; use that time to get to the scene you want to measure.
2. The window opens **automatically on the first input** — a key, a mouse move
   or click, or a controller change. Nothing to press, so the measurement starts
   on the input that actually begins the action.
3. Press **`ctrl+alt+enter`** to close the window; the numbers are printed.

```
FPS source: metalperftrace (Metal frame intervals, 1 s statistics)
window: 12.3s, 615 frames

  average   49.8 FPS
  1% low    35.9 FPS
  0.1% low  30.0 FPS
  median    49.9 FPS
  min/max   30.0 / 120.0 FPS
  frame     12.34 ms p99, 3 stutter(s)
```

Like the `bench` report, the average is frames / measured duration and the
**1% low** is the mean of the slowest 1% of frames (the CapFrameX / PresentMon
convention), not a single percentile. `--json`/`--csv` export the same numbers
and the raw samples; Ctrl-C closes the window early and still reports what was
measured.

Notes:

* Input during the settle period is ignored, so moving the mouse to focus the
  game before the countdown ends cannot open the window early. After it ends,
  *any* input opens it — put the mouse down once you are in position.
* Controller input is monitored automatically (the reader ignores the initial
  state snapshot, so a resting pad does not start the window); `--no-gamepad`
  leaves it out, and a missing controller is only a note, never an error.
* `--timeout S` caps the whole command (settle and waiting for the first input
  included), so a scripted run can never hang.

### Playback precision

Every event is aligned to an absolute monotonic timeline (`t0 + t_ms`) rather than
to accumulated sleeps, so lateness on one event never shifts the rest of the route.
For the duration of a run the player also asks the OS for better timing:
`QOS_CLASS_USER_INTERACTIVE` on macOS, `timeBeginPeriod(1)` on Windows (released
again when the run ends). What was applied - or why it could not be - is printed
and stored in the report's notes, together with the worst scheduling lateness.

Absolute scheduling keeps the *events* on time; it does not decide which timeline
is replayed. `--pacing time` replays the recorded times, but large recorded look
deltas are still split into chunks unless you ask for `--look-split raw` (see
[Mouse-look fidelity](#mouse-look-fidelity)); `--pacing frames` replaces both with
the frame-quantised program.

### Frame-paced replay (frame-rate independent input)

A game consumes input **once per rendered frame**. Replaying the recorded timeline
verbatim therefore delivers a different amount per frame on every device: a look
delta that was one 60 Hz frame during recording is spread over two 30 FPS frames
(and can be clipped by the game's per-frame delta limit) or over half a 144 FPS
frame (where two samples collapse into one). Sub-frame key taps disappear the same
way, and a per-frame smoothing camera turns a different total amount.

`--pacing frames` (the default) reduces that dependency by quantising the route
into a fixed **input program** first:

* motion is integrated into reference frames of `1000 / --input-hz` ms (default
  60 Hz), so each input frame carries an exact, frame-sized amount of look/scroll
  (rounding remainders are carried, so totals never drift);
* key and button holds are quantised to whole frames — a 30 ms tap becomes a
  one-frame hold instead of falling between two polls;
* labels land in the frame that contains them.

At replay the program is driven by a **frame clock**, one input frame per rendered
frame. The pace follows the frame intervals the FPS source reports (PresentMon's
per-present intervals on Windows, metalperftrace's Metal intervals on macOS;
`--frame-period-ms` pins it instead). Every device therefore receives the same
sequence of per-frame deltas, and only the wall-clock duration changes:

```
duration = input_frames x device frame time
```

That is a *fixed workload*: both a 30 FPS and a 144 FPS machine process the same
input frames, so **FPS is measured over the whole replay** (frames / whole-trace
duration, no warmup trim). `whole trace` is the headline row, `input frames`,
`frame pace`, `look timing` and `worst lateness` are printed with the metadata, and
the route's own `warmup_ms` is ignored unless you pass `--warmup-ms` explicitly.

Notes and limits:

* This is an **input-side** guarantee, not an identical in-game path. The pace is
  *estimated* from the presented frame intervals; it is not synchronised to the
  game's own input sampling, its simulation step or its present phase, and a game
  that couples movement, physics or camera smoothing to its frame rate still
  produces a different path from the same input program. Compare trajectories by
  eye against landmarks (see [Comparing results](#comparing-results)) instead of
  assuming equality.
* The replay is work-fixed, not time-fixed: a route that takes 3 minutes at
  60 FPS takes 6 minutes at 30 FPS (same input frames, an estimated pace). Raise
  `--timeout` accordingly for slow targets.
* Repeats are *not* resets: repeat 2 starts wherever repeat 1 ended. Use
  `--repeat 1` (one bench invocation per run) and put the character back on the
  recording's start position before each run unless the route returns there.
* Look deltas are not chunked in this mode: whatever the recording delivered
  inside one reference frame is posted as one delta. A game that clamps a single
  delta per frame can swallow a fast flick — `--pacing time --look-split chunk`
  splits it instead.
* With an overlay-OCR FPS source there are no per-frame intervals, so the pace
  keeps the `--input-hz` reference cadence; the report says so.
* The pace only starts following the device once the first frame intervals arrive
  (about 250 ms of PresentMon latency); pin it with `--frame-period-ms <recording
  frame time>` when you need the recording's per-frame amounts from the first
  frame.
* A game that smooths the camera by *time* rather than by frame behaves the same
  in both modes. For a trajectory comparison use the recorded timeline instead:
  `--pacing time` replays absolute event times (same wall-clock duration on every
  device), and `--look-split raw` keeps the recorded look distribution as well
  (see [Mouse-look fidelity](#mouse-look-fidelity)).

### Mouse-look fidelity

A look event stores the mouse delta the OS delivered, and replay posts exactly
those numbers back — measured 1:1: a posted `dx=100` reaches consumers unchanged,
a 20-event recording replays as the same 20 deltas in the same order. The DPI or
brand of the mouse used while benchmarking is therefore irrelevant: the **trace**
is the reference, not the device.

What still changes the result is what the game does with those numbers:

* **Large single deltas are chunked by default.** A game often clamps one delta
  per frame, so replaying a 900 px flick as a single event can be dropped or
  snapped. `--look-split chunk` (the default under `--pacing time`) therefore
  posts that delta as chunks ~8 ms apart, which means the turn finishes *after*
  its recorded time and no longer lines up with the movement recorded around it.
  `--look-split raw` posts every recorded delta once at its recorded time; use it
  when the trajectory itself is what you are comparing. The option needs a trace
  with `--pacing time` (a route interpolates its turns, and frame pacing
  quantises them) and is refused elsewhere. The report records which strategy ran
  in `look timing` (`chunk`, `raw`, `interpolated` for a route, or
  `frame-quantised` for `--pacing frames`) and repeats the choice in the notes.
* **The recorded deltas are already the ones the OS produced.** macOS scales
  physical movement (pointer acceleration, tracking speed) before it becomes an
  event delta, and that scaled value is what gets recorded. A game that reads the
  event stream — the usual case, including `NSEvent.deltaX/Y` — sees the same
  values again on replay, so nothing is lost. A game that instead reads **raw
  device counts** (its own HID reader, or "raw input" enabled) saw unscaled numbers
  while recording and sees our scaled ones while replaying; only that game's own
  raw-input off / in-game smoothing off brings the two back together.
* **`--simplify` re-times the turns.** It sums a burst of look events into one
  `turn` and replays it as even steps of the same duration: a burst of
  34,41,38,44,39,−22,−18 (7 events) becomes ten ~24-point steps — same total, same
  span, different distribution. A camera that is smoothed per frame notices, so
  bench the **trace** when fidelity matters and the route only when you are editing
  it.
* **Display mode.** A point is only meaningful relative to the game's window; keep
  the resolution and scaling identical between record and bench, as you already
  keep the graphics settings identical.
* **Frame-rate dependent cameras.** A game that smooths, accelerates or clamps the
  camera *per frame* turns a different total amount when the frame rate changes —
  and a benchmark changes the frame rate by definition. Same deltas, same
  direction, different amplitude is the signature; cap the frame rate to the
  recording's, or turn the game's mouse smoothing off, if you need the path itself
  to match.

For a keyboard/mouse trajectory comparison, replay the **trace** on the recorded
timeline with the recorded look distribution:

```bash
scripts/bench.sh bench routes/mine.trace.json --pacing time --look-split raw --repeat 1
```

That combination posts every recorded key/button event and every look delta once,
at its recorded time, and does not let the FPS source touch the schedule. What it
still cannot fix is the game's own frame-rate-dependent logic (see the bullet
above) or a game that ignores synthetic input.

If a replay turns less (or more) than the recording for the same route, that is
the raw-input/acceleration or the per-frame signature above, or an in-game
sensitivity change — not a mismatch in the injected deltas. Two quick checks
separate them: bench the same trace twice at two different frame-rate caps (a
per-frame camera changes with the frame rate), and re-record with pointer
acceleration off (`defaults write -g com.apple.mouse.linear -bool true`, then log
out and back in) to see whether the mismatch was the acceleration curve. Disabling
acceleration only affects *new* recordings — the numbers are already baked into an
existing trace, so re-record before comparing.

When the replay still turns a constant factor too far or too short — the signature
of a game scaling our deltas itself, e.g. reading raw device counts while we
recorded the OS's display-scaled points — calibrate the replay:
`bench … --look-scale 0.5` multiplies every injected delta (rounding remainders are
carried event to event, so the total turn is exact, and the report's notes record
that a scale was applied). Find the factor by replaying once and comparing how far
the camera travels against the recording.

### Controller (gamepad) replay

The second input route records a real controller's **state** and replays it into a
virtual device, instead of moving a keyboard and a mouse:

```
DualSense (or any SDL GameController)
   -> polled state changes        (trace: full snapshot + duration_ms)
   -> recorded timeline player    (same absolute clock as the mouse route)
   -> platform virtual gamepad    (Windows: vgamepad/ViGEmBus, macOS: CoreHID helper)
   -> unmodified game / emulator
```

What the first version covers: both sticks, both analog triggers, the d-pad and
the ordinary buttons. It does **not** cover touchpad coordinates, gyro, rumble or
adaptive triggers, so a DS4 touchpad click is not recorded (it has no canonical
name yet).

Recording and replaying (`record --gamepad` needs the `.[gamepad]` extra):

```bash
# 1. what can this machine read and emit?
scripts/bench.sh probe-gamepad --seconds 10      # move the sticks while it streams

# 2. record: keyboard/mouse as usual, plus controller state
scripts/bench.sh record --out routes/boss.trace.json --gamepad --gamepad-device DualSense

# 3. replay it (no --simplify: a controller trace is not convertible to a route)
scripts/bench.sh bench routes/boss.trace.json --pacing time
```

State semantics, deliberately identical to how the controller was recorded:

* every event is a **full snapshot** and is sent only when something changed; the
  virtual device holds it between changes, exactly like a real controller does
  between two polls (nothing is accumulated, interpolated or dead-zoned);
* `--pacing frames` sends at most one change per input frame, `--pacing time` sends
  it at its recorded time; both use the same recorded states;
* the run always ends with every axis centred, both triggers released and every
  button up - also after a timeout, an abort or a backend exception;
* `duration_ms` keeps a still-held state's length, so a route that ends while you
  are holding forward does not silently get shorter.

What can still differ, and why this is not an "identical path" guarantee:

* **Device identity.** Windows injection presents a virtual **Xbox 360** pad;
  a game that looks for a DualSense (or reads its raw HID descriptor) will not see
  one. macOS `HIDVirtualDevice` can present a generic gamepad, but only through a
  **signed helper** with the `com.apple.developer.hid.virtual.device` entitlement -
  a plain `python3` cannot create one. SDL's in-process virtual joystick is not
  visible to another process, so it is not a substitute.
* **Mapping and response curves.** A virtual pad's axes go through the same
  game-side dead zones, response curves and sensitivity settings as a real pad, but
  not necessarily through the driver-level ones the recording saw. The mapping that
  was used is written into the trace metadata (`gamepad.device/source/axes/triggers/
  poll_hz`), and nothing is auto-calibrated. Reading is verified against a real
  DualSense Edge (SDL 2.28 via pygame 2.6): `get_axis` returns raw int16 values, so
  sticks are divided by 32767 and triggers are `0..32767` with rest at 0 - that
  normalisation happens once, in the recorder.
* **Poll phase.** The game polls the virtual device on its own schedule; a short
  button tap can fall between two polls. The states are published at the recorded
  time and held, which is the best an external tool can do - it is reported, not
  claimed.

`probe-gamepad` reports reading and injection **separately**, because a machine can
support one and not the other. `doctor` prints the same checks (never fatal: the
keyboard/mouse route still works). If injection is unavailable, `bench` on a
controller trace fails with the missing piece - it never falls back to keyboard/mouse
or pretends the trace was replayed.

Known status of the two injection paths:

| platform | backend | status |
|---|---|---|
| Windows | `vgamepad` + ViGEmBus (virtual Xbox 360) | implemented; ViGEmBus is **archived upstream**, so validate it on your target machine before depending on it |
| macOS | CoreHID `HIDVirtualDevice` through a signed helper (`FPSBENCH_GAMEPAD_HELPER`) | helper source + build script in [helper/](helper/README.md); **blocked on Apple**: macOS 15+ and the `com.apple.developer.hid.virtual.device` entitlement, which cannot be self-signed (ad-hoc signing is killed by AMFI). Without a signed helper the backend refuses with the reason |

Optional input settings (per machine, not per game) can live in the config file
(`~/.fpsbench.json`); invalid values are a hard error, never a silent fallback:

```json
{"input": {"look_split": "raw", "gamepad_device": "DualSense", "gamepad_poll_hz": 250}}
```

### Comparing results

Use the same route (check `route_hash` in the metadata) and change **one** thing.
Compare `median of runs` in the report, then look at the per-segment table to see
where the difference comes from.

`--repeat` does not reset the game, so a path comparison needs a manual start
position. Put the character back on the recording's save/position and camera
heading before every run, use `--repeat 1`, and include a straight run, a turn on
its own and a turn while moving. Judge three things by eye against landmarks:
whether the route stays on the recording's path, whether the end point lands in
the same area, and whether the camera ends up pointing at the same target. First
compare runs that use the same configuration (that is the noise floor of the
game's own start state), then compare across graphics loads with the actual FPS
range recorded — the tool does not measure trajectory error automatically.

**Do not compare raw numbers across operating systems.** Hardware, drivers, the
FPS source (`metalperftrace` vs PresentMon) and the way the FPS counter rounds are
all different; each platform is only comparable against itself. The report records
`origin_platform`, `os`, `gpu`, resolution/refresh rate and the FPS source so a
comparison is always auditable.

## Known limitations

1. **You need an FPS source.** Both platforms have one built in (`metalperftrace`
   on macOS 27+, PresentMon on Windows) and both need `--process-name`. Before
   macOS 27, or when the game has no readable Metal layer, the fallback is overlay
   OCR — which needs a visible counter and a configured region.
2. **OCR yields a reading series, not per-frame data.** Most counters update about
   once per second and round to integers, so average FPS is trustworthy while
   1% / 0.1% lows are approximations. `metalperftrace` (macOS 27+) and PresentMon
   (Windows) give frame-timing data instead; PresentMon is the only source that
   sees *every* present. The report states the source next to the numbers.
3. Neither backend counts captured screen frames (refresh-rate cap, GPU
   contention with the game under test). `metalperftrace` is not "screen capture":
   it reads the OS's always-on Metal frame timing.
4. Frame pacing makes the *input* frame-rate independent (one quantised sample per
   rendered frame, see [Frame-paced replay](#frame-paced-replay-frame-rate-independent-input)),
   and the pace is estimated from the presented frame intervals rather than
   synchronised to the game. No external tool can make a game's own per-frame
   logic time-invariant, so the in-game path is *comparable*, not identical.
   Physics is still not deterministic: compare medians over several repeats, and
   use `--pacing time --look-split raw` when you need the recorded wall-clock
   duration and the recorded look distribution rather than a fixed input workload.
5. macOS Secure Input and Windows UIPI (elevated game / restricted integrity)
   silently block recording and injection — `doctor` warns about both.
6. Some games with exclusive input or anti-cheat drivers swallow synthetic input.
   macOS offers `--post-to-pid` (post events straight to the game process);
   on Windows, which has no equivalent, the same flag brings that process's
   window to the foreground instead, and the injection itself always uses
   scancodes. **Single-player games only** — do not use this on anything with an
   anti-cheat that forbids synthetic input.
7. Purely external and black-box: no memory reads, no process injection, no game
   modification.
8. The keyboard vocabulary is limited to keys that exist on both platforms for
   cross-platform traces (letters, digits, `shift`/`ctrl`/`alt`/`cmd`, function
   keys, arrows, keypad, common punctuation). `numlock`/`scrolllock`/`pause`/
   `menu`/`insert`/`f21`-`f24` do not exist on Apple keyboards and are skipped
   with a note if a Windows trace uses them.
9. **The controller route is per-platform and partly unverified.** Reading uses
   SDL2 (the `.[gamepad]` extra). Injection needs a virtual device: on Windows
   `vgamepad` + ViGEmBus (archived upstream, presents an **Xbox 360** pad), on
   macOS a signed CoreHID helper (macOS 15+, HID virtual-device entitlement) -
   without one, `bench` refuses a controller trace instead of substituting
   keyboard/mouse. A virtual pad is not a DualSense, game-side dead zones and
   response curves still apply, and a short tap can fall between two game polls.
   `probe-gamepad` reports both directions separately; nothing is auto-calibrated.
9. The interactive region picker needs a GUI session; `pick-region --manual` and
   `--region x,y,w,h` work without one (over SSH, CI, scripted setup).

## Troubleshooting

| symptom | likely cause | fix |
|---|---|---|
| `doctor`: Accessibility FAIL | permission not granted | System Settings > Privacy & Security > Accessibility, then restart the terminal |
| `doctor`: Screen Recording FAIL | permission not granted | same, under Screen Recording — only the OCR fallback needs it |
| `doctor`: Target window FAIL | the window is not on the current Space (a fullscreen game gets its own Space), is minimised, or macOS hides other apps' titles without Screen Recording | switch to the game window and retry; `fpsbench doctor --list-windows` shows what is visible right now |
| `doctor`: metalperftrace not found | macOS < 27 | use the OCR fallback: a visible counter, `doctor --region` / `pick-region` and (on macOS) Screen Recording |
| `metalperftrace reported no Metal layer session` | the `--process-name` filter matched nothing: wrong name, or the game is not running | the name you see (window/Dock title) is resolved automatically on macOS; if it still fails, pass the executable name (Activity Monitor) or use `--fps ocr` |
| `metalperftrace has a Metal layer for … but no frame reached the display` | the layer exists but nothing is being presented (occluded/minimised window, another Space, or the game is still loading) | bring the game window to the front, or pass `--window <title>` so fpsbench activates it, and retry once it renders |
| `metalperftrace` average is right but 1% lows look flat | only per-second interval statistics are exposed | expected: percentiles are approximations; Windows + PresentMon reports every present |
| `CGEventTapCreate returned NULL` | Accessibility (or Input Monitoring) missing | grant it, restart the app |
| OCR reads nothing / "no usable FPS value" | counter not visible in the region, or HUD off | turn the counter on, re-run `pick-region` while it is visible |
| FPS is suspiciously capped at the refresh rate | you are looking at a frame counter, not an FPS counter | use the FPS readout (Steam overlay, RTSS) |
| PresentMon never appears | wrong `--process-name`, or an unsupported argument style | `doctor` shows what was tried; pass the exact process name (e.g. `eldenring.exe`) |
| Playback does nothing in game | UIPI (Windows) / Secure Input (macOS) / game ignores synthetic input | run as administrator, switch away from Secure Input, or try `--post-to-pid` |
| Replay turns a different amount than the recording | the game reads raw device counts (or its own raw-input mode) or smooths the camera per frame | injected deltas are exact, so fix the *game's* side: raw input off, mouse smoothing off, sensitivity unchanged, bench the trace rather than a simplified route, and re-record if you changed the pointer settings — see [Mouse-look fidelity](#mouse-look-fidelity); for a constant factor use `bench --look-scale F` |
| The replay's path drifts (turn no longer lines up with movement) | `--pacing frames` estimates the frame pace instead of syncing to the game, or large deltas were chunked after their recorded time | compare on the recorded timeline: `bench TRACE --pacing time --look-split raw --repeat 1` (the report's `look timing` shows which strategy ran) — see [Mouse-look fidelity](#mouse-look-fidelity) |
| `bench` reports "repeat N produced no FPS samples" | game not in the map, wrong `--process-name`, counter hidden, or too-short route | do a `dry-run` first; make sure the route is long enough to average over |

## How this differs from existing tools

There is no existing tool that records a route, replays it into an *unmodified*
game and reports per-segment FPS — see [docs/RELATED-WORK.md](docs/RELATED-WORK.md)
for the full survey (with links, licences and what each project still leaves you
to build). The short version:

| | PresentMon / CapFrameX | benchmark-automation scripts | beamer / Mouser | **fpsbench** |
|---|---|---|---|---|
| purpose | capture FPS | click the game's *built-in* benchmark | remote control | benchmark a game that has no benchmark |
| your own route | — | ✗ | ✓ (as remote control) | ✓ recorded, then replayed |
| drift-free replay timeline | — | ✗ | partial | ✓ absolute timeline |
| per-segment FPS along the route | ✗ | ✗ | ✗ | ✓ label hotkeys |
| macOS + Windows | Windows only | Windows only | both | both |

The closest projects all assume the game *has* a benchmark mode (they automate
clicking its button), or drive gameplay with computer vision instead of replaying
a route, or are Windows-only, or keep their measurement core closed. fpsbench
takes the building blocks that are already mature — `metalperftrace` on macOS 27+,
PresentMon on Windows, raw input / `CGEventTap` capture as in
[beamer](https://github.com/kalkman-code/beamer) — and supplies the missing
assembly: record → replay → measure → per-segment report, on both platforms.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,macos]"     # or ".[dev,windows]" on Windows
.venv/bin/python -m pytest tests/ -q
```

Layout:

```
fpsbench/
├── cli.py                     doctor / record / bench / pick-region / dry-run
├── config.py                  ~/.fpsbench.json (FPS region and defaults)
├── core/                      platform independent, unit tested everywhere
│   ├── keycodes.py            canonical names <-> macOS vk / Windows scancode
│   ├── samples.py             FpsSample (frame intervals | ocr readings)
│   ├── ocrparse.py            OCR text -> FPS, step-hold, outlier rejection
│   ├── trace.py  route.py     the two data contracts (strict codecs)
│   ├── simplify.py            trace -> route compression
│   ├── quality.py             "is this recording useful?" + --trim-lead-in
│   ├── player.py              absolute-timeline scheduling, look interpolation
│   ├── stats.py               the two statistics semantics
│   ├── report.py              tables, JSON, CSV, ASCII timeline
│   ├── session.py             run metadata (os, gpu, display, source, hash)
│   └── stable.py              hashes that survive a JSON round trip
└── backends/
    ├── base.py                the four interfaces, platform autoload, checks
    ├── _ocr_fps.py            shared OCR sampling loop
    ├── _presentmon.py         PresentMon runner + CSV parser (shared)
    ├── _metalperftrace.py     metalperftrace runner + interval reconstruction (macOS)
    ├── _dispatch.py           auto -> the per-frame source else OCR, never silently
    ├── _win_types.py          Win32 structs (fixed-width, layout unit tested)
    ├── _capture_bmp.py        GDI pixels -> BMP bytes (unit tested)
    ├── macos/                 input.py record.py fps.py ocr.py preflight.py selection.py timing.py
    └── windows/               input.py record.py fps.py ocr.py preflight.py selection.py timing.py
```

`tests/fakes.py` provides fake backends, which is what makes the whole pipeline
(`record -> simplify -> bench -> report`, including the CLI) testable with no GPU,
no permissions and no game. `tests/test_win_types.py` additionally checks the
Win32 structure layouts against the documented sizes and offsets, because a wrong
offset is the classic reason `SendInput` fails on a real machine.

What still has to be verified on real hardware (a sandbox cannot inject input or
capture the screen):

* macOS: `scripts/bench.sh doctor` -> `dry-run` -> `record` -> `bench`, with
  Accessibility granted (Screen Recording only if you want the OCR fallback).
  `metalperftrace` itself has been exercised against a synthetic Metal window on
  macOS 27 (see [Related work](docs/RELATED-WORK.md)), including the per-second
  interval statistics and their reconstruction.
* macOS games: whether a real title's Metal layer is reported by `metalperftrace`
  the same way a plain `CAMetalLayer` window is, and how the interval statistics
  look when the game is throttled.
* Windows 11: `scripts\bench.ps1 doctor` -> the same, plus confirming that
  PresentMon is found and that Raw Input recording captures real turn deltas.
