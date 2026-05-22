"""`monitor` CLI entrypoint."""
from __future__ import annotations
import argparse
import subprocess
import sys
import time
from pathlib import Path

from .agents.base import AgentState
from .agents.claude import ClaudeAgent
from .core.state import MonitorState, write_state_atomic, write_text_atomic

STATE_DIR = Path("/tmp/ai-monitor")


def _log_error(msg: str, **context) -> None:
    """Append a timestamped error entry to /tmp/ai-monitor/error.log.

    Captures the current exception traceback when called inside an `except`
    block. Keeps the log bounded (~100KB) by rotating once over the cap.
    """
    import traceback, datetime
    log = STATE_DIR / "error.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size > 100_000:
            log.rename(log.with_suffix(".log.old"))
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.datetime.now().isoformat()} ---\n")
            f.write(f"{msg}\n")
            for k, v in context.items():
                f.write(f"  {k}: {v!r}\n"[:500])
            tb = traceback.format_exc()
            if tb and "NoneType: None" not in tb:
                f.write(tb)
    except OSError:
        pass


def cmd_refresh(config_path=None, remote_disabled: bool = False) -> int:
    """Refresh state by snapshotting all installed + enabled agents.

    config_path: optional Path to TOML config. Defaults to DEFAULT_PATH.
    remote_disabled: if True, skip remote endpoints (used in tests / CLI flag).

    Auto-skips agents whose CLI isn't installed (no ~/.claude/projects or
    ~/.codex/sessions) — so a Claude-only user doesn't get a phantom 0%
    Codex card in the menubar and vice versa.
    """
    from .core.config import load_config, DEFAULT_PATH
    cfg = load_config(config_path or DEFAULT_PATH)
    now_s = int(time.time())
    agents = []

    claude_dir = Path.home() / ".claude" / "projects"
    if cfg.claude.enabled and claude_dir.is_dir():
        agents.append(ClaudeAgent(
            plan_cap_5h=cfg.claude.plan_cap_5h,
            remote_disabled=remote_disabled,
        ).snapshot(now_s))

    codex_dir = Path.home() / ".codex" / "sessions"
    if cfg.codex.enabled and codex_dir.is_dir():
        from .agents.codex import CodexAgent
        agents.append(CodexAgent(
            weekly_cap_tokens=cfg.codex.weekly_cap_tokens,
        ).snapshot(now_s))

    state = MonitorState(generated_at=now_s, agents=agents)
    write_state_atomic(STATE_DIR / "state.json", state)
    # Notifications
    if cfg.notifications.enabled:
        from .core.notify import check_thresholds
        check_thresholds(
            state,
            thresholds=cfg.notifications.thresholds,
            dedup_path=STATE_DIR / "notified.json",
            enabled_per_agent={
                "claude": cfg.notifications.claude_enabled,
                "codex":  cfg.notifications.codex_enabled,
            },
        )
    return 0


def cmd_status(as_json: bool = False) -> int:
    state_file = STATE_DIR / "state.json"
    if not state_file.exists():
        print("no state yet — run `monitor refresh` first", file=sys.stderr)
        return 1
    text = state_file.read_text()
    if as_json:
        print(text)
        return 0
    import json as _json
    d = _json.loads(text)
    for a in d["agents"]:
        w = a.get("window") or {}
        billable = w.get("billable", 0)
        cap = w.get("cap", 0)
        print(f"{a['label']}: {w.get('pct', '—')}%  ({billable:,} / {cap:,})")
        for t in a.get("threads", [])[:5]:
            label = t.get('title') or t.get('first_msg') or ''
            print(f"  {t['sid'][:8]}  {t['project']:<12}  {t['billable']:,}  {label}")
    return 0


def cmd_doctor(write_config: bool = False) -> int:
    """Health check: report which inputs the data layer can read."""
    from .core.config import DEFAULT_PATH
    print("ai-monitor doctor")

    # Agent installs — auto-skip behavior keys off these existing.
    claude_dir = Path.home() / ".claude" / "projects"
    codex_dir  = Path.home() / ".codex" / "sessions"
    print(f"  [{ 'OK' if claude_dir.is_dir() else 'NOT FOUND' }] Claude projects: {claude_dir}")
    print(f"  [{ 'OK' if codex_dir.is_dir()  else 'NOT FOUND' }] Codex sessions:  {codex_dir}")

    # Claude OAuth token (Keychain)
    try:
        auth_keychain = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-g"],
            capture_output=True, text=True, timeout=2,
        ).returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        auth_keychain = False
    print(f"  [{ 'OK' if auth_keychain else 'MISSING' }] Claude OAuth token (Keychain)")

    # Codex auth file
    codex_auth = (Path.home() / ".codex" / "auth.json").exists()
    print(f"  [{ 'OK' if codex_auth else 'MISSING' }] Codex auth.json")

    # Swift menubar app
    app_path = Path("/Applications/AI Monitor.app")
    print(f"  [{ 'OK' if app_path.exists() else 'NOT INSTALLED' }] Swift menubar app: {app_path}")

    # Config
    if DEFAULT_PATH.exists():
        print(f"  [OK]      Config: {DEFAULT_PATH}")
    else:
        print(f"  [DEFAULTS]  Config: {DEFAULT_PATH} (using built-in defaults; run `monitor doctor --write-config`)")

    if write_config:
        if DEFAULT_PATH.exists():
            print(f"\nconfig already exists at {DEFAULT_PATH}; not overwriting")
        else:
            DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
            DEFAULT_PATH.write_text(_starter_config())
            print(f"\nwrote starter config to {DEFAULT_PATH}")
    return 0


def _starter_config() -> str:
    return '''# Generated by `monitor doctor --write-config`.
[tray]
color_hints = false
hide_zero   = true

[agents.claude]
enabled = true
plan_cap_5h = 14_000_000
remote_refresh_s = 300

[agents.codex]
enabled = true
plan = "chatgpt_plus"
weekly_cap_tokens = 20_000_000
week_reset_day = "monday"
week_reset_hour_local = 9

[intervals]
local_s  = 60
procs_s  = 30
remote_s = 300

[notifications]
enabled    = true
thresholds = [75, 90, 100]

[notifications.claude]
enabled = true

[notifications.codex]
enabled = true

[ignored]
projects = []
'''


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="monitor")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh")
    r.add_argument("--config", type=Path, default=None)
    r.add_argument("--no-remote", action="store_true")
    a = sub.add_parser("audit")
    a.add_argument("sid", nargs="?", default=None)

    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")

    d = sub.add_parser("doctor")
    d.add_argument("--write-config", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "audit":
        from .audit import find_jsonl, parse_transcript, categorise, render
        path = find_jsonl(args.sid)
        data = parse_transcript(path)
        cat = categorise(data)
        print(render(data, cat, path.stem, path))
        return 0
    if args.cmd == "refresh":
        return cmd_refresh(
            config_path=args.config,
            remote_disabled=args.no_remote,
        )
    if args.cmd == "status":
        return cmd_status(as_json=args.json)
    if args.cmd == "doctor":
        return cmd_doctor(write_config=args.write_config)
    return 1


if __name__ == "__main__":
    sys.exit(main())
