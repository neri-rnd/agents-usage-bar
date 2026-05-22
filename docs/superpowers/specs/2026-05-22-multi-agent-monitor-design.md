# ai_monitor — multi-agent monitor design

**Date:** 2026-05-22
**Status:** Approved, ready for implementation planning
**Supersedes:** Current `claude_monitor` (`claude.30s.sh` + `lib/`)

## Goal

Evolve `claude_monitor` into `ai_monitor`: a unified SwiftBar menubar plugin that tracks usage for both Claude Code and OpenAI Codex CLI in one tray icon, refactors the bash-heavy worker layer into a Python-first architecture, and adds config, a unified CLI, and threshold notifications.

## Constraints

- macOS only (SwiftBar).
- Plugin tick must stay fast: reading prerendered state from `/tmp` in ~20–30 ms. All heavy work happens in background workers.
- No new runtime daemons. Workers are short-running processes fired by the plugin when state is stale.
- Sticky-cache semantics for remote endpoints: a failed fetch must not erase the last-known-good data.
- Back-compat for the `#thread-title` marker (parsed from any user message; latest wins).
- Russian-language README and user-facing strings preserved.

## Architecture

### Directory layout

```
ai.30s.sh                       SwiftBar entrypoint (~40 lines, replaces claude.30s.sh)
                                – cats /tmp/ai-monitor/dropdown.txt to stdout
                                – fires `monitor refresh` in background if state stale
lib/
  ai_monitor/
    __init__.py
    agents/
      base.py                   Agent ABC: id, jsonl_glob(), processes(), remote_usage(),
                                  billable_from_msg(), limit_window()
      claude.py                 Claude impl (replaces claude-usage-*, claude-processes,
                                  claude-thread-context, claude-usage-remote, claude-thread-detail)
      codex.py                  Codex impl (new)
    core/
      jsonl.py                  Shared transcript walker — single source of truth
      processes.py              Shared ps/lsof helpers
      state.py                  Builds unified MonitorState dataclass + JSON serialization
      render.py                 SwiftBar formatter: MonitorState → dropdown.txt + tray string
      notify.py                 Threshold notifications via osascript, with dedup cache
      config.py                 ~/.config/ai-monitor.toml loader with defaults
    cli.py                      `monitor status / audit / refresh / doctor / install`
tests/
  test_jsonl.py                 fixture-driven parser tests
  test_state.py                 MonitorState merge logic
  test_render.py                golden-file dropdown snapshots
  test_config.py                TOML loader
  test_context_audit.py         existing; reworked to use core/jsonl.py
  fixtures/
    claude_session.jsonl
    codex_session.jsonl
docs/
  superpowers/specs/2026-05-22-multi-agent-monitor-design.md   (this file)
```

### Data flow per 30s tick

```
SwiftBar fires ai.30s.sh
  └─ cat /tmp/ai-monitor/dropdown.txt → stdout (~20 ms)
  └─ if state.json mtime > 30s ago: spawn `monitor refresh &`

monitor refresh
  ├─ ClaudeAgent.snapshot() → AgentState
  ├─ CodexAgent.snapshot()  → AgentState
  ├─ merge → MonitorState{agents, generated_at}
  ├─ atomic write /tmp/ai-monitor/state.json (PID-suffixed .tmp + os.rename)
  ├─ render → /tmp/ai-monitor/dropdown.txt (same atomic pattern)
  └─ notify.check_thresholds(state)
```

Per-component staleness budgets (carried over from current design, now enforced inside `Agent.snapshot`):
- remote usage: 300 s
- local JSONL aggregation: 60 s
- processes: 30 s
- thread context (first message + branch): 120 s

The `monitor refresh` worker is short-running and idempotent. Multiple concurrent invocations (e.g. user clicks "Re-fetch" while a tick-triggered refresh is running) are safe because every write is `*.tmp.<pid> → os.rename(target)`.

### MonitorState JSON schema

`/tmp/ai-monitor/state.json`:

```jsonc
{
  "generated_at": 1747890123,
  "agents": [
    {
      "id": "claude",
      "label": "Claude",
      "window": {
        "kind": "rolling_5h",
        "pct": 72,
        "resets_at": "2026-05-22T15:28:00Z",
        "billable": 8300000,
        "cap": 14000000
      },
      "secondary_windows": [
        {"kind": "rolling_7d", "pct": 22, "resets_at": "2026-05-29T15:28:00Z"}
      ],
      "extra_credits": {"pct": 12, "used": "1.23", "limit": "10.00", "ccy": "USD"},
      "threads": [
        {
          "sid": "4fe966a0-...",
          "project": "wg",
          "billable": 3200000,
          "pid": 3143,
          "active": false,
          "title": null,
          "first_msg": "поставь pencil cli",
          "branch": "deploy-lightsail",
          "requests": [
            {"epoch": 1747889800, "billable": 120000, "user_prompt": "find ~/.claude -type f"},
            {"epoch": 1747890050, "billable": 450000, "user_prompt": "npm run build"}
          ]
        }
      ],
      "by_model":   [{"name": "opus-4-7", "billable": 3600000}],
      "by_project": [{"name": "designs", "billable": 2400000}],
      "processes_no_sid": [{"entry": "cursor", "project": "wg", "count": 7}],
      "errors": [
        {"source": "remote", "code": "http_429", "at": 1747890000}
      ],
      "cache_ages": {"remote_s": 12, "local_s": 8, "procs_s": 4, "ctx_s": 30}
    },
    { "id": "codex", "label": "Codex", "window": {...}, "...": "same shape" }
  ]
}
```

Rationale: one schema = one contract. Plugin, CLI, and tests all consume the same JSON. New agents (Cursor agents, Gemini CLI, etc.) drop in as another `Agent` subclass without plumbing changes.

## Agents

### Agent ABC (`agents/base.py`)

```python
class Agent(ABC):
    id: str         # "claude", "codex"
    label: str      # "Claude", "Codex"

    @abstractmethod
    def jsonl_glob(self) -> Iterable[Path]: ...
    @abstractmethod
    def detect_processes(self) -> list[ProcessInfo]: ...
    @abstractmethod
    def fetch_remote_usage(self) -> RemoteUsage | None: ...
    @abstractmethod
    def billable_from_msg(self, msg: dict) -> int: ...
    @abstractmethod
    def limit_window(self) -> LimitWindow: ...  # rolling_5h, weekly, etc.

    # Concrete in base — same flow for both agents.
    def snapshot(self) -> AgentState: ...
```

### Claude (`agents/claude.py`)

| Concern | Implementation |
|---|---|
| Transcripts | `~/.claude/projects/<dir>/<sid>.jsonl` |
| sid | filename stem |
| Auth | macOS Keychain (`security find-generic-password -s "Claude Code-credentials"`) |
| Process detection | `ps … claude … --resume <uuid>` or `--session-id <uuid>` |
| Project (cwd) | `lsof -p <pid> -d cwd` |
| Usage event | `type=assistant`, `message.usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}` |
| Billable formula | `input + output + cache_creation` (cache_read excluded — ~10× cheaper) |
| Limit window | rolling 5h, server-anchored via `resets_at` |
| Remote endpoint | `https://api.anthropic.com/api/oauth/usage` |
| First user prompt | first `type=user` message whose stripped text does not start with `<` |
| Title marker | `#thread-title <name>` on any line of any user message; latest wins |

### Codex (`agents/codex.py`)

| Concern | Implementation |
|---|---|
| Transcripts | `~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<sid>.jsonl` |
| sid | last UUID in filename (regex `[0-9a-f-]{36}\.jsonl$`) |
| Auth | `~/.codex/auth.json` → `tokens.access_token` |
| Process detection | `ps … codex …`; sid flag TBD during implementation (try `--session-id`, `--resume`); fallback = active transcripts (mtime < 60s) only |
| Project (cwd) | `lsof -p <pid> -d cwd` (same as Claude) |
| Usage event | `type=event_msg` whose `payload.info.total_token_usage` contains `{input_tokens, output_tokens, cached_input_tokens, reasoning_output_tokens}` |
| Billable formula | `input + output + reasoning_output − cached_input` (reasoning counts as billable for o-series; cached_input is "free" analogous to Claude's cache_read) |
| Limit window | weekly (ChatGPT Plus/Pro). Anchored by remote endpoint if available, else by `config.codex.week_reset_day` + `week_reset_hour_local`. |
| Remote endpoint | **TBD during implementation** — investigate `chatgpt.com/backend-api/codex/usage` or similar. If none exists, weekly % is computed locally from JSONL summed over last 7 days against `config.codex.weekly_cap_tokens`. |
| First user prompt | first `type=response_item` with `payload.role=="user"` and stripped text not starting with `<` |
| Title marker | same `#thread-title` parsing as Claude (back-compat / shared core) |

**Unknowns explicitly flagged for the implementation plan (not design blockers):**
1. Codex sid flag in `ps`. Fallback design already specified.
2. Codex remote usage endpoint existence and shape. Fallback (local-only weekly) already specified.
3. Codex weekly reset wall-clock for ChatGPT Plus/Pro. Config defaults specified.

## UI / UX

### Tray

```
C72 X89
```

- `C` = Claude 5h pct. `X` = Codex weekly pct.
- Tray output is sanitized in `core/render.py` before being written to `dropdown.txt`: only `[A-Za-z0-9 ·%—]` survive, capped at 12 chars. Since the shell plugin just `cat`s the file, no shell-side sanitization is needed.
- Agent with no remote/local data: em-dash, e.g. `C— X12`.
- Agent disabled in config: that half is omitted, e.g. `C72`.
- Optional color hint (`config.tray.color_hints = true`, default `false`): a 🟢/🟡/🔴 dot prefix tracks `max(claude_pct, codex_pct)`:
  - 🟢 both < 75 %
  - 🟡 either in 75–90 %
  - 🔴 either > 90 %

### Dropdown layout

```
72% used (5h window)
resets in 0h44m (at 03:28)
7-day: 22% used · resets in 4d6h
extra credits: 12% (1.23 / 10.00 USD)

─── Codex ───
89% used (weekly window)
resets Mon at 09:00
today: 1.2M billable

─── Threads · last 5h ───
  🟢 alive · ✏️ writing now · 📋 click to copy '#thread-title '
  C 3.2M  4fe966a0  wg        🟢  "поставь pencil cli"
         └ 09:31  120k  "find ~/.claude -type f"
         └ 09:40  450k  "npm run build"
  X 543k  019e2c63  wg        ✏️
  C 220k  6e20586f  designs       "из говна и палок..."
  ── processes w/o sid ──
    cursor/wg × 7

─── By model ───
  C  opus-4-7   3.6M
  C  opus-4-6   2.4M
  X  gpt-5      890k
  X  o3-mini    330k

─── By project ───
  designs        2.4M
  wg             2.6M  (C 2.2M + X 450k)
  old            1.4M

─── caches ───
  Claude: remote 12s · local 8s · procs 4s
  Codex:  local 14s · procs 4s

Refresh menu (auto)
Re-fetch live usage
Re-scan transcripts
Re-scan processes
Open audit for newest thread
```

Notable changes from current:
- Claude is the top section (no header) since most opens are for Claude. Codex gets a clear `─── Codex ───` separator.
- Each thread row gets a `C` / `X` letter prefix so the agent is unambiguous when mixed.
- Per-thread submenu (existing `claude-thread-detail` per-request breakdown) works for Codex too — same shape.
- "By project" merges across agents; when both contributed, shows the split (`wg 2.6M (C 2.2M + X 450k)`).
- Caches footer split per-agent.
- New "Open audit for newest thread" surfaces `monitor audit` (which is currently invisible to the user).

### Thread row click behavior

| State | Click action | Has submenu? |
|---|---|---|
| Has explicit `#thread-title` | none (display only) | yes (per-request breakdown) |
| No title, has first_msg | copies `#thread-title ` to clipboard (existing) | yes |
| No title, no first_msg | copies `#thread-title ` to clipboard | no |

AppleScript-prompt-for-title was considered and dropped this round. `#thread-title` workflow stays.

### Notifications

- `core/notify.py` invokes `osascript -e 'display notification ...'` — no extra deps.
- Default thresholds per agent: 75, 90, 100 % of the primary window. Configurable.
- Dedup: each `(agent_id, threshold, resets_at)` fires at most once. Dedup state in `/tmp/ai-monitor/notified.json`.
- Body: `"Claude 5h: 90% used — resets in 0h44m"`.
- Suppressible globally (`[notifications] enabled = false`) or per-agent (`[notifications.codex] enabled = false`).

### Edge cases the renderer handles
- Agent disabled in config → its section is omitted entirely; its letter is omitted from tray.
- Agent has only errors (no usage data yet) → one-line stub `Codex: no data — last error: no_token (123s ago)`.
- Both agents quiet (zero billable in window) → headers still rendered with `0%` so the user can see the plugin is alive.

## Config

`~/.config/ai-monitor.toml`. Loaded by `core/config.py` with defaults. `monitor doctor --write-config` generates a starter file.

```toml
[tray]
color_hints = false
hide_zero   = true

[agents.claude]
enabled = true
plan_cap_5h = 14_000_000      # Claude Max default
remote_refresh_s = 300

[agents.codex]
enabled = true
plan = "chatgpt_plus"          # presets: chatgpt_plus, chatgpt_pro, chatgpt_team
weekly_cap_tokens = 20_000_000  # estimate, override per plan
week_reset_day = "monday"
week_reset_hour_local = 9

[intervals]
local_s  = 60
procs_s  = 30
remote_s = 300

[notifications]
enabled    = true
thresholds = [75, 90, 100]

[notifications.codex]
enabled = true

[ignored]
projects = []                  # short-name strings, hidden from breakdowns
```

## `monitor` CLI

Installed as `~/.local/bin/monitor` (or run via `python3 -m ai_monitor.cli`).

```
monitor status                     # human-readable text dump of MonitorState
monitor status --json              # raw state.json
monitor refresh                    # force full refresh
monitor refresh --local            # local JSONL only
monitor refresh --remote           # remote endpoints only
monitor refresh --procs            # processes only
monitor audit [sid]                # current context-audit.py behavior, multi-agent
                                   # sid omitted → newest transcript across both
monitor doctor                     # checks: paths exist (~/.claude, ~/.codex), auth
                                   #   readable, swiftbar plugin linked, write perms
                                   #   on /tmp/ai-monitor, Python deps importable
monitor doctor --write-config      # writes starter ~/.config/ai-monitor.toml
monitor install                    # creates the SwiftBar plugin symlink
```

## Migration plan

Four phases, each independently shippable and reversible.

**Phase A — internal refactor, no behavior change.**
Move all current Claude logic into `lib/ai_monitor/agents/claude.py` and `core/*`. Keep `claude.30s.sh` working with the existing `/tmp/claude-*.cache` formats by writing both legacy caches *and* the new `/tmp/ai-monitor/state.json` from the same code. Old tests still pass; users see no diff. Verify by running both the old and new plugin side-by-side for a day.

**Phase B — switch plugin to read new state.**
Replace `claude.30s.sh` body with the thin renderer that reads `state.json`. Drop the legacy `/tmp/claude-*.cache` writes from Phase A. Delete `lib/*.sh` workers. (`copy-thread-title.sh` stays — it is still invoked from the menu.) Rename plugin to `ai.30s.sh` and update the SwiftBar symlink.

**Phase C — add Codex agent.**
Implement `agents/codex.py`. Codex shows up in dropdown automatically. Tray gains `X` letter. Notifications start firing for Codex.

**Phase D — config + CLI + notifications.**
Land TOML config, `monitor` CLI, threshold notifications. Update README (Russian, mirror current voice).

## Testing

- `tests/test_jsonl.py` — fixtures: small Claude and Codex transcripts (≈10 lines each). Asserts correct billable totals, model/project breakdowns, first_msg extraction, title parsing, skipping `<…>` injections, timestamp parsing across the formats both agents emit.
- `tests/test_state.py` — given mock `AgentState` inputs, asserts merged `MonitorState` is correct: tray % logic, secondary windows, error rollups, "by project" cross-agent merge.
- `tests/test_render.py` — golden-file snapshots: a fixed `MonitorState` JSON renders to a known dropdown.txt. Catches accidental SwiftBar-format regressions.
- `tests/test_config.py` — TOML parsing, defaults, env-var overrides, malformed-file fallback.
- `tests/test_processes.py` — fixture-driven tests of `ps`/`lsof` line parsing (mocked subprocess output, no real processes).
- `tests/test_context_audit.py` — existing tests retained; reworked to use `core/jsonl.py` so its parsing matches the menubar's.
- No integration tests against live Anthropic/OpenAI endpoints or real `/tmp` cache files. Fixtures only.
- Test runner: `python3 -m pytest tests/`. Add a `Makefile` with `make test` and `make lint`.

## Out of scope (explicit YAGNI)

- Sparkline / usage trend graph.
- AppleScript prompt for thread titles (user declined this round).
- Hide-noisy-threads / sort toggles in dropdown (revisit after Phase D if needed).
- Other AI tools (Cursor agents, Gemini CLI) — the `Agent` ABC keeps the door open for later.
- Linux / Windows support — macOS-only via SwiftBar.
- Long-running daemon (decided against in scoping; see "Refactor depth" question).

## Open items for the implementation plan

These are not design questions; they are research items the implementation plan must resolve up front:

1. **Codex sid flag.** What does `codex` CLI pass as its session-id flag (if any)? Spec falls back gracefully if none.
2. **Codex remote usage endpoint.** Does one exist? What is its shape and auth? Spec falls back to local-only weekly if not.
3. **Codex ChatGPT Plus / Pro weekly reset wall-clock.** Confirm the actual reset day/hour for each plan. Update config defaults if they differ.
4. **Codex weekly cap by plan.** Confirm token caps for each ChatGPT plan tier. Update `weekly_cap_tokens` defaults.
