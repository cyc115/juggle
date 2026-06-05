"""Juggle project management — CLI commands and background assignment."""
from __future__ import annotations
import json
import logging
import os
import re as _re
import subprocess
import sys
import threading
from pathlib import Path

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))

from juggle_cli_common import _cheap_llm_call, get_db

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
                project_id = infer_project_id(topic, projects, db=db)
                if project_id != INBOX_PROJECT_ID:
                    db.update_thread(thread_uuid, project_id=project_id, assigned_by="auto")
                    log.info("assign_project_background: %s -> %s", thread_uuid[:8], project_id)
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
        "pid = infer_project_id({topic!r}, projects, db=db); "
        "pid != INBOX_PROJECT_ID and db.update_thread({thread_uuid!r}, project_id=pid, assigned_by='auto')"
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


def infer_project_id(topic: str, projects: list[dict], db=None) -> str:
    """Returns best project_id or INBOX. db is optional; when provided, adds few-shot examples + corrections."""
    if not projects:
        return INBOX_PROJECT_ID
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
    raw = _cheap_llm_call(prompt, timeout=15)
    if not raw:
        return INBOX_PROJECT_ID
    parsed = _extract_json(raw)
    pid = (parsed or {}).get("project_id", INBOX_PROJECT_ID)
    if pid not in valid_ids:
        log.warning("infer_project_id: invalid project_id %r in response: %r", pid, raw)
        return INBOX_PROJECT_ID
    return pid


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
    t = db.get_thread_by_user_label(args.thread_id)
    if not t:
        print(f"Thread not found: {args.thread_id}")
        sys.exit(1)
    p = db.get_project(args.project_id)
    if not p:
        print(f"Project not found: {args.project_id}")
        sys.exit(1)
    from_project = t.get("project_id", "INBOX")
    db.update_thread(t["id"], project_id=args.project_id, assigned_by="human")
    if from_project != args.project_id:
        db.log_project_correction(t["topic"], from_project=from_project, to_project=args.project_id)
    print(f"Thread [{args.thread_id}] -> project {args.project_id} ({p['name']})")


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
