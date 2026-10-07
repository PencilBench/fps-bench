# Related work: does this already exist?

This document records the survey behind fpsbench's design decisions, so the same
research does not have to be repeated. It answers one question — *is there an
existing tool that records a route, replays it into an unmodified game and reports
per-segment FPS?* — and then maps every reusable building block that was found.

Survey date: **2026-10-06**. Ecosystem moved fast around this date (PresentMon
2.6.0 shipped 2026-09-21, CapFrameX 1.9.2 on 2026-10-04), so re-check the versions
below before relying on them.

Claims are marked where they are *not* measured facts — see
[Not verified / open questions](#not-verified--open-questions) before quoting
anything as established. The two Apple-specific sections (`metalperftrace`, Metal
Performance HUD) were read from Apple's own documentation plus local probing, and
say exactly what was and was not exercised.

## Verdict

**No single project does the whole thing.** The ecosystem splits into two mature
halves that mostly ignore each other:

* **FPS/frametime capture** — production-grade on Windows (Intel PresentMon,
  CapFrameX) and, new in macOS 27, available on macOS too (Apple's
  `metalperftrace`).
* **Input automation** — cross-platform raw-mouse capture and injection is a
  solved problem (beamer, Mouser, Interception), but it is always used for
  remote control or key remapping, never to drive a scripted benchmark route.

Nothing was found that connects them. GitHub's "benchmark automation" label
currently means *clicking the game's own benchmark button for you*, which is
precisely the case fpsbench exists to cover: games that have no such button.

## Reusable building blocks

Worth adopting rather than rewriting. fpsbench already uses PresentMon (Windows)
and `metalperftrace` (macOS 27+, see [the follow-up](#follow-up-implemented)), so
this is the list to check before writing any further capture or injection code.

| Project | Platform | License | Activity at survey | What it gives you |
|---|---|---|---|---|
| [Intel PresentMon](https://github.com/GameTechDev/PresentMon) | Windows | MIT | v2.6.0 (2026-09-21), very active | ETW per-frame present timing. CLI: `--process_name`, `--output_file`, `--timed`, `--delay`, `--hotkey`, `--terminate_on_proc_exit`; column `MsBetweenPresents` = interval between consecutive `Present()` calls. Also `MsBetweenDisplayChange`, CPU/GPU frame durations. Needs membership in *Performance Log Users* or admin. |
| [CapFrameX](https://github.com/CXWorld/CapFrameX) | Windows | MIT (API reports NOASSERTION, likely bundled third-party) | v1.9.2 Beta (2026-10-04) | Frametime capture (bundles PresentMon 2.6.0), hardware telemetry, overlay, charts, run comparison. **New: ships an MCP server** (`source/CapFrameX.Mcp/Tools/`: `CaptureLifecycleTools`, `FrametimeTools`, `MetricsTools`, …) — the closest thing to a programmatic capture API on Windows. Does **not** automate input (capture is hotkey/timer triggered). |
| [System Informer](https://github.com/winsiderss/systeminformer) | Windows | MIT | very active | `plugins/ExtendedTools/framemon.cpp` consumes the **DXGI ETW provider** directly and computes per-process FPS from `MsBetweenPresents`. A clean MIT reference if you ever want to drop the PresentMon executable. |
| [benchlab](https://github.com/r3clusionn/bench-framework) | Windows | MIT | v0.3.0 (2026) | `benchlab trace record --preset game` captures kernel ETW plus "the graphics providers PresentMon uses"; `trace analyze` reports avg/min FPS, 1%/0.1% lows, stutters and slow-frame attribution. Capture is Windows-only, no input automation. |
| [AMD OCAT](https://github.com/GPUOpen-Tools/ocat) | Windows | **no LICENSE file** | dormant, last release v1.6.3 (2023-09-27) | D3D11/D3D12/Vulkan overlay, vendors PresentMon. Not a good dependency: unlicensed and unmaintained. |
| NVIDIA FrameView / [RTSS](https://www.guru3d.com/files-details/rtss-rivatuner-statistics-server-download.html) | Windows | closed-source freeware | active | FrameView and RTSS are the OSDs most OCR-based tools would read. RTSS is the de-facto FPS overlay source. |
| [BoringBoredom/PC-Optimization-Hub](https://github.com/BoringBoredom/PC-Optimization-Hub) | — | — | active | Not a tool: the community's written benchmark methodology (points at FrameView/PresentMon for frametimes and latency). Useful for wording the methodology section. |

## macOS: `metalperftrace` is the PresentMon analogue

The largest finding of this survey, and the one that most affects fpsbench: as of
**macOS 27**, the OS ships `/usr/bin/metalperftrace`, which reads
always-on-recorded Metal frame timing.

> "collects and inspects always-on recorded Metal graphics performance data. The
> system continuously records frame timing and related statistics for all Metal
> layers." — `man metalperftrace` (macOS 27)

Subcommands:

| Subcommand | Relevance |
|---|---|
| `collect [--last D \| --start D --end D] [--json] OUTDIR` | writes `.atrc` traces (openable in Instruments) |
| `overview [--json] [--json-include-timeline] [--aggregate --start --end] [--domain --state-label] TRACE` | **per-frame timeline**, and `--aggregate --start/--end` gives native per-window aggregation |
| `listen [--json] [--interval S] [--output FILE] [--process NAME] [--pid PID]` | live NDJSON stream, min interval 1.0 s |
| `setup --enable per-frame-metrics \| hud --pid PID` | enable per-frame signposts / the Metal HUD in a running process |

Verified on the development machine (macOS 27.0.1, Apple M6):

* ✅ the binary and its man page exist at the documented path;
* ✅ `metalperftrace collect --last 10s .` succeeded and wrote a real
  `MetalPerfTrace_*.atrc` file;
* ✅ `overview` parsed the trace (it reported "No session found", correct for a
  machine that was idle and running no Metal game);
* ✅ a synthetic `MTKView` window produced a live session: `listen --json` then
  reported per-second Metal interval statistics (`Count`, `Total/Min/Max/Average/
  StdDev (ms)`), and `overview --json-include-timeline` returned the same numbers
  as a timeline of 1 s intervals (no individual frame times);
* ⚠️ `listen --json` emitted nothing while a headless Metal compute workload ran.
  Metal frame timing is recorded **per layer/drawable**: a headless
  `MTLComputeCommandEncoder` loop has no `CAMetalLayer`, so there is nothing to
  time. Re-probed later with a synthetic `MTKMetalView` window (see
  [the follow-up](#follow-up-implemented)): the schema is now characterised, and
  an occluded/background window is reported with a frame count of 0.

Constraints and caveats:

* **Metal only.** Native OpenGL layers are not covered, but macOS games and
  CrossOver/GPTK-translated D3D titles go through Metal, so real-world coverage is
  broad.
* **macOS 27+ / Apple Silicon.** Only a macOS 27 man-page snapshot could be found,
  and the two projects below require macOS 27.0. Treat "macOS 27+" as the safe
  statement; the exact minimum version is **not independently verified**.
* **Verified in production use by two projects:**
  [zzoko/PerformanceHUD](https://github.com/zzoko/PerformanceHUD)
  (`MetalPerfTraceMonitor.swift` spawns `listen --json --pid <pid> --interval 1`)
  and [Acerola-1/hagimi-monitor](https://github.com/Acerola-1/hagimi-monitor)
  (same call, defensively no-ops when the binary is missing). PerformanceHUD
  notes it "does not use screen recording or pixel capture".

Related, same platform: the **Metal Performance HUD**, covered in its own section
[below](#macos-using-the-metal-performance-hud-to-measure-fps) — its FPS readout is
a 120-frame rolling average, and only its per-frame logging is automatable.

Also worth noting as an explicit confirmation of the macOS gap:
[LANCommander/GameOverlay.Capture](https://github.com/LANCommander/GameOverlay.Capture)
describes its Windows path (Windows Graphics Capture + PresentMon/ETW) as "fully
implemented and verified", Linux via MangoHud, and macOS as "`screencapture`
(video only in v1); the richer ScreenCaptureKit/AVFoundation backend is future
work".

On Linux the whole capture half is [MangoHud](https://github.com/flightlessmango/MangoHud)
(MIT, Vulkan/OpenGL layer, FPS logging) plus `mangoplot` — out of scope here, but
it is the nearest equivalent if fpsbench ever grows a third platform.

## macOS: using the Metal Performance HUD to measure FPS

The HUD is the *interactive* face of the same subsystem as `metalperftrace`, and
because it is the obvious thing to reach for when someone says "measure FPS on a
Mac", the exact semantics matter — two of them contradict how people usually use it.

**What the FPS number actually is.** Apple: *"Shows the rolling average of frames
per second for the past 120 frames. FPS is calculated by dividing 1 second with
the frame interval."* So the on-screen number is a **120-frame sliding window**,
not an average over your run. Averaging OCR readings of it is *not* the same as
the run's average FPS — the window smooths away exactly the stutters you are
trying to detect. ([metrics doc](https://developer.apple.com/documentation/xcode/understanding-metal-performance-hud-metrics))

Consequence: for a per-run average, OCR of the HUD number is the *worst* of the
three HUD routes, even though it is the easiest. Related knobs: `MTL_HUD_ELEMENTS`
selects which metrics appear, and `MTL_HUD_SHOW_METRICS_RANGE` reports the range
of metrics for the **last 1200 frames**.

### Route 1 — performance reports (true window average, but manually triggered)

*"When report generation begins, the system resets all existing metrics, and the
HUD automatically enables the performance insights feature."* Triggered from the
Metal HUD menu, duration **5 s – 30 min**, with a **3-second delay** before metrics
reset and collection starts; afterwards the report is written to the app's
temporary folder, or to the path in `MTL_HUD_REPORT_URL`
([reports doc](https://developer.apple.com/documentation/xcode/generating-performance-reports-with-metal-performance-hud)).

Because the metrics reset at the start, the report's average **is** the average for
the collection window — which aligns naturally with a route run. Relevant sections:

| Report section | Gives you |
|---|---|
| Frame interval distribution | frame interval histogram + "frame rate statistics, such as 99% high and **1% low**" |
| Performance metrics | HUD metrics with **average, minimum and maximum** ← the average FPS |
| Frame timing / Frame encoding | per-frame CPU+GPU command buffer and encoder tables (last frame of the report) |

**The catch, and it is decisive for automation:** report generation is started from
a **menu**, not by an environment variable. There is no documented way to trigger a
report programmatically, so this route cannot be driven by a script or a
`FpsBackend`. It improves measurement *quality* while staying manual.

### Route 2 — per-frame logging (the only automatable HUD route)

`MTL_HUD_LOG_ENABLED` *"Turns on logging for per-frame statistics."*
[MetalHUDToggle](https://github.com/jpcarvalhodev/MetalHUDToggle) — a working
third-party toggler — states those frame-stat logs land in the **unified log,
filterable in Console by `metal-HUD`**, which makes them capturable:

```bash
launchctl setenv MTL_HUD_ENABLED 1
launchctl setenv MTL_HUD_LOG_ENABLED 1
log stream --predicate 'eventMessage CONTAINS "metal-HUD"' --style compact > run.log
```

If the logged lines carry per-frame intervals, this is the only HUD path that
yields true per-frame data (so you can compute average, p99 and 1% low yourself,
PresentMon-style). **Flagged as unverified:** the exact log line format, and
whether this variable is deprecated — Apple's *Customizing* page still documents
it, but it is widely described as superseded by reports.

### Environment variables (from Apple's *Customizing* page)

`MTL_HUD_ENABLED` (the switch), `MTL_HUD_ELEMENTS` (metric selection),
`MTL_HUD_SCALE`, `MTL_HUD_OPACITY`, `MTL_HUD_ALIGNMENT`, `MTL_HUD_POSITION_X/Y`,
`MTL_HUD_SHOW_ZERO_METRICS`, `MTL_HUD_SHOW_METRICS_RANGE`, `MTL_HUD_METRIC_TIMEOUT`,
`MTL_HUD_LOG_ENABLED`, `MTL_HUD_LOG_SHADER_ENABLED`, `MTL_HUD_INSIGHTS_ENABLED`,
`MTL_HUD_INSIGHT_REPORT_INTERVAL`, `MTL_HUD_INSIGHT_TIMEOUT`,
`MTL_HUD_RUSAGE_UPDATE_INTERVAL`, `MTL_HUD_ENCODER_TIMING_ENABLED`,
`MTL_HUD_ENCODER_GPU_TIMELINE_FRAME_COUNT`, `MTL_HUD_ENCODER_GPU_TIMELINE_SWAP_DELTA`,
`MTL_HUD_REPORT_URL`, `MTL_HUD_CONFIG_FILE`, `MTL_HUD_DISABLE_MENU_BAR`,
`MTL_HUD_TEMP_STATE`. Full detail:
[customizing doc](https://developer.apple.com/documentation/xcode/customizing-metal-performance-hud).

Note `MTL_HUD_ENABLED` itself does **not** appear in either Apple page read for
this survey; its spelling comes from MetalHUDToggle and general usage. The
authoritative self-check is inside the HUD itself: the **Metal HUD menu → "Copy HUD
Configuration"** exports the exact variable set for the current state, and the
configuration panel (**opened by triple-clicking the HUD overlay**) has *Export HUD
Configuration*, producing a plist loadable via `MTL_HUD_CONFIG_FILE`.

### Operational traps

| Trap | Detail |
|---|---|
| Read once, at launch | Metal apps read these at startup: changing them only affects apps opened afterwards, so quit the game first. `MTL_HUD_ENABLED=1 %command%` in Steam launch options is cleaner than a global `launchctl setenv`. |
| `launchctl setenv` is not persistent | It does not survive a reboot (MetalHUDToggle has a "start at login with HUD on" mode for that reason). |
| Sandboxed games | `MTL_HUD_REPORT_URL` must be a path the *app* can write — inside its container. |
| No relaunch needed (sometimes) | `metalperftrace setup --enable hud --pid <pid>` enables the HUD in a **running** process. Documented by the tool; not verified against a real game. |

## Closest projects, and exactly why each is not enough

| Project | Platform | Covers | Gap |
|---|---|---|---|
| [YpS-YpS/Katana](https://github.com/YpS-YpS/Katana) | Windows, Python | Genuinely a "Game Benchmark Automation Framework": launches the game, CV template matching to navigate UI (`wait_for_template("cs2_fps_benchmark.png")`), runs the benchmark, standardises results | Navigates to the game's **built-in** benchmark map; no route record/replay; no FPS measurement of its own. No LICENSE. |
| [Ark0N/-CS2-Benchmark-Automation](https://github.com/Ark0N/-CS2-Benchmark-Automation) | Windows, AutoHotkey | One-click CS2 benchmark: launch, wait, start CapFrameX at a fixed offset, quit | Depends on the game's own benchmark map; no input route; single game. |
| [Pingsun1/benchmark-automation-toolkit](https://github.com/Pingsun1/benchmark-automation-toolkit) | Windows | N repeated runs, triggers CapFrameX, converts CapFrameX JSON → TSV | Assumes a built-in benchmark; no input replay. |
| [GuneyCel/Universal-Benchmarker](https://github.com/GuneyCel/Universal-Benchmarker) | Windows, Python + OpenCV | Closest in *intent*: CV-driven "autonomously driving gameplay in AAA titles while capturing real-time performance telemetry", frametimes, 1% lows, stutter tagging | **Telemetry core explicitly withheld from source**; no record/replay (adaptive CV clicking instead); Windows-only; portfolio piece. |
| [luppichristian/GameTest](https://github.com/luppichristian/GameTest) | Win32, C11, MIT | A real record/replay framework: captures keyboard, mouse *and* gamepad, injects deterministically on replay | **Only for games you build and link yourself** (SDL, in-process) — cannot target an unmodified retail game. |
| [brucethemoose/Minecraft-Performance-Flags-Benchmarks](https://github.com/brucethemoose/Minecraft-Performance-Flags-Benchmarks) | Windows, Python | Best working example of the *capture* half: spawns `presentmon.exe -process_name javaw.exe -output_file … -terminate_on_proc_exit` and parses `msBetweenPresents` into frametimes | Game is driven by scripted in-game commands, not input replay; single game; no LICENSE. |
| [rixbrnn/frame_inspect_tool](https://github.com/rixbrnn/frame_inspect_tool) | Python, MIT, cross-platform | The **only** dedicated FPS-OCR project: reads the RTSS/Afterburner OSD FPS field from frames via EasyOCR (Tesseract fallback) with auto-ROI detection and JSON output | Operates **offline on recordings**, not live; targets RTSS/Afterburner OSDs. |
| `rthomasv3/AutoGameBench`, `JacobGCode/game-performance-dashboard` | C# / Python | Descriptions claim automated game benchmarking | **Unverified** — READMEs 404 at survey time. Do not cite. |

No project was found that OCRs a *live* FPS counter on either macOS or Windows.
Searches for `fps ocr overlay counter`, `ocr fps counter screenshot`,
`screen fps reading ocr` returned no relevant results.

## Input record/replay prior art

fpsbench's recorders follow the patterns proven here. Note that all of these are
remote-control or remapping tools — the *mechanism* transfers, the *purpose* does
not.

* **[kalkman-code/beamer](https://github.com/kalkman-code/beamer)** (GPL-3.0,
  macOS + Windows) — the most complete cross-platform reference for raw mouse
  capture **and** injection. macOS side: listen-only `CGEventTapCreate` plus
  `kCGMouseEventDeltaX/Y`. Windows side: `RegisterRawInputDevices` with
  `RIDEV_INPUTSINK` + `WM_INPUT`/`GetRawInputData`; injection via `SendInput` with
  `MOUSEEVENTF_MOVE` and explicit `dx`/`dy`. Two comments there are worth
  internalising:
  * Raw Input has **no injected flag**: a `SendInput` move reaches a `WM_INPUT`
    sink exactly as a real hand does, which is why this works at all;
  * **UIPI can silently drop `SendInput`** when a higher-integrity window holds the
    foreground — they had to restart a service to recover. fpsbench surfaces this
    in `doctor` for the same reason.
* **[TomBadash/Mouser](https://github.com/TomBadash/Mouser)** — same two techniques
  in Python/PyObjC (`core/mouse_hook_macos.py`, `core/mouse_hook_windows.py`), but
  it is a Logitech remapper.
* **[oblitum/Interception](https://github.com/oblitum/Interception)** (+
  `AutoHotInterception`, `pyinterception`) — Windows **kernel-level** input driver.
  The escalation path if a game filters injected input; unnecessary for most
  single-player titles, which is why fpsbench uses `SendInput` with scancodes.
* **[noah-nuebling/mac-mouse-fix](https://github.com/noah-nuebling/mac-mouse-fix)** —
  `PointerFreeze.m` implements pointer freeze / puppet-cursor via event taps and
  private CGS APIs. Source-available (MMF License), not OSI.
* **[pqrs-org/Karabiner-Elements](https://github.com/pqrs-org/Karabiner-Elements)**
  (Unlicense) — remaps at the IOHID driver level and can emit synthetic HID
  events; the deepest macOS injection path, but config-driven, not a replay engine.
* **[Hammerspoon](https://github.com/Hammerspoon/hammerspoon)** (MIT) —
  `hs.eventtap` offers listen + post if you want a scripting layer.
* **[BlueM/cliclick](https://github.com/BlueM/cliclick)** — posts CGEvents with
  easing, but **absolute positions only** and no relative/delta mode, so it is not
  game-grade.

Python input libraries, and why fpsbench does not use them:

| Library | Verdict |
|---|---|
| [pydirectinput](https://github.com/learncodebygaming/pydirectinput) | `SendInput` + DirectInput **scan codes** — the right idea, but last pushed 2023-07-04, no LICENSE file, and it lacks scroll, drag and "hold mouse button while moving", which is exactly what continuous mouse-look needs. |
| [pyautogui](https://github.com/asweigart/pyautogui) | On Windows uses the deprecated `mouse_event`/`keybd_event`; its own docs concede this "may not work in some applications, particularly in video games and other software that rely on DirectX". `moveRel` is implemented as absolute repositioning. |
| [pynput](https://github.com/moses-palmer/pynput) | Cross-platform control + monitoring, but the docs warn that on Windows "virtual events sent by other processes may not be received" and that "the operating system does not consider the buttons to be truly pressed" — fatal for held-key WASD movement. |

## Positioning: how fpsbench differs

| | PresentMon / CapFrameX | Katana / CS2 wrappers | beamer / Mouser | Universal-Benchmarker | **fpsbench** |
|---|---|---|---|---|---|
| Works in a game with **no** benchmark mode | n/a (capture only) | ✗ needs built-in benchmark | n/a (not a benchmark tool) | ✓ (CV-driven) | **✓ route-based** |
| Records your own route | ✗ | ✗ | ✓ (as remote control) | ✗ | **✓** |
| Replays it with a drift-free timeline | ✗ | ✗ | partial | ✗ (adaptive CV) | **✓** |
| Per-segment FPS along the route | ✗ | ✗ | ✗ | ✗ | **✓ label hotkeys** |
| macOS **and** Windows 11 | ✗ (Win only) | ✗ (Win only) | ✓ | ✗ (Win only) | **✓** |
| Open source, source-complete | ✓ | partial | ✓ (GPL-3.0) | ✗ (core withheld) | ✓ MIT |

The two things that appear to be genuinely novel here are (a) **time-labelled
segments along a recorded route** (`ctrl+alt+1..9` on both macOS and Windows,
with `ctrl+alt+enter` starting recording and benchmarking on demand, once the game
is in position) so FPS can be attributed to a piece of the map, and (b) putting
**record → replay → measure → per-segment report** in one cross-platform harness.
Each piece is prior art; the assembly is not.

## External dependencies and licensing

fpsbench itself is MIT (`pyproject.toml`), but two facts matter when documenting
or redistributing it:

* **PresentMon is not bundled.** It is an external executable the user installs;
  `doctor` locates it and reports what it tried. MIT, so redistribution would be
  possible, but shipping a binary is a release-engineering decision, not a
  dependency.
* **`metalperftrace` is an OS component**, invoked as a subprocess. Nothing is
  vendored, so no additional licence obligation.
* **Reference code is not copied.** beamer is **GPL-3.0**; its patterns were
  studied, not its code. Keep it that way — copying GPL source into this MIT
  project would relicense it.
* There is **no `LICENSE` file in the repository yet**, although `pyproject.toml`
  declares MIT. Adding one is a prerequisite for publishing.

## Not verified / open questions

Listed so the next person does not mistake a survey claim for a measured fact.

1. **`metalperftrace --json` schema** — *settled* (see
   [the follow-up](#follow-up-implemented)): the live updates carry per-layer
   1 s interval statistics, and `--output FILE` is the per-update NDJSON channel
   fpsbench reads. Still unverified: whether it needs a permission grant, and its
   exact minimum OS version (macOS 27.0 is the lowest tested).
2. **Does any native Apple-Silicon game accept posted relative mouse deltas?**
   beamer proves capture + injection works Mac↔PC, but not that a game's raw-HID
   path honours synthetic events. Games that read `IOHIDDevice` directly may ignore
   them. This is the single biggest technical risk for the macOS route replay and
   needs an empirical test against a real title.
3. **Metal Performance HUD report file format** — Apple documents the report's
   *sections* (frame interval distribution, performance metrics with avg/min/max,
   performance insights, frame timing/encoding), but the file it writes was never
   inspected. Neither was: whether a report can be triggered without the menu (it
   appears not), and the exact format of `MTL_HUD_LOG_ENABLED` output, which is
   also widely described as deprecated in favour of reports. `MTL_HUD_ENABLED`
   itself does not appear in either Apple page read for this survey — its spelling
   comes from MetalHUDToggle and general usage, not from the documentation.
4. **NVIDIA FrameView specifics** — whether it exposes a command line was not
   confirmed. RTSS version numbers likewise not verified.
5. **Licence status** of `AMD OCAT` (no LICENSE file), `pydirectinput` (API
   NOASSERTION), `Katana`, `benchmark-automation-toolkit`, `Ark0N`, `GuneyCel`
   (none stated), `Minecraft-Performance-Flags-Benchmarks` (no LICENSE →
   all rights reserved by default), `cliclick` (NOASSERTION).
6. **Search coverage** — GitHub repository search is AND-semantics over
   name/description/README, so unusual naming can hide a match. Suggested
   follow-ups: `topic:game-benchmark`, `topic:input-recording`,
   `topic:macro-recorder`, `"timedemo" benchmark automation`,
   `"no benchmark mode"`, and the sim-racing / speedrun route tooling communities.
7. Repositories whose READMEs 404'd are **not** cited as working tools anywhere in
   this project.

## Why this matters for fpsbench's design

Two decisions in this repository are direct consequences of the survey:

* **No screen-capture frame counting.** It is capped by the refresh rate and the
  capture competes with the game for GPU time — see [Known limitations](../README.md#known-limitations).
  PresentMon (Windows) and `metalperftrace` (macOS 27) both avoid this; overlay OCR
  is the fallback when neither is available.
* **Overlay OCR is a shared fallback, not a platform hack.** The only prior art
  ([frame_inspect_tool](https://github.com/rixbrnn/frame_inspect_tool)) is offline
  and OSD-specific; fpsbench applies the same OCR parsing to a live counter on
  both platforms, which is why `core/ocrparse.py` carries the step-hold and
  outlier logic. It is now the *second* choice on both platforms rather than the
  only option on macOS.

### Follow-up: implemented

The most valuable next step suggested by this survey — a **`metalperftrace` FPS
backend on macOS 27+**, making the platform matrix symmetric with Windows
(`PresentMon` → `metalperftrace` → OCR, rather than macOS being OCR-only) — **is now
implemented** in `fpsbench/backends/_metalperftrace.py`. It gives frame timing on
macOS *without* screen-recording permission, without a visible counter and without
screen capture (so no GPU contention).

Implementing it settled open question 1 above (the JSON schema) and turned up two
things the documentation does not mention:

* **`listen --json` writes pretty-printed objects to stdout and block-buffers it**
  (16 KiB, so updates arrive in multi-second batches). The `--output FILE` it also
  supports is appended as one compact object per line, per update — that is the
  channel fpsbench tails, exactly like PresentMon's CSV.
* **There is no per-frame timeline.** `overview --json-include-timeline` (and each
  live update) carries one *interval* of statistics per second — `Count`,
  `Total (ms)`, `Min/Max/Average/StdDev (ms)` of the frame-on-glass interval — not
  individual frame times. fpsbench therefore reconstructs a frame list that
  preserves count, total and instantaneous min/max and puts the rest at the
  interval mean; the average is exact, the percentiles are approximations bounded
  by the 1 s interval, and the report says so. PresentMon remains the only source
  that sees every present.

Each interval also carries `Start`/`End Date` plus Mach-continuous seconds, which
is what lets a late (buffered) update be stamped at the moment it was *recorded*,
and the HUD's 3 s/menu-triggered report is not needed at all.

The macOS ranking below is therefore now about *which source fpsbench uses*:
`metalperftrace` is the default, and the next two rows are the fallback story.

For the macOS side there is a three-way ranking, by how trustworthy the resulting
*average FPS* is:

| Route | Average is | Automatable |
|---|---|---|
| `metalperftrace listen/overview --json` | true average FPS over each 1 s interval (frame-count weighted) | ✅ fully — this is what fpsbench uses |
| HUD performance report | true average of the collection window (metrics reset at start) | ⚠️ menu-triggered only, 3 s delay, 5 s–30 min |
| OCR of the HUD's FPS readout | a 120-frame rolling average | ✅ fully, but the number is the wrong kind of average |

The HUD's per-frame **logging** (`MTL_HUD_LOG_ENABLED`) would sit between the first
two rows *if* its output turns out to be parseable — that is question 3 above.
