#!/bin/bash
# Re-sign a PyInstaller .app with the Developer ID, ready for notarization.
#
# PyInstaller signs the bundle ad-hoc (TeamIdentifier=not set), which notarization
# rejects. Nested Mach-O files are signed first and the bundle last, because
# codesign seals what it finds: signing the outer bundle first and the insides
# afterwards invalidates the outer seal. (`--deep` appears to do this but Apple
# documents it as unsuitable for distribution — it cannot apply per-binary
# entitlements and silently skips some nested layouts.)
#
# Usage: ./sign-macos.sh <path-to-.app>
set -euo pipefail

APP=${1:?usage: sign-macos.sh <app>}
ID="Developer ID Application: SAMET VOLKAN CAVUSOGLU (X44QH5PVRJ)"
ENT="$(cd "$(dirname "$0")" && pwd)/entitlements.plist"

[ -d "$APP" ] || { echo "no such app: $APP" >&2; exit 1; }
[ -f "$ENT" ] || { echo "no entitlements at $ENT" >&2; exit 1; }

echo "› signing nested code"
# Every Mach-O inside, deepest first. `file` is the arbiter rather than the
# extension: a PyInstaller bundle carries plenty of unsuffixed executables.
find "$APP" -type f -print0 \
  | while IFS= read -r -d '' f; do
      case "$(file -b "$f" 2>/dev/null)" in
        *Mach-O*) printf '%s\0' "$f" ;;
      esac
    done \
  | xargs -0 -n1 -I{} codesign --force --timestamp --options runtime \
        --sign "$ID" {} >/dev/null 2>&1 || true

echo "› signing the bundle"
codesign --force --timestamp --options runtime --entitlements "$ENT" \
  --sign "$ID" "$APP"

echo "› verifying"
codesign --verify --deep --strict --verbose=2 "$APP" 2>&1 | tail -2
codesign -dv --verbose=2 "$APP" 2>&1 | grep -E "^Authority|^TeamIdentifier|flags"
