"""Juggle project management — CLI commands and background assignment."""
from __future__ import annotations
import json
import logging
import math
import os
import re as _re
import subprocess
import sys
import threading
from pathlib import Path

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))

from juggle_cli_common import _cheap_llm_call, get_db, llm_call

INBOX_PROJECT_ID = "INBOX"
log = logging.getLogger(__name__)

try:
    from rich.console import Console
    from rich.table import Table
    _console = Console()
except ImportError:
    _console = None  # type: ignore


def assign_project_background(
    db,
    thread_uuid: str,
    topic: str,
    _return_thread: bool = False,
) -> threading.Thread | None:
    """Fire-and-forget background project assignment via detached subprocess.

    Uses Popen(start_new_session=True) so create-thread returns instantly.
    _return_thread=True is test-only: falls back to daemon=False thread so
    tests can join() the result.
    """
    # _return_thread=True is test-only path
    if _return_thread:
        def _run():
            try:
                projects = db.get_active_projects()
                project_id, confidence = infer_project_id(topic, projects, db=db)
                if project_id != INBOX_PROJECT_ID:
                    db.update_thread(thread_uuid, project_id=project_id, assigned_by="auto",
                                     assigned_confidence=confidence)
                    log.info("assign_project_background: %s -> %s", thread_uuid[:8], project_id)
                else:
                    db.update_thread(thread_uuid, assigned_confidence=confidence)
            except Exception as e:
                log.warning("assign_project_background: silent failure: %s", e)
        t = threading.Thread(target=_run, daemon=False)
        t.start()
        return t

    # Normal path: detached subprocess, parent returns immediately
    script = (
        "import sys; sys.path.insert(0, {src!r}); "
        "from juggle_db import JuggleDB, DB_PATH; "
        "from juggle_cmd_projects import infer_project_id, INBOX_PROJECT_ID; "
        "db = JuggleDB(str(DB_PATH)); "
        "projects = db.get_active_projects(); "
        "pid, conf = infer_project_id({topic!r}, projects, db=db); "
        "pid != INBOX_PROJECT_ID and db.update_thread({thread_uuid!r}, project_id=pid, assigned_by='auto', assigned_confidence=conf) "
        "or db.update_thread({thread_uuid!r}, assigned_confidence=conf)"
    ).format(src=str(SRC_DIR), topic=topic, thread_uuid=thread_uuid)

    try:
        subprocess.Popen(
            ["python3", "-c", script],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:
        log.warning("assign_project_background: failed to spawn subprocess: %s", e)
    return None


def _extract_json(text: str) -> dict | None:
    """Extract first JSON object from text, stripping markdown fences."""
    text = _re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=_re.MULTILINE)
    text = _re.sub(r'```\s*$', '', text, flags=_re.MULTILINE).strip()
    m = _re.search(r'\{[^}]+\}', text)
    if m:
        try:
            return json.loads(m.group())
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _build_classifier_prompt(
    topic: str,
    projects: list[dict],
    positives_by_project: dict[str, list[dict]],
    corrections: list[dict],
) -> str:
    """Pure function: build the LLM classification prompt from structured inputs."""
    project_parts = []
    for p in projects:
        part = f'{p["id"]}: {p["name"]} — {p["objective"]}'
        mp = (p.get("match_profile") or "").strip()
        if mp:
            part += f' | profile: {mp}'
        examples = [t["topic"] for t in positives_by_project.get(p["id"], []) if t.get("topic")]
        if examples:
            part += f' | confirmed: {"; ".join(examples)}'
        project_parts.append(part)

    prompt = f'Topic: "{topic}". Projects: [{"; ".join(project_parts)}]. '
    if corrections:
        correction_parts = [
            f'"{c["topic"]}" -> {c["to_project"]}' for c in corrections
        ]
        prompt += f'Past corrections: [{"; ".join(correction_parts)}]. '
    prompt += (
        'Which project fits best? '
        'Return ONLY valid JSON: {"project_id": "<id_or_INBOX>", "confidence": <0.0-1.0>}'
    )
    return prompt


_SYNTH_MAX_HUMAN = 20
_SYNTH_MAX_AUTO = 10


def build_match_profile_prompt(
    project: dict,
    threads: list[dict],
    corrections: list[dict],
) -> str:
    """Pure function: build synthesis prompt for one project's match_profile.

    Weights human-assigned threads highest; auto-assigned weakly included to
    avoid feedback-loop reinforcement. Bounded to prevent token overrun.
    """
    human_topics = [t["topic"] for t in threads if t.get("assigned_by") == "human"]
    auto_topics = [t["topic"] for t in threads if t.get("assigned_by") != "human"]
    human_sample = human_topics[:_SYNTH_MAX_HUMAN]
    auto_sample = auto_topics[:_SYNTH_MAX_AUTO]

    correction_lines = [
        f'  - "{c["topic"]}" was moved OUT (to {c["to_project"]})'
        for c in (corrections or [])[:5]
    ]

    confirmed_section = "\n".join(f"  - {t}" for t in human_sample) or "  (none yet)"
    auto_section = "\n".join(f"  - {t}" for t in auto_sample) or "  (none yet)"
    correction_section = "\n".join(correction_lines) or "  (none)"

    return (
        f"Synthesize a match_profile for the project below.\n\n"
        f"Project: {project['name']} (id={project['id']})\n"
        f"Objective: {project['objective']}\n\n"
        f"Human-confirmed thread topics (trust these most):\n{confirmed_section}\n\n"
        f"Auto-assigned thread topics (use lightly):\n{auto_section}\n\n"
        f"Topics recently moved OUT of this project:\n{correction_section}\n\n"
        f"Write a match_profile with exactly three lines:\n"
        f"1. A compact 1-2 sentence description of what belongs in this project.\n"
        f"2. KEYWORDS: <5-10 comma-separated signal words>\n"
        f"3. NOT: <5-10 comma-separated words for sibling projects that should NOT match>\n\n"
        f"Output only those three lines. No preamble."
    )


def _assign_thread_to_project(
    db, thread_uuid: str, project_id: str, assigned_by: str = "human"
) -> None:
    """Assign a thread and mark the old project dirty if the project changed."""
    t = db.get_thread(thread_uuid)
    if not t:
        return
    old_project = t.get("project_id", INBOX_PROJECT_ID)
    db.update_thread(thread_uuid, project_id=project_id, assigned_by=assigned_by)
    if old_project != project_id and old_project != INBOX_PROJECT_ID:
        db.mark_project_dirty(old_project)
        threading.Thread(
            target=check_and_resynth_if_drifted,
            args=(db, old_project),
            daemon=True,
        ).start()


def synth_project(db, project_id: str, force: bool = False) -> str | None:
    """Synthesize match_profile for one project. Returns new profile or None if skipped.

    Skips if no threads exist and force=False (nothing to learn from).
    """
    project = db.get_project(project_id)
    if not project:
        return None
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT topic, assigned_by FROM threads "
            "WHERE project_id=? AND show_in_list=1 "
            "ORDER BY CASE assigned_by WHEN 'human' THEN 0 ELSE 1 END, last_active DESC",
            (project_id,),
        ).fetchall()
    threads = [dict(r) for r in rows]
    if not threads and not force:
        log.info("synth_project: skipping %s — no threads", project_id)
        return None
    corrections = db.get_recent_corrections(limit=10)
    prompt = build_match_profile_prompt(project, threads, corrections)
    result = llm_call(prompt, profile="cheap", timeout=20)
    if not result:
        log.warning("synth_project: LLM returned None for %s", project_id)
        return None
    db.set_match_profile(project_id, result.strip())
    log.info("synth_project: synthesized profile for %s", project_id)
    threading.Thread(
        target=resweep_inbox,
        args=(db,),
        kwargs={"limit": _RESWEEP_DEFAULT_LIMIT},
        daemon=True,
    ).start()
    return result.strip()


# ---------------------------------------------------------------------------
# Phase 3: BoW drift detector
# ---------------------------------------------------------------------------

def drift_score(centroid: list[float], target: list[float]) -> float:
    """Cosine distance in [0, 1]: 0 = identical direction, 1 = orthogonal."""
    dot = sum(a * b for a, b in zip(centroid, target))
    mag_a = math.sqrt(sum(a * a for a in centroid))
    mag_b = math.sqrt(sum(b * b for b in target))
    if mag_a == 0.0 or mag_b == 0.0:
        return 1.0
    cosine_sim = dot / (mag_a * mag_b)
    return 1.0 - max(-1.0, min(1.0, cosine_sim))


def _build_vocab(all_topics: list[str]) -> dict[str, int]:
    """Build word → index vocabulary from all topics (words > 2 chars, deduped)."""
    words: list[str] = []
    seen: set[str] = set()
    for topic in all_topics:
        for word in topic.lower().split():
            if word not in seen and len(word) > 2:
                seen.add(word)
                words.append(word)
    return {w: i for i, w in enumerate(words)}


def _topics_to_bow_vector(topics: list[str], vocab: dict[str, int]) -> list[float]:
    """Term-frequency BoW vector over a shared vocabulary."""
    vec = [0.0] * len(vocab)
    for topic in topics:
        for word in topic.lower().split():
            if word in vocab:
                vec[vocab[word]] += 1.0
    total = sum(vec)
    return [x / total for x in vec] if total > 0 else vec


_DRIFT_DEFAULT_THRESHOLD = 0.45


def check_and_resynth_if_drifted(
    db, project_id: str, threshold: float = _DRIFT_DEFAULT_THRESHOLD
) -> None:
    """Compute BoW centroid drift for project_id. Silent re-synth if above threshold."""
    project = db.get_project(project_id)
    if not project:
        return
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT topic FROM threads WHERE project_id=? AND show_in_list=1 "
            "ORDER BY last_active DESC LIMIT 50",
            (project_id,),
        ).fetchall()
    topics = [r["topic"] for r in rows]
    if len(topics) < 3:
        return
    profile_text = (project.get("match_profile") or "").strip()
    if not profile_text:
        return
    profile_words = profile_text.split()
    vocab = _build_vocab(topics + [profile_text])
    thread_centroid = _topics_to_bow_vector(topics, vocab)
    profile_vec = _topics_to_bow_vector(profile_words, vocab)
    score = drift_score(thread_centroid, profile_vec)
    log.info("check_and_resynth_if_drifted: %s drift=%.3f threshold=%.3f", project_id, score, threshold)
    if score > threshold:
        log.info("check_and_resynth_if_drifted: drift detected, re-synth %s", project_id)
        synth_project(db, project_id)


# ---------------------------------------------------------------------------
# Phase 4: INBOX re-sweep
# ---------------------------------------------------------------------------

_RESWEEP_DEFAULT_LIMIT = 50


def resweep_inbox(db, limit: int = _RESWEEP_DEFAULT_LIMIT) -> int:
    """Re-run project matching on INBOX threads. Returns count reclassified.

    Rate-limited to `limit` threads per call. Called automatically after profile update.
    """
    projects = db.get_active_projects()
    if not projects:
        return 0
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, topic FROM threads "
            "WHERE project_id='INBOX' AND show_in_list=1 "
            "ORDER BY COALESCE(assigned_confidence, 0.0) ASC, last_active DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    reclassified = 0
    for row in rows:
        pid, confidence = infer_project_id(row["topic"], projects, db=db)
        if pid != INBOX_PROJECT_ID:
            db.update_thread(row["id"], project_id=pid, assigned_by="auto",
                             assigned_confidence=confidence)
            reclassified += 1
            log.info("resweep_inbox: %s -> %s (conf=%.2f)", row["id"][:8], pid, confidence)
    return reclassified


_CONFIDENCE_THRESHOLD = 0.6


def infer_project_id(
    topic: str,
    projects: list[dict],
    db=None,
    confidence_threshold: float = _CONFIDENCE_THRESHOLD,
) -> tuple[str, float]:
    """Returns (project_id, confidence). Falls back to (INBOX, 0.0) on any failure.

    Confidence < threshold -> INBOX regardless of project returned.
    """
    if not projects:
        return INBOX_PROJECT_ID, 0.0
    valid_ids = {p["id"] for p in projects} | {INBOX_PROJECT_ID}

    positives_by_project: dict[str, list[dict]] = {}
    corrections: list[dict] = []
    if db:
        try:
            for p in projects:
                positives_by_project[p["id"]] = db.get_human_assigned_threads_by_project(p["id"], limit=5)
        except Exception:
            pass
        try:
            corrections = db.get_recent_corrections(limit=5)
        except Exception:
            pass

    prompt = _build_classifier_prompt(topic, projects, positives_by_project, corrections)
    raw = llm_call(prompt, profile="cheap", timeout=15)
    if not raw:
        return INBOX_PROJECT_ID, 0.0
    parsed = _extract_json(raw)
    pid = (parsed or {}).get("project_id", INBOX_PROJECT_ID)
    confidence = float((parsed or {}).get("confidence", 0.5))
    if pid not in valid_ids:
        log.warning("infer_project_id: invalid project_id %r in response: %r", pid, raw)
        return INBOX_PROJECT_ID, 0.0
    if confidence < confidence_threshold and pid != INBOX_PROJECT_ID:
        log.info("infer_project_id: low confidence %.2f for %r -> INBOX", confidence, pid)
        return INBOX_PROJECT_ID, confidence
    return pid, confidence


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------

def _resolve_project(db, query: str, include_closed: bool = False) -> dict | None:
    """Find a project by exact id or name substring. Returns None if not found."""
    projects = db.list_projects_with_state() if include_closed else db.list_projects()
    # exact id match first
    for p in projects:
        if p["id"].lower() == query.lower():
            return p
    # substring match on name
    ql = query.lower()
    for p in projects:
        if ql in p["name"].lower():
            return p
    return None


def cmd_project_list(args):
    db = get_db(init=True)
    projects = db.list_projects_with_state()
    if _console:
        table = Table(title="Projects (all)")
        table.add_column("ID", style="bold cyan")
        table.add_column("Name")
        table.add_column("Status")
        table.add_column("Last Active")
        table.add_column("Threads", justify="right")
        table.add_column("Summary")
        for p in sorted(projects, key=lambda x: (x["id"] == "INBOX", x.get("status") == "closed", x["id"])):
            summary_line = (p.get("summary") or "")[:60]
            last = (p.get("last_active") or "")[:16]
            style = "dim" if p.get("status") == "closed" else ""
            table.add_row(
                p["id"], p["name"], p["status"], last,
                str(p.get("thread_count", 0)), summary_line,
                style=style,
            )
        _console.print(table)
    else:
        for p in sorted(projects, key=lambda x: (x["id"] == "INBOX", x.get("status") == "closed", x["id"])):
            summary_line = (p.get("summary") or "")[:60]
            last = (p.get("last_active") or "")[:16]
            closed_tag = " [closed]" if p.get("status") == "closed" else ""
            print(
                f"{p['id']:<8} {p['name']:<25} {p['status']:<8} {last:<16} "
                f"{p.get('thread_count', 0):>3} threads  {summary_line}{closed_tag}"
            )


def cmd_project_close(args):
    db = get_db(init=True)
    query = " ".join(args.project_id) if isinstance(args.project_id, list) else args.project_id
    p = _resolve_project(db, query, include_closed=False)
    if not p:
        print(f"Project not found (active): {query}")
        sys.exit(1)
    if p["id"] == INBOX_PROJECT_ID:
        print("Cannot close the INBOX project.")
        sys.exit(1)

    print(f"Summarizing project {p['id']} ({p['name']})…")
    from juggle_project_summary import summarize_project
    from juggle_settings import get_settings
    sonnet = get_settings().get("title_gen", {}).get("sonnet_model", "claude-sonnet-4-6")

    def _llm(prompt: str) -> str:
        try:
            res = subprocess.run(
                ["claude", "-p", prompt, "--model", sonnet],
                capture_output=True, text=True, timeout=120,
            )
            return res.stdout.strip() if res.returncode == 0 else ""
        except Exception:
            return ""

    proj_summary, thread_summaries = summarize_project(db, p["id"], llm_fn=_llm)
    db.close_project(p["id"], proj_summary, thread_summaries)

    print(f"\nProject {p['id']} ({p['name']}) closed.")
    if proj_summary:
        print(f"Summary: {proj_summary}")
    print(f"\nRestore with: project open {p['id']}")


def cmd_project_open(args):
    db = get_db(init=True)
    query = " ".join(args.project_id) if isinstance(args.project_id, list) else args.project_id
    p = _resolve_project(db, query, include_closed=True)
    if not p:
        print(f"Project not found: {query}")
        sys.exit(1)
    if p.get("status") != "closed":
        print(f"Project {p['id']} is not closed (status: {p['status']}).")
        sys.exit(1)

    db.open_project(p["id"])
    threads = db.get_threads_by_project(p["id"])
    print(f"Project {p['id']} ({p['name']}) restored.")
    if p.get("summary"):
        print(f"Summary: {p['summary']}")
    if threads:
        print(f"\nRestored topics ({len(threads)}):")
        for t in threads:
            print(f"  [{t['user_label']}] {t.get('title') or t['topic']}")


def cmd_project_show(args):
    db = get_db(init=True)
    p = db.get_project(args.project_id)
    if not p:
        print(f"Project not found: {args.project_id}")
        sys.exit(1)
    criteria = json.loads(p.get("success_criteria") or "[]")
    if _console:
        _console.print(f"[bold cyan]{p['id']}[/bold cyan]  {p['name']}")
        _console.print(f"[dim]Status:[/dim]    {p['status']}")
        _console.print(f"[dim]Objective:[/dim] {p['objective']}")
        if criteria:
            _console.print("[dim]Success criteria:[/dim]")
            for c in criteria:
                _console.print(f"  - [ ] {c}")
        if p.get("out_of_scope"):
            _console.print(f"[dim]Out of scope:[/dim] {p['out_of_scope']}")
    else:
        print(f"{p['id']}  {p['name']}")
        print(f"Status:    {p['status']}")
        print(f"Objective: {p['objective']}")
        if criteria:
            print("Success criteria:")
            for c in criteria:
                print(f"  - [ ] {c}")
        if p.get("out_of_scope"):
            print(f"Out of scope: {p['out_of_scope']}")
    threads = db.get_threads_by_project(p["id"])
    if threads:
        print(f"\nThreads ({len(threads)}):")
        for t in threads:
            print(f"  [{t['user_label']}] {t['status']}  {t.get('title') or t['topic']}")


def cmd_project_assign(args):
    db = get_db(init=True)
    # Unpack: if thread_id is a list with >1 entries, last is project_id
    if isinstance(args.thread_id, list) and len(args.thread_id) > 1:
        project_id = args.thread_id[-1]
        thread_ids = args.thread_id[:-1]
    else:
        project_id = args.project_id
        thread_ids = args.thread_id if isinstance(args.thread_id, list) else [args.thread_id]

    p = db.get_project(project_id)
    if not p:
        print(f"Project not found: {project_id}")
        sys.exit(1)

    for tid_input in thread_ids:
        t = db.get_thread_by_user_label(tid_input)
        if not t:
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM threads WHERE user_label=? OR id=?",
                    (tid_input.upper(), tid_input),
                ).fetchone()
            t = dict(row) if row else None
        if not t:
            print(f"Thread not found: {tid_input}")
            continue
        from_project = t.get("project_id", "INBOX")
        _assign_thread_to_project(db, t["id"], project_id, assigned_by="human")
        if from_project != project_id:
            db.log_project_correction(t["topic"], from_project=from_project, to_project=project_id)
        print(f"Thread [{tid_input}] -> project {project_id} ({p['name']})")


def cmd_project_edit(args):
    db = get_db(init=True)
    if not db.get_project(args.project_id):
        print(f"Project not found: {args.project_id}")
        sys.exit(1)

    has_criterion = bool(getattr(args, "success_criterion", None))
    has_json = getattr(args, "success_criteria_json", None) is not None
    has_clear = getattr(args, "clear_success_criteria", False)

    if has_criterion and has_json:
        print("Error: --success-criterion and --success-criteria-json are mutually exclusive.")
        sys.exit(1)

    updates = {}
    if args.name:
        updates["name"] = args.name
    if args.objective:
        updates["objective"] = args.objective
    if args.out_of_scope is not None:
        updates["out_of_scope"] = args.out_of_scope

    if has_clear:
        updates["success_criteria"] = "[]"
    elif has_criterion:
        updates["success_criteria"] = json.dumps(args.success_criterion)
    elif has_json:
        try:
            parsed = json.loads(args.success_criteria_json)
        except json.JSONDecodeError:
            print("Error: --success-criteria-json is not valid JSON.")
            sys.exit(1)
        if not isinstance(parsed, list):
            print("Error: --success-criteria-json must be a JSON array.")
            sys.exit(1)
        if not all(isinstance(c, str) for c in parsed):
            print("Error: --success-criteria-json items must all be strings.")
            sys.exit(1)
        updates["success_criteria"] = json.dumps(parsed)

    if not updates:
        print("Nothing to update. Use --name, --objective, --out-of-scope, --success-criterion, --success-criteria-json, or --clear-success-criteria.")
        sys.exit(1)
    db.update_project(args.project_id, **updates)
    print(f"Project {args.project_id} updated.")


def cmd_project_synth(args):
    db = get_db(init=True)
    if getattr(args, "all", False):
        projects = db.get_active_projects()
    elif getattr(args, "dirty", False):
        projects = db.get_dirty_projects()
    else:
        p = db.get_project(args.project_id)
        if not p:
            print(f"Project not found: {args.project_id}")
            sys.exit(1)
        projects = [p]

    if not projects:
        print("No projects to synthesize.")
        return

    for p in projects:
        pid = p["id"]
        print(f"Synthesizing {pid} ({p['name']})...", end=" ", flush=True)
        result = synth_project(db, pid, force=getattr(args, "all", False))
        if result:
            preview = result.split("\n")[0][:80]
            print(f"done. Profile: {preview}")
        else:
            print("skipped (no threads).")


def cmd_project_create(args):
    db = get_db(init=True)
    if args.force:
        if not args.name or not args.objective:
            print("--force requires --name and --objective")
            sys.exit(1)
        criteria = json.loads(args.success_criteria) if args.success_criteria else []
        pid = db.create_project(
            name=args.name, objective=args.objective,
            success_criteria=json.dumps(criteria), out_of_scope=args.out_of_scope or "",
        )
        print(f"Created project {pid}: {args.name}")
        return
    _run_project_coach(db)


def cmd_project_critique(args):
    db = get_db(init=True)
    if args.project_id == INBOX_PROJECT_ID:
        print("INBOX cannot be critiqued.")
        sys.exit(1)
    if not db.get_project(args.project_id):
        print(f"Project not found: {args.project_id}")
        sys.exit(1)
    _run_project_coach(db)


def _run_project_coach(db) -> None:
    """Multi-turn Sonnet coach wizard. Guides user to a well-defined project definition."""
    existing = db.get_active_projects()
    existing_summary = "; ".join(f'{p["id"]}: {p["name"]}' for p in existing) or "none"
    system = (
        "You are a project definition coach. Help the user define a clear, achievable project.\n"
        f"Existing projects: {existing_summary}\n\n"
        "Your job:\n"
        "1. Ask targeted questions (max 3 total) to understand what done looks like\n"
        "2. Flag if the idea sounds like multiple projects\n"
        "3. Propose a sharpened definition with objective + 2-3 measurable success criteria\n"
        "4. Ask about out-of-scope only if boundaries seem ambiguous\n\n"
        "When ready, output ONLY this JSON (no other text):\n"
        '{"ready": true, "name": "...", "objective": "...", "success_criteria": ["..."], "out_of_scope": "..."}\n\n'
        "Until ready, output ONLY your next question."
    )
    conversation = [{"role": "system", "content": system}]
    print("\nWhat's your project? (can be vague — I'll help you sharpen it)\n")
    user_input = input("> ").strip()
    if not user_input:
        print("Cancelled.")
        return
    conversation.append({"role": "user", "content": user_input})
    from juggle_settings import get_settings
    sonnet = get_settings().get("title_gen", {}).get("sonnet_model", "claude-sonnet-4-6")
    for _ in range(7):
        prompt = "\n".join(
            f'{"User" if m["role"]=="user" else ("System" if m["role"]=="system" else "Coach")}: {m["content"]}'
            for m in conversation
        ) + "\nCoach:"
        try:
            res = subprocess.run(["claude", "-p", prompt, "--model", sonnet],
                                 capture_output=True, text=True, timeout=30)
            response = res.stdout.strip() if res.returncode == 0 else None
        except Exception:
            response = None
        if not response:
            print("Coach unavailable. Use --force to skip the wizard.")
            return
        try:
            start = response.find("{")
            if start != -1:
                data = json.loads(response[start:])
                if data.get("ready"):
                    _confirm_and_save(db, data)
                    return
        except json.JSONDecodeError:
            pass
        print(f"\n{response}\n")
        conversation.append({"role": "assistant", "content": response})
        answer = input("> ").strip()
        if not answer:
            print("Cancelled.")
            return
        conversation.append({"role": "user", "content": answer})
    print("Could not converge. Use --force to skip the wizard.")


def _confirm_and_save(db, data: dict) -> None:
    print("\n── Draft Project ──────────────────────")
    print(f"Name:      {data['name']}")
    print(f"Objective: {data['objective']}")
    print("Success criteria:")
    for c in data.get("success_criteria", []):
        print(f"  - [ ] {c}")
    if data.get("out_of_scope"):
        print(f"Out of scope: {data['out_of_scope']}")
    print("───────────────────────────────────────")
    answer = input("\nApprove? [Y/n/edit] ").strip().lower()
    if answer in ("", "y", "yes"):
        pid = db.create_project(
            name=data["name"], objective=data["objective"],
            success_criteria=json.dumps(data.get("success_criteria", [])),
            out_of_scope=data.get("out_of_scope", ""),
        )
        print(f"\nCreated project {pid}: {data['name']}")
    elif answer == "edit":
        print("Press Enter to keep current value.")
        data["name"] = input(f"Name [{data['name']}]: ").strip() or data["name"]
        data["objective"] = input(f"Objective [{data['objective']}]: ").strip() or data["objective"]
        _confirm_and_save(db, data)
    else:
        print("Cancelled.")
