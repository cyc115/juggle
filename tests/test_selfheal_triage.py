"""selfheal-triage-v2 P1 — pure triage logic + DB sweep/valve integration pins."""
from datetime import datetime, timezone

from juggle_db import JuggleDB
from selfheal_triage import (
    ALLOWLIST_VERSION,
    STRONG_SIGNAL_REGEX,
    classify_allowlist,
)


def test_classify_matches_sleep_timeout():
    """selfheal-v2 P1 (2026-06-21): anchored sleep-timeout transient -> rule id."""
    rid = classify_allowlist("B", "Bash", "sleep: command timed out after 120 seconds")
    assert rid is not None


def test_classify_matches_tmp_path_gone():
    """selfheal-v2 P1 (2026-06-21): tmp-path-gone is benign."""
    rid = classify_allowlist("FileNotFoundError", "juggle_worktree",
                             "no such file or directory: /tmp/juggle-xyz/foo")
    assert rid is not None


def test_classify_rejects_unanchored_substring():
    """selfheal-v2 P1 (2026-06-21): a matching regex on the WRONG entrypoint must NOT sweep."""
    # 'broken pipe' text but from a real app entrypoint with mismatched exc_type
    rid = classify_allowlist("ValueError", "juggle_graph_dispatch", "broken pipe")
    assert rid is None


def test_argparse_selfcall_never_swept():
    """selfheal-v2 P1 (2026-06-21): malformed juggle_cli self-call is a strong real-bug signal, never benign."""
    rid = classify_allowlist("B", "Bash",
                             "juggle_cli.py: error: argument command: invalid choice: 'complte-agent'")
    assert rid is None
    assert STRONG_SIGNAL_REGEX.search("error: argument command: invalid choice: 'x'")


def test_allowlist_version_is_int():
    assert isinstance(ALLOWLIST_VERSION, int) and ALLOWLIST_VERSION >= 1


def test_sweep_sets_matching_open_rows_to_non_issue(tmp_path):
    """selfheal-v2 P1 (2026-06-21): allowlist sweep hides transient open rows, leaves real ones."""
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    db.dedup_or_insert_error(
        "s_sleep", "B", None, "sleep: command timed out after 120 seconds", "Bash", "{}")
    db.dedup_or_insert_error(
        "s_real", "A", "ValueError", "ValueError: bad config", "juggle_graph_dispatch", "{}")
    swept = db.sweep_allowlist_to_nonissue(classify_allowlist, ALLOWLIST_VERSION)
    assert {s["signature_hash"] for s in swept} == {"s_sleep"}
    rows = {r["signature_hash"]: r["status"] for r in db.get_open_error_events(include_hidden=True)}
    assert rows["s_sleep"] == "non_issue"
    assert rows["s_real"] == "open"
