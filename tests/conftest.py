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
