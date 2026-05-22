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
