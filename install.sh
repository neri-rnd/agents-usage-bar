#!/bin/bash
# One-shot installer for ai_monitor.
#
# What it does:
#   1. Verifies Python 3.11+ and a working Swift compiler
#   2. Installs the `monitor` Python package (pip --user, with PEP-668 escape)
#   3. Adds the pip-user bin dir to PATH in your shell rc (idempotent)
#   4. Builds the Swift menubar app via swift/build.sh
#   5. Copies it to /Applications/ (killing any running instance first)
#   6. Launches it
#
# Re-run anytime — every step is idempotent.

set -euo pipefail

DIR="$(cd -P "$(dirname "$0")" && pwd)"
APP_NAME="AI Monitor"
APP_PATH="/Applications/${APP_NAME}.app"

step()  { printf "\n\033[1;36m▸ %s\033[0m\n" "$*"; }
ok()    { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn()  { printf "  \033[33m!\033[0m %s\n" "$*"; }
fail()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

# ----------------------------------------------------------------------------
step "1/5  Checking prerequisites"

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not found. Install Python 3.11+ (brew install python@3.13 or similar)."
fi
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PYMAJOR=$(echo "$PYVER" | cut -d. -f1)
PYMINOR=$(echo "$PYVER" | cut -d. -f2)
if [[ "$PYMAJOR" -lt 3 || ("$PYMAJOR" -eq 3 && "$PYMINOR" -lt 11) ]]; then
  fail "python3 $PYVER too old. Need 3.11+ (we use stdlib tomllib)."
fi
ok "python3 $PYVER"

if ! command -v swiftc >/dev/null 2>&1; then
  fail "swiftc not found. Install Xcode Command Line Tools: xcode-select --install"
fi
ok "swift $(swift --version 2>&1 | head -1 | awk '{print $4}')"

# ----------------------------------------------------------------------------
step "2/5  Installing Python package (pip --user)"

# --break-system-packages is required on Homebrew Python 3.13+ (PEP 668)
# and harmless on other Pythons. --user keeps it out of system site-packages.
PIP_FLAGS="--user --break-system-packages"

if python3 -m pip install $PIP_FLAGS -e "$DIR" >/tmp/ai-monitor-pip.log 2>&1; then
  ok "installed ai_monitor (editable)"
else
  warn "pip install failed — full log at /tmp/ai-monitor-pip.log"
  tail -5 /tmp/ai-monitor-pip.log
  exit 1
fi

USER_BIN="$(python3 -m site --user-base)/bin"
if [[ ! -x "$USER_BIN/monitor" ]]; then
  fail "monitor binary not found at $USER_BIN/monitor — pip install didn't place it where expected"
fi
ok "monitor binary at $USER_BIN/monitor"

# ----------------------------------------------------------------------------
step "3/5  Ensuring PATH covers $USER_BIN"

case "${SHELL##*/}" in
  zsh)  RC="$HOME/.zshrc" ;;
  bash) RC="$HOME/.bashrc" ;;
  *)    RC="$HOME/.profile" ;;
esac

MARKER="# ai_monitor — added by install.sh"
if [[ -f "$RC" ]] && grep -qF "$MARKER" "$RC"; then
  ok "PATH line already present in $RC"
elif echo ":$PATH:" | grep -qF ":$USER_BIN:"; then
  ok "$USER_BIN already on \$PATH"
else
  printf "\n%s\nexport PATH=\"%s:\$PATH\"\n" "$MARKER" "$USER_BIN" >> "$RC"
  ok "appended PATH line to $RC (new shells will pick it up)"
fi

# ----------------------------------------------------------------------------
step "4/5  Building Swift menubar app"

(cd "$DIR/swift" && ./build.sh) >/tmp/ai-monitor-build.log 2>&1 || {
  warn "build failed — full log at /tmp/ai-monitor-build.log"
  tail -10 /tmp/ai-monitor-build.log
  exit 1
}
BUILT_APP="$DIR/swift/build/${APP_NAME}.app"
if [[ ! -d "$BUILT_APP" ]]; then
  fail "build claimed success but $BUILT_APP not found"
fi
ok "built $BUILT_APP"

# ----------------------------------------------------------------------------
step "5/5  Installing to /Applications and launching"

if pgrep -f "${APP_NAME}.app/Contents/MacOS/AIMonitor" >/dev/null 2>&1; then
  pkill -f "${APP_NAME}.app/Contents/MacOS/AIMonitor" || true
  sleep 1
  ok "stopped running instance"
fi

rm -rf "$APP_PATH"
cp -R "$BUILT_APP" "$APP_PATH"
ok "installed to $APP_PATH"

# Strip the macOS quarantine attribute so unsigned-app warning is one-time
# only if the user re-downloaded the source; for local builds it's harmless.
xattr -cr "$APP_PATH" 2>/dev/null || true

open "$APP_PATH"
sleep 1
if pgrep -f "${APP_NAME}.app/Contents/MacOS/AIMonitor" >/dev/null 2>&1; then
  ok "launched — check your menubar"
else
  warn "open command issued but process not detected; if macOS blocked it:"
  warn "  System Settings → Privacy & Security → 'Open Anyway'"
fi

echo
echo "Done. The app polls 'monitor refresh' every 30s; first refresh writes"
echo "/tmp/ai-monitor/state.json (also fetches Claude OAuth /usage if signed in)."
echo
echo "Next steps:"
echo "  monitor doctor                    # health check"
echo "  monitor doctor --write-config     # optional starter config"
echo
echo "Run ./uninstall.sh to remove everything."
