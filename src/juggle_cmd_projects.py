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
                    db.update_thread(thread_uuid, project_id=project_id)
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
        "pid != INBOX_PROJECT_ID and db.update_thread({thread_uuid!r}, project_id=pid)"
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


def infer_project_id(topic: str, projects: list[dict], db=None) -> str:
    """Returns best project_id or INBOX. db is optional; when provided, adds few-shot thread examples."""
    if not projects:
        return INBOX_PROJECT_ID
    valid_ids = {p["id"] for p in projects} | {INBOX_PROJECT_ID}

    project_parts = []
    for p in projects:
        part = f'{p["id"]}: {p["name"]} — {p["objective"]}'
        if db:
            try:
                existing = db.get_threads_by_project(p["id"])
                topics = [t["topic"] for t in existing if t.get("topic")][:5]
                if topics:
                    part += f' | examples: {"; ".join(topics)}'
            except Exception:
                pass
        project_parts.append(part)

    project_list = "; ".join(project_parts)
    prompt = (
        f'Topic: "{topic}". '
        f'Projects: [{project_list}]. '
        f'Which project fits best? '
        f'Return ONLY valid JSON with no explanation, no markdown fences: {{"project_id": "<id_or_INBOX>"}}'
    )
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

def cmd_project_list(args):
    db = get_db(init=True)
    projects = db.list_projects()
    if _console:
        table = Table(title="Projects")
        table.add_column("ID", style="bold cyan")
        table.add_column("Name")
        table.add_column("Status")
        table.add_column("Threads", justify="right")
        for p in sorted(projects, key=lambda x: (x["id"] == "INBOX", x["id"])):
            count = db.count_threads_by_project(p["id"])
            table.add_row(p["id"], p["name"], p["status"], str(count))
        _console.print(table)
    else:
        for p in sorted(projects, key=lambda x: (x["id"] == "INBOX", x["id"])):
            count = db.count_threads_by_project(p["id"])
            print(f"{p['id']:<8} {p['name']:<30} {p['status']:<10} {count} threads")


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
    db.update_thread(t["id"], project_id=args.project_id)
    print(f"Thread [{args.thread_id}] -> project {args.project_id} ({p['name']})")


def cmd_project_edit(args):
    db = get_db(init=True)
    if not db.get_project(args.project_id):
        print(f"Project not found: {args.project_id}")
        sys.exit(1)
    updates = {}
    if args.name:
        updates["name"] = args.name
    if args.objective:
        updates["objective"] = args.objective
    if args.out_of_scope is not None:
        updates["out_of_scope"] = args.out_of_scope
    if not updates:
        print("Nothing to update. Use --name, --objective, or --out-of-scope.")
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
