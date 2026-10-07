#!/usr/bin/env bash
# Build (and optionally sign) the CoreHID virtual gamepad helper.
#
#   helper/build.sh                          # build only (device creation will fail)
#   helper/build.sh --sign "Developer ID Application: Your Name (TEAMID)"
#   helper/build.sh --sign -                 # ad-hoc: AMFI kills it (see README)
#
# Signing needs the com.apple.developer.hid.virtual.device entitlement granted to
# the signing identity; see helper/README.md.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
identity=""
target_macos="15.0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sign) identity="${2:?--sign needs an identity}"; shift 2 ;;
    --target) target_macos="${2:?--target needs a version}"; shift 2 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if ! command -v swiftc >/dev/null 2>&1; then
  echo "swiftc not found: install Xcode or the Command Line Tools (xcode-select --install)" >&2
  exit 1
fi

out="$root/helper/fpsbench-gamepad-helper"
swiftc -O -parse-as-library -target "arm64-apple-macos${target_macos}" \
  "$here/fpsbench-gamepad-helper.swift" -o "$out"
echo "built $out"

if [[ -z "$identity" ]]; then
  echo "unsigned: HIDVirtualDevice(properties:) returns nil at runtime."
  echo "re-run with --sign \"Developer ID Application: ... (TEAMID)\" once the entitlement is granted."
  exit 0
fi

codesign --force --sign "$identity" \
  --entitlements "$here/entitlements.plist" \
  --options runtime --timestamp "$out"
echo "signed $out"
codesign --verify --strict --verbose=2 "$out" || true
codesign -d --entitlements - "$out" || true
