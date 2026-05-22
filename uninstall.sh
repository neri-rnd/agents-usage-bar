#!/bin/bash
# Mirror of install.sh — undoes everything it set up.
#
# Removes:
#   - /Applications/AI Monitor.app
#   - the running app process
#   - the pip --user package (`monitor` binary)
#   - /tmp/ai-monitor/ runtime state
#   - ~/.config/ai-monitor.toml (if you opted into config)
#   - the PATH line in ~/.zshrc / ~/.bashrc that install.sh added
#
# Leaves alone:
#   - the cloned source repo
#   - your shell history, other PATH entries, other dotfile contents

set -euo pipefail

APP_NAME="AI Monitor"
APP_PATH="/Applications/${APP_NAME}.app"

step()  { printf "\n\033[1;36m▸ %s\033[0m\n" "$*"; }
ok()    { printf "  \033[32m✓\033[0m %s\n" "$*"; }
note()  { printf "  \033[2m·\033[0m %s\n" "$*"; }

# ----------------------------------------------------------------------------
step "1/5  Stopping app + removing bundle"
if pgrep -f "${APP_NAME}.app/Contents/MacOS/AIMonitor" >/dev/null 2>&1; then
  pkill -f "${APP_NAME}.app/Contents/MacOS/AIMonitor" || true
  sleep 1
  ok "killed running app"
else
  note "app not running"
fi
if [[ -d "$APP_PATH" ]]; then
  rm -rf "$APP_PATH"
  ok "removed $APP_PATH"
else
  note "$APP_PATH not present"
fi

# ----------------------------------------------------------------------------
step "2/5  Uninstalling Python package"
if python3 -m pip show ai_monitor >/dev/null 2>&1; then
  python3 -m pip uninstall -y --break-system-packages ai_monitor >/dev/null 2>&1 || true
  ok "uninstalled ai_monitor"
else
  note "ai_monitor not installed via pip"
fi

# ----------------------------------------------------------------------------
step "3/5  Clearing runtime state"
if [[ -d /tmp/ai-monitor ]]; then
  rm -rf /tmp/ai-monitor
  ok "removed /tmp/ai-monitor"
else
  note "/tmp/ai-monitor not present"
fi

# ----------------------------------------------------------------------------
step "4/5  Removing user config"
if [[ -f "$HOME/.config/ai-monitor.toml" ]]; then
  rm -f "$HOME/.config/ai-monitor.toml"
  ok "removed ~/.config/ai-monitor.toml"
else
  note "~/.config/ai-monitor.toml not present"
fi

# ----------------------------------------------------------------------------
step "5/5  Cleaning PATH line from shell rc"
case "${SHELL##*/}" in
  zsh)  RC="$HOME/.zshrc" ;;
  bash) RC="$HOME/.bashrc" ;;
  *)    RC="$HOME/.profile" ;;
esac
MARKER="# ai_monitor — added by install.sh"
if [[ -f "$RC" ]] && grep -qF "$MARKER" "$RC"; then
  # Drop the marker line and the next line (the export PATH=...).
  # Portable: pipe through awk that skips the marker and the line after it.
  awk -v marker="$MARKER" '
    BEGIN { skip = 0 }
    {
      if ($0 == marker) { skip = 2 }
      if (skip > 0) { skip--; next }
      print
    }
  ' "$RC" > "$RC.ai-monitor.tmp" && mv "$RC.ai-monitor.tmp" "$RC"
  ok "cleaned PATH line from $RC"
else
  note "no install.sh marker in $RC"
fi

echo
echo "Done. The source repo at $(cd -P "$(dirname "$0")" && pwd) is untouched."
echo "Run ./install.sh to reinstall."
