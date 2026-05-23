import os
from pathlib import Path
from ai_monitor.agents.claude import (
    ClaudeAgent, billable_from_msg, aggregate_window,
)


def test_billable_excludes_cache_read():
    msg = {
        "message": {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 9999,
            }
        }
    }
    assert billable_from_msg(msg) == 170


def test_billable_handles_missing_usage():
    assert billable_from_msg({}) == 0
    assert billable_from_msg({"message": {}}) == 0


def test_aggregate_window_collects_models_and_threads(claude_jsonl):
    # Run against the fixture for sid "sid-test", project "p".
    # Window is wide so all messages count.
    agg = aggregate_window(
        [(claude_jsonl, "p", "sid-test")],
        since_s=0, midnight_s=0,
    )
    # Fixture has 2 assistant messages: 1700 + 2300 billable.
    assert agg["billable_win"] == 1700 + 2300
    assert agg["cacheread_win"] == 4000
    assert agg["billable_day"] == 1700 + 2300
    assert agg["by_thread"]["sid-test"] == 4000
    assert agg["thread_proj"]["sid-test"] == "p"
    # Model name normalization is left to the renderer; aggregator keeps full string.
    assert "claude-opus-4-7-20260201" in agg["by_model"]


def test_aggregate_window_filters_by_since(claude_jsonl):
    # since_s after the first assistant message → only second one counts.
    # First assistant at 2026-05-22T10:00:05Z, second at 10:02:30Z.
    cutoff = 1779444000 + 60  # 10:01:00 (using verified epoch for 2026-05-22T10:00:00Z)
    agg = aggregate_window(
        [(claude_jsonl, "p", "sid-test")],
        since_s=cutoff, midnight_s=0,
    )
    assert agg["billable_win"] == 2300


def test_claude_snapshot_against_fixture(tmp_path, monkeypatch, claude_jsonl):
    # Stage a fake ~/.claude/projects layout:
    #   tmp/projects/-Users-foo-wg/sid-fixture.jsonl  ← copy of fixture
    proj_dir = tmp_path / "projects" / "-Users-foo-wg"
    proj_dir.mkdir(parents=True)
    (proj_dir / "sid-fixture.jsonl").write_bytes(claude_jsonl.read_bytes())

    monkeypatch.setattr(
        "ai_monitor.agents.claude.PROJECTS_DIR", tmp_path / "projects"
    )

    from ai_monitor.agents.claude import ClaudeAgent
    agent = ClaudeAgent(plan_cap_5h=14_000_000, remote_disabled=True)
    state = agent.snapshot(now_s=1779444000 + 60)  # one minute after first msg

    assert state.id == "claude"
    assert state.window.kind == "rolling_5h"
    assert state.window.cap == 14_000_000
    assert state.window.billable == 1700 + 2300
    # No remote enabled → only local data, no errors.
    assert state.window.resets_at is None or state.window.resets_at.endswith("Z")
    # Thread parsed out.
    sids = {t.sid for t in state.threads}
    assert "sid-fixture" in sids
    titles = {t.title for t in state.threads}
    assert "my-thread" in titles  # picked up #thread-title marker


def test_claude_remote_parse_handles_oauth_payload():
    from ai_monitor.agents.claude import parse_remote_payload
    payload = {
        "five_hour": {"utilization": 72.4, "resets_at": "2026-05-22T15:28:00Z"},
        "seven_day": {"utilization": 22.0, "resets_at": "2026-05-29T15:28:00Z"},
        "extra_usage": {
            "is_enabled": True, "utilization": 12.0,
            "monthly_limit": "10.00", "used_credits": "1.23", "currency": "USD",
        },
    }
    parsed = parse_remote_payload(payload)
    assert parsed["five_hour"].pct == 72
    assert parsed["five_hour"].resets_at == "2026-05-22T15:28:00Z"
    assert parsed["seven_day"].pct == 22
    assert parsed["extra"].pct == 12
    assert parsed["extra"].used == "1.23"


def test_claude_remote_parse_missing_keys():
    from ai_monitor.agents.claude import parse_remote_payload
    out = parse_remote_payload({})
    assert out["five_hour"] is None
    assert out["seven_day"] is None
    assert out["seven_day_sonnet"] is None
    assert out["seven_day_opus"] is None
    assert out["extra"] is None
