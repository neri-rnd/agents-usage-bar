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
    each other (each writer's .tmp filename is unique). On any write error,
    the .tmp file is cleaned up to avoid orphans.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, indent=2)
        os.rename(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path, text: str) -> None:
    """Same atomic pattern, for the rendered dropdown.txt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.rename(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
