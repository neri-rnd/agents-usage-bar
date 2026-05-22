"""Codex CLI agent — research findings (Task 14).

## Storage layout (verified)

  ~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<uuid>.jsonl
  ~/.codex/auth.json  → tokens.access_token

  Observed examples (macOS, May 2026):
    ~/.codex/sessions/2026/05/18/rollout-2026-05-18T14-56-47-019e3af2-219c-7323-ba04-4e63e15ce1a5.jsonl
    ~/.codex/sessions/2025/10/05/rollout-2025-10-05T18-56-07-0199b516-6510-7f73-ae4d-e1d7456ab1b6.jsonl

  auth.json keys: auth_mode, OPENAI_API_KEY, tokens, last_refresh
  tokens keys: id_token, access_token, refresh_token, account_id

## Session ID flag in `ps` (Item 1)

  No session-ID flag observed — the native Codex binary is invoked bare, without any
  --session-id, --resume, --rollout, or similar argument. Live process command lines seen:

    node /opt/homebrew/bin/codex
    /opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/
      vendor/aarch64-apple-darwin/codex/codex

  The active rollout UUID is NOT passed on the CLI — it must be inferred by tracking which
  rollout-*.jsonl file is currently being written to (via lsof or mtime comparison).

  Codex-ACP (Zed external agent) runs as a separate binary:
    ~/Library/Application Support/Zed/external_agents/registry/codex-acp/<ver>/codex-acp

  Fall-back strategy: active session = rollout file with most-recent mtime that matches
  the PID's open file descriptors (lsof -p <pid>).

## Remote usage endpoint (Item 2)

  None found — all probed endpoints returned non-200:

    https://chatgpt.com/backend-api/codex/usage         → HTTP 403 (Cloudflare HTML wall)
    https://chatgpt.com/backend-api/usage               → HTTP 403
    https://chatgpt.com/backend-api/conversation/limits → HTTP 403
    https://api.openai.com/v1/usage                     → HTTP 401 (JWT access_token rejected)
    https://api.openai.com/v1/codex/usage               → HTTP 404

  The chatgpt.com endpoints reject the Bearer token from auth.json with a Cloudflare
  HTML 403 response (not a JSON API error). The OpenAI REST API endpoint rejects the
  same JWT with a 401 — the access_token is a ChatGPT session token, not an OpenAI
  API key. No working remote endpoint was found.

  HOWEVER — rate-limit data IS embedded in the JSONL files themselves. The event_msg
  records of type "token_count" carry a rate_limits object whenever Codex receives
  limit-headroom from the server:

    {
      "type": "event_msg",
      "payload": {
        "type": "token_count",
        "info": {
          "total_token_usage": {
            "input_tokens": 2396884,
            "cached_input_tokens": 2228992,
            "output_tokens": 7054,
            "reasoning_output_tokens": 2717,
            "total_tokens": 2403938
          },
          "last_token_usage": { ... }
        },
        "rate_limits": {
          "limit_id": "codex",
          "limit_name": null,
          "primary": {
            "used_percent": 12.0,
            "window_minutes": 300,
            "resets_at": 1779476384
          },
          "secondary": {
            "used_percent": 30.0,
            "window_minutes": 10080,
            "resets_at": 1779822029
          },
          "credits": null,
          "plan_type": "prolite",
          "rate_limit_reached_type": null
        }
      }
    }

  Strategy: scan the most-recently-written rollout-*.jsonl files, find the latest
  event_msg record where rate_limits is non-null, and use those values directly.
  This is more reliable than any remote probe.

## Weekly reset (Item 3)

  Server-anchored reset times are embedded in the rate_limits.secondary.resets_at field
  (Unix epoch seconds) of JSONL event_msg records. Observed example:

    secondary.window_minutes = 10080  (= exactly 7 days)
    secondary.resets_at     = 1779573648  => 2026-05-23 22:00 UTC
    secondary.used_percent  = 23%

  Primary window: 300 minutes (5 hours), resets_at also in epoch seconds.
  Plan observed: "prolite"

  Implementation: read the latest non-null rate_limits from any rollout file modified
  in the last 24h. Expose primary window as the 5h limit and secondary as the 7-day
  weekly window. If no recent file has rate_limits (e.g. offline), fall back to:
    - Primary:   rolling 300-minute window computed locally
    - Secondary: rolling 7-day window computed locally against config.codex.weekly_cap_tokens

## Billable formula (already known)

  billable = input_tokens + output_tokens + reasoning_output_tokens - cached_input_tokens

  Source: ~/.codex/sessions/.../*.jsonl event_msg records with
  payload.info.total_token_usage.{input_tokens, output_tokens, cached_input_tokens,
  reasoning_output_tokens}

  Note: total_token_usage is the cumulative sum for the whole session; last_token_usage
  is the delta for the most recent API call. Aggregate billable across sessions by
  summing the last event_msg per session (or the last token_count per turn), not every
  intermediate record.

## Implementation notes for Task 15

  Binary patterns for list_pids():
    - "node" process with argv[1] matching "*/codex" or "*/codex.js"  (interactive TUI)
    - Native binary matching "*codex-darwin-arm64*/codex/codex"        (spawned by node wrapper)
    - "codex-acp" binary under ~/Library/Application Support/Zed/     (Zed agent — track separately)
    - The native binary (PID 55018) has no CLI args; its open files reveal the active JSONL

  Active session detection:
    - lsof -p <native_pid> | grep '.jsonl' → extracts the path of the active rollout file
    - The rollout filename encodes the session UUID: rollout-<ISO>-<uuid>.jsonl
    - UUID can be confirmed against the session_meta event in the file

  Rate-limit sourcing (no remote endpoint):
    - Scan all rollout-*.jsonl files modified in the last 24 hours
    - Find the latest event_msg where payload.rate_limits is not null
    - Extract primary (5h) and secondary (7d) windows with resets_at timestamps
    - Expose as LimitWindow(kind="rolling_5h", ...) and LimitWindow(kind="weekly", ...)

  Weekly cap fallback (when no rate_limits record found):
    - config.codex.weekly_cap_tokens default: 20_000_000 (plan-dependent placeholder)
    - Aggregate billable tokens from all sessions in the rolling 7-day window
"""
from __future__ import annotations
import re
import time
from pathlib import Path
from typing import Iterable, Optional

from .base import Agent, AgentState, LimitWindow, ThreadInfo, AgentError
from ..core.jsonl import iter_messages, parse_iso_to_epoch, find_title
from ..core.processes import list_pids, classify_entry, pid_cwd

SESSIONS_DIR = Path.home() / ".codex" / "sessions"
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def sid_from_filename(name: str) -> Optional[str]:
    m = UUID_RE.search(name)
    return m.group(1) if m else None


def _is_token_count_event(msg: dict) -> bool:
    return (
        msg.get("type") == "event_msg"
        and (msg.get("payload") or {}).get("type") == "token_count"
    )


def billable_from_session(msgs: Iterable[dict]) -> int:
    """Return the session's billable tokens.

    `total_token_usage` is cumulative — return the LAST event_msg's total.
    billable = input + output + reasoning_output - cached_input
    """
    last_total = None
    for m in msgs:
        if not _is_token_count_event(m):
            continue
        info = ((m.get("payload") or {}).get("info")) or {}
        last_total = info.get("total_token_usage")
    if not last_total:
        return 0
    return (
        (last_total.get("input_tokens") or 0)
        + (last_total.get("output_tokens") or 0)
        + (last_total.get("reasoning_output_tokens") or 0)
        - (last_total.get("cached_input_tokens") or 0)
    )


def _is_phantom_rate_limits(rl: dict) -> bool:
    """Codex emits paired rate_limits events: the real numbers, then a
    `primary=0, secondary=0` phantom. Treat the phantom as no-data so the
    real number from the same turn wins.
    """
    if not rl:
        return True
    p = (rl.get("primary") or {}).get("used_percent")
    s = (rl.get("secondary") or {}).get("used_percent")
    return (p in (0, 0.0, None)) and (s in (0, 0.0, None))


def latest_rate_limits(msgs: Iterable[dict]) -> Optional[dict]:
    """Return the most recent NON-PHANTOM payload.rate_limits dict, or None."""
    last = None
    for m in msgs:
        if not _is_token_count_event(m):
            continue
        rl = (m.get("payload") or {}).get("rate_limits")
        if rl and not _is_phantom_rate_limits(rl):
            last = rl
    return last


def latest_rate_limits_with_ts(msgs: Iterable[dict]) -> Optional[tuple[dict, int]]:
    """Return (rate_limits, event_epoch) for the most recent non-phantom event."""
    last_rl: Optional[dict] = None
    last_ts: int = 0
    for m in msgs:
        if not _is_token_count_event(m):
            continue
        rl = (m.get("payload") or {}).get("rate_limits")
        if rl and not _is_phantom_rate_limits(rl):
            ts = parse_iso_to_epoch(m.get("timestamp") or "")
            if ts > last_ts:
                last_ts = ts
                last_rl = rl
    if last_rl is None:
        return None
    return last_rl, last_ts


def model_from_session(msgs: Iterable[dict]) -> Optional[str]:
    """Return the model name from the most recent turn_context record.

    Codex emits `type=turn_context, payload.model=<name>` whenever the model
    is set or changed. The `event_msg/token_count` records do NOT carry a
    model field — only token totals and rate_limits. Fallback: also check
    token_count.info.model in case a future format moves it there.
    """
    last = None
    for m in msgs:
        if m.get("type") == "turn_context":
            model = (m.get("payload") or {}).get("model")
            if model:
                last = model
            continue
        if _is_token_count_event(m):
            info = ((m.get("payload") or {}).get("info")) or {}
            model = info.get("model")
            if model:
                last = model
    return last


def extract_codex_user_text(msg: dict) -> Optional[str]:
    """Return text of the first user response_item content block, or None.

    Codex uses content blocks of `type=input_text` for user input (not `text`
    like Claude). Both shapes are accepted so old fixtures still work.
    """
    if msg.get("type") != "response_item":
        return None
    payload = msg.get("payload") or {}
    if payload.get("role") != "user":
        return None
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("text", "input_text"):
                return b.get("text") or ""
    return ""


# Substrings that mark a "user" response_item as system-injected boilerplate
# (AGENTS.md, environment_context, turn_aborted, etc.) — those should not be
# treated as the thread's first real user prompt.
CODEX_INJECTION_PREFIXES = (
    "<",                     # XML-tagged injections: <environment_context>, <turn_aborted>, ...
    "# AGENTS.md",           # repo-aware system content
    "# Repository Guidelines",
)


def _is_codex_injection(text: str) -> bool:
    s = text.lstrip()
    return any(s.startswith(p) for p in CODEX_INJECTION_PREFIXES)


def cwd_from_codex_session(msgs: Iterable[dict]) -> Optional[str]:
    """Read payload.cwd from the first session_meta line."""
    for m in msgs:
        if m.get("type") == "session_meta":
            return (m.get("payload") or {}).get("cwd")
        # session_meta is always first; bail early.
        break
    return None


def codex_active_sid_for_pid(pid: int) -> Optional[str]:
    """Use lsof to find which rollout-*.jsonl the codex pid has open."""
    import subprocess
    import shutil
    if not shutil.which("lsof"):
        return None
    try:
        out = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    for line in out.stdout.splitlines():
        # Match the rollout-*.jsonl path anywhere on the line.
        if "rollout-" in line and ".jsonl" in line:
            # Extract the file path (last token usually).
            for tok in line.split():
                if "rollout-" in tok and tok.endswith(".jsonl"):
                    sid = sid_from_filename(Path(tok).name)
                    if sid:
                        return sid
    return None


def _epoch_to_iso(epoch) -> Optional[str]:
    """Convert a unix epoch (int/float) to ISO 8601 'Z' string. None on invalid input."""
    if epoch is None:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OSError):
        return None


class CodexAgent(Agent):
    id = "codex"
    label = "Codex"

    def __init__(self, weekly_cap_tokens: int = 20_000_000):
        self.weekly_cap_tokens = weekly_cap_tokens

    def snapshot(self, now_s: int) -> AgentState:
        errors: list[AgentError] = []
        files = list(self._recent_transcripts(now_s))

        per_session_bill: dict[str, int] = {}
        per_session_model: dict[str, str] = {}
        per_session_proj: dict[str, str] = {}
        per_session_ctx: dict[str, dict] = {}
        all_msgs_per_file: dict[Path, list[dict]] = {}

        for path, proj, sid in files:
            msgs = list(iter_messages(path))
            all_msgs_per_file[path] = msgs
            per_session_bill[sid] = billable_from_session(msgs)
            per_session_proj[sid] = proj
            m = model_from_session(msgs)
            if m:
                per_session_model[sid] = m
            # Extract title + first_msg per session.
            # Codex's first user response_item is usually system-injected
            # (AGENTS.md + environment_context); skip those via the prefix
            # match. The real first user prompt is response_item #2+.
            ctx: dict = {"title": None, "first_msg": None}
            for j in msgs:
                text = extract_codex_user_text(j)
                if text:
                    t = find_title(text)
                    if t:
                        ctx["title"] = t
                    if ctx["first_msg"] is None and not _is_codex_injection(text):
                        ctx["first_msg"] = (
                            text.lstrip()
                            .replace("\n", " ")
                            .replace("\r", " ")
                            .strip()[:60]
                        )
            per_session_ctx[sid] = ctx

        # Pick the freshest rate_limits across ALL recent files (by event
        # timestamp, not file mtime). Rationale: a user can hop between
        # multiple Codex sessions; the latest token_count event in any of
        # them is the most up-to-date snapshot of their quota.
        # Caveat: rate_limits only reflect quota seen by *this* set of CLI
        # sessions. Consumption via the Codex desktop app or chatgpt.com
        # is not visible here (no public API endpoint).
        rate_limits = None
        rate_limits_ts = 0
        for path, msgs in all_msgs_per_file.items():
            result = latest_rate_limits_with_ts(msgs)
            if result is None:
                continue
            rl, ts = result
            if ts > rate_limits_ts:
                rate_limits = rl
                rate_limits_ts = ts

        # If the freshest rate_limits is older than 15 minutes, the numbers
        # are likely stale — record as an info-level error so the renderer
        # can show a "as of …" hint.
        if rate_limits and rate_limits_ts:
            age = now_s - rate_limits_ts
            if age > 15 * 60:
                errors.append(AgentError(
                    source="local",
                    code=f"stale_rate_limits_{age // 60}m",
                    at=rate_limits_ts,
                ))

        # PRIMARY window for Codex = WEEKLY (the binding ChatGPT Plus/Pro limit;
        # this is what shows in the tray as "X<N>" and in the Codex section header).
        if rate_limits and rate_limits.get("secondary"):
            s = rate_limits["secondary"]
            window = LimitWindow(
                kind="weekly",
                pct=int((s.get("used_percent") or 0) + 0.5),
                resets_at=_epoch_to_iso(s.get("resets_at")),
                billable=0, cap=0,
            )
        else:
            # Local fallback: sum billable across all sessions in last 7 days
            total_weekly = sum(per_session_bill.values())
            pct = int(total_weekly * 100 / max(self.weekly_cap_tokens, 1) + 0.5)
            window = LimitWindow(
                kind="weekly", pct=pct, resets_at=None,
                billable=total_weekly, cap=self.weekly_cap_tokens,
            )

        # SECONDARY window = 5h (less binding for Codex; shown only if present).
        secondary = []
        if rate_limits and rate_limits.get("primary"):
            p = rate_limits["primary"]
            secondary.append(LimitWindow(
                kind="rolling_5h",
                pct=int((p.get("used_percent") or 0) + 0.5),
                resets_at=_epoch_to_iso(p.get("resets_at")),
                billable=0, cap=0,
            ))

        # Threads list (top 20 by billable)
        sorted_sids = sorted(per_session_bill.items(), key=lambda kv: -kv[1])[:20]
        threads = []
        for sid, bill in sorted_sids:
            ctx = per_session_ctx.get(sid, {})
            threads.append(ThreadInfo(
                sid=sid, project=per_session_proj.get(sid, "?"),
                billable=bill, pid=None, active=False,
                title=ctx.get("title"), first_msg=ctx.get("first_msg"),
                branch=None,
            ))

        # Detect processes
        sid_to_pid, no_sid = self._detect_processes()
        for t in threads:
            t.pid = sid_to_pid.get(t.sid)

        # By model
        by_model: dict[str, int] = {}
        for sid, bill in per_session_bill.items():
            m = per_session_model.get(sid) or "unknown"
            by_model[m] = by_model.get(m, 0) + bill

        # By project
        by_proj: dict[str, int] = {}
        for sid, bill in per_session_bill.items():
            p = per_session_proj.get(sid, "?")
            by_proj[p] = by_proj.get(p, 0) + bill

        return AgentState(
            id=self.id, label=self.label,
            window=window,
            secondary_windows=secondary,
            extra_credits=None,
            threads=threads,
            by_model=sorted(
                ({"name": m, "billable": c} for m, c in by_model.items()),
                key=lambda d: -d["billable"],
            ),
            by_project=sorted(
                ({"name": p, "billable": c} for p, c in by_proj.items()),
                key=lambda d: -d["billable"],
            )[:5],
            processes_no_sid=no_sid,
            errors=errors,
            cache_ages={},
        )

    def _recent_transcripts(self, now_s: int) -> Iterable[tuple[Path, str, str]]:
        if not SESSIONS_DIR.is_dir():
            return
        cutoff = now_s - 8 * 86400  # 8 days, slightly wider than weekly
        for jl in SESSIONS_DIR.rglob("rollout-*.jsonl"):
            try:
                if jl.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            sid = sid_from_filename(jl.name)
            if not sid:
                continue
            # Try to extract cwd from session_meta (first line)
            cwd = None
            for j in iter_messages(jl):
                # Codex always puts session_meta as the first record; we only
                # check the first line and bail regardless.
                if j.get("type") == "session_meta":
                    cwd = (j.get("payload") or {}).get("cwd")
                break
            proj = Path(cwd).name if cwd else "?"
            yield jl, proj, sid

    def _detect_processes(self) -> tuple[dict[str, int], list[dict]]:
        sid_to_pid: dict[str, int] = {}
        no_sid: list[dict] = []
        import collections
        counts: collections.Counter = collections.Counter()
        # Match common codex binary paths
        for pid, cmd in list_pids(["codex-darwin-arm64/codex/codex", "/codex", "codex-acp"]):
            # No CLI session-ID — use lsof to find the open JSONL
            sid = codex_active_sid_for_pid(pid)
            entry = classify_entry(cmd)
            cwd = pid_cwd(pid) or ""
            proj = Path(cwd).name if cwd else "?"
            if sid:
                sid_to_pid[sid] = pid
            else:
                counts[(entry, proj)] += 1
        for (entry, proj), n in counts.items():
            no_sid.append({"entry": entry, "project": proj, "count": n})
        return sid_to_pid, no_sid
