import sys
from pathlib import Path
from unittest.mock import patch
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

PROJECTS = [
    {"id": "P1", "name": "Investing Automation", "objective": "Automate stock idea generation"},
    {"id": "P2", "name": "LifeOS Dev", "objective": "Build AI assistant platform"},
]


def test_infer_exact_match():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value='{"project_id": "P1"}'):
        assert infer_project_id("automate investing ideas", PROJECTS) == "P1"


def test_infer_empty_projects_returns_inbox_without_llm_call():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call") as mock:
        result = infer_project_id("some topic", [])
    mock.assert_not_called()
    assert result == "INBOX"


def test_infer_unknown_project_id_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value='{"project_id": "P99"}'):
        assert infer_project_id("some topic", PROJECTS) == "INBOX"


def test_infer_llm_returns_inbox_sentinel():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value='{"project_id": "INBOX"}'):
        assert infer_project_id("random topic", PROJECTS) == "INBOX"


def test_infer_llm_returns_none_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value=None):
        assert infer_project_id("some topic", PROJECTS) == "INBOX"


def test_infer_invalid_json_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value="not json at all"):
        assert infer_project_id("some topic", PROJECTS) == "INBOX"
