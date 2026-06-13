"""
juggle_cmd_agents_tasks — Task dispatch to pooled agents.

Owns: cmd_send_task (worktree guard + template + interactive/one-shot dispatch)
      and cmd_send_message.
Must not own: pool lifecycle, completion/failure handlers, worktree helpers.

Shared symbols are accessed through juggle_cmd_agents_common (_com) at call
time so test monkeypatches on _com.<symbol> take effect.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import juggle_cmd_agents_common as _com


def cmd_send_task(args):
    _db_path = getattr(args, "db_path", None)
    db = _com.get_db(db_path=_db_path) if isinstance(_db_path, str) else _com.get_db()
    agent = db.get_agent(args.agent_id)
    if agent is None:
        print(f"Error: Agent {args.agent_id} not found.")
        sys.exit(1)

    prompt_path = Path(args.prompt_file)
    if not prompt_path.exists():
        print(f"Error: Prompt file {args.prompt_file} not found.")
        sys.exit(1)

    # Graph-task guard (DA B5): tick-owned tasks are dispatched ONLY by the
    # autopilot watchdog tick (which passes force_task). Checked before any
    # tmux side effects.
    from juggle_cmd_agents_graph import check_task_guard

    guard_err = check_task_guard(
        db, agent.get("assigned_thread"), force=getattr(args, "force_task", False)
    )
    if guard_err:
        print(f"Error: {guard_err}")
        sys.exit(1)

    sys.path.insert(0, str(_com.SRC_DIR))
    mgr = _com.JuggleTmuxManager()

    _role = agent.get("role")
    _dispatch_cfg = _com._get_settings().get("agent", {})
    _agent_harness = agent.get("harness") or _dispatch_cfg.get("harness") or "claude"
    adapter = _com.get_adapter(_role, agent_cfg=dict(_dispatch_cfg, harness=_agent_harness))

    pane_id = agent["pane_id"]

    # Recreate a missing pane. start_agent_in_pane relaunches the REPL for an
    # interactive harness and is a no-op for a one-shot one (which just needs a
    # live shell pane to run the per-task process in, dispatched below).
    if not mgr.verify_pane(pane_id):
        mgr.ensure_session()
        new_pane_id = mgr.spawn_pane()
        mgr.start_agent_in_pane(new_pane_id, role=_role)
        db.update_agent(args.agent_id, pane_id=new_pane_id)
        pane_id = new_pane_id
        agent = db.get_agent(args.agent_id)
        is_new = True
    else:
        is_new = False

    # ── Worktree auto-create + hard guard (coder/planner only) ───────────────
    thread_uuid_wt = agent.get("assigned_thread")
    thread_wt = db.get_thread(thread_uuid_wt) if thread_uuid_wt else None
    _worktree_context = ""

    if _role in ("coder", "planner") and thread_wt:
        thread_label_wt = (thread_wt.get("user_label") or thread_wt["id"][:6])
        allow_main_wt = getattr(args, "allow_main", False)

        # Explicit CLI overrides: persist to thread and reload
        _v = getattr(args, "worktree_path", None)
        cli_wt_path = _v.strip() if isinstance(_v, str) else ""
        _v = getattr(args, "worktree_branch", None)
        cli_wt_branch = _v.strip() if isinstance(_v, str) else ""
        _v = getattr(args, "main_repo_path", None)
        cli_main_repo = _v.strip() if isinstance(_v, str) else ""

        # Resolve target repo: (1) --main-repo-path arg (2) agent.repo_path (3) thread repo
        # Never fall back to os.getcwd() — if unresolved, skip auto-create
        repo_path_wt = (
            cli_main_repo
            or (agent.get("repo_path") or "").strip()
            or (thread_wt.get("main_repo_path") or "").strip()
        )
        if cli_wt_path:
            db.update_thread(
                thread_uuid_wt,
                worktree_path=cli_wt_path,
                worktree_branch=cli_wt_branch or thread_wt.get("worktree_branch"),
                main_repo_path=cli_main_repo or repo_path_wt,
            )
            thread_wt = db.get_thread(thread_uuid_wt)

        existing_wt = (thread_wt.get("worktree_path") or "").strip()

        if not existing_wt and repo_path_wt and not allow_main_wt:
            ok_wt, wt_path_new, branch_new, msg_wt = _com._create_worktree(
                repo_path_wt, thread_label_wt
            )
            if ok_wt:
                db.update_thread(
                    thread_uuid_wt,
                    worktree_path=wt_path_new,
                    worktree_branch=branch_new,
                    main_repo_path=repo_path_wt,
                )
                thread_wt = db.get_thread(thread_uuid_wt)
                existing_wt = wt_path_new
                print(f"[juggle] {msg_wt}", file=sys.stderr)
            else:
                print(f"[juggle] WARNING: worktree auto-create failed: {msg_wt}", file=sys.stderr)

        # Hard guard: refuse main-worktree dispatch for coder/planner
        if not existing_wt and repo_path_wt and not allow_main_wt:
            print(
                f"Error: Cannot dispatch {_role} task without an isolated worktree "
                f"(repo={repo_path_wt}). Worktree auto-create failed. "
                f"Use --allow-main to override (bypass is logged)."
            )
            sys.exit(1)

        if allow_main_wt and repo_path_wt:
            print(
                f"[juggle] WARNING: --allow-main used for {_role} on {repo_path_wt} "
                f"(thread {thread_label_wt}) — main-worktree guard bypassed.",
                file=sys.stderr,
            )

        # Inject worktree CWD preamble (comes after UNIVERSAL_PREAMBLE)
        if existing_wt:
            branch_label_wt = (thread_wt.get("worktree_branch") or "") if thread_wt else ""
            _worktree_context = (
                f"## Working Directory\n"
                f"This task runs in an isolated worktree. "
                f"cd into it before any git or file operations:\n"
                f"```bash\ncd {existing_wt}\n```\n"
                f"Branch: `{branch_label_wt}`\n\n---\n\n"
            )
    # ── End worktree guard ────────────────────────────────────────────────────

    prompt = prompt_path.read_text()

    # Prepend role task template (unless --no-template)
    skip_template = getattr(args, "no_template", False)
    if not skip_template:
        role = agent.get("role")
        if role:
            templates = _com._get_settings().get("task_templates", {})
            template = templates.get(role, "")
            if template:
                qg = _com._get_settings()["agent"].get("quality_gate_skill", "mike:pre-pr")
                template = template.replace("{quality_gate_skill}", qg)
                prompt = template + "\n---\n\n" + prompt.rstrip()

    full_prompt = _com.UNIVERSAL_PREAMBLE + _worktree_context + prompt.rstrip()

    # Inline the role anchor for harnesses that don't run juggle's hooks. Claude
    # Code injects it via its UserPromptSubmit hook, so decorate_task is a no-op
    # there; config-only harnesses get the anchor prepended here instead.
    try:
        full_prompt = adapter.decorate_task(_role, full_prompt)
    except Exception:
        pass

    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(args.agent_id, last_active=now)
    if adapter.is_interactive:
        pane_hash = mgr.send_task(pane_id, full_prompt, is_new=is_new)
        oneshot_pid = None
    else:
        # One-shot: spawn a fresh `<harness> ... <prompt>` process in the pane;
        # it runs to completion and exits. No warm REPL, no marker polling.
        pane_hash, oneshot_pid = mgr.run_task_oneshot(
            pane_id, full_prompt, role=_role, model=agent.get("model")
        )
    now_iso = datetime.now(timezone.utc).isoformat()
    # Persist harness + model for all agents; oneshot_pid for one-shot agents.
    _update_fields: dict = dict(
        last_task=full_prompt,
        last_send_task_pane_hash=pane_hash,
        last_send_task_at=now_iso,
        harness=adapter.id,
        model=adapter._cfg.get("model") or agent.get("model"),
    )
    if not adapter.is_interactive and oneshot_pid is not None:
        _update_fields["oneshot_pid"] = oneshot_pid
    db.update_agent(args.agent_id, **_update_fields)

    # Ledger (best-effort, NEVER breaks dispatch): record this dispatch's INPUT
    # (the full sent prompt) keyed by thread + project/topic/task so the
    # orchestrator can pair it with the OUTPUT at completion. thread_id is the
    # universal key; project_id defaults to INBOX (non-project) via the thread.
    try:
        thread_uuid = agent.get("assigned_thread")
        if thread_uuid:
            from dbops import db_graph, db_topics

            _thread = db.get_thread(thread_uuid) or {}
            _task = db_graph.get_task_by_thread(db, thread_uuid)
            _topic = db_topics.get_topic_by_thread(db, thread_uuid)
            # VCS provenance (best-effort): repo_path from the agent (fall back to
            # the thread's worktree). detect()->vcs_type, before_sha=head, was_dirty.
            _repo_path = agent.get("repo_path") or _thread.get("worktree_path")
            _vcs_type = _before_sha = _was_dirty = None
            if _repo_path:
                try:
                    import vcs as _vcs

                    _vcs_type = _vcs.detect(_repo_path)
                    _backend = _vcs.get_backend(_vcs_type)
                    if _backend:
                        _before_sha = _backend.head(_repo_path)
                        _was_dirty = _backend.is_dirty(_repo_path)
                except Exception:  # noqa: BLE001
                    pass
            db.supersede_open_runs(thread_uuid)
            run_id = db.insert_agent_run(
                thread_id=thread_uuid,
                input_prompt=full_prompt,
                agent_id=args.agent_id,
                role=_role,
                model=_update_fields.get("model"),
                harness=adapter.id,
                project_id=_thread.get("project_id"),
                topic_id=_topic["id"] if _topic else None,
                task_id=_task["id"] if _task else None,
                repo_path=_repo_path,
                vcs_type=_vcs_type,
                before_sha=_before_sha,
                was_dirty=_was_dirty,
            )
            db.update_agent(args.agent_id, current_run_id=run_id)
    except Exception as _exc:  # noqa: BLE001
        print(f"[juggle] WARNING: ledger insert failed: {_exc}", file=sys.stderr)

    print(f"Task sent to agent {args.agent_id[:8]} (pane {pane_id}).")


def cmd_send_message(args):
    db = _com.get_db()
    agent = db.get_agent(args.agent_id)
    if agent is None:
        if getattr(args, "json_out", False):
            print(json.dumps({"ok": False, "error": f"Agent {args.agent_id} not found"}))
        else:
            print(f"Error: Agent {args.agent_id} not found.")
        sys.exit(1)

    pane_id = agent["pane_id"]
    from juggle_tmux import JuggleTmuxManager

    mgr = JuggleTmuxManager()
    try:
        result = mgr.send_message(pane_id, args.text)
    except RuntimeError as e:
        if getattr(args, "json_out", False):
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"Error: {e}")
        sys.exit(1)

    if result == "queued":
        if getattr(args, "json_out", False):
            print(json.dumps({"ok": True, "status": "queued", "agent_id": args.agent_id, "pane_id": pane_id}))
        else:
            print(f"Message queued for agent {args.agent_id[:8]} (pane {pane_id}) — will process at turn end.")
    else:
        if getattr(args, "json_out", False):
            print(json.dumps({"ok": True, "status": "sent", "agent_id": args.agent_id, "pane_id": pane_id}))
        else:
            print(f"Message sent to agent {args.agent_id[:8]} (pane {pane_id}).")
