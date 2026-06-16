"""TDD: specific, non-duplicate auto topic-title generation.

User feedback (2026-06-16): auto titles were vague and near-interchangeable —
'Improve Agent Dispatch Efficiency' vs '… Orchestration' vs '… Reliability'.
These tests pin three behaviours on the title path in juggle_cli_common:

1. A whole title made only of generic filler ('improve', 'efficiency',
   'system architecture') is rejected so generation falls back to specifics.
2. Two titles whose *content* words match are detected as interchangeable.
3. Generation is dedup-aware: a freshly generated title that collides with an
   existing topic's title gets disambiguated from the topic's own content.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_settings import DEFAULTS  # noqa: E402


# ---------------------------------------------------------------------------
# Pure helpers — generic-filler ban + interchangeability
# ---------------------------------------------------------------------------


def test_is_generic_title_rejects_filler_only():
    from juggle_cli_common import _is_generic_title

    assert _is_generic_title("Improve System Efficiency") is True
    assert _is_generic_title("System Architecture") is True
    assert _is_generic_title("Improve Reliability And Performance") is True


def test_is_generic_title_keeps_specific():
    from juggle_cli_common import _is_generic_title

    assert _is_generic_title("OAuth Token Refresh Flow") is False
    assert _is_generic_title("Improve Agent Dispatch Pool") is False  # 'agent','dispatch','pool'
    assert _is_generic_title("Cockpit Graph Pane Scrolling") is False


def test_titles_interchangeable_on_content_words():
    from juggle_cli_common import _titles_interchangeable

    # differ only by trailing filler → interchangeable
    assert _titles_interchangeable(
        "Improve Agent Dispatch Efficiency", "Improve Agent Dispatch Reliability"
    ) is True
    # genuinely different specifics → not interchangeable
    assert _titles_interchangeable(
        "Agent Dispatch Pool", "OAuth Login Flow"
    ) is False


# ---------------------------------------------------------------------------
# Test helpers (mirror test_title_gen.py's mocking shape)
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> dict:
    base = {
        "title_gen": {
            "openrouter_enabled": True,
            "openrouter_model": "meta-llama/llama-3.1-8b-instruct:free",
            "haiku_model": "claude-haiku-4-5-20251001",
            "timeout_secs": 5,
        },
        "llm_profiles": DEFAULTS["llm_profiles"],
    }
    base.update(overrides)
    return base


def _db(threads=None) -> MagicMock:
    db = MagicMock()
    db.update_thread = MagicMock()
    db.get_all_threads = MagicMock(return_value=list(threads or []))
    return db


def _urlopen_ok(content: str):
    import json as _json

    payload = _json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=resp)


# ---------------------------------------------------------------------------
# Generation — generic title is rejected, falls back to topic specifics
# ---------------------------------------------------------------------------


def test_generic_llm_title_falls_back_to_specific(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread

    db = _db()
    monkeypatch.setenv("OPENROUTER_KEY", "sk-test-key")
    mock_urlopen = _urlopen_ok("Improve System Efficiency")  # pure filler

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("urllib.request.urlopen", mock_urlopen):
            with patch("subprocess.run",
                       MagicMock(return_value=MagicMock(returncode=1, stdout=""))):
                title = _generate_title_for_thread(
                    db, "uuid-1", "wire up agent dispatch pool sizing"
                )

    # The generic LLM output is discarded; we keep the topic's specifics.
    assert title != "Improve System Efficiency"
    assert "Agent" in title and "Dispatch" in title


# ---------------------------------------------------------------------------
# Generation — dedup-aware against an existing topic title
# ---------------------------------------------------------------------------


def test_generation_is_dedup_aware(monkeypatch):
    from juggle_cli_common import _generate_title_for_thread, _titles_interchangeable

    existing = [
        {"id": "other", "title": "Improve Agent Dispatch Efficiency", "topic": "x"}
    ]
    db = _db(threads=existing)
    monkeypatch.setenv("OPENROUTER_KEY", "sk-test-key")
    # LLM returns a title interchangeable with the existing one.
    mock_urlopen = _urlopen_ok("Improve Agent Dispatch Reliability")

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("urllib.request.urlopen", mock_urlopen):
            title = _generate_title_for_thread(
                db, "uuid-1", "agent dispatch retry backoff handling"
            )

    # Final stored title must NOT be interchangeable with the existing topic.
    assert not _titles_interchangeable(title, "Improve Agent Dispatch Efficiency")
    db.update_thread.assert_called_once_with("uuid-1", title=title)


def test_dedup_excludes_self(monkeypatch):
    """Re-running generation for a thread must not dedupe against its own title."""
    from juggle_cli_common import _generate_title_for_thread

    existing = [
        {"id": "uuid-1", "title": "Agent Dispatch Pool Sizing", "topic": "x"}
    ]
    db = _db(threads=existing)
    monkeypatch.setenv("OPENROUTER_KEY", "sk-test-key")
    mock_urlopen = _urlopen_ok("Agent Dispatch Pool Sizing")

    with patch("juggle_settings.get_settings", return_value=_cfg()):
        with patch("urllib.request.urlopen", mock_urlopen):
            title = _generate_title_for_thread(db, "uuid-1", "agent dispatch pool sizing")

    assert title == "Agent Dispatch Pool Sizing"  # unchanged — self is not a duplicate
