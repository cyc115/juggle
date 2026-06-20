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

    prompt = prompt_path.read_text()
    thread_uuid = agent.get("assigned_thread")

    # Resolve CLI worktree overrides (send_task_to_agent accepts them via params)
    _v = getattr(args, "worktree_path", None)
    cli_wt_path = _v.strip() if isinstance(_v, str) else None
    _v = getattr(args, "worktree_branch", None)
    cli_wt_branch = _v.strip() if isinstance(_v, str) else None
    _v = getattr(args, "main_repo_path", None)
    cli_main_repo = _v.strip() if isinstance(_v, str) else None

    import juggle_dispatch_core as _dc

    try:
        _dc.send_task_to_agent(
            db,
            args.agent_id,
            thread_uuid,
            prompt,
            force_task=getattr(args, "force_task", False),
            skip_template=getattr(args, "no_template", False),
            allow_main=getattr(args, "allow_main", False),
            worktree_path_override=cli_wt_path or None,
            worktree_branch_override=cli_wt_branch or None,
            main_repo_override=cli_main_repo or None,
            db_path=_db_path,
        )
    except RuntimeError as e:
        err = str(e)
        if "cannot dispatch" in err and "worktree" in err:
            print(
                "Error: Cannot dispatch task without an isolated worktree. "
                "Worktree auto-create failed. Use --allow-main to override (bypass is logged)."
            )
        elif "task guard" in err:
            print(f"Error: {err.replace('task guard refused: ', '')}")
        else:
            print(f"Error: {e}")
        sys.exit(1)

    # Re-fetch agent to get updated pane_id (may have changed due to pane recreation)
    updated = db.get_agent(args.agent_id)
    pane_id = updated["pane_id"] if updated else agent["pane_id"]
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
