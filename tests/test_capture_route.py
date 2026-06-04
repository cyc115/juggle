import sys
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

FAKE_VAULT = "/fake/vault"


def _route(text, vault=FAKE_VAULT):
    from juggle_cmd_capture import resolve_capture_destination
    return resolve_capture_destination(text, vault_root=vault)


# ── lifeos ────────────────────────────────────────────────────────────────────

def test_lifeos_keyword():
    r = _route("fix the lifeos deployment")
    assert r["destination"] == f"{FAKE_VAULT}/projects/lifeos/TODO.md"
    assert r["is_vault"] is True
    assert r["project"] == "lifeos"


def test_telegram_bot_keyword():
    r = _route("update the telegram-bot handler")
    assert r["project"] == "lifeos"


def test_hindsight_keyword():
    r = _route("hindsight memory is failing")
    assert r["project"] == "lifeos"


def test_ec2_agent_keyword():
    r = _route("restart EC2-agent process")
    assert r["project"] == "lifeos"


def test_daemon_keyword():
    r = _route("daemon crashed again")
    assert r["project"] == "lifeos"


# ── juggle ────────────────────────────────────────────────────────────────────

def test_juggle_keyword():
    r = _route("fix juggle watchdog bug")
    assert r["destination"] == f"{FAKE_VAULT}/projects/juggle/TODO.md"
    assert r["is_vault"] is True
    assert r["project"] == "juggle"


def test_orchestrator_keyword():
    r = _route("orchestrator is dropping threads")
    assert r["project"] == "juggle"


def test_juggle_thread_keyword():
    r = _route("juggle-thread routing is broken")
    assert r["project"] == "juggle"


# ── real-estate ───────────────────────────────────────────────────────────────

def test_real_estate_keyword():
    r = _route("research real-estate opportunities downtown")
    assert r["destination"] == f"{FAKE_VAULT}/projects/real-estate/TODO.md"
    assert r["project"] == "real-estate"


def test_mortgage_keyword():
    r = _route("call bank about mortgage rates")
    assert r["project"] == "real-estate"


def test_apartment_keyword():
    r = _route("apartment lease renewal in June")
    assert r["project"] == "real-estate"


def test_property_keyword():
    r = _route("property inspection scheduled")
    assert r["project"] == "real-estate"


# ── ai-engineering ────────────────────────────────────────────────────────────

def test_ai_engineering_keyword():
    r = _route("build an embedding pipeline for search")
    assert r["destination"] == f"{FAKE_VAULT}/projects/ai-engineering/TODO.md"
    assert r["project"] == "ai-engineering"


def test_model_training_keyword():
    r = _route("model-training run failed on GPU")
    assert r["project"] == "ai-engineering"


# ── automation ────────────────────────────────────────────────────────────────

def test_automation_keyword():
    r = _route("write a script to automate backups")
    assert r["destination"] == f"{FAKE_VAULT}/projects/automation/TODO.md"
    assert r["project"] == "automation"


def test_launchd_keyword():
    r = _route("update launchd plist timing")
    assert r["project"] == "automation"


def test_cron_keyword():
    r = _route("add cron job for weekly cleanup")
    assert r["project"] == "automation"


def test_scheduled_task_keyword():
    r = _route("scheduled-task is not firing")
    assert r["project"] == "automation"


# ── trading-edge ──────────────────────────────────────────────────────────────

def test_trading_edge_keyword():
    r = _route("fix the trading-edge DB schema")
    assert r["destination"] == "/Users/mikechen/github/trading-edge/TODO.md"
    assert r["is_vault"] is False
    assert r["project"] == "trading-edge"


def test_news_ingest_keyword():
    r = _route("fix the news-ingest reuters adapter")
    assert r["destination"] == "/Users/mikechen/github/trading-edge/TODO.md"
    assert r["is_vault"] is False
    assert r["project"] == "trading-edge"


def test_news_insights_keyword():
    r = _route("add news-insights threshold tuning")
    assert r["project"] == "trading-edge"


def test_bb_browser_keyword():
    r = _route("update bb-browser adapter for seekingalpha")
    assert r["project"] == "trading-edge"


def test_subreddit_keyword():
    r = _route("add subreddit r/options to ingest config")
    assert r["project"] == "trading-edge"


def test_backtest_keyword():
    r = _route("run backtest on value factor")
    assert r["project"] == "trading-edge"


def test_trading_edge_two_words():
    r = _route("look into trading edge momentum signals")
    assert r["project"] == "trading-edge"


def test_news_adapter_two_words():
    r = _route("build a news adapter for reuters")
    assert r["project"] == "trading-edge"


# ── INBOX ─────────────────────────────────────────────────────────────────────

def test_no_match_returns_inbox():
    r = _route("buy more SPY")
    assert r["destination"] == f"{FAKE_VAULT}/inbox.md"
    assert r["is_vault"] is True
    assert r["project"] == "INBOX"


def test_generic_task_returns_inbox():
    r = _route("schedule dentist appointment")
    assert r["project"] == "INBOX"


# ── case-insensitivity ────────────────────────────────────────────────────────

def test_case_insensitive_upper():
    r = _route("LIFEOS deployment fix")
    assert r["project"] == "lifeos"


def test_case_insensitive_mixed():
    r = _route("LifeOS Telegram-Bot issue")
    assert r["project"] == "lifeos"


def test_case_insensitive_trading():
    r = _route("Update Trading-Edge config")
    assert r["project"] == "trading-edge"


# ── priority / precedence ─────────────────────────────────────────────────────

def test_priority_lifeos_beats_automation():
    # "script" is automation; "lifeos" is lifeos — lifeos wins (higher priority)
    r = _route("write a lifeos script to restart the daemon")
    assert r["project"] == "lifeos"


def test_priority_juggle_beats_automation():
    r = _route("write a juggle script for thread management")
    assert r["project"] == "juggle"


def test_priority_trading_edge_beats_automation():
    # "script" is automation; "news-ingest" is trading-edge — trading-edge wins
    r = _route("write a news-ingest script for reuters")
    assert r["project"] == "trading-edge"


# ── JSON shape ────────────────────────────────────────────────────────────────

def test_json_shape_vault():
    r = _route("fix lifeos daemon")
    assert set(r.keys()) >= {"destination", "is_vault", "project"}
    assert isinstance(r["is_vault"], bool)
    assert isinstance(r["destination"], str)
    assert isinstance(r["project"], str)


def test_json_shape_non_vault():
    r = _route("trading-edge backtest fix")
    assert r["is_vault"] is False
    assert r["destination"].startswith("/")
