"""juggle_cmd_loop_create — transactional single-topic loop creation (loop-entity
V1, Phase 4).

``create_loop_atomic`` is the ATOMIC create (critique §Axis-5): allocating the
L-id, creating the ``kind='loop'`` project, loading the validated single-topic
template graph, and inserting the loop row with ``next_run`` all happen inside ONE
DB transaction on ONE connection. Any step raising rolls the WHOLE thing back
(``conn.rollback()``), so a half-created loop can never leave a ``kind='loop'``
project claiming a P-slot, nor an orphan loop/graph row.

Why a single-transaction rollback (not project-close-as-abort): ``close_project``
only flips ``status='closed'`` — it leaves the projects ROW behind. The atomicity
contract here is ZERO orphan rows on failure (no project, no loop, no nodes), which
only a real transactional rollback delivers. The low-level node writers
(``db_topics.create_topic`` / ``db_graph.create_task`` / ``set_task_topic`` /
``replace_edges``) all honour a caller-passed ``conn`` and do NOT commit (``_cx``),
so threading one connection through them makes the create truly all-or-nothing.

Stable-topic model (V2 §0b): the loop gets ONE long-lived topic with a STABLE id
``<L#>-<topic>`` (no run prefix), created ONCE here; each fire attaches a new
run-seq namespaced TASK generation ``<L#>-r<seq>-<task>`` under it (the r-prefix on
the TASK ids is the collision guard vs the guarded-upsert refusal on re-fire).

The OS-schedule path is unchanged — it reuses ``juggle_scheduler`` and does NOT
route through here. The unified ``schedule:create`` router (``commands/schedule/
create.md``) chooses OS-schedule XOR loop, and only the loop branch calls this.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone

from dbops.schema import _now
from juggle_loop_instantiate import create_stable_topic, instantiate_generation
from juggle_loop_template_validator import LoopTemplateError, validate_loop_template

_CADENCE_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_CADENCE_RE = re.compile(
    r"(?:every\s+)?(\d+)\s*(s|m|h|d|sec|second|seconds|min|minute|minutes|"
    r"hour|hours|day|days)\b",
    re.IGNORECASE,
)
_DAILY_RE = re.compile(r"daily\s+at\s+(\d{1,2}):(\d{2})", re.IGNORECASE)


def compute_next_run(cadence: str, now: datetime | None = None) -> str:
    """Derive the first ``next_run`` ISO timestamp from a cadence string.

    Supports ``every Nm|Nh|Ns|Nd`` (and the spelled-out units) and ``daily at
    HH:MM``. Fail-loud on an unparseable cadence — a loop with no schedulable
    next_run would silently never fire (critique §Axis-6: next_run is the timer).

    Defaults to UTC-aware ``datetime.now(timezone.utc)`` so the emitted ISO string
    is byte-comparable with ``dbops.schema._now()`` — Phase 5's fire check compares
    ``next_run`` against ``_now()``, and a naive/local timestamp here would make
    that comparison wrong (tz offset + missing ``+00:00`` suffix)."""
    now = now or datetime.now(timezone.utc)
    m = _DAILY_RE.search(cadence or "")
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            # The regex accepts \d{1,2}:\d{2} (e.g. 30:70); reject out-of-range as a
            # LoopTemplateError so cmd_loop_create fails loud instead of a bare
            # datetime.replace ValueError tracebacking past its except handler.
            raise LoopTemplateError(
                f"invalid time in cadence {cadence!r}: {hh:02d}:{mm:02d}"
            )
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand = cand + timedelta(days=1)
        return cand.isoformat()
    m = _CADENCE_RE.search(cadence or "")
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()[0]  # s/m/h/d — first letter disambiguates
        return (now + timedelta(seconds=n * _CADENCE_UNIT_SECONDS[unit])).isoformat()
    raise LoopTemplateError(
        f"unparseable cadence {cadence!r} — expected 'every Nm|Nh|Ns|Nd' or "
        f"'daily at HH:MM'"
    )


def create_loop_atomic(db, *, template, cadence, name=None, objective="",
                       now=None, max_consecutive_failures=3, session_id=None):
    """Validate + atomically create a single-topic loop. Returns a dict with
    ``loop_id``/``project_id``/``topic_id``/``node_ids``/``next_run``/``thread_id``.

    The template is validated (partition rule, V1 single-topic) BEFORE any write.
    All DB writes run on ONE connection with no intermediate commit; any exception
    rolls back the whole create (ZERO orphan rows).

    The loop is bound to its OWN thread (``loops.thread_id``) so a failing iteration's
    HIGH action item and any loop-scoped notifications land on that named thread —
    surfaced in the cockpit ACTION PANE on the loop — instead of the null-thread
    ORPHAN bucket. The thread is created via the shared ``create_thread`` primitive
    on THIS connection, so it commits/rolls back atomically with the loop."""
    norm = validate_loop_template(template)  # raises LoopTemplateError pre-write
    topic = norm["topics"][0]
    next_run = compute_next_run(cadence, now)

    conn = db._connect()
    try:
        ts = _now()
        used_p = {r[0] for r in conn.execute("SELECT id FROM projects").fetchall()}
        project_id = db._next_project_label(used_p)
        used_l = {r[0] for r in conn.execute("SELECT id FROM loops").fetchall()}
        loop_id = db._next_loop_label(used_l)
        run_seq = 0
        # Stable-topic model (§0b): the loop gets ONE long-lived topic (a STABLE id,
        # no run prefix) created here, ONCE — not a fresh topic per fire. Each fire
        # attaches a new run-namespaced TASK generation under it.
        topic_id = f"{loop_id}-{topic['id']}"
        gen_prefix = f"{loop_id}-r{run_seq}-"

        conn.execute(
            "INSERT INTO projects (id,name,objective,success_criteria,out_of_scope,"
            "status,kind,created_at,last_active) VALUES (?,?,?,?,?,'active','loop',?,?)",
            (project_id, name or f"loop {loop_id}", objective, "[]", "", ts, ts),
        )

        # Create the stable topic, then materialize the r0 task generation under it
        # via the shared writer (juggle_loop_instantiate) — one source of truth for
        # how a generation's nodes/edges are laid down, reused by the fire re-fire.
        # Only role + delivery are persisted (no nodes.model column yet — V2 §2).
        create_stable_topic(db, conn, project_id=project_id, topic_id=topic_id, topic=topic)
        node_ids = [topic_id] + instantiate_generation(
            db, conn, project_id=project_id, topic_id=topic_id,
            gen_prefix=gen_prefix, tasks=topic["tasks"],
        )

        # The loop's OWN thread — failure action items + loop-scoped notifications
        # attach here (not the null-thread ORPHAN bucket). Reuses the shared
        # create_thread primitive on THIS conn (conn=) so the thread commits/rolls
        # back atomically with the project + graph + loop row.
        thread_id = db.create_thread(
            name or f"loop {loop_id}",
            session_id or f"loop:{loop_id}",
            project_id=project_id,
            conn=conn,
        )

        conn.execute(
            "INSERT INTO loops (id, project_id, thread_id, cadence, status, run_seq, "
            "next_run, last_run_at, consecutive_failures, max_consecutive_failures, "
            "created_at, updated_at) VALUES (?,?,?,?,'active',?,?,NULL,0,?,?,?)",
            (loop_id, project_id, thread_id, cadence, run_seq, next_run,
             max_consecutive_failures, ts, ts),
        )
        conn.commit()
    except Exception:
        conn.rollback()  # ZERO orphan rows — no project, no loop, no nodes
        raise
    finally:
        conn.close()

    return {
        "loop_id": loop_id, "project_id": project_id, "topic_id": topic_id,
        "node_ids": node_ids, "next_run": next_run, "thread_id": thread_id,
    }


def cmd_loop_create(args):
    """CLI: ``juggle loop create --template <file.json> --cadence '<cadence>'``.

    The unified schedule:create router builds the validated single-topic template
    JSON, then calls this. Prints the created loop/project/topic ids."""
    import juggle_cli_common as _common

    db = _common.get_db()
    try:
        template = json.loads(open(args.template, encoding="utf-8").read())
    except (OSError, ValueError) as e:
        print(f"Error: could not read template {args.template!r}: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        result = create_loop_atomic(
            db, template=template, cadence=args.cadence,
            name=getattr(args, "name", None), objective=getattr(args, "objective", "") or "",
        )
    except LoopTemplateError as e:
        print(f"Error: invalid loop template — {e}", file=sys.stderr)
        sys.exit(1)
    print(
        f"Loop {result['loop_id']} created (project {result['project_id']}, topic "
        f"{result['topic_id']}, next_run {result['next_run']})."
    )
