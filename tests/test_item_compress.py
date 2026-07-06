"""Concise cockpit item formatter (juggle_item_compress).

Incident 2026-07-05: cockpit action items + completion notifications rendered as
multi-paragraph verbatim dumps (the verbose PR #62 [SL] item), blowing the
~280-char / 2-5-line budget and making the Action Items + Notifications panels
unreadable. compress_item() collapses over-budget free text to the canonical
<=5-line (action) / 1-line (notification) format at the write seam, LLM few-shot
when a key is present, deterministic truncate-with-pointer when not — never
raising, never blocking item creation.

Tests assert CONSTRAINTS (the LLM is non-deterministic), not exact strings.
The LLM is MOCKED here; the one live-key smoke lives in
test_item_compress_smoke.py.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import juggle_item_compress as jic

# --- Canonical golden fixtures (user-approved, 2026-07-05) ------------------

# The two verbose INPUTS that blew the budget (abbreviated but multi-paragraph
# + well over 280 chars / 5 lines).
VERBOSE_ACTION_INPUT = """\
I reviewed PR #62 (the weekly-autofix branch). Here is the full picture.

The branch runs the automated fixers and cleans 22 genuine F401/F841 findings
across the codebase — these are all real unused-import / unused-variable
findings, not false positives, so the change is necessary and worth landing.

However it is NOT clean as-is. FX-1 in that branch dropped `import time` from
juggle_cockpit.py, which breaks 2 cockpit patch-target tests that patch
`juggle_cockpit.time`. The rest of the suite I verified is safe.

The fix is one line: restore `import time  # noqa` at the top of the module.
After applying it locally I re-ran the full suite and 4132 tests pass.

My recommendation is to apply the one-line fix and then ff-merge the branch.
Let me know / ack to proceed."""

VERBOSE_NOTIFICATION_INPUT = """\
Done. I finished the autofix fix-agent task on PR #62.

Summary of what happened: the branch was merged at ff9f051 after I applied the
one-line import fix. In total the run cleaned 22 lint findings (F401/F841).
Along the way I caught and fixed 1 regression — FX-1 had dropped `import time`
from juggle_cockpit.py — and re-verified the full suite: 4132 tests pass.

Everything is green and landed on main."""

# The canonical OUTPUTS the LLM is expected to produce (baked into the doc).
CANONICAL_ACTION_OUTPUT = (
    "⚡ [SL] PR #62 weekly-autofix — merge?\n"
    "Necessary: cleans 22 real F401/F841 findings. Not clean as-is — FX-1 "
    "dropped import time from juggle_cockpit.py → 2 cockpit patch-target tests "
    "fail (rest verified safe).\n"
    "Fix = 1 line (import time # noqa), re-verified 4132 pass.\n"
    "→ Rec: fix + ff-merge. Ack to proceed."
)

CANONICAL_NOTIFICATION_OUTPUT = (
    "✓ [SL] PR #62 (autofix) merged @ ff9f051 — 22 lint findings cleaned; "
    "caught + fixed 1 regression (import time). 4132 pass."
)


@pytest.fixture
def with_key(monkeypatch):
    """Pretend an LLM key is present so the compress path is exercised."""
    monkeypatch.setenv("OPENROUTER_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _assert_action_ok(out: str):
    lines = out.splitlines()
    assert 1 <= len(lines) <= jic.ACTION_MAX_LINES, f"action >5 lines: {out!r}"
    assert any("→" in ln or "Rec:" in ln for ln in lines), f"no rec line: {out!r}"


def _assert_notification_ok(out: str):
    assert "\n" not in out, f"notification not 1 line: {out!r}"
    assert len(out) <= jic.NOTIFICATION_MAX_CHARS, f"notification >280: {out!r}"
    assert out.startswith(jic.NOTIFICATION_MARKER), f"no check mark: {out!r}"


# --- Golden: LLM path (mocked) ---------------------------------------------


def test_action_golden_llm_compresses_to_canonical(monkeypatch, with_key):
    """RED before juggle_item_compress exists. Over-budget action prose →
    LLM (mocked to canonical) → <=5 lines with a rec line."""
    monkeypatch.setattr(jic, "_cheap_llm_call", lambda *a, **k: CANONICAL_ACTION_OUTPUT)
    out = jic.compress_item("action", "SL", VERBOSE_ACTION_INPUT)
    _assert_action_ok(out)
    assert "22" in out  # concrete counts preserved


def test_notification_golden_llm_compresses_to_canonical(monkeypatch, with_key):
    monkeypatch.setattr(
        jic, "_cheap_llm_call", lambda *a, **k: CANONICAL_NOTIFICATION_OUTPUT
    )
    out = jic.compress_item("notification", "SL", VERBOSE_NOTIFICATION_INPUT)
    _assert_notification_ok(out)
    assert "ff9f051" in out


# --- Short items pass through untouched -------------------------------------


def test_short_notification_passes_through_unchanged(with_key):
    short = "✓ [SL] merged @ ff9f051 — 4132 pass."
    assert jic.compress_item("notification", "SL", short) == short


def test_short_action_passes_through_unchanged(with_key):
    short = "⚡ [SL] merge PR #62?\n→ Rec: fix + ff-merge. Ack to proceed."
    assert jic.compress_item("action", "SL", short) == short


# --- Validator repairs over-budget LLM output ------------------------------


def test_llm_overflow_action_is_repaired_to_budget(monkeypatch, with_key):
    """LLM returns 8 lines → validator repairs to <=5 lines, keeps a rec line."""
    bloated = "\n".join(
        ["⚡ [SL] line 1"] + [f"line {i}" for i in range(2, 8)] + ["→ Rec: go."]
    )
    monkeypatch.setattr(jic, "_cheap_llm_call", lambda *a, **k: bloated)
    out = jic.compress_item("action", "SL", VERBOSE_ACTION_INPUT)
    _assert_action_ok(out)


def test_llm_overflow_notification_is_repaired_to_budget(monkeypatch, with_key):
    """LLM returns multi-line >280 → repaired to exactly 1 line, <=280, ✓."""
    bloated = "✓ [SL] " + ("x" * 400) + "\nsecond line here"
    monkeypatch.setattr(jic, "_cheap_llm_call", lambda *a, **k: bloated)
    out = jic.compress_item("notification", "SL", VERBOSE_NOTIFICATION_INPUT)
    _assert_notification_ok(out)


# --- Fail-open: no key / LLM error → deterministic truncate-with-pointer -----


def test_no_key_action_falls_back_deterministically(monkeypatch):
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def _boom(*a, **k):  # must never be called when no key
        raise AssertionError("LLM must not be called without a key")

    monkeypatch.setattr(jic, "_cheap_llm_call", _boom)
    out = jic.compress_item("action", "SL", VERBOSE_ACTION_INPUT, thread_ref="SL")
    _assert_action_ok(out)
    assert "see thread messages" in out


def test_no_key_notification_falls_back_deterministically(monkeypatch):
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = jic.compress_item(
        "notification", "SL", VERBOSE_NOTIFICATION_INPUT, thread_ref="SL"
    )
    _assert_notification_ok(out)
    assert "see thread messages" in out


def test_llm_error_falls_back_and_never_raises(monkeypatch, with_key):
    """The write seam must never block item creation — an LLM exception is
    swallowed and the deterministic fallback is returned."""
    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(jic, "_cheap_llm_call", _boom)
    out = jic.compress_item("notification", "SL", VERBOSE_NOTIFICATION_INPUT)
    _assert_notification_ok(out)


def test_llm_returns_none_falls_back(monkeypatch, with_key):
    monkeypatch.setattr(jic, "_cheap_llm_call", lambda *a, **k: None)
    out = jic.compress_item("action", "SL", VERBOSE_ACTION_INPUT)
    _assert_action_ok(out)


def test_empty_text_returns_empty(with_key):
    assert jic.compress_item("notification", "SL", "") == ""
    assert jic.compress_item("action", "SL", None) == ""


# --- Regression pins at the WRITE seams -------------------------------------
# Incident 2026-07-05: the verbose PR #62 [SL] item — the [auto-decision] path
# pasted the orchestrator reply verbatim and the completion notification used
# the agent's raw `agent complete` string, both blowing the cockpit budget.


@pytest.fixture
def seam_db(tmp_path, monkeypatch):
    """A tmp JuggleDB with no LLM key → the seams take the deterministic path
    (no real subprocess in the suite)."""
    from juggle_db import JuggleDB

    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    tid = db.create_thread("PR #62 weekly-autofix review", session_id="s1")
    db.set_current_thread(tid)
    with db._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO session (key, value) VALUES ('session_id', 's1')"
        )
        conn.commit()
    label = db.get_thread(tid).get("user_label")
    return db, tid, label


def test_prose_decision_seam_compresses_verbose_item_2026_07_05(seam_db):
    """2026-07-05 verbose PR#62 [SL] item: record_prose_decision must file an
    action item within the ≤5-line budget, not the verbatim multi-paragraph
    prose."""
    from juggle_hooks_prose import record_prose_decision

    db, _tid, _label = seam_db
    verbose = VERBOSE_ACTION_INPUT + "\n\nYour call — should I proceed?"
    record_prose_decision(db, verbose)

    items = [
        i for i in db.get_open_action_items()
        if i["message"].startswith("[auto-decision]")
    ]
    assert len(items) == 1
    body = items[0]["message"][len("[auto-decision] "):]
    assert len(body.splitlines()) <= jic.ACTION_MAX_LINES, body


def test_complete_agent_seam_compresses_verbose_notification_2026_07_05(seam_db):
    """2026-07-05: the AGENT_COMPLETE notification must be the 1-line budget,
    not the agent's raw multi-paragraph `agent complete` string."""
    import juggle_cli_common as _common
    from juggle_cmd_agents_complete import cmd_complete_agent

    db, tid, label = seam_db
    args = type("A", (), {})()
    args.thread_id = label
    args.result_summary = VERBOSE_NOTIFICATION_INPUT
    args.retain_text = None
    args.open_questions = None
    args.handoff = None
    args.role = "coder"

    import juggle_spool_cli_common as _spool
    # Force the non-spool path.
    _orig = _common.get_db
    _common.get_db = lambda: db
    _spool_orig = _spool.spool_event_if_agent
    _spool.spool_event_if_agent = lambda *a, **k: False
    try:
        cmd_complete_agent(args)
    finally:
        _common.get_db = _orig
        _spool.spool_event_if_agent = _spool_orig

    notifs = db.get_notifications_for_session("s1")
    agent_notifs = [n for n in notifs if "\n" not in n["message"]]
    assert agent_notifs, f"expected a 1-line notification, got {notifs}"
    for n in notifs:
        assert len(n["message"].splitlines()) <= 1, n["message"]


def test_notify_seam_compresses_over_budget_message_2026_07_05(
    seam_db, tmp_path, monkeypatch
):
    """2026-07-05: an over-budget `notify` message must land as the 1-line
    cockpit budget, not the raw multi-paragraph blob."""
    import juggle_cli_common as _common
    from juggle_cmd_agents import cmd_notify

    db, _tid, label = seam_db
    # Force the non-spool path (this suite runs inside a juggle-juggle-* worktree).
    for var in ("JUGGLE_IS_AGENT", "JUGGLE_ORCHESTRATOR", "JUGGLE_AGENT_WORKTREE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    db.set_active(True)
    args = type("A", (), {})()
    args.thread_id = label
    args.message = VERBOSE_NOTIFICATION_INPUT

    monkeypatch.setattr(_common, "get_db", lambda: db)
    cmd_notify(args)

    notifs = db.get_notifications_for_session("s1")
    assert notifs, "expected a notification"
    for n in notifs:
        _assert_notification_ok(n["message"])
