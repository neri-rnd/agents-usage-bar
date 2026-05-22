# ai_monitor

Native macOS menubar app that tracks your **Claude Code** and **OpenAI Codex CLI** usage in real time.

- Per-agent 5h and weekly windows with progress bars
- Live "active session" indicator (✏️ writing now / 🟢 process alive — both visible internally; the UI shows them as just *active*)
- Per-project rollups with expandable session detail
- macOS notifications at 75% / 90% / 100% thresholds
- Auto-adapts to light/dark mode; brand icons in the menubar
- Auto-skips agents you don't have installed

## Architecture

```
swift/                          ← native SwiftUI menubar app (the UI)
  AIMonitor.swift                 single file, ~700 lines
  Resources/                      bundled lobehub brand icons
  Info.plist, build.sh

lib/ai_monitor/                 ← Python data layer (`monitor` CLI)
  cli.py                          monitor refresh / status / audit / doctor
  core/
    jsonl.py                      shared transcript parser
    processes.py                  ps/lsof helpers
    state.py                      MonitorState dataclass + atomic JSON write
    notify.py                     osascript notifications + dedup
    config.py                     TOML loader (~/.config/ai-monitor.toml)
  agents/
    base.py                       Agent ABC + dataclasses
    claude.py                     OAuth /usage fetch (5min sticky cache),
                                  JSONL parsing, ps/lsof for live processes
    codex.py                      JSONL-embedded rate_limits, lsof for sid
  audit.py                        `monitor audit [sid]` — context-bloat report
```

The Swift app shells out to `monitor refresh` every 30s, then reads `/tmp/ai-monitor/state.json` and renders. `monitor refresh` does all the heavy lifting:

- **Claude**: fetches `/api/oauth/usage` (cached 5 min so Anthropic doesn't 429 us), walks `~/.claude/projects/*.jsonl` for thread context, scans `ps` for live `claude --resume <sid>` processes.
- **Codex**: walks `~/.codex/sessions/**/rollout-*.jsonl` for both usage events and rate_limits (the public API isn't accessible to non-server clients, but every Codex session writes its own rate_limits to disk), maps live `codex` processes to sessions via `lsof`.

State is written atomically — Swift never blocks on I/O.

## Build & install

You need a Mac with **Xcode Command Line Tools** (Swift 6.0+) and **Python 3.11+**. Tested on macOS 13+.

```bash
# 1. Python data layer
python3 -m pip install --user --break-system-packages -e .
# (--break-system-packages is needed on Homebrew Python; harmless elsewhere)

# Add to PATH if it isn't already (one-time):
echo 'export PATH="$HOME/Library/Python/3.13/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# 2. Swift menubar app
cd swift
./build.sh
cp -R "build/AI Monitor.app" /Applications/
open "/Applications/AI Monitor.app"
```

Click the menubar icon — the dropdown should show your usage. First launch fetches `/api/oauth/usage` if you have a Claude Code session signed in.

### Health check

```bash
monitor doctor
```

Reports which agent dirs exist, whether Claude OAuth is in the Keychain, whether Codex's `auth.json` is present, etc.

### Optional config

```bash
monitor doctor --write-config   # creates ~/.config/ai-monitor.toml
```

Lets you tune plan caps, notification thresholds, per-agent enable flags. Defaults work out of the box.

## Sharing with a friend

```bash
cd swift && ./build.sh
# Send them the repo + the built AI Monitor.app
```

They need to:

1. Clone the repo (or just receive the files)
2. `python3 -m pip install --user --break-system-packages -e <repo>` to get the `monitor` CLI
3. Drag `AI Monitor.app` into `/Applications/`
4. **First-launch security gate.** The .app isn't notarized by Apple ($99/year cert), so macOS will block it on first launch: System Settings → Privacy & Security → "Open Anyway". One-time.

Alternative for step 4: `xattr -cr "/Applications/AI Monitor.app"` strips the quarantine attribute.

If you want a real installer (`.dmg`), look at how [ClaudeUsageBar's build.sh](https://github.com/Artzainnn/ClaudeUsageBar) does it — `hdiutil create` + a layout AppleScript, ~40 extra lines.

## Codex-only? Claude-only? Both?

The app auto-skips agents you don't have installed:

- If `~/.claude/projects` doesn't exist → no Claude card, no `C` in tray
- If `~/.codex/sessions` doesn't exist → no Codex card, no `X` in tray
- If neither exists → tray shows `—`, dropdown shows "no data yet"

You can also disable explicitly in `~/.config/ai-monitor.toml`:

```toml
[agents.codex]
enabled = false
```

## Testing

```bash
make test     # 54 python tests
```

Covers the JSONL walker, process parser, billable math, rate-limits extraction, config loader, notification dedup, and state-to-JSON serialization.

## CLI surface

```
monitor refresh                  full snapshot, writes state.json
monitor refresh --no-remote      skip OAuth fetch (offline)
monitor status                   human-readable summary
monitor status --json            raw state.json
monitor audit [sid]              context-bloat report for a transcript
monitor doctor                   environment health check
monitor doctor --write-config    write a starter ~/.config/ai-monitor.toml
```

## Why the OAuth fetch is cached

Anthropic's `/api/oauth/usage` rate-limits aggressively. We cache its response at `/tmp/ai-monitor/remote-claude.json` for 5 min. If a fetch fails (429 or network), we keep serving the stale cache and surface a banner so you know.

The Swift app's 30s poll therefore hits the network at most once every 5 min — well under any reasonable limit.

## Limitations

- **Codex desktop app / chatgpt.com use isn't visible.** We read rate_limits from each Codex CLI session's JSONL. The quota is shared with the desktop app and the web, but those don't write to disk in a way we can read. When the freshest rate_limits we see is more than 15 min old, the agent header shows a stale-data warning.
- **First-launch macOS quarantine** as noted above.

## License

Source: yours. The brand icons under `swift/Resources/` are from [@lobehub/icons](https://github.com/lobehub/lobe-icons) (MIT). The Anthropic and OpenAI marks belong to those companies — fine for personal use, replace if you publish anywhere visible.
