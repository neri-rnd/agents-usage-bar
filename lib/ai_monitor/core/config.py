"""TOML config loader with defaults. Stable schema is the source of truth."""
from __future__ import annotations
import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "tray": {"color_hints": False, "hide_zero": True, "icons": True},
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
    icons: bool = True   # render brand icons via `image=` (Pillow); False = text-only


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


def _only_known(cls, d: dict) -> dict:
    """Filter dict to keys that are actual fields of dataclass `cls`."""
    field_names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in d.items() if k in field_names}


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
            icons=bool(merged["tray"].get("icons", True)),
        ),
        claude=ClaudeAgentSection(**_only_known(ClaudeAgentSection, merged["agents"]["claude"])),
        codex=CodexAgentSection(**_only_known(CodexAgentSection, merged["agents"]["codex"])),
        intervals=IntervalsSection(**_only_known(IntervalsSection, merged["intervals"])),
        notifications=NotificationsSection(
            enabled=bool(merged["notifications"]["enabled"]),
            thresholds=list(merged["notifications"]["thresholds"]),
            claude_enabled=bool(merged["notifications"]["claude"]["enabled"]),
            codex_enabled=bool(merged["notifications"]["codex"]["enabled"]),
        ),
        ignored_projects=list(merged["ignored"]["projects"]),
    )


DEFAULT_PATH = Path.home() / ".config" / "ai-monitor.toml"
