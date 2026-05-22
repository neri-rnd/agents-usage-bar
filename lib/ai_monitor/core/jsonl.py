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
            if block.get("type") == "text":
                return block.get("text") or ""
        return ""


def find_title(text: str) -> Optional[str]:
    """Return the last #thread-title marker value in `text`, or None."""
    last = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(TITLE_TAG):
            after = s[len(TITLE_TAG):]
            # Require whitespace right after the tag, or end-of-line.
            if after and not after[0].isspace():
                continue
            rest = after.strip()
            if rest:
                last = rest
    return last
