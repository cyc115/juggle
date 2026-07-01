"""send-task forward-link wiring (2026-06-30 topic-graph-state-unify F2).

After a successful dispatch, the work is parented to the owning feature topic —
either the explicit --topic, or the current thread when it is a human-facing
conversation. Never raises on forward-link failure (dispatch already succeeded).
"""
from argparse import Namespace


def _ns(**kw):
    kw.setdefault("no_template", True)
    kw.setdefault("worktree_path", None)
    kw.setdefault("worktree_branch", None)
    kw.setdefault("main_repo_path", None)
    kw.setdefault("allow_main", False)
    kw.setdefault("topic", None)
    return Namespace(**kw)


def _seed(juggle_db, tmp_path, monkeypatch):
    import juggle_dispatch_core as dc

    monkeypatch.setattr(dc, "send_task_to_agent", lambda *a, **k: None)
    feature = juggle_db.create_thread(topic="feature", session_id="s")
    juggle_db.add_message(feature, role="user", content="build the login page please")
    agent_thread = juggle_db.create_thread(topic="agent", session_id="s")
    aid = juggle_db.create_agent(role="coder", pane_id="%1")
    juggle_db.update_agent(aid, assigned_thread=agent_thread)
    pf = tmp_path / "p.md"
    pf.write_text("do the work")
    return feature, agent_thread, aid, pf


def _child_count(db, parent):
    with db._connect() as c:
        return c.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind='task' AND parent_id=?", (parent,)
        ).fetchone()[0]


def test_send_task_forward_links_to_explicit_topic(juggle_db, tmp_path, monkeypatch):
    import juggle_cmd_agents_tasks as t

    feature, agent_thread, aid, pf = _seed(juggle_db, tmp_path, monkeypatch)
    label = juggle_db.get_thread(feature)["user_label"]
    args = _ns(agent_id=aid, prompt_file=str(pf), topic=label, db_path=str(juggle_db.db_path))
    t.cmd_send_task(args)
    assert _child_count(juggle_db, feature) == 1


def test_send_task_infers_current_conversation(juggle_db, tmp_path, monkeypatch):
    import juggle_cmd_agents_tasks as t

    feature, agent_thread, aid, pf = _seed(juggle_db, tmp_path, monkeypatch)
    juggle_db.set_current_thread(feature)
    args = _ns(agent_id=aid, prompt_file=str(pf), topic=None, db_path=str(juggle_db.db_path))
    t.cmd_send_task(args)
    assert _child_count(juggle_db, feature) == 1


def test_send_task_no_topic_no_link(juggle_db, tmp_path, monkeypatch):
    """No --topic and current thread is not a human-facing conversation → no link."""
    import juggle_cmd_agents_tasks as t

    feature, agent_thread, aid, pf = _seed(juggle_db, tmp_path, monkeypatch)
    juggle_db.set_current_thread(agent_thread)  # current is the agent thread (no human msg)
    args = _ns(agent_id=aid, prompt_file=str(pf), topic=None, db_path=str(juggle_db.db_path))
    t.cmd_send_task(args)
    assert _child_count(juggle_db, feature) == 0
    assert _child_count(juggle_db, agent_thread) == 0
