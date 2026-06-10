"""
juggle_cmd_agents_pool — Agent pool spawn/list/status commands.

Owns: cmd_spawn_agent, cmd_list_agents, cmd_check_agents.
Must not own: get/release/decommission lifecycle, task dispatch, completion.

All shared symbols are accessed through juggle_cmd_agents_common (_com) so
that tests can monkeypatch _com.<symbol> and have the patches take effect.
"""

import json
import sys
from datetime import datetime, timezone

import juggle_cmd_agents_common as _com


def cmd_check_agents(_):
    db = _com.get_db()
    threads = db.get_all_threads()
    background = [
        {
            "thread_id": t.get("user_label") or t["id"][:8],
            "task_id": t.get("agent_task_id", ""),
            "topic": t["topic"],
        }
        for t in threads
        if t["status"] == "background"
    ]
    print(json.dumps(background))


def cmd_spawn_agent(args):
    db = _com.get_db()
    db.init_db()
    sys.path.insert(0, str(_com.SRC_DIR))
    from juggle_tmux import JuggleTmuxManager

    mgr = JuggleTmuxManager()
    try:
        agent = mgr.spawn_agent(db, args.role, model=getattr(args, "model", None))
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"{agent['id']} {agent['pane_id']}")


def cmd_list_agents(args):
    db = _com.get_db()
    # Self-heal: reconcile stale busy one-shot agents before listing.
    sys.path.insert(0, str(_com.SRC_DIR))
    try:
        from juggle_tmux import reconcile_oneshot_agents
        reconcile_oneshot_agents(db)
    except Exception:
        pass

    agents = db.get_all_agents()
    if not agents:
        print("No agents.")
        return

    now = datetime.now(timezone.utc)

    def _agent_age(a) -> str:
        """Age from busy_since if busy, else last_active (fallback)."""
        ts = None
        if a.get("status") == "busy" and a.get("busy_since"):
            ts = a["busy_since"]
        else:
            ts = a.get("last_active")
        if not ts:
            return "-"
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            secs = int((now - dt).total_seconds())
            return (
                f"{secs}s"
                if secs < 60
                else (f"{secs // 60}m" if secs < 3600 else f"{secs // 3600}h")
            )
        except (ValueError, TypeError):
            return "-"

    def _agent_topic_label(a) -> str:
        if a.get("assigned_thread"):
            t = db.get_thread(a["assigned_thread"])
            if t:
                return t.get("user_label") or t["id"][:6]
        return "-"

    for a in agents:
        short_id = a["id"][:8]
        role = a.get("role") or "-"
        status = a.get("status") or "-"
        pane = a.get("pane_id") or "-"
        topic_lbl = _agent_topic_label(a)
        age = _agent_age(a)
        harness = a.get("harness") or "-"
        model = a.get("model")
        hmodel = f"{harness}/{model}" if model else harness
        print(f"{short_id} {role:<8} {status:<5} {pane} [{topic_lbl}] {age:<4} {hmodel}")
