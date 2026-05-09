"""Tests for title generation fallback chain and settings defaults."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_settings import DEFAULTS


def test_title_gen_defaults_present():
    tg = DEFAULTS.get("title_gen")
    assert tg is not None, "title_gen section missing from DEFAULTS"
    assert tg["openrouter_enabled"] is True
    assert tg["openrouter_model"] == "meta-llama/llama-3.1-8b-instruct:free"
    assert "openrouter_api_key" not in tg, "API key must not appear in config defaults — use OPENROUTER_KEY env var"
    assert tg["haiku_model"] == "claude-haiku-4-5-20251001"
    assert tg["timeout_secs"] == 10


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> dict:
    base = {
        "title_gen": {
            "openrouter_enabled": True,
            "openrouter_model": "meta-llama/llama-3.1-8b-instruct:free",
            "openrouter_api_key": "",
            "haiku_model": "claude-haiku-4-5-20251001",
            "timeout_secs": 5,
        }
    }
    base["title_gen"].update(overrides)
    return base


def _db() -> MagicMock:
    db = MagicMock()
    db.update_thread = MagicMock()
    return db


def _urlopen_ok(title: str) -> MagicMock:
    """Return a mock for urllib.request.urlopen that yields a 200 response."""
    resp_data = json.dumps(
        {"choices": [{"message": {"content": title}}]}
    ).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = resp_data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=mock_resp)


# ---------------------------------------------------------------------------
# Tier 1 — OpenRouter (key from OPENROUTER_KEY env var)
# ---------------------------------------------------------------------------

def test_tier1_success_uses_openrouter_title(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread
    db = _db()
    monkeypatch.setenv("OPENROUTER_KEY", "sk-test-key")
    mock_urlopen = _urlopen_ok("Build Auth System With OAuth")

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("urllib.request.urlopen", mock_urlopen):
            title = _generate_title_for_thread(db, "uuid-1", "Implement OAuth login flow")

    assert title == "Build Auth System With OAuth"
    db.update_thread.assert_called_once_with("uuid-1", title="Build Auth System With OAuth")


def test_tier1_skipped_when_env_key_missing(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread
    db = _db()
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="Haiku Fallback Title\n"))

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("urllib.request.urlopen") as mock_urlopen:
            with patch("subprocess.run", mock_run):
                _generate_title_for_thread(db, "uuid-1", "OAuth login")

    mock_urlopen.assert_not_called()


def test_tier1_skipped_when_openrouter_disabled(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread
    db = _db()
    monkeypatch.setenv("OPENROUTER_KEY", "sk-test-key")
    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="Haiku Title\n"))

    with patch("juggle_settings.get_settings", return_value=_cfg(openrouter_enabled=False)):
        with patch("urllib.request.urlopen") as mock_urlopen:
            with patch("subprocess.run", mock_run):
                _generate_title_for_thread(db, "uuid-1", "Something")

    mock_urlopen.assert_not_called()


def test_tier1_exception_falls_to_tier2(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread
    db = _db()
    monkeypatch.setenv("OPENROUTER_KEY", "sk-test-key")
    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="Haiku Result\n"))

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("urllib.request.urlopen", side_effect=Exception("network error")):
            with patch("subprocess.run", mock_run):
                title = _generate_title_for_thread(db, "uuid-1", "Some task")

    assert title == "Haiku Result"
    call_args = mock_run.call_args[0][0]
    assert "--model" in call_args
    assert "claude-haiku-4-5-20251001" in call_args


def test_tier1_overlong_response_falls_to_tier2(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread
    db = _db()
    monkeypatch.setenv("OPENROUTER_KEY", "sk-test-key")
    long_title = " ".join(f"word{i}" for i in range(16))  # 16 words — exceeds 15-word limit
    mock_urlopen = _urlopen_ok(long_title)
    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="Good Short Title\n"))

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("urllib.request.urlopen", mock_urlopen):
            with patch("subprocess.run", mock_run):
                title = _generate_title_for_thread(db, "uuid-1", "Some task")

    assert title == "Good Short Title"


# ---------------------------------------------------------------------------
# Tier 2 — claude -p --model haiku
# ---------------------------------------------------------------------------

def test_tier2_success_stores_and_returns_title(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread
    db = _db()
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="  Generated Haiku Title  \n"))

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("subprocess.run", mock_run):
            title = _generate_title_for_thread(db, "uuid-1", "Do the thing")

    assert title == "Generated Haiku Title"
    db.update_thread.assert_called_once_with("uuid-1", title="Generated Haiku Title")


def test_tier2_nonzero_returncode_falls_to_tier3(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread
    db = _db()
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    mock_run = MagicMock(return_value=MagicMock(returncode=1, stdout="some output"))

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("subprocess.run", mock_run):
            title = _generate_title_for_thread(db, "uuid-1", "one two three four five six")

    assert title == "one two three four five"


def test_tier2_empty_stdout_falls_to_tier3(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread
    db = _db()
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=""))

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("subprocess.run", mock_run):
            title = _generate_title_for_thread(db, "uuid-1", "one two three four five six")

    assert title == "one two three four five"


def test_tier2_file_not_found_falls_to_tier3(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread
    db = _db()
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    mock_run = MagicMock(side_effect=FileNotFoundError("claude not found"))

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("subprocess.run", mock_run):
            title = _generate_title_for_thread(db, "uuid-1", "alpha beta gamma delta epsilon eta")

    assert title == "alpha beta gamma delta epsilon"


# ---------------------------------------------------------------------------
# Tier 3 — first 5 words
# ---------------------------------------------------------------------------

def test_tier3_empty_topic_stores_empty_string(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread
    db = _db()
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("subprocess.run", MagicMock(side_effect=Exception("fail"))):
            title = _generate_title_for_thread(db, "uuid-1", "")

    assert title == ""
    db.update_thread.assert_called_once_with("uuid-1", title="")


def test_tier3_db_updated_with_fallback(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread
    db = _db()
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("subprocess.run", MagicMock(return_value=MagicMock(returncode=2, stdout=""))):
            title = _generate_title_for_thread(
                db, "uuid-1", "apple banana cherry date elderberry fig"
            )

    assert title == "apple banana cherry date elderberry"
    db.update_thread.assert_called_once_with("uuid-1", title="apple banana cherry date elderberry")
