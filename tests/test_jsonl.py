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
    assert parse_iso_to_epoch(None) == 0


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


def test_iter_messages_returns_empty_for_missing_file(tmp_path):
    assert list(iter_messages(tmp_path / "ghost.jsonl")) == []


def test_extract_user_text_finds_text_even_after_tool_result():
    j = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "u1"},
        {"type": "text", "text": "real user text"},
    ]}}
    assert extract_user_text(j) == "real user text"


def test_find_title_requires_word_boundary():
    # No whitespace after tag → not a real marker, no match.
    assert find_title("#thread-titlefoo bar") is None
    # End-of-line right after tag → no value, no match.
    assert find_title("#thread-title") is None
    # Tab is whitespace, should count.
    assert find_title("#thread-title\tspaced") == "spaced"
