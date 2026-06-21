"""selfheal v2 p2 Task 1 — group_key pure-function pins (normalize → hash).

No DB, no I/O. Asserts: same-family→same key, discriminator-differs→different
key, the real 8-hash incident collapses to one key, and the deliberate
under-aggregation trade (DA fix f).
"""
from selfheal_grouping import normalize, normalize_entrypoint, innermost_app_frame, group_key


def _a(exc_type, tb, ep="juggle_cli.py"):
    return {"error_class": "A", "exc_type": exc_type, "entrypoint": ep, "traceback": tb}


TB_L42 = ('Traceback (most recent call last):\n'
          '  File "/Users/x/juggle/src/juggle_cmd_thread.py", line 42, in cmd_send\n'
          '    foo()\nKeyError: \'agent\'\n')
TB_L88 = TB_L42.replace("line 42", "line 88")  # same bug, line moved


def test_normalize_masks_volatile_keeps_discriminators():
    assert normalize("/tmp/juggle-juggle-DH/x") == normalize("/tmp/juggle-juggle-ZZ/y")  # paths→<PATH>
    assert "<UUID>" in normalize("id 550e8400-e29b-41d4-a716-446655440000")
    assert "<PANE>" in normalize("pane %1234 died")
    # discriminators preserved:
    assert "404" in normalize("HTTP 404 not found") and "500" in normalize("HTTP 500")
    assert normalize("HTTP 404") != normalize("HTTP 500")
    assert "recall" in normalize("invalid choice: 'recall'")  # argparse subcommand kept


def test_innermost_frame_drops_lineno():
    assert innermost_app_frame(TB_L42) == "juggle_cmd_thread.py:cmd_send"
    assert innermost_app_frame(TB_L42) == innermost_app_frame(TB_L88)
    assert innermost_app_frame("ToolError: nonzero exit") is None  # class B → None


def test_normalize_entrypoint_basename_lowercased():
    assert normalize_entrypoint("/Users/x/juggle/src/juggle_cli.py") == "juggle_cli.py"
    assert normalize_entrypoint("Bash") == "bash"
    assert normalize_entrypoint(None) == ""


def test_8_hashes_one_bug_collapses_to_one_group():
    """REGRESSION (2026-06-21 selfheal v2 p2): line-number drift produced N
    signature_hashes for ONE bug; group_key must collapse them."""
    assert group_key(_a("KeyError", TB_L42)) == group_key(_a("KeyError", TB_L88))


def test_exc_type_is_hard_partition():
    """KeyError vs ValueError with identical frames must NOT over-aggregate (research §5)."""
    assert group_key(_a("KeyError", TB_L42)) != group_key(_a("ValueError", TB_L42))


def test_entrypoint_is_hard_partition():
    assert group_key(_a("KeyError", TB_L42, ep="juggle_cli.py")) != \
           group_key(_a("KeyError", TB_L42, ep="cockpit"))


def test_error_class_A_never_groups_with_B():
    b = {"error_class": "B", "exc_type": None, "entrypoint": "Bash", "traceback": "broken pipe"}
    a = _a("BrokenPipeError", "  File \"/x/juggle_x.py\", line 1, in f\nBrokenPipeError")
    assert group_key(a) != group_key(b)


def test_under_aggregation_trade_keeps_small_codes_distinct():
    """REGRESSION (2026-06-21 selfheal v2 p2): the conservative normalizer
    DELIBERATELY under-aggregates — two errors differing only in a small (<=3
    digit) status/exit code stay in DISTINCT groups. We accept more groups
    (wasted triage time) to avoid the ASYMMETRIC over-aggregation danger of
    silently swallowing a new bug (research §5.5: bias to under-normalize)."""
    # Class B: message-driven group_key; small codes are discriminators.
    b404 = {"error_class": "B", "exc_type": None, "entrypoint": "Bash", "traceback": "HTTP 404 not found"}
    b500 = {"error_class": "B", "exc_type": None, "entrypoint": "Bash", "traceback": "HTTP 500 server error"}
    assert group_key(b404) != group_key(b500)
    # but a 4+ digit volatile run (pane id, pid, big number) DOES collapse:
    bp1 = {"error_class": "B", "exc_type": None, "entrypoint": "Bash", "traceback": "worker 12345 crashed"}
    bp2 = {"error_class": "B", "exc_type": None, "entrypoint": "Bash", "traceback": "worker 67890 crashed"}
    assert group_key(bp1) == group_key(bp2)
