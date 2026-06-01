import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_cheap_llm_call_returns_none_on_all_failures():
    from juggle_cli_common import _cheap_llm_call
    with patch("juggle_cli_common.subprocess.run", side_effect=Exception("fail")), \
         patch.dict("os.environ", {"OPENROUTER_KEY": ""}):
        result = _cheap_llm_call("test prompt", timeout=1)
    assert result is None


def test_cheap_llm_call_returns_haiku_result_when_openrouter_absent():
    from juggle_cli_common import _cheap_llm_call
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "some response"
    with patch("juggle_cli_common.subprocess.run", return_value=mock_result), \
         patch.dict("os.environ", {"OPENROUTER_KEY": ""}):
        result = _cheap_llm_call("test prompt", timeout=5)
    assert result == "some response"
