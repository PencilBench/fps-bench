# macOS virtual gamepad helper (CoreHID `HIDVirtualDevice`)

fpsbench records a controller with SDL, but replaying it needs a **system-wide
virtual gamepad** - something another process (the game) can enumerate and read.
On macOS the only driver-free way to create one is CoreHID's `HIDVirtualDevice`,
and that API refuses to create a device unless the calling binary carries the

```
com.apple.developer.hid.virtual.device
```

entitlement. A Python interpreter cannot carry it, hence this small helper: it
owns the device and speaks JSON lines with `fpsbench/backends/macos/gamepad.py`.

## Status: verified compile, blocked on the entitlement

Measured on macOS 27.2.1 (Apple Silicon):

| binary | result |
|---|---|
| unsigned helper | `HIDVirtualDevice(properties:)` returns `nil`, helper exits 1 |
| ad-hoc signed (`codesign --sign -`) **with** the entitlement | process is killed by AMFI (`Killed: 9`) |

So the entitlement is **not installable**: it is a restricted entitlement that
Apple has to grant to your Team ID (or your app must be notarised/approved with
it). Everything else in this directory is ready for that moment.

## What you need from Apple

1. A paid **Apple Developer Program** membership.
2. An **explicit App ID** for the helper (e.g. `com.yourname.fpsbench-helper`)
   in *Certificates, Identifiers & Profiles*.
3. Check *Additional Capabilities* on that App ID for
   `HID Virtual Device` / `com.apple.developer.hid.virtual.device`.
   If it is not offered there, file an **entitlement request** with Apple
   (they gate virtual-HID entitlements; expect to describe the use case - a
   local benchmarking tool that replays recorded single-player input). There is
   no self-service switch for this one.
4. A **Developer ID Application** certificate (for distributing the helper) or
   an *Apple Development* certificate (for your own machine), plus a macOS
   provisioning profile for that App ID that contains the entitlement.
5. Sign with the entitlement **and** the profile:

```bash
# once the profile exists
cp ~/Downloads/fpsbench_helper.provisionprofile helper/embedded.provisionprofile

codesign --force --sign "Developer ID Application: Your Name (TEAMID)" \
  --entitlements helper/entitlements.plist \
  --options runtime --timestamp \
  helper/fpsbench-gamepad-helper
```

For a *Development* certificate the profile has to be embedded as
`Contents/embedded.provisionprofile` inside a bundle; the helper is a plain
executable, so Developer ID + the entitlement is the simpler route.

## Build and validate

```bash
helper/build.sh                     # compile only (still blocked at runtime)
helper/build.sh --sign "Developer ID Application: Your Name (TEAMID)"

# 1. the entitlement is actually in the signature
codesign -d --entitlements - helper/fpsbench-gamepad-helper

# 2. the device can be created (prints the virtual device on success)
echo '{"cmd":"hello"}' | helper/fpsbench-gamepad-helper

# 3. fpsbench end to end: reading AND injection
export FPSBENCH_GAMEPAD_HELPER="$PWD/helper/fpsbench-gamepad-helper"
scripts/bench.sh probe-gamepad --pulse
```

`probe-gamepad --pulse` sends a short stick/trigger/button pattern and centres it
again, so you can watch it in a second process (`gamepad-tool`, Steam's controller
test, or `hidutil list`) and then in the actual game.

## Protocol (keep in sync with the Python client)

One JSON object per line, one reply per line:

```text
-> {"cmd":"hello"}                       <- {"ok":true,"device":"CoreHID virtual gamepad (1209:0001)"}
-> {"cmd":"state","lx":0.5,"ly":-0.2,"rx":0,"ry":0,"l2":0.4,"r2":0,"buttons":["a"]}
                                          <- {"ok":true}
-> {"cmd":"neutral"}                     <- {"ok":true}
-> {"cmd":"quit"}                        <- {"ok":true}
```

`lx/ly/rx/ry` are `-1..1` (y **down**, SDL's convention), `l2/r2` are `0..1`,
`buttons` uses SDL's names (`a b x y leftshoulder rightshoulder dpup dpdown
dpleft dpright back start guide leftstick rightstick`). States are full
snapshots; the device holds the last report until the next one.

## Device identity

The helper presents a **generic 15-button / 6-axis game pad** (vendor/product
`0x1209:0x0001` by default, override with `FPSBENCH_GP_VID` / `FPSBENCH_GP_PID`).
That is enough for engines that accept a standard HID gamepad, but it is **not** a
DualSense: a game that checks for a DualSense (or reads its raw report format)
will not see one. Emulating a DualSense would mean shipping its exact report
descriptor and report layout; do that only once a target game is known to need it.

## If Apple declines the entitlement

* **DriverKit (`HIDDriverKit`)**: a virtual HID device implemented as a system
  extension - the route Karabiner-Elements uses. It needs no
  `com.apple.developer.hid.virtual.device`, but it does need the DriverKit
  entitlement, a dext to be approved for the machine, and the user to allow the
  system extension. Much bigger than a helper; out of scope here.
* **Windows**: `vgamepad` + ViGEmBus already works from Python (virtual Xbox 360
  pad). Replay the same trace there.
* **Keyboard/mouse route**: always available and needs no entitlement at all.
* SDL's in-process virtual joystick is **not** an option: it is only visible
  inside the process that created it, so the game cannot see it.
