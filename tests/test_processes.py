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


def test_classify_entry_cursor_app_bundle():
    assert classify_entry("/Applications/Cursor.app/Contents/Resources/.../claude") == "cursor"


def test_classify_entry_code_app_bundle():
    # A path containing /Code.app/ but neither "vscode" nor "Visual Studio Code"
    assert classify_entry("/Applications/Code.app/Contents/Resources/.../claude") == "vscode"


def test_summarize_args_picks_known_flags():
    cmd = "claude --model claude-opus-4-7-20260201 --effort high --name worker"
    assert summarize_args(cmd) == "name=worker m=opus-4-7 e=high"


def test_summarize_args_model_only():
    assert summarize_args("claude --model claude-opus-4-7-20260201") == "m=opus-4-7"


def test_summarize_args_returns_empty_when_no_known_flags():
    assert summarize_args("claude --foo bar --baz qux") == ""


def test_summarize_args_effort_only():
    assert summarize_args("claude --effort high") == "e=high"
