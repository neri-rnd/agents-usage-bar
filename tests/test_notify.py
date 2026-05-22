import json
from ai_monitor.core.notify import check_thresholds, _crossed
from ai_monitor.core.state import MonitorState
from ai_monitor.agents.base import AgentState, LimitWindow


def _agent(pct: int, agent_id="claude") -> AgentState:
    return AgentState(
        id=agent_id, label=agent_id.title(),
        window=LimitWindow(kind="rolling_5h", pct=pct,
                           resets_at="2026-05-22T20:00:00Z",
                           billable=0, cap=14_000_000),
    )


def test_crossed_detects_threshold():
    assert _crossed(prev_pct=70, cur_pct=76, thresholds=[75, 90]) == 75
    assert _crossed(prev_pct=76, cur_pct=80, thresholds=[75, 90]) is None
    assert _crossed(prev_pct=89, cur_pct=91, thresholds=[75, 90]) == 90


def test_check_thresholds_fires_once_per_window(tmp_path):
    state_file = tmp_path / "notified.json"

    fired = []
    def fake_notify(title, body):
        fired.append((title, body))

    # First call at 76% — should fire 75.
    state = MonitorState(generated_at=1, agents=[_agent(76)])
    check_thresholds(state, thresholds=[75, 90, 100],
                     dedup_path=state_file, notify=fake_notify)
    assert len(fired) == 1
    assert "75%" in fired[0][1]

    # Second call still in same window, pct rose to 80 — should NOT re-fire 75.
    state2 = MonitorState(generated_at=2, agents=[_agent(80)])
    check_thresholds(state2, thresholds=[75, 90, 100],
                     dedup_path=state_file, notify=fake_notify)
    assert len(fired) == 1

    # Pct rose to 91 — should fire 90.
    state3 = MonitorState(generated_at=3, agents=[_agent(91)])
    check_thresholds(state3, thresholds=[75, 90, 100],
                     dedup_path=state_file, notify=fake_notify)
    assert len(fired) == 2
    assert "90%" in fired[1][1]


def test_check_thresholds_respects_per_agent_disable(tmp_path):
    state_file = tmp_path / "notified.json"
    fired = []
    def fake_notify(title, body):
        fired.append((title, body))

    state = MonitorState(generated_at=1, agents=[
        _agent(95, agent_id="claude"),
        _agent(95, agent_id="codex"),
    ])
    check_thresholds(
        state, thresholds=[90], dedup_path=state_file, notify=fake_notify,
        enabled_per_agent={"claude": True, "codex": False},
    )
    # Only claude fires; codex suppressed
    assert len(fired) == 1
    assert "Claude" in fired[0][0]
