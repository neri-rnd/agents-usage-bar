import json
from ai_monitor.agents.base import (
    AgentState, ProcessInfo, ThreadInfo, ThreadRequest, LimitWindow,
    RemoteUsage, AgentError,
)


def test_agent_state_to_dict_roundtrip():
    s = AgentState(
        id="claude",
        label="Claude",
        window=LimitWindow(kind="rolling_5h", pct=72, resets_at="2026-05-22T15:28:00Z",
                           billable=8_300_000, cap=14_000_000),
        secondary_windows=[],
        extra_credits=None,
        threads=[
            ThreadInfo(sid="abc", project="wg", billable=3_200_000, pid=3143,
                       active=False, title=None, first_msg="hi", branch="main",
                       requests=[ThreadRequest(epoch=1, billable=100, user_prompt="x")]),
        ],
        by_model=[{"name": "opus-4-7", "billable": 1000}],
        by_project=[{"name": "wg", "billable": 1000}],
        processes_no_sid=[{"entry": "cursor", "project": "wg", "count": 7}],
        errors=[AgentError(source="remote", code="http_429", at=1747890000)],
        cache_ages={"remote_s": 12, "local_s": 8, "procs_s": 4, "ctx_s": 30},
    )
    d = s.to_dict()
    # JSON-roundtrip must succeed (catches accidental non-serializable fields).
    parsed = json.loads(json.dumps(d))
    assert parsed["id"] == "claude"
    assert parsed["window"]["pct"] == 72
    assert parsed["threads"][0]["requests"][0]["billable"] == 100


from ai_monitor.core.state import MonitorState, write_state_atomic


def test_monitor_state_to_json(tmp_path):
    s = MonitorState(generated_at=1747890123, agents=[])
    p = tmp_path / "state.json"
    write_state_atomic(p, s)
    assert p.exists()
    text = p.read_text()
    assert '"generated_at": 1747890123' in text
    assert '"agents": []' in text


def test_write_state_atomic_uses_pid_suffix(tmp_path, monkeypatch):
    """Two simultaneous writes must not corrupt each other."""
    seen_paths = []

    real_rename = __import__("os").rename
    def spy(src, dst):
        seen_paths.append(src)
        real_rename(src, dst)
    monkeypatch.setattr("os.rename", spy)

    s = MonitorState(generated_at=1, agents=[])
    p = tmp_path / "state.json"
    write_state_atomic(p, s)
    assert any(".tmp." in str(x) for x in seen_paths)


def test_write_state_atomic_cleans_up_tmp_on_error(tmp_path, monkeypatch):
    """If json.dump raises, the .tmp file must not be left behind."""
    def raise_during_dump(*args, **kwargs):
        raise RuntimeError("simulated serialization failure")
    monkeypatch.setattr("ai_monitor.core.state.json.dump", raise_during_dump)

    s = MonitorState(generated_at=1, agents=[])
    p = tmp_path / "state.json"
    import pytest
    with pytest.raises(RuntimeError, match="simulated"):
        write_state_atomic(p, s)
    # No orphan .tmp files left behind.
    leftover = list(tmp_path.glob("*.tmp.*"))
    assert leftover == [], f"orphan .tmp files: {leftover}"


def test_cli_refresh_writes_state(tmp_path, monkeypatch):
    """cmd_refresh writes state.json (the only file the Swift app consumes)."""
    monkeypatch.setattr("ai_monitor.cli.STATE_DIR", tmp_path)
    # Auto-skip relies on these dirs existing; point Claude to a fake-empty dir,
    # Codex stays disabled via the config below.
    fake_claude = tmp_path / "claude-projects"
    fake_claude.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("ai_monitor.agents.claude.PROJECTS_DIR", fake_claude)

    from ai_monitor.cli import cmd_refresh
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("""
[agents.codex]
enabled = false
""")
    # We need ~/.claude/projects to "exist" for auto-skip to allow Claude through.
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    rc = cmd_refresh(config_path=cfg_path, remote_disabled=True)
    assert rc == 0
    assert (tmp_path / "state.json").exists()
