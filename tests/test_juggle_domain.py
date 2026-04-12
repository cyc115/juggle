"""Tests for juggle agent domain isolation (v1.5.0)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


# ------------------------------------------------------------------
# Domain registry
# ------------------------------------------------------------------

def test_initial_domains_seeded(db):
    domains = db.get_domains()
    assert "juggle" in domains
    assert "vault" in domains
    assert "work" in domains


def test_register_domain(db):
    db.register_domain("myproject")
    assert db.is_known_domain("myproject")


def test_is_known_domain_false_for_unknown(db):
    assert not db.is_known_domain("nonexistent-domain")


def test_register_domain_idempotent(db):
    db.register_domain("juggle")
    db.register_domain("juggle")
    assert db.get_domains().count("juggle") == 1


# ------------------------------------------------------------------
# Domain paths
# ------------------------------------------------------------------

def test_initial_domain_paths_seeded(db):
    paths = {m["path_fragment"]: m["domain"] for m in db.get_domain_paths()}
    assert paths.get("/github/juggle") == "juggle"
    assert paths.get("/Documents/personal") == "vault"
    assert paths.get("/work/") == "work"


def test_add_domain_path(db):
    db.register_domain("ml")
    db.add_domain_path("/projects/ml", "ml")
    paths = {m["path_fragment"]: m["domain"] for m in db.get_domain_paths()}
    assert paths["/projects/ml"] == "ml"


def test_add_domain_path_replace(db):
    db.register_domain("ml")
    db.add_domain_path("/github/juggle", "ml")
    paths = {m["path_fragment"]: m["domain"] for m in db.get_domain_paths()}
    assert paths["/github/juggle"] == "ml"


def test_infer_domain_from_prompt_match(db):
    assert db.infer_domain_from_prompt("fix bug in /github/juggle/src/cli.py") == "juggle"
    assert db.infer_domain_from_prompt("scan /Documents/personal/inbox.md") == "vault"


def test_infer_domain_from_prompt_no_match(db):
    assert db.infer_domain_from_prompt("buy groceries") is None


# ------------------------------------------------------------------
# create_thread with domain
# ------------------------------------------------------------------

def test_create_thread_with_domain(db):
    tid = db.create_thread("juggle work", session_id="s1", domain="juggle")
    thread = db.get_thread(tid)
    assert thread["domain"] == "juggle"


def test_create_thread_null_domain(db):
    tid = db.create_thread("general task", session_id="s1")
    thread = db.get_thread(tid)
    assert thread["domain"] is None


# ------------------------------------------------------------------
# get_best_agent domain filtering
# ------------------------------------------------------------------

def test_null_thread_only_gets_null_domain_agents(db):
    """Thread with domain=null only gets agents with domain=null."""
    a_juggle = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(a_juggle, domain="juggle")

    a_null = db.create_agent(role="coder", pane_id="%2")
    # a_null has domain=None (default)

    result = db.get_best_agent("thread-1", domain=None)
    assert result is not None
    assert result["id"] == a_null


def test_null_thread_no_null_agents_returns_none(db):
    """Thread with domain=null and only domain-stamped agents → None (spawn fresh)."""
    a_juggle = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(a_juggle, domain="juggle")

    result = db.get_best_agent("thread-1", domain=None)
    assert result is None


def test_domain_thread_gets_matching_domain_agent(db):
    """Thread with domain='juggle' accepts domain='juggle' agents."""
    a_juggle = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(a_juggle, domain="juggle")

    result = db.get_best_agent("thread-1", domain="juggle")
    assert result is not None
    assert result["id"] == a_juggle


def test_domain_thread_gets_null_domain_agent(db):
    """Thread with domain='juggle' also accepts domain=null (fresh) agents."""
    a_null = db.create_agent(role="coder", pane_id="%1")
    # a_null has domain=None

    result = db.get_best_agent("thread-1", domain="juggle")
    assert result is not None
    assert result["id"] == a_null


def test_domain_thread_rejects_cross_domain_agent(db):
    """Thread with domain='juggle' must NOT reuse domain='vault' agent."""
    a_vault = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(a_vault, domain="vault")

    result = db.get_best_agent("thread-1", domain="juggle")
    assert result is None


def test_cross_domain_isolation(db):
    """Juggle agent is never returned for a vault thread."""
    a_juggle = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(a_juggle, domain="juggle")

    a_vault = db.create_agent(role="coder", pane_id="%2")
    db.update_agent(a_vault, domain="vault")

    juggle_result = db.get_best_agent("thread-juggle", domain="juggle")
    vault_result = db.get_best_agent("thread-vault", domain="vault")

    assert juggle_result["id"] == a_juggle
    assert vault_result["id"] == a_vault


def test_domain_filter_still_scores_by_context(db):
    """Within matching domain agents, context_threads scoring still applies."""
    a1 = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(a1, domain="juggle")

    a2 = db.create_agent(role="coder", pane_id="%2")
    db.update_agent(a2, domain="juggle", context_threads='["thread-x"]')

    result = db.get_best_agent("thread-x", domain="juggle")
    assert result["id"] == a2


# ------------------------------------------------------------------
# Agent domain stamped at assignment (via update_agent)
# ------------------------------------------------------------------

def test_agent_domain_persisted(db):
    agent_id = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(agent_id, domain="vault")
    agent = db.get_agent(agent_id)
    assert agent["domain"] == "vault"
