# ai_monitor (multi-agent) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `claude_monitor` into `ai_monitor` — a Python-first SwiftBar plugin that tracks both Claude Code and OpenAI Codex CLI usage in one tray icon, with TOML config, threshold notifications, and a unified `monitor` CLI.

**Architecture:** Background `monitor refresh` worker writes `/tmp/ai-monitor/state.json` + a pre-rendered `dropdown.txt`. A 40-line shell plugin `cat`s the dropdown every 30s. All parsing/aggregation lives in Python modules under `lib/ai_monitor/`. A pluggable `Agent` ABC isolates Claude- and Codex-specific concerns so adding more agents later costs only one file.

**Tech Stack:** Python 3.11+ (stdlib only — `tomllib`, `subprocess`, `urllib.request`, `dataclasses`, `pathlib`, `json`), SwiftBar (bash plugin), pytest for tests, macOS `osascript` for notifications, macOS `security` CLI for Keychain.

**Spec:** [`docs/superpowers/specs/2026-05-22-multi-agent-monitor-design.md`](../specs/2026-05-22-multi-agent-monitor-design.md)

---

## File map

**Created**
- `pyproject.toml` — package + test config
- `Makefile` — `make test`, `make lint`, `make install`
- `lib/ai_monitor/__init__.py`
- `lib/ai_monitor/core/__init__.py`
- `lib/ai_monitor/core/jsonl.py` — shared JSONL walker
- `lib/ai_monitor/core/processes.py` — `ps`/`lsof` helpers
- `lib/ai_monitor/core/state.py` — `MonitorState` dataclass + JSON serialization
- `lib/ai_monitor/core/render.py` — SwiftBar formatter
- `lib/ai_monitor/core/notify.py` — threshold notifications
- `lib/ai_monitor/core/config.py` — TOML loader
- `lib/ai_monitor/agents/__init__.py`
- `lib/ai_monitor/agents/base.py` — `Agent` ABC, `AgentState` / `ProcessInfo` / `ThreadInfo` dataclasses
- `lib/ai_monitor/agents/claude.py`
- `lib/ai_monitor/agents/codex.py`
- `lib/ai_monitor/cli.py` — `monitor status / refresh / audit / doctor / install`
- `lib/ai_monitor/audit.py` — ported `context-audit.py`
- `ai.30s.sh` — new thin plugin (40 lines)
- `tests/conftest.py`
- `tests/test_jsonl.py`
- `tests/test_processes.py`
- `tests/test_claude_agent.py`
- `tests/test_codex_agent.py`
- `tests/test_state.py`
- `tests/test_render.py`
- `tests/test_render__dropdown.txt` (golden file)
- `tests/test_config.py`
- `tests/test_notify.py`
- `tests/fixtures/claude_session.jsonl`
- `tests/fixtures/codex_session.jsonl`
- `tests/fixtures/auth.json`

**Modified**
- `README.md` — title, install section, plugin filename
- `tests/test_context_audit.py` — reworked to import from `ai_monitor.audit`

**Deleted (after Phase B verified)**
- `claude.30s.sh`
- `lib/claude-usage-local-cache.sh`
- `lib/claude-usage-aggregate.py`
- `lib/claude-processes.sh`
- `lib/claude-thread-context.sh`
- `lib/claude-thread-context.py`
- `lib/claude-thread-detail-cache.sh`
- `lib/claude-thread-detail.py`
- `lib/claude-usage-remote.sh`
- `lib/context-audit.py`

**Kept (still invoked from menu)**
- `lib/copy-thread-title.sh`

---

## Phase 0 — Preflight

### Task 0: Initialize git repo and pin Python

**Files:** `.gitignore` (create), `pyproject.toml` (create)

- [ ] **Step 1: Decide repo location**

Per the spec note, `/Users/user/AI/limits` is not currently a git repo (source lives in Dropbox). Ask the user where to do version control before continuing:

```
1. git init here at /Users/user/AI/limits and commit locally
2. Switch to the Dropbox source path and commit there
3. Skip version control for this plan (use `git stash`-style local snapshots only)
```

Halt and ask if not already decided. The rest of this plan assumes option 1 (git init here).

- [ ] **Step 2: Initialize git**

```bash
cd /Users/user/AI/limits
git init
```

Expected: `Initialized empty Git repository in /Users/user/AI/limits/.git/`

- [ ] **Step 3: Create .gitignore**

Write `.gitignore`:

```
__pycache__/
*.pyc
.pytest_cache/
.venv/
*.egg-info/
/tmp-*

# SwiftBar runtime caches
/tmp/ai-monitor/
```

- [ ] **Step 4: Create pyproject.toml**

Write `pyproject.toml`:

```toml
[project]
name = "ai_monitor"
version = "0.1.0"
description = "Menubar monitor for Claude Code and OpenAI Codex CLI usage"
requires-python = ">=3.11"

[project.scripts]
monitor = "ai_monitor.cli:main"

[tool.setuptools.packages.find]
where = ["lib"]
include = ["ai_monitor*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["lib"]
```

- [ ] **Step 5: Create Makefile**

Write `Makefile`:

```makefile
.PHONY: test lint install dev-install

test:
	python3 -m pytest tests/ -v

lint:
	python3 -m py_compile lib/ai_monitor/**/*.py

dev-install:
	python3 -m pip install --user -e .

install:
	python3 -m pip install --user .
```

- [ ] **Step 6: Verify Python 3.11+ and pytest**

```bash
python3 --version
python3 -c "import tomllib; print('tomllib OK')"
python3 -m pip install --user pytest
python3 -m pytest --version
```

Expected: Python ≥3.11, tomllib import succeeds, pytest installed.

- [ ] **Step 7: Initial commit**

```bash
git add .gitignore pyproject.toml Makefile
git commit -m "chore: bootstrap ai_monitor python package"
```

---

## Phase A — Internal refactor (Claude logic ported to Python, no plugin behavior change)

### Task 1: Package skeleton + fixtures

**Files:**
- Create: `lib/ai_monitor/__init__.py`, `lib/ai_monitor/core/__init__.py`, `lib/ai_monitor/agents/__init__.py`
- Create: `tests/conftest.py`, `tests/fixtures/claude_session.jsonl`, `tests/fixtures/codex_session.jsonl`, `tests/fixtures/auth.json`

- [ ] **Step 1: Create empty package init files**

```bash
mkdir -p lib/ai_monitor/core lib/ai_monitor/agents tests/fixtures
touch lib/ai_monitor/__init__.py lib/ai_monitor/core/__init__.py lib/ai_monitor/agents/__init__.py
```

- [ ] **Step 2: Write tests/conftest.py**

```python
from pathlib import Path
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def claude_jsonl():
    return FIXTURES / "claude_session.jsonl"


@pytest.fixture
def codex_jsonl():
    return FIXTURES / "codex_session.jsonl"


@pytest.fixture
def codex_auth_json():
    return FIXTURES / "auth.json"
```

- [ ] **Step 3: Write tests/fixtures/claude_session.jsonl**

Five lines representing a real Claude transcript shape. Each line is a JSON object on its own line:

```
{"type":"user","timestamp":"2026-05-22T10:00:00Z","gitBranch":"feat-x","message":{"role":"user","content":[{"type":"text","text":"#thread-title my-thread\nset up tests"}]}}
{"type":"assistant","timestamp":"2026-05-22T10:00:05Z","message":{"role":"assistant","model":"claude-opus-4-7-20260201","content":[{"type":"text","text":"ok"}],"usage":{"input_tokens":1000,"output_tokens":200,"cache_creation_input_tokens":500,"cache_read_input_tokens":4000}}}
{"type":"user","timestamp":"2026-05-22T10:01:00Z","message":{"role":"user","content":[{"type":"text","text":"<ide_opened_file>ignored</ide_opened_file>"}]}}
{"type":"user","timestamp":"2026-05-22T10:02:00Z","message":{"role":"user","content":[{"type":"text","text":"now run the suite"}]}}
{"type":"assistant","timestamp":"2026-05-22T10:02:30Z","message":{"role":"assistant","model":"claude-opus-4-7-20260201","content":[{"type":"text","text":"running"}],"usage":{"input_tokens":2000,"output_tokens":300,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}
```

- [ ] **Step 4: Write tests/fixtures/codex_session.jsonl**

Five lines mirroring Codex's actual shape (verified earlier in this conversation: `{type, payload, timestamp}` with type values `session_meta`, `event_msg`, `response_item`):

```
{"type":"session_meta","timestamp":"2026-05-22T10:00:00Z","payload":{"id":"019e2c63-7e6f-70b0-b304-e84fabf52597","cwd":"/Users/user/AI/limits"}}
{"type":"response_item","timestamp":"2026-05-22T10:00:01Z","payload":{"role":"user","content":[{"type":"text","text":"#thread-title codex-test\nhello"}]}}
{"type":"event_msg","timestamp":"2026-05-22T10:00:10Z","payload":{"kind":"turn_complete","info":{"model":"gpt-5","total_token_usage":{"input_tokens":800,"output_tokens":300,"cached_input_tokens":200,"reasoning_output_tokens":150}}}}
{"type":"response_item","timestamp":"2026-05-22T10:01:00Z","payload":{"role":"user","content":[{"type":"text","text":"<bash-input>ignored</bash-input>"}]}}
{"type":"event_msg","timestamp":"2026-05-22T10:01:30Z","payload":{"kind":"turn_complete","info":{"model":"o3-mini","total_token_usage":{"input_tokens":1200,"output_tokens":400,"cached_input_tokens":0,"reasoning_output_tokens":500}}}}
```

- [ ] **Step 5: Write tests/fixtures/auth.json**

```json
{
  "OPENAI_API_KEY": null,
  "auth_mode": "chatgpt",
  "last_refresh": "2026-05-21T17:11:00Z",
  "tokens": {
    "access_token": "FAKE_TOKEN_FOR_TESTS",
    "account_id": "abc",
    "id_token": "xyz",
    "refresh_token": "rrr"
  }
}
```

- [ ] **Step 6: Run pytest to confirm collection works**

```bash
make test
```

Expected: `collected 0 items` (no tests yet, but pytest discovers the package).

- [ ] **Step 7: Commit**

```bash
git add lib/ai_monitor/ tests/
git commit -m "feat(ai_monitor): package skeleton + fixtures"
```

---

### Task 2: `core/jsonl.py` — shared transcript walker

**Files:**
- Create: `lib/ai_monitor/core/jsonl.py`
- Test: `tests/test_jsonl.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/test_jsonl.py`:

```python
from ai_monitor.core.jsonl import iter_messages, parse_iso_to_epoch, extract_user_text, find_title


def test_iter_messages_yields_json_objects(claude_jsonl):
    msgs = list(iter_messages(claude_jsonl))
    assert len(msgs) == 5
    assert msgs[0]["type"] == "user"
    assert msgs[1]["type"] == "assistant"


def test_iter_messages_skips_blank_and_malformed(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"type":"user"}\n\n{not valid}\n{"type":"assistant"}\n')
    msgs = list(iter_messages(p))
    assert [m["type"] for m in msgs] == ["user", "assistant"]


def test_parse_iso_handles_z_suffix_and_micros():
    assert parse_iso_to_epoch("2026-05-22T10:00:00Z") == 1779444000
    assert parse_iso_to_epoch("2026-05-22T10:00:00.123Z") == 1779444000
    assert parse_iso_to_epoch("") == 0
    assert parse_iso_to_epoch("not a date") == 0


def test_extract_user_text_str_content():
    j = {"type": "user", "message": {"content": "hello"}}
    assert extract_user_text(j) == "hello"


def test_extract_user_text_list_with_text_block():
    j = {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}
    assert extract_user_text(j) == "hi"


def test_extract_user_text_tool_result_returns_empty():
    j = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1"}]}}
    assert extract_user_text(j) == ""


def test_extract_user_text_non_user_returns_none():
    j = {"type": "assistant", "message": {"content": "ignored"}}
    assert extract_user_text(j) is None


def test_find_title_returns_last_marker():
    assert find_title("#thread-title first\n#thread-title second") == "second"
    assert find_title("no marker here") is None
    assert find_title("#thread-title   spaced   ") == "spaced"
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
python3 -m pytest tests/test_jsonl.py -v
```

Expected: ImportError / collection failure (`ai_monitor.core.jsonl` doesn't exist yet).

- [ ] **Step 3: Implement `core/jsonl.py`**

Write `lib/ai_monitor/core/jsonl.py`:

```python
"""Shared JSONL transcript helpers used by both Claude and Codex agents."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

TITLE_TAG = "#thread-title"


def iter_messages(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a .jsonl file, skipping blank/malformed lines."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def parse_iso_to_epoch(ts: str) -> int:
    """Parse an ISO8601 timestamp to a unix epoch. Returns 0 on failure."""
    if not ts:
        return 0
    try:
        # fromisoformat handles "+00:00" but not bare "Z" until 3.11.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


def extract_user_text(j: dict) -> Optional[str]:
    """Return the user-text portion of a message.

    Returns None for non-user messages so callers can skip; returns ""
    for user messages whose content is purely a tool_result (those are
    not new requests).
    """
    if j.get("type") != "user":
        return None
    msg = j.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return ""
            if block.get("type") == "text":
                return block.get("text") or ""
    return ""


def find_title(text: str) -> Optional[str]:
    """Return the last #thread-title marker value in `text`, or None."""
    last = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(TITLE_TAG):
            rest = s[len(TITLE_TAG):].strip()
            if rest:
                last = rest
    return last
```

- [ ] **Step 4: Run tests, confirm pass**

```bash
python3 -m pytest tests/test_jsonl.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add lib/ai_monitor/core/jsonl.py tests/test_jsonl.py
git commit -m "feat(core): shared JSONL walker with parse/extract helpers"
```

---

### Task 3: `core/processes.py` — ps/lsof helpers

**Files:**
- Create: `lib/ai_monitor/core/processes.py`
- Test: `tests/test_processes.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/test_processes.py`:

```python
from ai_monitor.core.processes import (
    parse_ps_line,
    extract_flag_value,
    classify_entry,
    summarize_args,
)


def test_parse_ps_line_extracts_pid_and_command():
    pid, cmd = parse_ps_line("  3143 /usr/local/bin/claude --resume abc-def --model opus")
    assert pid == 3143
    assert cmd == "/usr/local/bin/claude --resume abc-def --model opus"


def test_parse_ps_line_handles_no_match():
    assert parse_ps_line("not a ps line") == (None, None)


def test_extract_flag_value_finds_resume():
    cmd = "/usr/local/bin/claude --resume 4fe966a0-1234 --model opus"
    assert extract_flag_value(cmd, "--resume") == "4fe966a0-1234"


def test_extract_flag_value_returns_none_when_absent():
    assert extract_flag_value("claude --foo bar", "--resume") is None


def test_classify_entry_cursor():
    assert classify_entry("/User/foo/.cursor/extensions/anthropic.claude/native-binary/claude --x") == "cursor"


def test_classify_entry_vscode():
    assert classify_entry("/Applications/Visual Studio Code.app/vscode-server/.../claude --x") == "vscode"


def test_classify_entry_pencil():
    assert classify_entry("/usr/local/pencil/claude --x") == "pencil"


def test_classify_entry_default_cli():
    assert classify_entry("/Users/foo/.claude/local/bin/claude") == "cli"


def test_summarize_args_picks_known_flags():
    cmd = "claude --model claude-opus-4-7-20260201 --effort high --name worker"
    assert summarize_args(cmd) == "name=worker m=opus-4-7 e=high"
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m pytest tests/test_processes.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `core/processes.py`**

Write `lib/ai_monitor/core/processes.py`:

```python
"""Helpers for parsing `ps` / `lsof` output without forking per field."""
from __future__ import annotations
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional


PS_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+)$")
FLAG_VAL_RE_CACHE: dict[str, re.Pattern] = {}


def parse_ps_line(line: str) -> tuple[Optional[int], Optional[str]]:
    """Parse one `ps -o pid=,command=` line into (pid, command)."""
    m = PS_LINE_RE.match(line)
    if not m:
        return None, None
    return int(m.group(1)), m.group(2)


def extract_flag_value(cmd: str, flag: str) -> Optional[str]:
    """Extract `<value>` from `... <flag> <value> ...`. Returns None if absent."""
    pat = FLAG_VAL_RE_CACHE.get(flag)
    if pat is None:
        pat = re.compile(rf"{re.escape(flag)}\s+(\S+)")
        FLAG_VAL_RE_CACHE[flag] = pat
    m = pat.search(cmd)
    return m.group(1) if m else None


def classify_entry(cmd: str) -> str:
    """Best-effort classification of the launcher based on binary path."""
    if "cursor/extensions" in cmd or ".cursor/" in cmd:
        return "cursor"
    if "vscode" in cmd or "Visual Studio Code" in cmd:
        return "vscode"
    if "pencil" in cmd:
        return "pencil"
    return "cli"


_MODEL_STRIP_RE = re.compile(r"^claude-|^codex-|-20\d+$")


def summarize_args(cmd: str) -> str:
    """Build a short summary string from claude/codex flags found in `cmd`."""
    parts = []
    name = extract_flag_value(cmd, "--name")
    if name:
        parts.append(f"name={name}")
    model = extract_flag_value(cmd, "--model")
    if model:
        short = _MODEL_STRIP_RE.sub("", model)
        # Strip the date suffix that survived (e.g. "opus-4-7-20260201" -> "opus-4-7")
        short = re.sub(r"-20\d+$", "", short)
        parts.append(f"m={short}")
    effort = extract_flag_value(cmd, "--effort")
    if effort:
        parts.append(f"e={effort}")
    return " ".join(parts)


def pid_cwd(pid: int) -> Optional[str]:
    """Return the working directory of a pid via `lsof`. None on permission error."""
    if not shutil.which("lsof"):
        return None
    try:
        out = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=2,
        )
        for line in out.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return None


def list_pids(binary_patterns: list[str]) -> list[tuple[int, str]]:
    """Run `ps -axww` and return [(pid, command), ...] matching any pattern.

    `binary_patterns` are simple substring matches against the command. The
    caller filters more precisely if needed.
    """
    try:
        out = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    results = []
    for line in out.stdout.splitlines():
        pid, cmd = parse_ps_line(line)
        if pid is None or cmd is None:
            continue
        if any(p in cmd for p in binary_patterns):
            results.append((pid, cmd))
    return results
```

- [ ] **Step 4: Run, confirm pass**

```bash
python3 -m pytest tests/test_processes.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add lib/ai_monitor/core/processes.py tests/test_processes.py
git commit -m "feat(core): ps/lsof helpers with pattern matching"
```

---

### Task 4: `agents/base.py` — Agent ABC and shared dataclasses

**Files:**
- Create: `lib/ai_monitor/agents/base.py`

- [ ] **Step 1: Write the test for dataclass serialization**

Append to `tests/test_state.py` (create the file):

```python
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
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m pytest tests/test_state.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `agents/base.py`**

Write `lib/ai_monitor/agents/base.py`:

```python
"""Agent abstract base + shared dataclasses for the MonitorState schema."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class ThreadRequest:
    epoch: int
    billable: int
    user_prompt: str


@dataclass
class ThreadInfo:
    sid: str
    project: str
    billable: int
    pid: Optional[int]
    active: bool
    title: Optional[str]
    first_msg: Optional[str]
    branch: Optional[str]
    requests: list[ThreadRequest] = field(default_factory=list)


@dataclass
class ProcessInfo:
    pid: int
    sid: Optional[str]
    cwd: Optional[str]
    entry: str
    args: str


@dataclass
class LimitWindow:
    kind: str               # "rolling_5h", "rolling_7d", "weekly", ...
    pct: int
    resets_at: Optional[str]  # ISO8601
    billable: int
    cap: int


@dataclass
class RemoteUsage:
    pct: int
    used: str
    limit: str
    ccy: str


@dataclass
class AgentError:
    source: str    # "remote", "local", "procs"
    code: str
    at: int


@dataclass
class AgentState:
    id: str
    label: str
    window: Optional[LimitWindow]
    secondary_windows: list[LimitWindow] = field(default_factory=list)
    extra_credits: Optional[RemoteUsage] = None
    threads: list[ThreadInfo] = field(default_factory=list)
    by_model: list[dict] = field(default_factory=list)
    by_project: list[dict] = field(default_factory=list)
    processes_no_sid: list[dict] = field(default_factory=list)
    errors: list[AgentError] = field(default_factory=list)
    cache_ages: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Agent(ABC):
    """One AI tool we track. Implementations live in agents/claude.py, agents/codex.py."""
    id: str = ""
    label: str = ""

    @abstractmethod
    def snapshot(self, now_s: int) -> AgentState:
        """Build a full AgentState. Must not raise — collect errors into state.errors."""
        ...
```

- [ ] **Step 4: Run, confirm pass**

```bash
python3 -m pytest tests/test_state.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add lib/ai_monitor/agents/base.py tests/test_state.py
git commit -m "feat(agents): Agent ABC and shared dataclasses"
```

---

### Task 5: `agents/claude.py` — usage aggregation

**Files:**
- Create: `lib/ai_monitor/agents/claude.py`
- Test: `tests/test_claude_agent.py`

- [ ] **Step 1: Write the failing tests for billable + aggregation**

Write `tests/test_claude_agent.py`:

```python
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
    cutoff = 1779444000 + 60  # 10:01:00
    agg = aggregate_window(
        [(claude_jsonl, "p", "sid-test")],
        since_s=cutoff, midnight_s=0,
    )
    assert agg["billable_win"] == 2300
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m pytest tests/test_claude_agent.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement billable + aggregator portion of `agents/claude.py`**

Write `lib/ai_monitor/agents/claude.py` (this file grows in later tasks; for now only the aggregation pieces):

```python
"""Claude Code agent implementation."""
from __future__ import annotations
import collections
import time
from pathlib import Path
from typing import Iterable, Optional

from ..agents.base import Agent, AgentState
from ..core.jsonl import iter_messages, parse_iso_to_epoch

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


def project_short(dir_name: str) -> str:
    """Decode Claude's project dir name to a human-readable short name.

    Claude encodes paths like `-Users-foo-AI-limits`. We want the last
    path segment, e.g. "limits".
    """
    decoded = dir_name.lstrip("-").replace("-", "/")
    return decoded.rstrip("/").split("/")[-1] or dir_name


class ClaudeAgent(Agent):
    id = "claude"
    label = "Claude"

    def snapshot(self, now_s: int) -> AgentState:
        # Filled in in Task 6.
        raise NotImplementedError("snapshot wired up in Task 6")
```

- [ ] **Step 4: Run tests, confirm pass**

```bash
python3 -m pytest tests/test_claude_agent.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add lib/ai_monitor/agents/claude.py tests/test_claude_agent.py
git commit -m "feat(claude): billable formula and per-window aggregator"
```

---

### Task 6: `ClaudeAgent.snapshot` — assemble AgentState

**Files:**
- Modify: `lib/ai_monitor/agents/claude.py`
- Test: `tests/test_claude_agent.py`

- [ ] **Step 1: Add the test**

Append to `tests/test_claude_agent.py`:

```python
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
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m pytest tests/test_claude_agent.py::test_claude_snapshot_against_fixture -v
```

Expected: `NotImplementedError` from the placeholder.

- [ ] **Step 3: Replace `ClaudeAgent.snapshot` with the full implementation**

Replace the bottom of `lib/ai_monitor/agents/claude.py`:

```python
from ..agents.base import AgentState, LimitWindow, ThreadInfo, ThreadRequest, AgentError
from ..core.jsonl import extract_user_text, find_title
from ..core.processes import list_pids, extract_flag_value, classify_entry, pid_cwd, summarize_args


class ClaudeAgent(Agent):
    id = "claude"
    label = "Claude"

    def __init__(self, plan_cap_5h: int, remote_disabled: bool = False):
        self.plan_cap_5h = plan_cap_5h
        self.remote_disabled = remote_disabled

    def snapshot(self, now_s: int) -> AgentState:
        errors: list[AgentError] = []
        # 1) Discover transcripts modified in last 2 days.
        files = list(self._recent_transcripts())

        since_s = now_s - 5 * 3600  # local fallback window
        midnight_s = self._local_midnight(now_s)

        agg = aggregate_window(files, since_s=since_s, midnight_s=midnight_s)

        # 2) Build per-thread context (first_msg / branch / title).
        ctx = self._scan_thread_context(files)

        threads = []
        for sid, bill in agg["by_thread"].most_common(20):
            c = ctx.get(sid, {})
            threads.append(ThreadInfo(
                sid=sid, project=agg["thread_proj"].get(sid, "?"),
                billable=bill, pid=None, active=False,
                title=c.get("title"), first_msg=c.get("first_msg"),
                branch=c.get("branch"),
            ))

        # 3) Live processes — map sid → pid where possible.
        sid_to_pid, no_sid = self._detect_processes()
        for t in threads:
            t.pid = sid_to_pid.get(t.sid)
        # 4) Active sids = transcripts with mtime in last 60s.
        active = self._active_sids(files, now_s)
        for t in threads:
            if t.sid in active:
                t.active = True

        # 5) Window object (local-only fallback; remote refines it in a later task).
        pct = int(agg["billable_win"] * 100 / max(self.plan_cap_5h, 1) + 0.5)
        window = LimitWindow(
            kind="rolling_5h", pct=pct, resets_at=None,
            billable=agg["billable_win"], cap=self.plan_cap_5h,
        )

        return AgentState(
            id=self.id, label=self.label,
            window=window,
            secondary_windows=[],
            extra_credits=None,
            threads=threads,
            by_model=[{"name": m, "billable": c} for m, c in agg["by_model"].most_common()],
            by_project=[{"name": p, "billable": c} for p, c in agg["by_project"].most_common(5)],
            processes_no_sid=no_sid,
            errors=errors,
            cache_ages={},
        )

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
                if i > 80 and not entry["title"]:
                    # past header, only keep scanning for titles
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
```

- [ ] **Step 4: Run all tests**

```bash
python3 -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add lib/ai_monitor/agents/claude.py tests/test_claude_agent.py
git commit -m "feat(claude): full snapshot — threads, processes, active flags"
```

---

### Task 7: Claude remote usage fetch (OAuth from Keychain)

**Files:**
- Modify: `lib/ai_monitor/agents/claude.py`
- Test: `tests/test_claude_agent.py`

- [ ] **Step 1: Add the test (mocked)**

Append to `tests/test_claude_agent.py`:

```python
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
    assert parse_remote_payload({}) == {"five_hour": None, "seven_day": None, "extra": None}
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m pytest tests/test_claude_agent.py -v -k remote
```

Expected: ImportError.

- [ ] **Step 3: Add remote-fetch logic to `agents/claude.py`**

Append to `lib/ai_monitor/agents/claude.py`:

```python
import json
import subprocess
import urllib.request
from ..agents.base import RemoteUsage


def parse_remote_payload(d: dict) -> dict:
    """Parse `/api/oauth/usage` payload into a dict of windows + extra credits."""
    def win(key):
        v = d.get(key)
        if not v:
            return None
        return LimitWindow(
            kind="rolling_5h" if key == "five_hour" else "rolling_7d",
            pct=int((v.get("utilization") or 0) + 0.5),
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
    return {"five_hour": win("five_hour"), "seven_day": win("seven_day"), "extra": extra}


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
```

Now wire remote into `ClaudeAgent.snapshot` — modify the snapshot method to call remote when `not self.remote_disabled`:

```python
    def snapshot(self, now_s: int) -> AgentState:
        errors: list[AgentError] = []
        files = list(self._recent_transcripts())

        # Remote first (so we can anchor the local window on the server's reset).
        remote_windows = {"five_hour": None, "seven_day": None, "extra": None}
        if not self.remote_disabled:
            token = fetch_oauth_token()
            if token:
                payload, err = fetch_remote_usage(token)
                if payload:
                    remote_windows = parse_remote_payload(payload)
                else:
                    errors.append(AgentError(source="remote", code=err or "unknown", at=now_s))
            else:
                errors.append(AgentError(source="remote", code="no_token", at=now_s))

        # Anchor local window on the remote reset if known.
        anchored_since = self._anchor_since(now_s, remote_windows["five_hour"])
        midnight_s = self._local_midnight(now_s)
        agg = aggregate_window(files, since_s=anchored_since, midnight_s=midnight_s)
        # ... rest unchanged from Task 6 ...
        ctx = self._scan_thread_context(files)
        threads = []
        for sid, bill in agg["by_thread"].most_common(20):
            c = ctx.get(sid, {})
            threads.append(ThreadInfo(
                sid=sid, project=agg["thread_proj"].get(sid, "?"),
                billable=bill, pid=None, active=False,
                title=c.get("title"), first_msg=c.get("first_msg"),
                branch=c.get("branch"),
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
            secondary_windows=[w for w in [remote_windows["seven_day"]] if w],
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
```

(Delete the placeholder `snapshot` body from Task 6; the snapshot above is the full version.)

- [ ] **Step 4: Run all tests**

```bash
python3 -m pytest tests/ -v
```

Expected: all pass; `test_claude_snapshot_against_fixture` still passes because the test sets `remote_disabled=True`.

- [ ] **Step 5: Commit**

```bash
git add lib/ai_monitor/agents/claude.py tests/test_claude_agent.py
git commit -m "feat(claude): remote OAuth /usage fetch with server-anchored window"
```

---

### Task 8: `core/state.py` — MonitorState builder

**Files:**
- Create: `lib/ai_monitor/core/state.py`
- Modify: `tests/test_state.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_state.py`:

```python
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
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m pytest tests/test_state.py -v -k monitor_state
```

Expected: ImportError.

- [ ] **Step 3: Implement `core/state.py`**

Write `lib/ai_monitor/core/state.py`:

```python
"""MonitorState — top-level snapshot written to /tmp/ai-monitor/state.json."""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List

from ..agents.base import AgentState


@dataclass
class MonitorState:
    generated_at: int
    agents: List[AgentState] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "agents": [a.to_dict() for a in self.agents],
        }


def write_state_atomic(path: Path, state: MonitorState) -> None:
    """Write state to disk atomically.

    Uses a PID-suffixed .tmp + os.rename so concurrent writers can't trample
    each other (each writer's .tmp filename is unique).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2)
    os.rename(tmp, path)


def write_text_atomic(path: Path, text: str) -> None:
    """Same atomic pattern, for the rendered dropdown.txt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.rename(tmp, path)
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_state.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add lib/ai_monitor/core/state.py tests/test_state.py
git commit -m "feat(core): MonitorState + atomic PID-suffixed writes"
```

---

### Task 9: `core/render.py` — SwiftBar formatter (tray + dropdown)

**Files:**
- Create: `lib/ai_monitor/core/render.py`
- Test: `tests/test_render.py`, `tests/test_render__dropdown.txt`

- [ ] **Step 1: Write the failing test using a golden file**

Write `tests/test_render.py`:

```python
import json
from pathlib import Path
from ai_monitor.core.render import render_tray, render_dropdown
from ai_monitor.core.state import MonitorState
from ai_monitor.agents.base import (
    AgentState, LimitWindow, ThreadInfo, ThreadRequest, AgentError, RemoteUsage,
)


def _sample_state() -> MonitorState:
    claude = AgentState(
        id="claude", label="Claude",
        window=LimitWindow(kind="rolling_5h", pct=72,
                           resets_at="2026-05-22T15:28:00Z",
                           billable=8_300_000, cap=14_000_000),
        secondary_windows=[LimitWindow(kind="rolling_7d", pct=22,
                                       resets_at="2026-05-29T15:28:00Z",
                                       billable=0, cap=0)],
        extra_credits=RemoteUsage(pct=12, used="1.23", limit="10.00", ccy="USD"),
        threads=[
            ThreadInfo(sid="4fe966a0-aaaa-bbbb-cccc-dddddddddddd", project="wg",
                       billable=3_200_000, pid=3143, active=False,
                       title=None, first_msg="set up tests", branch="main",
                       requests=[
                           ThreadRequest(epoch=1779789660, billable=120_000,
                                         user_prompt="find ~/.claude -type f"),
                           ThreadRequest(epoch=1779789900, billable=450_000,
                                         user_prompt="npm run build"),
                       ]),
        ],
        by_model=[{"name": "claude-opus-4-7-20260201", "billable": 3_600_000}],
        by_project=[{"name": "wg", "billable": 2_400_000}],
        processes_no_sid=[{"entry": "cursor", "project": "wg", "count": 7}],
        errors=[],
        cache_ages={"remote_s": 12, "local_s": 8, "procs_s": 4},
    )
    codex = AgentState(
        id="codex", label="Codex",
        window=LimitWindow(kind="weekly", pct=89,
                           resets_at="2026-05-25T09:00:00Z",
                           billable=0, cap=0),
        threads=[],
        by_model=[],
        by_project=[],
        processes_no_sid=[],
        errors=[AgentError(source="remote", code="no_endpoint", at=0)],
        cache_ages={"local_s": 14, "procs_s": 4},
    )
    return MonitorState(generated_at=1779793200, agents=[claude, codex])


def test_render_tray_compact():
    state = _sample_state()
    out = render_tray(state, color_hints=False)
    assert out == "C72 X89"


def test_render_tray_with_color_hint():
    state = _sample_state()
    out = render_tray(state, color_hints=True)
    # 89% → above 75 but below 90 → 🟡
    assert "🟡" in out


def test_render_tray_omits_disabled_agent():
    state = _sample_state()
    # Remove codex
    state.agents = [state.agents[0]]
    assert render_tray(state, color_hints=False) == "C72"


def test_render_dropdown_matches_golden(tmp_path):
    state = _sample_state()
    out = render_dropdown(state)
    golden = (Path(__file__).parent / "test_render__dropdown.txt").read_text()
    assert out == golden, "dropdown drift — see diff"
```

- [ ] **Step 2: Generate the golden file**

Write `tests/test_render__dropdown.txt` (treat this as the spec — once it passes, freeze it):

```
72% used (5h window) | size=12
resets in 0h44m (at 18:28) | size=12
7-day: 22% used · resets in 7d0h | size=11
extra credits: 12% (1.23 / 10.00 USD) | size=11

─── Codex ─── | size=12
89% used (weekly window) | size=12
last error: no_endpoint | size=10

─── Threads · last 5h ─── | size=12
  🟢 alive · ✏️ writing now · 📋 click to copy '#thread-title ' | size=10
C 3.2M  4fe966a0  wg            🟢  set up tests  📋 | size=12
       └ 18:01  120k  find ~/.claude -type f | size=11
       └ 18:05  450k  npm run build | size=11
  ── processes w/o sid ── | size=10
    cursor/wg × 7 | size=11

─── By model ─── | size=12
  C  opus-4-7      3.6M | size=12

─── By project ─── | size=12
  wg              2.4M | size=12

─── caches ─── | size=10
  Claude: remote 12s · local 8s · procs 4s | size=10
  Codex:  local 14s · procs 4s | size=10
```

(Note: HH:MM values are computed from epoch in the engineer's timezone — the golden file uses the test's frozen epoch `1779789660` and `1779789900`. Adjust to UTC-based formatting in render.py — see Step 3 — so the golden is timezone-stable.)

- [ ] **Step 3: Run, confirm fail**

```bash
python3 -m pytest tests/test_render.py -v
```

Expected: ImportError.

- [ ] **Step 4: Implement `core/render.py`**

Write `lib/ai_monitor/core/render.py`:

```python
"""SwiftBar formatter: MonitorState → tray string + dropdown.txt body."""
from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Optional

from .state import MonitorState
from ..agents.base import AgentState, LimitWindow


_TRAY_SANITIZE = re.compile(r"[^A-Za-z0-9 ·%—]")


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1000:.0f}k"
    return str(n)


def _hhmm_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M")


def _countdown(resets_at_iso: Optional[str], now_s: int) -> tuple[str, str]:
    """Return ("Xh Ym", "HH:MM") for "resets in" / "at". Empty strings on failure."""
    if not resets_at_iso:
        return "", ""
    try:
        s = resets_at_iso[:-1] + "+00:00" if resets_at_iso.endswith("Z") else resets_at_iso
        dt = datetime.fromisoformat(s)
        target = int(dt.timestamp())
        diff = max(0, target - now_s)
        h, m = diff // 3600, (diff % 3600) // 60
        return f"{h}h{m}m", _hhmm_utc(target)
    except (ValueError, TypeError):
        return "", ""


def _agent_letter(a: AgentState) -> str:
    return {"claude": "C", "codex": "X"}.get(a.id, a.id[0].upper())


def render_tray(state: MonitorState, color_hints: bool = False) -> str:
    """Build the single tray string, e.g. "C72 X89"."""
    parts = []
    pct_max = -1
    for a in state.agents:
        letter = _agent_letter(a)
        pct = a.window.pct if a.window else None
        if pct is None:
            parts.append(f"{letter}—")
        else:
            parts.append(f"{letter}{pct}")
            pct_max = max(pct_max, pct)
    body = " ".join(parts)
    if color_hints and pct_max >= 0:
        if pct_max > 90:
            body = "🔴 " + body
        elif pct_max >= 75:
            body = "🟡 " + body
        else:
            body = "🟢 " + body
    # Sanitize + cap at 12 chars worth of content (color emoji not counted).
    sanitized = _TRAY_SANITIZE.sub("", body)
    return sanitized[:16]  # color emoji is 1 grapheme but multiple chars


def render_dropdown(state: MonitorState) -> str:
    """Build the full SwiftBar dropdown body."""
    lines: list[str] = []
    now_s = state.generated_at

    # Primary agent (Claude) at top.
    primary = state.agents[0] if state.agents else None
    if primary and primary.window:
        w = primary.window
        until, at = _countdown(w.resets_at, now_s)
        lines.append(f"{w.pct}% used (5h window) | size=12")
        if until:
            lines.append(f"resets in {until} (at {at}) | size=12")
        for sw in primary.secondary_windows:
            su, _ = _countdown(sw.resets_at, now_s)
            tail = f" · resets in {su}" if su else ""
            lines.append(f"7-day: {sw.pct}% used{tail} | size=11")
        if primary.extra_credits:
            e = primary.extra_credits
            lines.append(f"extra credits: {e.pct}% ({e.used} / {e.limit} {e.ccy}) | size=11")
        if primary.errors:
            lines.append(f"last error: {primary.errors[-1].code} | size=10")
        lines.append("")

    # Secondary agents
    for a in state.agents[1:]:
        lines.append(f"─── {a.label} ─── | size=12")
        if a.window:
            until, _ = _countdown(a.window.resets_at, now_s)
            lines.append(f"{a.window.pct}% used (weekly window) | size=12")
            if until:
                lines.append(f"resets in {until} | size=12")
        if a.errors:
            lines.append(f"last error: {a.errors[-1].code} | size=10")
        lines.append("")

    # Threads (merged across agents, sorted by billable desc).
    merged_threads = []
    for a in state.agents:
        for t in a.threads:
            merged_threads.append((a, t))
    merged_threads.sort(key=lambda at: -at[1].billable)

    if merged_threads:
        lines.append("─── Threads · last 5h ─── | size=12")
        lines.append("  🟢 alive · ✏️ writing now · 📋 click to copy '#thread-title ' | size=10")
        for a, t in merged_threads:
            letter = _agent_letter(a)
            marks = ""
            if t.pid:
                marks += "🟢"
            if t.active:
                marks += "✏️"
            short = t.sid[:8]
            label = t.title or t.first_msg or ""
            if label and not t.title:
                label = label + "  📋"
            elif not label:
                label = "📋 set title"
            lines.append(
                f"{letter} {_fmt_tokens(t.billable):<6} {short}  {t.project:<10}  "
                f"{marks or '  '}  {label} | size=12"
            )
            for r in t.requests:
                hm = _hhmm_utc(r.epoch)
                lines.append(
                    f"       └ {hm}  {_fmt_tokens(r.billable):<6}  {r.user_prompt} | size=11"
                )

    # processes w/o sid (only from primary for now)
    if primary and primary.processes_no_sid:
        lines.append("  ── processes w/o sid ── | size=10")
        for p in primary.processes_no_sid:
            lines.append(f"    {p['entry']}/{p['project']} × {p['count']} | size=11")
        lines.append("")

    # By model
    if any(a.by_model for a in state.agents):
        lines.append("─── By model ─── | size=12")
        for a in state.agents:
            letter = _agent_letter(a)
            for m in a.by_model:
                short = re.sub(r"^claude-|^codex-|-20\d+$", "", m["name"])
                short = re.sub(r"-20\d+$", "", short)
                lines.append(f"  {letter}  {short:<12} {_fmt_tokens(m['billable'])} | size=12")

    # By project (merged)
    by_proj: dict[str, dict[str, int]] = {}
    for a in state.agents:
        letter = _agent_letter(a)
        for p in a.by_project:
            slot = by_proj.setdefault(p["name"], {})
            slot[letter] = slot.get(letter, 0) + p["billable"]
    if by_proj:
        lines.append("─── By project ─── | size=12")
        items = sorted(by_proj.items(), key=lambda kv: -sum(kv[1].values()))[:5]
        for name, splits in items:
            total = sum(splits.values())
            line = f"  {name:<14}  {_fmt_tokens(total)}"
            if len(splits) > 1:
                parts = " + ".join(f"{l} {_fmt_tokens(v)}" for l, v in splits.items())
                line += f"  ({parts})"
            lines.append(line + " | size=12")

    # Caches footer
    lines.append("─── caches ─── | size=10")
    for a in state.agents:
        bits = " · ".join(f"{k.replace('_s','')} {v}s" for k, v in a.cache_ages.items())
        lines.append(f"  {a.label}: {bits} | size=10")

    return "\n".join(lines) + "\n"
```

- [ ] **Step 5: Run, confirm pass**

```bash
python3 -m pytest tests/test_render.py -v
```

If the golden file diff fails: inspect, then either fix render.py or regenerate the golden file from the new output (only if the new output is the intended layout — capture it with `pytest -s -k golden` and a temporary print).

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add lib/ai_monitor/core/render.py tests/test_render.py tests/test_render__dropdown.txt
git commit -m "feat(core): SwiftBar renderer + golden-file dropdown test"
```

---

### Task 10: `cli.py` — `monitor refresh` entrypoint

**Files:**
- Create: `lib/ai_monitor/cli.py`

- [ ] **Step 1: Write the test for `refresh`**

Append to `tests/test_state.py`:

```python
def test_cli_refresh_writes_state_and_dropdown(tmp_path, monkeypatch):
    monkeypatch.setattr("ai_monitor.cli.STATE_DIR", tmp_path)
    monkeypatch.setattr(
        "ai_monitor.agents.claude.PROJECTS_DIR", tmp_path / "no-projects"
    )
    from ai_monitor.cli import cmd_refresh
    rc = cmd_refresh(claude_cap=14_000_000, remote_disabled=True, codex_enabled=False)
    assert rc == 0
    assert (tmp_path / "state.json").exists()
    assert (tmp_path / "dropdown.txt").exists()
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m pytest tests/test_state.py -v -k refresh
```

Expected: ImportError.

- [ ] **Step 3: Implement `cli.py`**

Write `lib/ai_monitor/cli.py`:

```python
"""`monitor` CLI entrypoint."""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

from .agents.base import AgentState
from .agents.claude import ClaudeAgent
from .core.render import render_dropdown, render_tray
from .core.state import MonitorState, write_state_atomic, write_text_atomic

STATE_DIR = Path("/tmp/ai-monitor")


def cmd_refresh(claude_cap: int, remote_disabled: bool, codex_enabled: bool) -> int:
    now_s = int(time.time())
    agents = []
    if claude_cap > 0:
        agents.append(ClaudeAgent(plan_cap_5h=claude_cap, remote_disabled=remote_disabled).snapshot(now_s))
    if codex_enabled:
        # Filled in in Phase C.
        from .agents.codex import CodexAgent
        agents.append(CodexAgent().snapshot(now_s))
    state = MonitorState(generated_at=now_s, agents=agents)
    write_state_atomic(STATE_DIR / "state.json", state)
    write_text_atomic(STATE_DIR / "dropdown.txt", render_dropdown(state))
    write_text_atomic(STATE_DIR / "tray.txt", render_tray(state) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="monitor")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh")
    r.add_argument("--no-remote", action="store_true")
    r.add_argument("--no-codex", action="store_true")
    r.add_argument("--claude-cap", type=int, default=14_000_000)
    args = p.parse_args(argv)
    if args.cmd == "refresh":
        return cmd_refresh(
            claude_cap=args.claude_cap,
            remote_disabled=args.no_remote,
            codex_enabled=not args.no_codex,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run, confirm pass**

```bash
python3 -m pytest tests/test_state.py -v -k refresh
```

Expected: 1 passed (`test_cli_refresh_writes_state_and_dropdown`).

- [ ] **Step 5: Smoke test end-to-end**

```bash
python3 -m pytest tests/ -v
python3 -m ai_monitor.cli refresh --no-remote --no-codex
cat /tmp/ai-monitor/tray.txt
ls -la /tmp/ai-monitor/
```

Expected: all tests pass; `tray.txt` contains a `C<N>` string from your real `~/.claude/projects` data.

- [ ] **Step 6: Commit**

```bash
git add lib/ai_monitor/cli.py tests/test_state.py
git commit -m "feat(cli): monitor refresh entrypoint writes state + dropdown + tray"
```

---

## Phase B — Switch plugin to read new state

### Task 11: New thin `ai.30s.sh`

**Files:**
- Create: `ai.30s.sh`

- [ ] **Step 1: Write `ai.30s.sh`**

Write `/Users/user/AI/limits/ai.30s.sh`:

```bash
#!/bin/bash
# <bitbar.title>AI Monitor</bitbar.title>
# <bitbar.version>1.0</bitbar.version>
# <bitbar.author>gt</bitbar.author>
# <bitbar.desc>Claude + Codex usage monitor</bitbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>

export PATH="/usr/sbin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Resolve real path (called via SwiftBar symlink).
SCRIPT="${BASH_SOURCE[0]}"
while [ -L "$SCRIPT" ]; do
  link_dir="$(cd -P "$(dirname "$SCRIPT")" && pwd)"
  SCRIPT="$(readlink "$SCRIPT")"
  [[ "$SCRIPT" != /* ]] && SCRIPT="$link_dir/$SCRIPT"
done
DIR="$(cd -P "$(dirname "$SCRIPT")" && pwd)"

STATE_DIR=/tmp/ai-monitor
TRAY="$STATE_DIR/tray.txt"
DROPDOWN="$STATE_DIR/dropdown.txt"
NOW=$(date +%s)

# --- TRAY ---
if [[ -f "$TRAY" ]]; then
  cat "$TRAY"
else
  echo "—"
fi

# --- DROPDOWN ---
echo "---"
if [[ -f "$DROPDOWN" ]]; then
  cat "$DROPDOWN"
else
  echo "no data — running first refresh… | size=12"
fi

# --- background refresh if state stale (>30s) ---
state_age=999999
if [[ -f "$STATE_DIR/state.json" ]]; then
  state_age=$(( NOW - $(stat -f %m "$STATE_DIR/state.json") ))
fi
if [[ $state_age -ge 30 ]]; then
  ( python3 "$DIR/lib/ai_monitor/cli.py" refresh >/dev/null 2>&1 & )
fi

# --- footer ---
echo "---"
echo "Refresh menu | refresh=true"
echo "Force refresh | bash='python3' param1='$DIR/lib/ai_monitor/cli.py' param2='refresh' terminal=false refresh=true"
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x ai.30s.sh
```

- [ ] **Step 3: Smoke test the plugin output**

```bash
./ai.30s.sh
```

Expected: prints the tray line, `---`, then either the dropdown content or `no data` — exits cleanly.

- [ ] **Step 4: Update SwiftBar symlink** (manual step the engineer must take)

Tell the user to run (don't run silently — destructive):

```bash
# Replace the existing symlink. The engineer (or user) does this manually:
ln -sf "/Users/user/AI/limits/ai.30s.sh" "$HOME/Documents/SwiftBar/ai.30s.sh"
rm -f "$HOME/Documents/SwiftBar/claude.30s.sh"
```

Then in SwiftBar → Refresh All. Verify the tray shows the new format.

- [ ] **Step 5: Commit**

```bash
git add ai.30s.sh
git commit -m "feat: thin SwiftBar plugin reads /tmp/ai-monitor state"
```

---

### Task 12: Remove legacy `claude.30s.sh` and shell workers

**Files (delete):**
- `claude.30s.sh`
- `lib/claude-usage-local-cache.sh`
- `lib/claude-usage-aggregate.py`
- `lib/claude-processes.sh`
- `lib/claude-thread-context-cache.sh`
- `lib/claude-thread-context.py`
- `lib/claude-thread-detail-cache.sh`
- `lib/claude-thread-detail.py`
- `lib/claude-usage-remote.sh`

- [ ] **Step 1: Verify the new plugin is working before deleting**

```bash
# Confirm user has run the symlink swap from Task 11 step 4 and the new tray
# is visible. Engineer should pause here and visually confirm.
ls -la ~/Documents/SwiftBar/
```

Expected: only `ai.30s.sh` symlink (no `claude.30s.sh`).

- [ ] **Step 2: Delete legacy files**

```bash
git rm claude.30s.sh \
       lib/claude-usage-local-cache.sh \
       lib/claude-usage-aggregate.py \
       lib/claude-processes.sh \
       lib/claude-thread-context-cache.sh \
       lib/claude-thread-context.py \
       lib/claude-thread-detail-cache.sh \
       lib/claude-thread-detail.py \
       lib/claude-usage-remote.sh
```

- [ ] **Step 3: Confirm `lib/copy-thread-title.sh` is still there**

```bash
ls lib/copy-thread-title.sh
```

Expected: file still exists (it's invoked from the menu, keep it).

- [ ] **Step 4: Run full test suite**

```bash
make test
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git commit -m "refactor: drop legacy bash workers and claude.30s.sh"
```

---

### Task 13: Port `context-audit.py` into the package

**Files:**
- Create: `lib/ai_monitor/audit.py`
- Modify: `tests/test_context_audit.py`
- Delete: `lib/context-audit.py`

- [ ] **Step 1: Copy `lib/context-audit.py` to `lib/ai_monitor/audit.py`**

```bash
cp lib/context-audit.py lib/ai_monitor/audit.py
```

- [ ] **Step 2: Refactor `audit.py` to use `core/jsonl.py` helpers**

Edit `lib/ai_monitor/audit.py`:

- Replace the local copies of `extract_user_text`, `find_title`, and the timestamp parser with imports from `ai_monitor.core.jsonl`:

```python
from .core.jsonl import iter_messages, parse_iso_to_epoch, extract_user_text, find_title
```

- Replace any inline JSONL reading loop with `iter_messages(path)`.
- Keep `categorise`, `render`, `describe`, `recommend`, `main`, and CLI entrypoint.

- [ ] **Step 3: Update `tests/test_context_audit.py` imports**

Edit `tests/test_context_audit.py`:

Replace the `load_module` function and `SCRIPT` constant with:

```python
import ai_monitor.audit as mod
```

Delete the `subprocess.run` smoke test (`test_cli_runs`) — replace with a direct call:

```python
def test_cli_runs(tmp_path, monkeypatch):
    jp = tmp_path / "abc12345.jsonl"
    make_fake_jsonl(jp)
    monkeypatch.setattr("ai_monitor.audit.Path.home", lambda: tmp_path)
    proj = tmp_path / ".claude" / "projects" / "fake"
    proj.mkdir(parents=True)
    (proj / "abc12345.jsonl").write_bytes(jp.read_bytes())
    path = mod.find_jsonl("abc12345")
    data = mod.parse_transcript(path)
    cat = mod.categorise(data)
    report = mod.render(data, cat, "abc12345", path)
    assert "Context audit for abc12345" in report
```

- [ ] **Step 4: Delete old script**

```bash
git rm lib/context-audit.py
```

- [ ] **Step 5: Add `audit` CLI subcommand**

Modify `lib/ai_monitor/cli.py` — append to `main()`:

```python
    a = sub.add_parser("audit")
    a.add_argument("sid", nargs="?", default=None)
    ...
    if args.cmd == "audit":
        from .audit import find_jsonl, parse_transcript, categorise, render
        path = find_jsonl(args.sid)
        data = parse_transcript(path)
        cat = categorise(data)
        print(render(data, cat, path.stem, path))
        return 0
```

- [ ] **Step 6: Run tests and smoke the CLI**

```bash
make test
python3 -m ai_monitor.cli audit
```

Expected: tests pass; `audit` prints a report against the newest transcript in your real `~/.claude/projects`.

- [ ] **Step 7: Commit**

```bash
git add lib/ai_monitor/audit.py lib/ai_monitor/cli.py tests/test_context_audit.py
git commit -m "refactor: port context-audit into ai_monitor package with shared jsonl helpers"
```

---

## Phase C — Codex agent

### Task 14: Research the Codex unknowns

**Files:** (notes go directly into `agents/codex.py` docstring and config defaults)

- [ ] **Step 1: Look for sid flag in real Codex process command lines**

```bash
ps -axww -o command= | grep -i codex | head -20
```

Look for any `--session-id`, `--resume`, `--rollout`, `--continue`, or similar uuid-bearing flag. Record what you find (or "none observed") in the implementation comment in `agents/codex.py`.

- [ ] **Step 2: Probe the OpenAI/ChatGPT usage endpoint**

```bash
# Read the token (do not print to a log file).
TOKEN=$(python3 -c 'import json; print(json.load(open("/Users/user/.codex/auth.json"))["tokens"]["access_token"])')

# Try the most likely endpoints. Stop after the first 200.
for url in \
  "https://chatgpt.com/backend-api/codex/usage" \
  "https://chatgpt.com/backend-api/usage" \
  "https://api.openai.com/v1/usage" \
  "https://chatgpt.com/backend-api/conversation/limits"; do
  echo "=== $url ==="
  curl -sS -o /tmp/codex-probe.json -w "%{http_code}\n" \
    -H "Authorization: Bearer $TOKEN" \
    -H "OpenAI-Beta: chatgpt-2024-09-30" \
    --max-time 8 "$url"
  head -c 500 /tmp/codex-probe.json
  echo ""
done
```

Record the working endpoint URL (or "none found — fall back to local-only weekly") in `agents/codex.py` docstring.

- [ ] **Step 3: Probe ChatGPT Plus weekly reset behavior**

If endpoint exists, inspect the response for `resets_at` / `period_start` / equivalent fields. If not, set safe defaults in code comments:
- Weekly window: 7 rolling days from the earliest message in the period, OR
- Fixed reset at Monday 00:00 local — to be revised once we have evidence.

- [ ] **Step 4: Document findings in `agents/codex.py`**

Open `lib/ai_monitor/agents/codex.py` (created next task) and write the research notes as a module docstring before implementing.

- [ ] **Step 5: Commit the research notes file**

(Notes will be part of the next commit when the file is created.)

---

### Task 15: `agents/codex.py` — local aggregation

**Files:**
- Create: `lib/ai_monitor/agents/codex.py`
- Test: `tests/test_codex_agent.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/test_codex_agent.py`:

```python
from pathlib import Path
from ai_monitor.agents.codex import (
    CodexAgent, billable_from_codex_msg, sid_from_filename,
)


def test_billable_includes_reasoning_excludes_cached():
    msg = {
        "type": "event_msg",
        "payload": {"info": {"total_token_usage": {
            "input_tokens": 800, "output_tokens": 300,
            "cached_input_tokens": 200, "reasoning_output_tokens": 150,
        }}}
    }
    # 800 + 300 + 150 - 200 = 1050
    assert billable_from_codex_msg(msg) == 1050


def test_billable_handles_missing_usage():
    assert billable_from_codex_msg({"type": "event_msg", "payload": {}}) == 0
    assert billable_from_codex_msg({"type": "session_meta"}) == 0


def test_sid_from_filename():
    name = "rollout-2026-05-15T19-06-18-019e2c63-7e6f-70b0-b304-e84fabf52597.jsonl"
    assert sid_from_filename(name) == "019e2c63-7e6f-70b0-b304-e84fabf52597"


def test_sid_from_filename_returns_none_when_no_uuid():
    assert sid_from_filename("garbage.jsonl") is None


def test_codex_snapshot_against_fixture(tmp_path, monkeypatch, codex_jsonl):
    # Stage fake ~/.codex/sessions/2026/05/22/rollout-...-fakeuuid.jsonl
    day = tmp_path / "sessions" / "2026" / "05" / "22"
    day.mkdir(parents=True)
    target = day / "rollout-2026-05-22T10-00-00-00000000-1111-2222-3333-444444444444.jsonl"
    target.write_bytes(codex_jsonl.read_bytes())

    monkeypatch.setattr("ai_monitor.agents.codex.SESSIONS_DIR", tmp_path / "sessions")

    agent = CodexAgent(weekly_cap_tokens=20_000_000, remote_disabled=True)
    state = agent.snapshot(now_s=1779444000 + 60)
    assert state.id == "codex"
    assert state.window.kind == "weekly"
    # Two usage events: 800+300+150-200=1050 plus 1200+400+500-0=2100 = 3150
    assert state.window.billable == 3150
    assert "00000000-1111-2222-3333-444444444444" in {t.sid for t in state.threads}
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m pytest tests/test_codex_agent.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `agents/codex.py`**

Write `lib/ai_monitor/agents/codex.py`:

```python
"""Codex CLI agent implementation.

Research notes (from Task 14):
- sid flag in `ps`:  <FILL IN — observed flag name or "none observed">
- Remote usage endpoint:  <FILL IN — URL or "none found">
- Weekly window anchor:  <FILL IN — server-anchored or local-only>

Storage layout:
  ~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<uuid>.jsonl
Auth file:
  ~/.codex/auth.json → tokens.access_token

Billable formula:
  input + output + reasoning_output - cached_input
"""
from __future__ import annotations
import collections
import re
import time
from pathlib import Path
from typing import Iterable, Optional

from .base import Agent, AgentState, LimitWindow, ThreadInfo, AgentError
from ..core.jsonl import iter_messages, parse_iso_to_epoch, extract_user_text, find_title
from ..core.processes import list_pids, extract_flag_value, classify_entry, pid_cwd

SESSIONS_DIR = Path.home() / ".codex" / "sessions"
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def sid_from_filename(name: str) -> Optional[str]:
    m = UUID_RE.search(name)
    return m.group(1) if m else None


def billable_from_codex_msg(msg: dict) -> int:
    if msg.get("type") != "event_msg":
        return 0
    info = ((msg.get("payload") or {}).get("info")) or {}
    u = info.get("total_token_usage") or {}
    return (
        (u.get("input_tokens") or 0)
        + (u.get("output_tokens") or 0)
        + (u.get("reasoning_output_tokens") or 0)
        - (u.get("cached_input_tokens") or 0)
    )


def model_from_codex_msg(msg: dict) -> Optional[str]:
    if msg.get("type") != "event_msg":
        return None
    info = ((msg.get("payload") or {}).get("info")) or {}
    return info.get("model")


def extract_codex_user_text(msg: dict) -> Optional[str]:
    """Codex puts user prompts in `type=response_item, payload.role=user`."""
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
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text") or ""
    return ""


def cwd_from_codex_session(path: Path) -> Optional[str]:
    """Read `payload.cwd` from the session_meta line, if present."""
    for j in iter_messages(path):
        if j.get("type") == "session_meta":
            return (j.get("payload") or {}).get("cwd")
        break
    return None


class CodexAgent(Agent):
    id = "codex"
    label = "Codex"

    def __init__(self, weekly_cap_tokens: int = 20_000_000, remote_disabled: bool = False):
        self.weekly_cap_tokens = weekly_cap_tokens
        self.remote_disabled = remote_disabled

    def snapshot(self, now_s: int) -> AgentState:
        errors: list[AgentError] = []
        files = list(self._recent_transcripts(now_s))

        since_s = now_s - 7 * 86400  # 7-day rolling
        midnight_s = self._local_midnight(now_s)

        bill_win = 0
        by_model: collections.Counter = collections.Counter()
        by_proj: collections.Counter = collections.Counter()
        by_thread: collections.Counter = collections.Counter()
        thread_proj: dict[str, str] = {}
        thread_ctx: dict[str, dict] = {}

        for path, proj, sid in files:
            for j in iter_messages(path):
                ep = parse_iso_to_epoch(j.get("timestamp") or "")
                if ep == 0 or ep < since_s:
                    continue
                bill = billable_from_codex_msg(j)
                if bill > 0:
                    bill_win += bill
                    model = model_from_codex_msg(j) or "unknown"
                    by_model[model] += bill
                    by_proj[proj] += bill
                    by_thread[sid] += bill
                    thread_proj[sid] = proj
                text = extract_codex_user_text(j)
                if text:
                    ctx = thread_ctx.setdefault(sid, {"title": None, "first_msg": None})
                    t = find_title(text)
                    if t:
                        ctx["title"] = t
                    if ctx["first_msg"] is None:
                        s = text.lstrip()
                        if not s.startswith("<"):
                            ctx["first_msg"] = (s.replace("\n", " ").replace("\r", " ").strip()[:60])

        pct = int(bill_win * 100 / max(self.weekly_cap_tokens, 1) + 0.5)
        window = LimitWindow(
            kind="weekly", pct=pct, resets_at=None,
            billable=bill_win, cap=self.weekly_cap_tokens,
        )

        threads = []
        for sid, bill in by_thread.most_common(20):
            c = thread_ctx.get(sid, {})
            threads.append(ThreadInfo(
                sid=sid, project=thread_proj.get(sid, "?"),
                billable=bill, pid=None, active=False,
                title=c.get("title"), first_msg=c.get("first_msg"),
                branch=None,
            ))

        sid_to_pid, no_sid = self._detect_processes()
        for t in threads:
            t.pid = sid_to_pid.get(t.sid)

        return AgentState(
            id=self.id, label=self.label,
            window=window,
            threads=threads,
            by_model=[{"name": m, "billable": c} for m, c in by_model.most_common()],
            by_project=[{"name": p, "billable": c} for p, c in by_proj.most_common(5)],
            processes_no_sid=no_sid,
            errors=errors,
            cache_ages={},
        )

    def _recent_transcripts(self, now_s: int) -> Iterable[tuple[Path, str, str]]:
        if not SESSIONS_DIR.is_dir():
            return
        cutoff = now_s - 8 * 86400  # slightly wider than 7d
        for jl in SESSIONS_DIR.rglob("rollout-*.jsonl"):
            try:
                if jl.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            sid = sid_from_filename(jl.name)
            if not sid:
                continue
            cwd = cwd_from_codex_session(jl) or ""
            proj = Path(cwd).name if cwd else "?"
            yield jl, proj, sid

    @staticmethod
    def _local_midnight(now_s: int) -> int:
        lt = time.localtime(now_s)
        return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))

    def _detect_processes(self) -> tuple[dict[str, int], list[dict]]:
        sid_to_pid: dict[str, int] = {}
        no_sid: list[dict] = []
        counts: collections.Counter = collections.Counter()
        # Adjust the binary patterns to whatever Task 14 step 1 observed.
        for pid, cmd in list_pids(["/codex", "/codex-cli", ".codex/bin/codex"]):
            # If sid flag exists (set in Task 14 research), use it here:
            sid = (
                extract_flag_value(cmd, "--session-id")
                or extract_flag_value(cmd, "--resume")
                or extract_flag_value(cmd, "--rollout")
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
```

- [ ] **Step 4: Run all tests**

```bash
make test
```

Expected: all green, including the 5 new codex tests.

- [ ] **Step 5: Smoke-test against real Codex data**

```bash
python3 -m ai_monitor.cli refresh --no-remote
cat /tmp/ai-monitor/tray.txt   # should now show "C<N> X<N>"
cat /tmp/ai-monitor/dropdown.txt | head -40
```

Expected: tray has both letters; dropdown has a `─── Codex ───` section.

- [ ] **Step 6: Commit**

```bash
git add lib/ai_monitor/agents/codex.py tests/test_codex_agent.py
git commit -m "feat(codex): local-only Codex agent (weekly window, reasoning tokens billable)"
```

---

### Task 16: Codex remote usage fetch (skip-if-not-found)

**Files:**
- Modify: `lib/ai_monitor/agents/codex.py`
- Test: `tests/test_codex_agent.py`

- [ ] **Step 1: Decide based on Task 14 step 2 findings**

If Task 14 found a working endpoint, implement the fetcher mirroring Claude's structure. If not, **skip this task entirely** — the local-only weekly window already works, and the spec explicitly allows the fall-through. Commit a marker comment:

```python
# Remote endpoint: none found during Task 14 research. Weekly window is local-only.
# Revisit if OpenAI documents one.
```

- [ ] **Step 2: If implementing — add fetcher + test**

Append to `lib/ai_monitor/agents/codex.py`:

```python
import json
import urllib.request


def fetch_codex_token() -> Optional[str]:
    """Read access_token from ~/.codex/auth.json."""
    try:
        data = json.loads(Path.home().joinpath(".codex", "auth.json").read_text())
        return data.get("tokens", {}).get("access_token")
    except (OSError, json.JSONDecodeError):
        return None


def fetch_codex_remote(token: str, endpoint: str, timeout_s: float = 8.0):
    """Hit the discovered Codex usage endpoint. Returns (payload, err)."""
    req = urllib.request.Request(
        endpoint,
        headers={
            "Authorization": f"Bearer {token}",
            "OpenAI-Beta": "chatgpt-2024-09-30",
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
```

Wire into `CodexAgent.snapshot` analogously to Claude's snapshot (Task 7) — try remote first, fall back to local.

Add an integration test with a mocked `urllib.request.urlopen` (use `unittest.mock.patch`).

- [ ] **Step 3: Run tests**

```bash
make test
```

- [ ] **Step 4: Commit**

```bash
git add lib/ai_monitor/agents/codex.py tests/test_codex_agent.py
git commit -m "feat(codex): remote usage fetch (if endpoint discovered)"
```

---

## Phase D — Config, notifications, polish

### Task 17: `core/config.py` — TOML loader

**Files:**
- Create: `lib/ai_monitor/core/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

Write `tests/test_config.py`:

```python
from ai_monitor.core.config import load_config, Config, DEFAULTS


def test_load_config_uses_defaults_when_missing(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.claude.enabled is True
    assert cfg.claude.plan_cap_5h == DEFAULTS["agents"]["claude"]["plan_cap_5h"]
    assert cfg.codex.enabled is True


def test_load_config_overrides_partial(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("""
[agents.claude]
plan_cap_5h = 999_999

[notifications]
enabled = false
""")
    cfg = load_config(f)
    assert cfg.claude.plan_cap_5h == 999_999
    assert cfg.notifications.enabled is False
    # Untouched fields keep defaults.
    assert cfg.claude.remote_refresh_s == DEFAULTS["agents"]["claude"]["remote_refresh_s"]


def test_load_config_malformed_falls_back_to_defaults(tmp_path):
    f = tmp_path / "broken.toml"
    f.write_text("this is = not valid TOML }")
    cfg = load_config(f)
    assert cfg.claude.enabled is True  # didn't crash
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m pytest tests/test_config.py -v
```

- [ ] **Step 3: Implement `core/config.py`**

Write `lib/ai_monitor/core/config.py`:

```python
"""TOML config loader with defaults. Stable schema is the source of truth."""
from __future__ import annotations
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "tray": {"color_hints": False, "hide_zero": True},
    "agents": {
        "claude": {
            "enabled": True,
            "plan_cap_5h": 14_000_000,
            "remote_refresh_s": 300,
        },
        "codex": {
            "enabled": True,
            "plan": "chatgpt_plus",
            "weekly_cap_tokens": 20_000_000,
            "week_reset_day": "monday",
            "week_reset_hour_local": 9,
        },
    },
    "intervals": {"local_s": 60, "procs_s": 30, "remote_s": 300},
    "notifications": {
        "enabled": True,
        "thresholds": [75, 90, 100],
        "claude": {"enabled": True},
        "codex": {"enabled": True},
    },
    "ignored": {"projects": []},
}


@dataclass
class TraySection:
    color_hints: bool = False
    hide_zero: bool = True


@dataclass
class ClaudeAgentSection:
    enabled: bool = True
    plan_cap_5h: int = 14_000_000
    remote_refresh_s: int = 300


@dataclass
class CodexAgentSection:
    enabled: bool = True
    plan: str = "chatgpt_plus"
    weekly_cap_tokens: int = 20_000_000
    week_reset_day: str = "monday"
    week_reset_hour_local: int = 9


@dataclass
class IntervalsSection:
    local_s: int = 60
    procs_s: int = 30
    remote_s: int = 300


@dataclass
class NotificationsSection:
    enabled: bool = True
    thresholds: list[int] = field(default_factory=lambda: [75, 90, 100])
    claude_enabled: bool = True
    codex_enabled: bool = True


@dataclass
class Config:
    tray: TraySection = field(default_factory=TraySection)
    claude: ClaudeAgentSection = field(default_factory=ClaudeAgentSection)
    codex: CodexAgentSection = field(default_factory=CodexAgentSection)
    intervals: IntervalsSection = field(default_factory=IntervalsSection)
    notifications: NotificationsSection = field(default_factory=NotificationsSection)
    ignored_projects: list[str] = field(default_factory=list)


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path) -> Config:
    """Load TOML config; fall back to defaults silently if missing/broken."""
    merged = DEFAULTS
    try:
        with open(path, "rb") as f:
            user = tomllib.load(f)
        merged = _deep_merge(DEFAULTS, user)
    except (OSError, tomllib.TOMLDecodeError):
        pass

    return Config(
        tray=TraySection(
            color_hints=bool(merged["tray"]["color_hints"]),
            hide_zero=bool(merged["tray"]["hide_zero"]),
        ),
        claude=ClaudeAgentSection(**merged["agents"]["claude"]),
        codex=CodexAgentSection(**merged["agents"]["codex"]),
        intervals=IntervalsSection(**merged["intervals"]),
        notifications=NotificationsSection(
            enabled=bool(merged["notifications"]["enabled"]),
            thresholds=list(merged["notifications"]["thresholds"]),
            claude_enabled=bool(merged["notifications"]["claude"]["enabled"]),
            codex_enabled=bool(merged["notifications"]["codex"]["enabled"]),
        ),
        ignored_projects=list(merged["ignored"]["projects"]),
    )


DEFAULT_PATH = Path.home() / ".config" / "ai-monitor.toml"
```

- [ ] **Step 4: Wire config into `cli.py`**

Modify `lib/ai_monitor/cli.py` `cmd_refresh`:

```python
def cmd_refresh(config_path: Optional[Path] = None) -> int:
    from .core.config import load_config, DEFAULT_PATH
    cfg = load_config(config_path or DEFAULT_PATH)
    now_s = int(time.time())
    agents = []
    if cfg.claude.enabled:
        agents.append(ClaudeAgent(plan_cap_5h=cfg.claude.plan_cap_5h).snapshot(now_s))
    if cfg.codex.enabled:
        from .agents.codex import CodexAgent
        agents.append(CodexAgent(weekly_cap_tokens=cfg.codex.weekly_cap_tokens).snapshot(now_s))
    state = MonitorState(generated_at=now_s, agents=agents)
    write_state_atomic(STATE_DIR / "state.json", state)
    write_text_atomic(STATE_DIR / "dropdown.txt", render_dropdown(state))
    write_text_atomic(STATE_DIR / "tray.txt", render_tray(state, cfg.tray.color_hints) + "\n")
    # Notifications wired in Task 18.
    return 0
```

Update `main()` to pass `--config <path>`:

```python
r.add_argument("--config", type=Path, default=None)
...
if args.cmd == "refresh":
    return cmd_refresh(config_path=args.config)
```

- [ ] **Step 5: Run tests**

```bash
make test
```

- [ ] **Step 6: Commit**

```bash
git add lib/ai_monitor/core/config.py lib/ai_monitor/cli.py tests/test_config.py
git commit -m "feat(config): TOML loader with defaults; refresh reads config"
```

---

### Task 18: `core/notify.py` — threshold notifications

**Files:**
- Create: `lib/ai_monitor/core/notify.py`
- Test: `tests/test_notify.py`

- [ ] **Step 1: Write failing tests**

Write `tests/test_notify.py`:

```python
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
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m pytest tests/test_notify.py -v
```

- [ ] **Step 3: Implement `core/notify.py`**

Write `lib/ai_monitor/core/notify.py`:

```python
"""Threshold notifications via osascript. Dedup state in /tmp/ai-monitor/notified.json."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .state import MonitorState


def _osascript_notify(title: str, body: str) -> None:
    """Default notifier. Best-effort — failures are silent."""
    # Escape double quotes for AppleScript.
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
) -> None:
    """Fire a notification for each agent that crossed a threshold this tick.

    Dedup key: (agent_id, window_kind, resets_at, threshold). When resets_at
    changes (new window), the dedup is cleared for that agent.
    """
    notified = _load_state(dedup_path)
    for a in state.agents:
        if not a.window:
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
```

- [ ] **Step 4: Wire into `cli.py`**

Modify `lib/ai_monitor/cli.py` `cmd_refresh`, after writing state files:

```python
    from .core.notify import check_thresholds
    if cfg.notifications.enabled:
        check_thresholds(
            state,
            thresholds=cfg.notifications.thresholds,
            dedup_path=STATE_DIR / "notified.json",
        )
    return 0
```

- [ ] **Step 5: Run tests**

```bash
make test
```

- [ ] **Step 6: Commit**

```bash
git add lib/ai_monitor/core/notify.py lib/ai_monitor/cli.py tests/test_notify.py
git commit -m "feat(notify): threshold notifications via osascript with dedup"
```

---

### Task 19: Round out the `monitor` CLI — status / doctor / install

**Files:**
- Modify: `lib/ai_monitor/cli.py`

- [ ] **Step 1: Add `status`, `doctor`, `install` subcommands**

Append to `lib/ai_monitor/cli.py` `main()`:

```python
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")

    d = sub.add_parser("doctor")
    d.add_argument("--write-config", action="store_true")

    i = sub.add_parser("install")

    args = p.parse_args(argv)
    if args.cmd == "status":
        return cmd_status(as_json=args.json)
    if args.cmd == "doctor":
        return cmd_doctor(write_config=args.write_config)
    if args.cmd == "install":
        return cmd_install()
```

Add the command implementations:

```python
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
        print(f"{a['label']}: {w.get('pct', '—')}%  ({w.get('billable', 0):,} / {w.get('cap', 0):,})")
        for t in a.get("threads", [])[:5]:
            print(f"  {t['sid'][:8]}  {t['project']:<12}  {t['billable']:,}  {t.get('title') or t.get('first_msg') or ''}")
    return 0


def cmd_doctor(write_config: bool = False) -> int:
    from .core.config import DEFAULT_PATH
    ok = True
    print("ai-monitor doctor")
    paths = [
        ("Claude projects", Path.home() / ".claude" / "projects"),
        ("Codex sessions",  Path.home() / ".codex" / "sessions"),
        ("SwiftBar plugins", Path.home() / "Documents" / "SwiftBar"),
        ("State dir",       STATE_DIR),
    ]
    for label, p in paths:
        present = p.exists()
        print(f"  [{ 'OK' if present else 'MISSING' }] {label}: {p}")
        if not present and label != "State dir":
            ok = False

    # Auth
    auth_keychain = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-g"],
        capture_output=True, text=True,
    ).returncode == 0
    print(f"  [{ 'OK' if auth_keychain else 'MISSING' }] Claude Keychain entry")

    codex_auth = (Path.home() / ".codex" / "auth.json").exists()
    print(f"  [{ 'OK' if codex_auth else 'MISSING' }] Codex auth.json")

    # Plugin symlink
    plugin_link = Path.home() / "Documents" / "SwiftBar" / "ai.30s.sh"
    print(f"  [{ 'OK' if plugin_link.exists() else 'MISSING' }] SwiftBar symlink (run `monitor install` to create)")

    if write_config:
        if DEFAULT_PATH.exists():
            print(f"config already exists at {DEFAULT_PATH}; not overwriting")
        else:
            DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
            DEFAULT_PATH.write_text(_starter_config())
            print(f"wrote starter config to {DEFAULT_PATH}")
    return 0 if ok else 1


def cmd_install() -> int:
    """Create the SwiftBar plugin symlink."""
    here = Path(__file__).resolve().parent.parent.parent  # repo root
    plugin = here / "ai.30s.sh"
    if not plugin.exists():
        print(f"ai.30s.sh not found at {plugin}", file=sys.stderr)
        return 1
    target_dir = Path.home() / "Documents" / "SwiftBar"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "ai.30s.sh"
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(plugin)
    print(f"symlinked {target} → {plugin}")
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
```

- [ ] **Step 2: Smoke-test each subcommand**

```bash
python3 -m ai_monitor.cli refresh
python3 -m ai_monitor.cli status
python3 -m ai_monitor.cli status --json | head
python3 -m ai_monitor.cli doctor
python3 -m ai_monitor.cli audit
```

Expected: each prints sensible output; `doctor` lists OK/MISSING for the four paths and the auth entries.

- [ ] **Step 3: Run tests**

```bash
make test
```

- [ ] **Step 4: Commit**

```bash
git add lib/ai_monitor/cli.py
git commit -m "feat(cli): monitor status/doctor/install subcommands"
```

---

### Task 20: README update

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace install section**

Open `README.md`. The current install block uses `claude.30s.sh`; replace with:

```bash
brew install --cask swiftbar
defaults write com.ameba.SwiftBar PluginDirectory -string "$HOME/Documents/SwiftBar"

# Install the Python package (editable, so future edits don't need re-install):
python3 -m pip install --user -e /Users/user/AI/limits

# Create the SwiftBar plugin symlink:
monitor install

# Optionally generate a starter config:
monitor doctor --write-config

open -a SwiftBar
```

- [ ] **Step 2: Update the "Tray contents" example to the new compact format**

Replace the current `59%` tray sample with:

```
C72 X89
```

And update the dropdown sample to mirror the golden file (`tests/test_render__dropdown.txt`).

- [ ] **Step 3: Add a "Codex" section explaining the weekly window**

Add (in Russian, matching the existing voice):

```markdown
## Codex

Параллельно с Claude мониторится OpenAI Codex CLI. Транскрипты лежат в
`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, авторизация через
`~/.codex/auth.json`. Окно лимита — недельное (ChatGPT Plus/Pro). Тарифные
пресеты и недельные капы настраиваются в `~/.config/ai-monitor.toml`.
```

- [ ] **Step 4: Update the "Архитектура" section to point to `lib/ai_monitor/`**

Replace the existing tree with:

```
ai.30s.sh                       ← SwiftBar плагин (cat-ает /tmp/ai-monitor/dropdown.txt)
lib/ai_monitor/
  cli.py                          monitor status / refresh / audit / doctor / install
  core/                           jsonl, processes, state, render, notify, config
  agents/
    base.py                       Agent ABC
    claude.py
    codex.py
  audit.py                        context-audit (старая утилита, теперь в пакете)
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: update README for ai_monitor (Codex + monitor CLI + new plugin)"
```

---

## Phase E — Verification and cutover

### Task 21: Side-by-side verification

- [ ] **Step 1: Run full test suite**

```bash
make test
```

Expected: every test green.

- [ ] **Step 2: Manual SwiftBar verification**

In SwiftBar → Refresh All. Verify:
- [ ] Tray shows `C<N> X<N>` (or with color dot if enabled).
- [ ] Dropdown shows Claude section at top, Codex section below.
- [ ] Threads listed with `C` / `X` letters.
- [ ] Per-thread submenu shows per-request breakdown.
- [ ] "Force refresh" menu item runs `monitor refresh` and updates within ~2s.

- [ ] **Step 3: Compare numbers against `claude /usage`**

In a fresh Claude Code session, run `/usage`. The 5h % must match the tray's `C<N>` within ±1 (server-anchored). If it diverges by more than 2%, investigate before declaring done — most likely the OAuth fetch path is broken and we fell back to local-only.

- [ ] **Step 4: Compare Codex weekly numbers against ChatGPT Plus dashboard if accessible**

If a Codex usage endpoint was discovered in Task 14, the dropdown's Codex % should match. If local-only, accept the weekly_cap_tokens estimate as a known approximation and document in README.

- [ ] **Step 5: Notification smoke test**

Temporarily set `plan_cap_5h = 100_000` in `~/.config/ai-monitor.toml`, run a quick Claude session to push billable above 100k, run `monitor refresh`. Expect a macOS notification. Revert the config.

- [ ] **Step 6: Final commit**

If any tweaks were needed during verification, commit them with `fix:` prefix. Otherwise: nothing to commit.

---

### Task 22: Update auto-memory

- [ ] **Step 1: Update `/Users/user/.claude/projects/-Users-user-AI-limits/memory/project_claude_monitor.md`**

Update the existing project memory file to reflect the new state — rename references from `claude_monitor` to `ai_monitor`, mention Codex support, update file paths, list the `monitor` CLI.

- [ ] **Step 2: Update `/Users/user/.claude/projects/-Users-user-AI-limits/memory/MEMORY.md`**

Rename the index entry from "claude_monitor project" to "ai_monitor project" and update the one-line hook.

(Note: the memory rename is plain editing, not a code change — no commit.)

---

## Self-review

### Spec coverage

| Spec section | Implemented in |
|---|---|
| Architecture / directory layout | Task 1, 4, 5, 6, 7, 8, 9, 10, 11, 13, 17, 18, 19 |
| MonitorState schema | Task 4 (dataclasses), Task 8 (writer) |
| Agent ABC | Task 4 |
| Claude agent (transcripts, processes, remote) | Task 5, 6, 7 |
| Codex agent (transcripts, processes, remote, billable formula) | Task 14, 15, 16 |
| Tray format `C72 X89` + color hints | Task 9 |
| Dropdown layout | Task 9 (golden file in Task 9) |
| Thread row click behavior | Task 9 (renderer keeps existing copy-thread-title affordance) |
| Notifications with dedup | Task 18 |
| Edge cases (disabled agent, errors-only, all-zero) | Task 9 (renderer handles all three) |
| Config TOML | Task 17 |
| `monitor` CLI (status / refresh / audit / doctor / install) | Task 10, 13, 19 |
| Migration phases A/B/C/D | Mapped to plan phases A (Tasks 1–10), B (11–13), C (14–16), D (17–20) |
| Testing strategy | Task 1 (fixtures), Task 2/3/4/8/9/15/17/18 (per-module), Task 13 (context-audit retained) |
| README update | Task 20 |

All spec sections covered.

### Placeholder scan

Searched for: TBD, TODO, "implement later", "etc.", "similar to", "add appropriate". Two intentional placeholders remain:
1. **Task 14 research notes** — `<FILL IN — observed flag name…>` etc. — explicitly research outputs that the engineer must paste before continuing. Cannot pre-fill.
2. **Task 16** — gated on Task 14's findings. Step 1 explicitly says "skip if no endpoint found" — not a placeholder, a branch.

### Type consistency

- `AgentState`, `ThreadInfo`, `LimitWindow`, `ProcessInfo`, `RemoteUsage`, `AgentError` are defined once in Task 4 and used consistently in Tasks 5, 6, 7, 8, 9, 15, 16, 18.
- `billable_from_msg` (Claude) vs `billable_from_codex_msg` (Codex) — intentionally different functions, named to disambiguate.
- `iter_messages`, `parse_iso_to_epoch`, `extract_user_text`, `find_title` — same names everywhere, all from `core.jsonl`.
- `STATE_DIR` constant used identically in Tasks 10, 13, 18, 19.
- `write_state_atomic` / `write_text_atomic` — names match between Task 8 (definition) and Tasks 10, 18 (callers).

No drift detected.
