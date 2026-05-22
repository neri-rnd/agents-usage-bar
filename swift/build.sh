#!/bin/bash
# Build AI Monitor.app from the single-file Swift source.
# Output: build/AI Monitor.app
set -euo pipefail

DIR="$(cd -P "$(dirname "$0")" && pwd)"
BUILD="$DIR/build"
APP="$BUILD/AI Monitor.app"
BIN="$APP/Contents/MacOS/AIMonitor"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Compile
swiftc -O -parse-as-library \
  -target arm64-apple-macos13.0 \
  -framework AppKit -framework SwiftUI -framework Combine \
  "$DIR/AIMonitor.swift" \
  -o "$BIN"

cp "$DIR/Info.plist" "$APP/Contents/Info.plist"
# Bundle brand icons (lobehub mono SVGs rasterized to PNG@18 + @36).
if [[ -d "$DIR/Resources" ]]; then
  cp "$DIR/Resources/"*.png "$APP/Contents/Resources/" 2>/dev/null || true
fi
chmod +x "$BIN"

echo "Built: $APP"
echo ""
echo "To install:"
echo "  cp -R \"$APP\" /Applications/"
echo "  open \"/Applications/AI Monitor.app\""
