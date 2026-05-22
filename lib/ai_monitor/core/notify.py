"""Threshold notifications via osascript. Dedup state in /tmp/ai-monitor/notified.json."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .state import MonitorState


def _osascript_notify(title: str, body: str) -> None:
    """Default notifier. Best-effort — failures are silent."""
    title = title.replace('"', '\\"')
    body = body.replace('"', '\\"')
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            timeout=3, capture_output=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _crossed(prev_pct: int, cur_pct: int, thresholds: list[int]) -> Optional[int]:
    """Return the largest threshold crossed by going prev → cur, or None."""
    crossed_now = [t for t in thresholds if prev_pct < t <= cur_pct]
    return max(crossed_now) if crossed_now else None


def _load_state(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))


def check_thresholds(
    state: MonitorState,
    thresholds: list[int],
    dedup_path: Path,
    notify: Callable[[str, str], None] = _osascript_notify,
    enabled_per_agent: Optional[dict[str, bool]] = None,
) -> None:
    """Fire a notification for each agent that crossed a threshold this tick.

    Dedup key: (agent_id, window_kind, resets_at, threshold). When resets_at
    changes (new window), the dedup is cleared for that agent.

    enabled_per_agent: optional mapping of agent id → bool. If provided and an
    agent's id maps to False, notifications for that agent are suppressed.
    """
    notified = _load_state(dedup_path)
    for a in state.agents:
        if not a.window:
            continue
        if enabled_per_agent is not None and not enabled_per_agent.get(a.id, True):
            continue
        key_base = f"{a.id}|{a.window.kind}|{a.window.resets_at or ''}"
        prev = notified.get(key_base, {"pct": 0, "fired": []})
        prev_pct = prev.get("pct", 0)
        cur_pct = a.window.pct
        already_fired = set(prev.get("fired", []))
        for t in sorted(thresholds):
            if t in already_fired:
                continue
            if prev_pct < t <= cur_pct:
                title = f"{a.label} {a.window.kind}"
                body = f"{a.label} {a.window.kind.replace('rolling_', '')}: {t}% used"
                if a.window.resets_at:
                    body += f" — resets at {a.window.resets_at}"
                notify(title, body)
                already_fired.add(t)
        notified[key_base] = {"pct": cur_pct, "fired": sorted(already_fired)}
    _save_state(dedup_path, notified)
