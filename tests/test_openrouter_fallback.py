#!/usr/bin/env python3
"""Graceful OpenRouter -> claude -p / FTS fallback for research+search.

With OPENROUTER_KEY unset, generation (synthesis, Haiku filter) must degrade to
`claude -p` and semantic KB search must degrade to FTS keyword search — never
sys.exit(1).
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))


def _write_config(tmp_path, monkeypatch, db_path):
    """Point juggle settings at a tmp KB + empty vault, isolated config."""
    cfg = {
        "paths": {"vault": str(tmp_path / "vault"), "vault_name": "test"},
        "research_kb": {"db_path": str(db_path)},
    }
    (tmp_path / "vault").mkdir(exist_ok=True)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(cfg_path))


def _seed_kb(db_path):
    from juggle_research_kb import ResearchKB

    kb = ResearchKB(str(db_path))
    kb.init_db()
    kb.insert_article(
        title="Apple orchard farming guide",
        url="https://example.com/apple",
        score=200,
        date="2026-01-01",
        source="hn",
        summary="How to grow apple trees in an orchard.",
        body="apple orchard farming",
    )
    return kb


def _mock_urlopen(response_text: str):
    resp = MagicMock()
    resp.read.return_value = json.dumps(
        {"choices": [{"message": {"content": response_text}}]}
    ).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# --- Task 1: llm_call max_tokens / json_mode ---------------------------------


def test_llm_call_respects_max_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_KEY", "testkey")
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from llm_calls import llm_call

    with patch("urllib.request.urlopen", return_value=_mock_urlopen("ok")):
        with patch("urllib.request.Request") as mock_req:
            llm_call("hi", profile="cheap", max_tokens=2048)
    body = json.loads(mock_req.call_args[0][1])
    assert body["max_tokens"] == 2048


def test_llm_call_default_max_tokens_backward_compatible(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_KEY", "testkey")
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from llm_calls import llm_call

    with patch("urllib.request.urlopen", return_value=_mock_urlopen("ok")):
        with patch("urllib.request.Request") as mock_req:
            llm_call("hi", profile="cheap")
    body = json.loads(mock_req.call_args[0][1])
    assert body["max_tokens"] == 200


def test_synthesis_profile_exists_with_large_max_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from juggle_settings import get_settings

    profiles = get_settings()["llm_profiles"]
    assert "synthesis" in profiles
    assert profiles["synthesis"]["max_tokens"] >= 2048


def test_llm_profiles_default_to_deepseek_v4(tmp_path, monkeypatch):
    """Code defaults use the deepseek-v4 family (2026-07-01 chore)."""
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from juggle_settings import get_settings

    profiles = get_settings()["llm_profiles"]
    assert profiles["cheap"]["openrouter_model"] == "deepseek/deepseek-v4-flash"
    assert profiles["cheap"]["fallback_model"] == "claude-haiku-4-5-20251001"
    assert profiles["normal"]["openrouter_model"] == "deepseek/deepseek-v4-pro"
    assert profiles["normal"]["fallback_model"] == "sonnet"
    assert profiles["synthesis"]["openrouter_model"] == "deepseek/deepseek-v4-flash"
    assert profiles["synthesis"]["fallback_model"] == "sonnet"
    assert profiles["synthesis"]["max_tokens"] == 2048


# --- Task 2: research synthesis + haiku filter fall back to claude -p ---------


def test_research_synthesis_falls_back_to_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_KEY", "")
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    import juggle_cmd_research as R

    monkeypatch.setattr(
        "llm_calls.run_claude_p", lambda *a, **k: "## Summary\nsynthesized via claude"
    )
    out = R.synthesize("topic", "some context", vault_name="test")
    assert "synthesized via claude" in out


def test_research_run_does_not_exit_when_key_unset(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "kb.db"
    _seed_kb(db_path)
    _write_config(tmp_path, monkeypatch, db_path)
    monkeypatch.setenv("OPENROUTER_KEY", "")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import juggle_cmd_research as R

    monkeypatch.setattr(
        "llm_calls.run_claude_p", lambda *a, **k: "## Summary\nclaude synthesis"
    )
    # Must NOT raise SystemExit
    asyncio.run(
        R.run(topic="apple", no_web=True, verbose=False, web_results_json=None)
    )
    out = capsys.readouterr().out
    assert "claude synthesis" in out


def test_haiku_filter_falls_back_to_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_KEY", "")
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    import juggle_cmd_search as S

    payload = json.dumps({"kb": [{"title": "x", "url": "u", "reason": "r"}], "web": []})
    monkeypatch.setattr("llm_calls.run_claude_p", lambda *a, **k: payload)
    result = asyncio.run(
        S.haiku_filter("q", [{"title": "x", "url": "u"}], [])
    )
    assert result["kb"][0]["url"] == "u"


# --- Task 3: embeddings degrade to FTS ---------------------------------------


def test_offline_search_falls_back_to_fts_when_key_unset(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "kb.db"
    _seed_kb(db_path)
    _write_config(tmp_path, monkeypatch, db_path)
    monkeypatch.setenv("OPENROUTER_KEY", "")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import juggle_search_offline as O

    monkeypatch.setattr(sys, "argv", ["prog", "apple"])
    # Must NOT raise SystemExit
    asyncio.run(O.main())
    out = capsys.readouterr().out
    assert "mode=fts" in out
    assert "Apple orchard" in out


def test_search_kb_falls_back_to_fts_when_key_unset(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "kb.db"
    _seed_kb(db_path)
    _write_config(tmp_path, monkeypatch, db_path)
    monkeypatch.setenv("OPENROUTER_KEY", "")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import juggle_cmd_search as S

    args = MagicMock(query="apple", no_kb=False, no_web=True, filter=False,
                     web_results="", k=10)
    asyncio.run(S.main(args))
    out = capsys.readouterr().out
    assert "Apple orchard" in out


# --- Task 4: doc/env name consistency ----------------------------------------


def test_no_openrouter_api_key_in_code_or_docs():
    root = Path(__file__).parent.parent
    offenders = []
    for sub in ("src", "docs"):
        for p in (root / sub).rglob("*"):
            if p.suffix in (".py", ".md", ".toml", ".example") and p.is_file():
                if "OPENROUTER_API_KEY" in p.read_text(errors="ignore"):
                    offenders.append(str(p.relative_to(root)))
    assert not offenders, f"OPENROUTER_API_KEY should be OPENROUTER_KEY in: {offenders}"
