"""Audit a Claude Code thread transcript and report context bloat.

Usage (via CLI):
    monitor audit [sid]

If sid is omitted, the most recently modified jsonl under ~/.claude/projects/
is used. If sid is a short prefix (e.g. 8 chars), it is resolved via glob.

The report categorises heavy tool_results (>5000 chars), oversized Read /
Bash / WebFetch outputs, duplicate Reads, MCP dumps and failed tool calls.
Char counts are approximate (~4 chars per token).
"""
import json
import sys
from pathlib import Path
from collections import defaultdict

from .core.jsonl import iter_messages, parse_iso_to_epoch, TITLE_TAG

HEAVY_THRESHOLD = 5000
LARGE_THRESHOLD = 10000
TOP_N = 10


def find_jsonl(sid_arg):
    """Return Path to jsonl. If sid_arg is None, take newest by mtime."""
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        sys.exit("error: ~/.claude/projects not found")
    if sid_arg:
        # Try exact match first, then prefix glob.
        exact = list(projects.glob(f"*/{sid_arg}.jsonl"))
        if exact:
            return exact[0]
        prefix = list(projects.glob(f"*/{sid_arg}*.jsonl"))
        if prefix:
            return prefix[0]
        sys.exit(f"error: no jsonl matching sid {sid_arg!r}")
    # Newest by mtime.
    candidates = list(projects.glob("*/*.jsonl"))
    if not candidates:
        sys.exit("error: no jsonl files found")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def fmt_ts(iso):
    """Extract HH:MM from ISO timestamp; empty string if missing."""
    if not iso:
        return ""
    # iso like 2026-05-20T09:54:12.123Z
    try:
        t = iso.split("T", 1)[1]
        return t[:5]
    except Exception:
        return ""


def tool_result_size(content):
    """Return char length of a tool_result.content (str or list of blocks)."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                total += len(block.get("text") or "")
            # tool_reference / image blocks don't contribute to text bloat
        return total
    return 0


def tool_result_text_preview(content, n=200):
    """First n chars of the text part of a tool_result.content."""
    if isinstance(content, str):
        return content[:n]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return (block.get("text") or "")[:n]
    return ""


def short_cmd(cmd, n=100):
    if not cmd:
        return ""
    s = cmd.replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def fmt_num(n):
    return f"{n:,}"


def parse_transcript(path):
    """Stream the jsonl and collect everything we need.

    Returns a dict with keys: tool_uses (id -> meta), tool_results (list),
    user_prompts (list), title, last_ts, assistant_turns.
    """
    tool_uses = {}  # id -> {name, input, ts, idx}
    results = []  # list of {use_id, size, is_error, content, ts}
    user_first = None
    title = None
    last_ts = None
    assistant_turns = 0
    idx = 0

    for j in iter_messages(path):
        t = j.get("type")
        ts = j.get("timestamp")
        if ts:
            last_ts = ts

        if t == "user":
            msg = j.get("message") or {}
            c = msg.get("content")
            if isinstance(c, str):
                stripped = c.lstrip()
                if user_first is None and not stripped.startswith("<"):
                    user_first = stripped.replace("\n", " ").strip()
                for line2 in c.splitlines():
                    s = line2.strip()
                    if s.startswith(TITLE_TAG):
                        rest = s[len(TITLE_TAG):].strip()
                        if rest:
                            title = rest
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        text = b.get("text") or ""
                        stripped = text.lstrip()
                        if user_first is None and not stripped.startswith("<"):
                            user_first = stripped.replace("\n", " ").strip()
                        for line2 in text.splitlines():
                            s = line2.strip()
                            if s.startswith(TITLE_TAG):
                                rest = s[len(TITLE_TAG):].strip()
                                if rest:
                                    title = rest
                    elif bt == "tool_result":
                        tc = b.get("content")
                        results.append({
                            "use_id": b.get("tool_use_id"),
                            "size": tool_result_size(tc),
                            "is_error": bool(b.get("is_error")),
                            "preview": tool_result_text_preview(tc),
                            "ts": ts,
                            "idx": idx,
                        })
        elif t == "assistant":
            assistant_turns += 1
            msg = j.get("message") or {}
            c = msg.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_uses[b.get("id")] = {
                            "name": b.get("name") or "",
                            "input": b.get("input") or {},
                            "ts": ts,
                            "idx": idx,
                        }
        idx += 1

    return {
        "tool_uses": tool_uses,
        "results": results,
        "user_first": user_first,
        "title": title,
        "last_ts": last_ts,
        "assistant_turns": assistant_turns,
    }


def categorise(data):
    """Join tool_uses with results, return enriched list + category buckets."""
    joined = []
    for r in data["results"]:
        use = data["tool_uses"].get(r["use_id"]) or {}
        joined.append({
            **r,
            "name": use.get("name") or "<unknown>",
            "input": use.get("input") or {},
            "use_ts": use.get("ts") or r["ts"],
        })

    heavy = [r for r in joined if r["size"] > HEAVY_THRESHOLD]
    heavy_sorted = sorted(heavy, key=lambda r: -r["size"])[:TOP_N]

    large_reads = [r for r in joined if r["name"] == "Read" and r["size"] > LARGE_THRESHOLD]
    large_bash = [r for r in joined if r["name"] == "Bash" and r["size"] > LARGE_THRESHOLD]
    webfetch = [r for r in joined if r["name"] == "WebFetch" and r["size"] > HEAVY_THRESHOLD]
    mcp_big = [r for r in joined if r["name"].startswith("mcp__") and r["size"] > HEAVY_THRESHOLD]
    failed = [r for r in joined if r["is_error"]]

    # Duplicate Reads: same file_path used by >1 Read tool_use, regardless of size.
    read_paths = defaultdict(list)
    for use_id, use in data["tool_uses"].items():
        if use.get("name") == "Read":
            fp = (use.get("input") or {}).get("file_path")
            if fp:
                read_paths[fp].append(use.get("ts") or "")
    dup_reads = {fp: ts_list for fp, ts_list in read_paths.items() if len(ts_list) > 1}

    return {
        "joined": joined,
        "heavy": heavy_sorted,
        "large_reads": large_reads,
        "large_bash": large_bash,
        "webfetch": webfetch,
        "mcp_big": mcp_big,
        "failed": failed,
        "dup_reads": dup_reads,
    }


def render(data, cat, sid, path):
    out = []
    title = data["title"] or (data["user_first"] or "(no user prompt)")
    if len(title) > 80:
        title = title[:79] + "…"

    out.append(f"# Context audit for {sid}")
    out.append("")
    out.append(f"- Thread: {title}")
    out.append(f"- Total assistant turns: {data['assistant_turns']}")
    out.append(f"- Last activity: {data['last_ts'] or '?'}")
    out.append(f"- File: {path}")
    out.append("")

    # Aggregate totals.
    total_chars = sum(r["size"] for r in cat["joined"])
    heavy_chars = sum(r["size"] for r in cat["joined"] if r["size"] > HEAVY_THRESHOLD)
    by_type = defaultdict(int)
    for r in cat["joined"]:
        if r["size"] > HEAVY_THRESHOLD:
            by_type[r["name"]] += r["size"]

    # --- Heavy artifacts (top N by size) ---
    out.append(f"## Heavy artifacts (top {TOP_N} by size)")
    out.append("")
    if not cat["heavy"]:
        out.append("_None over 5,000 chars._")
    else:
        for r in cat["heavy"]:
            label = describe(r)
            out.append(f"- [{fmt_ts(r['use_ts'])}] {r['name']} {label} — {fmt_num(r['size'])} chars")
    out.append("")

    # --- Large Reads ---
    if cat["large_reads"]:
        out.append("## Large Read calls (>10,000 chars)")
        out.append("")
        for r in cat["large_reads"]:
            fp = r["input"].get("file_path") or "?"
            out.append(f"- [{fmt_ts(r['use_ts'])}] {fp} — {fmt_num(r['size'])} chars")
        out.append("")

    # --- Duplicate Reads ---
    if cat["dup_reads"]:
        out.append("## Duplicate Reads")
        out.append("")
        for fp, ts_list in sorted(cat["dup_reads"].items(), key=lambda kv: -len(kv[1])):
            times = ", ".join(fmt_ts(t) for t in ts_list if t)
            out.append(f"- {fp} — read {len(ts_list)} times at {times}")
        out.append("")

    # --- Large Bash ---
    if cat["large_bash"]:
        out.append("## Bash with large output (>10,000 chars)")
        out.append("")
        for r in cat["large_bash"]:
            cmd = short_cmd(r["input"].get("command") or "")
            out.append(f"- [{fmt_ts(r['use_ts'])}] `{cmd}` — {fmt_num(r['size'])} chars")
        out.append("")

    # --- WebFetch ---
    if cat["webfetch"]:
        out.append("## WebFetch calls")
        out.append("")
        for r in cat["webfetch"]:
            url = r["input"].get("url") or "?"
            out.append(f"- [{fmt_ts(r['use_ts'])}] {url} — {fmt_num(r['size'])} chars")
        out.append("")

    # --- MCP ---
    if cat["mcp_big"]:
        out.append("## MCP tool calls with large dumps")
        out.append("")
        for r in cat["mcp_big"]:
            out.append(f"- [{fmt_ts(r['use_ts'])}] {r['name']} — {fmt_num(r['size'])} chars")
        out.append("")

    # --- Failed ---
    if cat["failed"]:
        out.append("## Failed tool calls")
        out.append("")
        for r in cat["failed"]:
            extra = ""
            if r["name"] == "Bash":
                cmd = short_cmd(r["input"].get("command") or "", 80)
                extra = f" `{cmd}`"
            elif r["name"] == "Read":
                extra = f" {r['input'].get('file_path') or ''}"
            prev = r["preview"].replace("\n", " ").strip()[:120]
            out.append(f"- [{fmt_ts(r['use_ts'])}] {r['name']}{extra} — {fmt_num(r['size'])} chars: {prev}")
        out.append("")

    # --- Summary ---
    out.append("## Summary")
    out.append("")
    if total_chars == 0:
        out.append("_No tool_results in this thread._")
        return "\n".join(out)
    pct = heavy_chars * 100 // max(total_chars, 1)
    out.append(f"- Total tool_result chars: {fmt_num(total_chars)} (~{fmt_num(total_chars // 4)} tokens)")
    out.append(f"- From artifacts >5,000 chars: {fmt_num(heavy_chars)} ({pct}% of all tool_result chars)")
    if by_type:
        top_offenders = sorted(by_type.items(), key=lambda kv: -kv[1])[:3]
        parts = []
        for name, size in top_offenders:
            share = size * 100 // max(heavy_chars, 1)
            parts.append(f"{name} ({share}%)")
        out.append(f"- Top offender types: {' / '.join(parts)}")
    out.append("")
    out.append(f"**Recommendation:** {recommend(cat, pct)}")
    return "\n".join(out)


def describe(r):
    """Short identifier for a heavy artifact line."""
    name = r["name"]
    inp = r["input"]
    if name == "Read":
        return inp.get("file_path") or ""
    if name == "Bash":
        return "`" + short_cmd(inp.get("command") or "") + "`"
    if name == "WebFetch":
        return inp.get("url") or ""
    if name == "Grep":
        return inp.get("pattern") or ""
    if name == "Glob":
        return inp.get("pattern") or ""
    return ""


def recommend(cat, pct):
    if pct >= 60:
        return ("контекст уже >60% — обдумай /compact с явной инструкцией дропнуть "
                "перечисленные heavy artifacts, либо начни новый тред с резюме.")
    if cat["dup_reads"]:
        return ("есть дубликаты Read — следующий компакшен или новый тред "
                "стоит запускать с инструкцией читать файлы один раз.")
    if cat["large_bash"]:
        return ("крупный Bash output тащится в каждый запрос — в следующий раз "
                "пайпай в head / wc / grep вместо полного дампа.")
    if pct >= 30:
        return "пока терпимо, но heavy artifacts уже заметная доля — следи."
    return "контекст чистый, ничего срочного делать не надо."


def main():
    sid_arg = sys.argv[1] if len(sys.argv) > 1 else None
    path = find_jsonl(sid_arg)
    sid = path.stem
    data = parse_transcript(path)
    cat = categorise(data)
    print(render(data, cat, sid, path))


if __name__ == "__main__":
    main()
