#!/bin/bash
# Build the macOS disk image.
#
# Why a DMG and not just a zip: a zip leaves the app in ~/Downloads, and macOS
# then runs it *translocated* — from a randomised read-only copy. Permissions
# the user grants (Accessibility, Local Network) are recorded against the path
# they granted them for, so they do not apply to the copy that is running, and
# the helper fails silently. Dragging the app to /Applications in Finder is what
# clears translocation, so the install window is not decoration: it is the fix.
#
# Layout inside the image: the app plus a symlink to /Applications, side by side,
# which is the idiom every Mac user already knows.
#
# Usage: ./make-dmg.sh <path-to-.app> <output.dmg>
set -euo pipefail

APP=${1:?usage: make-dmg.sh <app> <out.dmg>}
OUT=${2:?usage: make-dmg.sh <app> <out.dmg>}
VOL="V-Pad Helper"
ID="Developer ID Application: SAMET VOLKAN CAVUSOGLU (X44QH5PVRJ)"

[ -d "$APP" ] || { echo "no such app: $APP" >&2; exit 1; }

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE" "$OUT.tmp.dmg" 2>/dev/null || true' EXIT

echo "› staging"
/usr/bin/ditto "$APP" "$STAGE/$(basename "$APP")"
ln -s /Applications "$STAGE/Applications"

# Read-write first so Finder can be asked to lay the window out, then compress.
echo "› creating read-write image"
rm -f "$OUT" "$OUT.tmp.dmg"
hdiutil create -quiet -srcfolder "$STAGE" -volname "$VOL" \
  -fs HFS+ -format UDRW -ov "$OUT.tmp.dmg"

echo "› arranging the install window"
MOUNT=$(hdiutil attach -readwrite -noverify -noautoopen "$OUT.tmp.dmg" \
        | grep -o '/Volumes/.*' | head -1)
# Finder automation can be unavailable (no permission, headless session). The
# window layout is a nicety; a plain icon view still shows both items and still
# lets the user drag. So a failure here must not fail the build.
osascript <<EOF 2>/dev/null || echo "  (Finder layout skipped — image is still valid)"
tell application "Finder"
  tell disk "$VOL"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {200, 160, 700, 480}
    set opts to the icon view options of container window
    set arrangement of opts to not arranged
    set icon size of opts to 96
    set position of item "$(basename "$APP")" of container window to {130, 150}
    set position of item "Applications" of container window to {370, 150}
    close
  end tell
end tell
EOF
sync
hdiutil detach -quiet "$MOUNT"

echo "› compressing"
hdiutil convert -quiet "$OUT.tmp.dmg" -format UDZO -imagekey zlib-level=9 -o "$OUT"

echo "› signing the image"
codesign --force --timestamp --sign "$ID" "$OUT"
codesign --verify --verbose=2 "$OUT" 2>&1 | tail -2

echo "✓ $OUT  ($(du -h "$OUT" | cut -f1))"
