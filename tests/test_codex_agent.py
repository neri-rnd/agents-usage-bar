from pathlib import Path
from ai_monitor.agents.codex import (
    CodexAgent, billable_from_session, sid_from_filename,
    latest_rate_limits, extract_codex_user_text, model_from_session,
)
from ai_monitor.core.jsonl import iter_messages


def test_sid_from_filename():
    name = "rollout-2026-05-15T19-06-18-019e2c63-7e6f-70b0-b304-e84fabf52597.jsonl"
    assert sid_from_filename(name) == "019e2c63-7e6f-70b0-b304-e84fabf52597"
    assert sid_from_filename("garbage.jsonl") is None


def test_billable_from_session_uses_last_total(codex_jsonl):
    """total_token_usage is cumulative — use the LAST event_msg's total, not the sum."""
    msgs = list(iter_messages(codex_jsonl))
    bill = billable_from_session(msgs)
    # Last event_msg has total: input=2000, output=700, cached=300, reasoning=650.
    # billable = 2000 + 700 + 650 - 300 = 3050.
    assert bill == 3050


def test_billable_from_session_handles_no_event_msg():
    assert billable_from_session([{"type": "session_meta"}]) == 0
    assert billable_from_session([]) == 0


def test_latest_rate_limits_extracts_windows(codex_jsonl):
    msgs = list(iter_messages(codex_jsonl))
    rl = latest_rate_limits(msgs)
    assert rl is not None
    # Last record has primary=15%, secondary=32%
    assert rl["primary"]["used_percent"] == 15.0
    assert rl["primary"]["window_minutes"] == 300
    assert rl["secondary"]["used_percent"] == 32.0


def test_latest_rate_limits_returns_none_when_missing():
    msgs = [{"type": "event_msg", "payload": {"type": "token_count", "info": {}}}]
    assert latest_rate_limits(msgs) is None


def test_extract_codex_user_text_finds_first_user_response_item():
    msg = {"type": "response_item", "payload": {"role": "user",
            "content": [{"type": "text", "text": "hello"}]}}
    assert extract_codex_user_text(msg) == "hello"


def test_extract_codex_user_text_returns_none_for_assistant():
    msg = {"type": "response_item", "payload": {"role": "assistant",
            "content": [{"type": "text", "text": "ignored"}]}}
    assert extract_codex_user_text(msg) is None


def test_codex_snapshot_against_fixture(tmp_path, monkeypatch, codex_jsonl):
    # Stage a fake ~/.codex/sessions/2026/05/22/rollout-...-fakeuuid.jsonl
    day = tmp_path / "sessions" / "2026" / "05" / "22"
    day.mkdir(parents=True)
    target = day / "rollout-2026-05-22T10-00-00-00000000-1111-2222-3333-444444444444.jsonl"
    target.write_bytes(codex_jsonl.read_bytes())

    monkeypatch.setattr("ai_monitor.agents.codex.SESSIONS_DIR", tmp_path / "sessions")

    agent = CodexAgent(weekly_cap_tokens=20_000_000)
    state = agent.snapshot(now_s=1779444000 + 60)
    assert state.id == "codex"
    # PRIMARY window for Codex = WEEKLY (rate_limits.secondary in JSONL)
    assert state.window.kind == "weekly"
    assert state.window.pct == 32  # 32.0 → 32
    # SECONDARY window = 5h (rate_limits.primary in JSONL)
    assert len(state.secondary_windows) >= 1
    assert state.secondary_windows[0].kind == "rolling_5h"
    assert state.secondary_windows[0].pct == 15
    # Thread parsed out
    sids = {t.sid for t in state.threads}
    assert "00000000-1111-2222-3333-444444444444" in sids
    titles = {t.title for t in state.threads}
    assert "codex-test" in titles
