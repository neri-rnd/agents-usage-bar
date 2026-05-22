"""Helpers for parsing `ps` / `lsof` output without forking per field."""
from __future__ import annotations
import re
import shutil
import subprocess
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
    if "cursor/extensions" in cmd or ".cursor/" in cmd or "/Cursor.app/" in cmd:
        return "cursor"
    if "vscode" in cmd or "Visual Studio Code" in cmd or "/Code.app/" in cmd:
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
