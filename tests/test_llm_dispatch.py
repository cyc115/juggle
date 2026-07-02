import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _mock_urlopen(response_text: str):
    """Return a context-manager mock that simulates urllib urlopen."""
    import json as _json
    resp = MagicMock()
    resp.read.return_value = _json.dumps({
        "choices": [{"message": {"content": response_text}}]
    }).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_llm_call_cheap_uses_cheap_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_KEY", "testkey")
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from juggle_cli_common import llm_call
    with patch("urllib.request.urlopen", return_value=_mock_urlopen("result")):
        with patch("urllib.request.Request") as mock_req:
            llm_call("hello", profile="cheap")
    body = mock_req.call_args[0][1]
    import json
    parsed = json.loads(body)
    assert "deepseek" in parsed["model"]


def test_llm_call_normal_uses_normal_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_KEY", "testkey")
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from juggle_cli_common import llm_call
    with patch("urllib.request.urlopen", return_value=_mock_urlopen("result")):
        with patch("urllib.request.Request") as mock_req:
            llm_call("hello", profile="normal")
    body = mock_req.call_args[0][1]
    import json
    parsed = json.loads(body)
    assert "deepseek" in parsed["model"]


def test_llm_call_unknown_profile_raises():
    from juggle_cli_common import llm_call
    with pytest.raises(ValueError, match="Unknown LLM profile"):
        llm_call("hello", profile="bogus")


def test_llm_call_openrouter_failure_falls_back_to_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_KEY", "testkey")
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from juggle_cli_common import llm_call
    with patch("urllib.request.urlopen", side_effect=Exception("network error")):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0, stdout="fallback result")
            result = llm_call("hello", profile="cheap")
    assert result == "fallback result"


def test_cheap_llm_call_shim_still_works(tmp_path, monkeypatch):
    """_cheap_llm_call must remain callable and delegate to llm_call."""
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from juggle_cli_common import _cheap_llm_call
    with patch("juggle_cli_common.llm_call", return_value="ok") as mock_llm:
        result = _cheap_llm_call("test")
    mock_llm.assert_called_once_with("test", profile="cheap", timeout=10)
    assert result == "ok"


def test_llm_call_disable_reasoning_sets_openrouter_field():
    """llm_call(disable_reasoning=True) adds reasoning:{enabled:false} to the OpenRouter payload."""
    import os
    os.environ["OPENROUTER_KEY"] = "testkey"
    from juggle_cli_common import llm_call
    with patch("urllib.request.urlopen", return_value=_mock_urlopen("result")):
        with patch("urllib.request.Request") as mock_req:
            llm_call("hello", profile="cheap", disable_reasoning=True)
    body = mock_req.call_args[0][1]
    import json
    parsed = json.loads(body)
    assert parsed["reasoning"] == {"enabled": False}


def test_llm_call_default_no_reasoning_field():
    """llm_call default (disable_reasoning=False) omits the reasoning field."""
    import os
    os.environ["OPENROUTER_KEY"] = "testkey"
    from juggle_cli_common import llm_call
    with patch("urllib.request.urlopen", return_value=_mock_urlopen("result")):
        with patch("urllib.request.Request") as mock_req:
            llm_call("hello", profile="cheap")
    body = mock_req.call_args[0][1]
    import json
    parsed = json.loads(body)
    assert "reasoning" not in parsed
