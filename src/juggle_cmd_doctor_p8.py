"""Juggle CLI — `doctor --pre-p8-check` runner (Gate A + Gate B readiness report).

Extracted from juggle_cmd_doctor to keep that module under the LOC gate. This is
the CLI-side adapter: it resolves the live DB + shipped src/ dir, calls the pure
gates in dbops.p8_readiness, and renders JSON or human text. Read-only; runs in
BOTH dry & non-dry doctor modes. Exit code mirrors rep["pass"].
"""
from __future__ import annotations

import json as _json
import sqlite3
from pathlib import Path


def run_pre_p8_check(json_out: bool) -> int:
    """Gate A (static legacy-ref scan) + Gate B (nodes mirror readiness) report.

    Scans the SHIPPED package dir (this file's parent = ``src/``) so the report
    proves the *running binary* is clean, not just a dev checkout. Returns 0 iff
    both gates clear (or legacy already dropped), else 1.
    """
    from juggle_db import DB_PATH

    src_root = Path(__file__).resolve().parent          # src/
    if not Path(DB_PATH).exists():
        rep = {"static": None,
               "runtime": {"ready": False, "already_dropped": False,
                           "reasons": ["db-missing"]},
               "pass": False}
    else:
        from dbops.p8_readiness import pre_p8_report
        conn = sqlite3.connect(str(DB_PATH))
        try:
            rep = pre_p8_report(conn, src_root)
        finally:
            conn.close()

    if json_out:
        print(_json.dumps(rep))
    else:
        s = rep["static"]
        if s is None:
            static_line = "FAIL:?"
        elif s["fail"] == 0:
            static_line = "PASS:0"
        else:
            static_line = f"FAIL:{s['fail']}"
        print(f"pre-p8 STATIC: {static_line}")
        rt = rep["runtime"]
        if rt["already_dropped"]:
            print("pre-p8 RUNTIME: ALREADY-DROPPED")
        elif rt["ready"]:
            print("pre-p8 RUNTIME: READY")
        else:
            print(f"pre-p8 RUNTIME: BLOCKED — {', '.join(rt['reasons'])}")
    return 0 if rep["pass"] else 1
