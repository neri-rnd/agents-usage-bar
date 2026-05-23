"""Claude Code agent implementation."""
from __future__ import annotations
import collections
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

from .base import Agent, AgentState, LimitWindow, RemoteUsage, ThreadInfo, ThreadRequest, AgentError
from ..core.jsonl import iter_messages, parse_iso_to_epoch, extract_user_text, find_title
from ..core.processes import list_pids, extract_flag_value, classify_entry, pid_cwd, summarize_args

PROJECTS_DIR = Path.home() / ".claude" / "projects"


def billable_from_msg(msg: dict) -> int:
    """Return billable tokens for one assistant message (cache_read excluded)."""
    usage = ((msg.get("message") or {}).get("usage")) or {}
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("output_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    )


def cache_read_from_msg(msg: dict) -> int:
    usage = ((msg.get("message") or {}).get("usage")) or {}
    return usage.get("cache_read_input_tokens") or 0


def aggregate_window(files: Iterable, since_s: int, midnight_s: int) -> dict:
    """Aggregate billable tokens across many transcripts.

    `files` is an iterable of (path, project_short_name, sid) triples.
    Returns dict with billable_win, cacheread_win, billable_day, earliest,
    by_model (Counter), by_project (Counter), by_thread (Counter),
    thread_proj (sid → project).
    """
    bill_win = 0
    cread_win = 0
    bill_day = 0
    earliest: Optional[int] = None
    by_model: collections.Counter = collections.Counter()
    by_proj: collections.Counter = collections.Counter()
    by_thread: collections.Counter = collections.Counter()
    thread_proj: dict[str, str] = {}

    for path, proj, sid in files:
        for j in iter_messages(Path(path)):
            if j.get("type") != "assistant":
                continue
            ep = parse_iso_to_epoch(j.get("timestamp") or "")
            if ep == 0:
                continue
            bill = billable_from_msg(j)
            if bill == 0:
                continue
            if ep >= midnight_s:
                bill_day += bill
            if ep >= since_s:
                bill_win += bill
                cread_win += cache_read_from_msg(j)
                model = (j.get("message") or {}).get("model") or "unknown"
                by_model[model] += bill
                by_proj[proj] += bill
                if sid:
                    by_thread[sid] += bill
                    thread_proj[sid] = proj
                if earliest is None or ep < earliest:
                    earliest = ep

    return {
        "billable_win": bill_win,
        "cacheread_win": cread_win,
        "billable_day": bill_day,
        "earliest": earliest or 0,
        "by_model": by_model,
        "by_project": by_proj,
        "by_thread": by_thread,
        "thread_proj": thread_proj,
    }


def context_usage_for_session(msgs: Iterable[dict]) -> Optional[tuple[int, int]]:
    """Return (current_context_tokens, model_context_window) from the latest
    assistant turn in the session. Approximates what Claude Code's /context
    command shows: total input the model sees on the next turn.
    """
    last_usage = None
    last_model = None
    for m in msgs:
        if m.get("type") != "assistant":
            continue
        msg = m.get("message") or {}
        u = msg.get("usage")
        if u:
            last_usage = u
            last_model = msg.get("model")
    if not last_usage:
        return None
    cur = (
        (last_usage.get("input_tokens") or 0)
        + (last_usage.get("cache_read_input_tokens") or 0)
        + (last_usage.get("cache_creation_input_tokens") or 0)
    )
    # Heuristic on context window. Most Claude models are 200k; the 1M variant
    # encodes "[1m]" in the model id string. cur > 200k → assume 1M.
    name = (last_model or "").lower()
    if "1m" in name or cur > 200_000:
        max_ctx = 1_000_000
    else:
        max_ctx = 200_000
    return cur, max_ctx


def project_short(dir_name: str) -> str:
    """Decode Claude's project dir name to a human-readable short name.

    Claude encodes paths like `-Users-foo-AI-limits`. We want the last
    path segment, e.g. "limits".
    """
    decoded = dir_name.lstrip("-").replace("-", "/")
    return decoded.rstrip("/").split("/")[-1] or dir_name


def parse_remote_payload(d: dict) -> dict:
    """Parse `/api/oauth/usage` payload into windows + extra credits.

    Returns:
      - five_hour:    Current Session (5h) LimitWindow or None
      - seven_day:    All-models weekly LimitWindow or None
      - seven_day_sonnet:  Sonnet-only weekly (Pro plan tracks this) or None
      - seven_day_opus:    Opus-only weekly (Max plan) or None when null
      - extra:        RemoteUsage if extra_usage.is_enabled else None
    """
    def win(key: str, kind: str) -> Optional[LimitWindow]:
        v = d.get(key)
        if not v:
            return None
        ut = v.get("utilization")
        if ut is None:
            return None
        return LimitWindow(
            kind=kind,
            pct=int(ut + 0.5),
            resets_at=v.get("resets_at") or None,
            billable=0, cap=0,  # remote endpoint doesn't expose absolute caps
        )

    extra = None
    e = d.get("extra_usage") or {}
    if e.get("is_enabled"):
        extra = RemoteUsage(
            pct=int((e.get("utilization") or 0) + 0.5),
            used=str(e.get("used_credits") or "0"),
            limit=str(e.get("monthly_limit") or "0"),
            ccy=str(e.get("currency") or ""),
        )
    return {
        "five_hour":        win("five_hour", "rolling_5h"),
        "seven_day":        win("seven_day", "rolling_7d"),
        "seven_day_sonnet": win("seven_day_sonnet", "rolling_7d_sonnet"),
        "seven_day_opus":   win("seven_day_opus",   "rolling_7d_opus"),
        "extra":            extra,
    }


def fetch_oauth_token() -> Optional[str]:
    """Read `Claude Code-credentials` from macOS Keychain → access_token."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
        return data.get("claudeAiOauth", {}).get("accessToken")
    except json.JSONDecodeError:
        return None


def fetch_remote_usage(token: str, timeout_s: float = 10.0) -> tuple[Optional[dict], Optional[str]]:
    """Hit /api/oauth/usage. Returns (payload_dict, error_code)."""
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status != 200:
                return None, f"http_{resp.status}"
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None, "network"


# Sticky cache for the OAuth /usage response. Anthropic rate-limits this
# endpoint aggressively; we want the menubar to refresh every 30s without
# hammering the API. TTL = 5 min; on fetch failure we keep serving the
# last-known-good payload until the cache itself expires.
_REMOTE_CACHE_PATH = Path("/tmp/ai-monitor/remote-claude.json")
_REMOTE_CACHE_TTL_S = 300


def cached_remote_usage(
    token: str,
    now_s: int,
    ttl_s: int = _REMOTE_CACHE_TTL_S,
) -> tuple[Optional[dict], Optional[str]]:
    """Return (payload, err). Uses on-disk cache to throttle live fetches.

    - Fresh cache (< ttl_s old): returns cached payload, no fetch.
    - Stale cache: refetch. On success, write new cache and return it.
      On failure, return the stale cached payload (still useful) plus the
      error code so callers can surface it.
    - No cache + failure: returns (None, err_code).
    """
    cached_payload: Optional[dict] = None
    cached_at: int = 0
    try:
        text = _REMOTE_CACHE_PATH.read_text()
        data = json.loads(text)
        cached_at = int(data.get("fetched_at", 0))
        cached_payload = data.get("payload")
        if cached_payload and now_s - cached_at < ttl_s:
            return cached_payload, None
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    payload, err = fetch_remote_usage(token)
    if payload is not None:
        try:
            _REMOTE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _REMOTE_CACHE_PATH.write_text(
                json.dumps({"fetched_at": now_s, "payload": payload})
            )
        except OSError:
            pass
        return payload, None
    # fetch failed → return whatever stale cache we have, with the error code
    return cached_payload, err


class ClaudeAgent(Agent):
    id = "claude"
    label = "Claude"

    def __init__(self, plan_cap_5h: int, remote_disabled: bool = False):
        self.plan_cap_5h = plan_cap_5h
        self.remote_disabled = remote_disabled

    def snapshot(self, now_s: int) -> AgentState:
        errors: list[AgentError] = []
        files = list(self._recent_transcripts())

        # Remote first (so we can anchor the local window on the server's reset).
        remote_windows = {
            "five_hour": None,
            "seven_day": None,
            "seven_day_sonnet": None,
            "seven_day_opus": None,
            "extra": None,
        }
        if not self.remote_disabled:
            token = fetch_oauth_token()
            if token:
                payload, err = cached_remote_usage(token, now_s)
                if payload:
                    remote_windows = parse_remote_payload(payload)
                if err:
                    # Either no cache at all (and live failed), or cache is
                    # stale and the refresh attempt failed — surface it so
                    # the user sees the issue but bars keep showing data.
                    errors.append(AgentError(source="remote", code=err, at=now_s))
            else:
                errors.append(AgentError(source="remote", code="no_token", at=now_s))

        # Anchor local window on the remote reset if known.
        anchored_since = self._anchor_since(now_s, remote_windows["five_hour"])
        midnight_s = self._local_midnight(now_s)
        agg = aggregate_window(files, since_s=anchored_since, midnight_s=midnight_s)

        ctx = self._scan_thread_context(files)
        # Sid -> file path so we can re-walk to extract per-session context %.
        sid_to_path = {sid: path for path, _, sid in files}

        threads = []
        for sid, bill in agg["by_thread"].most_common(20):
            c = ctx.get(sid, {})
            ctx_pct, ctx_tok, ctx_max = (None, None, None)
            if path := sid_to_path.get(sid):
                result = context_usage_for_session(iter_messages(path))
                if result:
                    ctx_tok, ctx_max = result
                    if ctx_max > 0:
                        ctx_pct = int(ctx_tok * 100 / ctx_max + 0.5)
            threads.append(ThreadInfo(
                sid=sid, project=agg["thread_proj"].get(sid, "?"),
                billable=bill, pid=None, active=False,
                title=c.get("title"), first_msg=c.get("first_msg"),
                branch=c.get("branch"),
                context_pct=ctx_pct, context_tokens=ctx_tok, context_max=ctx_max,
            ))
        sid_to_pid, no_sid = self._detect_processes()
        for t in threads:
            t.pid = sid_to_pid.get(t.sid)
        active = self._active_sids(files, now_s)
        for t in threads:
            if t.sid in active:
                t.active = True

        # Prefer the remote window pct when available (matches what /usage shows).
        if remote_windows["five_hour"]:
            window = remote_windows["five_hour"]
            window.billable = agg["billable_win"]
            window.cap = self.plan_cap_5h
        else:
            pct = int(agg["billable_win"] * 100 / max(self.plan_cap_5h, 1) + 0.5)
            window = LimitWindow(
                kind="rolling_5h", pct=pct, resets_at=None,
                billable=agg["billable_win"], cap=self.plan_cap_5h,
            )

        return AgentState(
            id=self.id, label=self.label,
            window=window,
            secondary_windows=[
                w for w in [
                    remote_windows["seven_day"],
                    remote_windows["seven_day_sonnet"],
                    remote_windows["seven_day_opus"],
                ] if w
            ],
            extra_credits=remote_windows["extra"],
            threads=threads,
            by_model=[{"name": m, "billable": c} for m, c in agg["by_model"].most_common()],
            by_project=[{"name": p, "billable": c} for p, c in agg["by_project"].most_common(5)],
            processes_no_sid=no_sid,
            errors=errors,
            cache_ages={},
        )

    @staticmethod
    def _anchor_since(now_s: int, five_hour: Optional[LimitWindow]) -> int:
        """Return the 5h-window start. Server-anchored if reset_at known."""
        default = now_s - 5 * 3600
        if not five_hour or not five_hour.resets_at:
            return default
        ep = parse_iso_to_epoch(five_hour.resets_at)
        if ep <= 0:
            return default
        candidate = ep - 5 * 3600
        if 0 < candidate < now_s:
            return candidate
        return default

    # ----- helpers -----

    def _recent_transcripts(self) -> Iterable[tuple[Path, str, str]]:
        if not PROJECTS_DIR.is_dir():
            return
        cutoff = time.time() - 2 * 86400
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            proj = project_short(proj_dir.name)
            for jl in proj_dir.glob("*.jsonl"):
                try:
                    if jl.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
                yield jl, proj, jl.stem

    @staticmethod
    def _local_midnight(now_s: int) -> int:
        lt = time.localtime(now_s)
        return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))

    @staticmethod
    def _scan_thread_context(files):
        """Return {sid: {title, first_msg, branch}}."""
        out = {}
        for path, _proj, sid in files:
            entry = {"title": None, "first_msg": None, "branch": None}
            for i, j in enumerate(iter_messages(path)):
                if i > 80:
                    # past header window — only keep parsing lines that may carry
                    # a #thread-title marker. Branch + first_msg are already set
                    # if they were going to be set.
                    text = extract_user_text(j) or ""
                    if "#thread-title" not in text:
                        continue
                if entry["branch"] is None:
                    b = j.get("gitBranch")
                    if b:
                        entry["branch"] = b
                text = extract_user_text(j)
                if text:
                    t = find_title(text)
                    if t:
                        entry["title"] = t
                    if entry["first_msg"] is None:
                        stripped = text.lstrip()
                        if not stripped.startswith("<"):
                            entry["first_msg"] = (
                                stripped.replace("\n", " ").replace("\r", " ").strip()[:60]
                            )
            if entry["title"] or entry["first_msg"] or entry["branch"]:
                out[sid] = entry
        return out

    def _detect_processes(self) -> tuple[dict[str, int], list[dict]]:
        sid_to_pid: dict[str, int] = {}
        no_sid: list[dict] = []
        counts: collections.Counter = collections.Counter()
        for pid, cmd in list_pids(["/claude", "native-binary/claude", "claude-agent-sdk"]):
            sid = (
                extract_flag_value(cmd, "--resume")
                or extract_flag_value(cmd, "--session-id")
            )
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

    @staticmethod
    def _active_sids(files, now_s: int) -> set[str]:
        active = set()
        for path, _proj, sid in files:
            try:
                if now_s - int(path.stat().st_mtime) <= 60:
                    active.add(sid)
            except OSError:
                continue
        return active
