"""Tests for ai_monitor.audit (formerly lib/context-audit.py)."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from ai_monitor import audit as mod


def make_fake_jsonl(path):
    """Write a small fake transcript with all the artifact categories."""
    big_read_text = "A" * 12000  # large Read result
    big_bash_text = "B" * 15000  # large Bash result
    small_text = "ok"
    failed_text = "error: command not found"

    rows = [
        # --- user prompt with title ---
        {
            "type": "user",
            "timestamp": "2026-05-20T09:30:00Z",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "#thread-title test-thread\nplease audit this thread"},
            ]},
        },
        # --- assistant calls Read on /tmp/foo (1st time) ---
        {
            "type": "assistant",
            "timestamp": "2026-05-20T09:31:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "u1", "name": "Read",
                 "input": {"file_path": "/tmp/foo.ts"}},
            ], "usage": {"input_tokens": 100, "output_tokens": 50}},
        },
        # --- tool_result for u1: large ---
        {
            "type": "user",
            "timestamp": "2026-05-20T09:31:05Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "u1",
                 "content": big_read_text},
            ]},
        },
        # --- assistant calls Bash (large output) ---
        {
            "type": "assistant",
            "timestamp": "2026-05-20T09:32:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "u2", "name": "Bash",
                 "input": {"command": "find ~/.claude -type f"}},
            ]},
        },
        {
            "type": "user",
            "timestamp": "2026-05-20T09:32:30Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "u2",
                 "content": [{"type": "text", "text": big_bash_text}]},
            ]},
        },
        # --- assistant calls Read on same /tmp/foo (duplicate) ---
        {
            "type": "assistant",
            "timestamp": "2026-05-20T09:40:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "u3", "name": "Read",
                 "input": {"file_path": "/tmp/foo.ts"}},
            ]},
        },
        {
            "type": "user",
            "timestamp": "2026-05-20T09:40:05Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "u3",
                 "content": small_text},
            ]},
        },
        # --- failed Bash ---
        {
            "type": "assistant",
            "timestamp": "2026-05-20T09:45:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "u4", "name": "Bash",
                 "input": {"command": "npm run build"}},
            ]},
        },
        {
            "type": "user",
            "timestamp": "2026-05-20T09:45:10Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "u4",
                 "is_error": True,
                 "content": failed_text},
            ]},
        },
    ]
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_report_contents():
    with tempfile.TemporaryDirectory() as d:
        jp = Path(d) / "sid-test.jsonl"
        make_fake_jsonl(jp)
        data = mod.parse_transcript(jp)
        cat = mod.categorise(data)
        report = mod.render(data, cat, "sid-test", jp)

    print(report)
    print("---")

    # Title is picked up.
    assert "test-thread" in report, "title missing"

    # Heavy artifacts section mentions the big Bash command + Read path.
    assert "find ~/.claude -type f" in report, "heavy bash command missing"
    assert "/tmp/foo.ts" in report, "large Read file_path missing"

    # Large Read section is present with the size > 10k.
    assert "Large Read calls" in report, "large reads section missing"

    # Duplicate Reads section names the file.
    assert "Duplicate Reads" in report, "duplicate reads section missing"
    assert "read 2 times" in report, "duplicate count missing"

    # Failed tool calls section catches the npm build failure.
    assert "Failed tool calls" in report, "failed section missing"
    assert "npm run build" in report, "failed command missing"

    # Summary line with totals.
    assert "Total tool_result chars" in report, "summary missing"

    print("OK test_report_contents")


def test_cli_runs(tmp_path, monkeypatch):
    jp = tmp_path / "abc12345.jsonl"
    make_fake_jsonl(jp)
    # Stage a fake ~/.claude/projects layout that find_jsonl can resolve.
    monkeypatch.setattr("ai_monitor.audit.Path.home", lambda: tmp_path)
    proj = tmp_path / ".claude" / "projects" / "fake"
    proj.mkdir(parents=True)
    (proj / "abc12345.jsonl").write_bytes(jp.read_bytes())
    path = mod.find_jsonl("abc12345")
    data = mod.parse_transcript(path)
    cat = mod.categorise(data)
    report = mod.render(data, cat, "abc12345", path)
    assert "Context audit for abc12345" in report


if __name__ == "__main__":
    test_report_contents()
    print("ALL TESTS PASSED")
