"""Pure tree-render helper (2026-06-30 topic-graph-state-unify R3)."""
from juggle_cockpit_tree import TreeChild, load_topic_children, tree_lines

_G = lambda s: {"verified": "●", "running": "◐"}.get(s, "○")  # noqa: E731


def test_expanded_lists_each_child():
    kids = [TreeChild("t1", "verified"), TreeChild("t2", "running")]
    lines = tree_lines("[K] build login", kids, expanded=True, glyph_for=_G, width=40)
    plain = [line.plain for line in lines]
    assert plain[0] == "[K] build login"
    assert any("t1" in p and "●" in p for p in plain[1:])
    assert any("t2" in p and "◐" in p for p in plain[1:])
    assert len(plain) == 3


def test_collapsed_rolls_up():
    kids = [TreeChild("t1", "verified"), TreeChild("t2", "verified")]
    lines = tree_lines("[K] build login", kids, expanded=False, glyph_for=_G, width=40)
    plain = [line.plain for line in lines]
    assert len(plain) == 2
    assert "2/2 done" in plain[1]


def test_no_children_parent_only():
    lines = tree_lines("[K] idea", [], expanded=True, glyph_for=_G, width=40)
    assert [line.plain for line in lines] == ["[K] idea"]


def test_title_shown_when_present_id_as_fallback():
    kids = [TreeChild("t1", "verified", "Build login form"), TreeChild("t2", "running")]
    lines = tree_lines("[K] build login", kids, expanded=True, glyph_for=_G, width=40)
    plain = [line.plain for line in lines]
    assert any("Build login form" in p for p in plain[1:])
    assert any("t2" in p for p in plain[1:])


def test_expanded_caps_at_six_lines_with_overflow_marker():
    kids = [TreeChild(f"t{i}", "running") for i in range(9)]
    lines = tree_lines("[K] many tasks", kids, expanded=True, glyph_for=_G, width=40)
    plain = [line.plain for line in lines]
    assert len(plain) == 7  # parent + 6 (5 children + overflow marker)
    assert "t0" in plain[1]
    assert "t4" in plain[5]
    assert "… +4 more" in plain[6]


# ── load_topic_children: graph-dispatched thread resolution ────────────────────
#
# 2026-07-03 — Topics pane showed no nesting for graph-dispatched threads (CX):
# graph tasks are parented to the GRAPH TOPIC node, not the conversation node,
# so the direct parent_id=<thread> probe always missed and children stayed ().


def _make_db(tmp_path):
    from juggle_db import JuggleDB

    d = JuggleDB(db_path=str(tmp_path / "tree.db"))
    d.init_db()
    d.set_active(True)
    return d


def _bind_dispatch(conn, topic_id, thread_id):
    conn.execute(
        "INSERT INTO node_edges (node_id, depends_on_id, kind) VALUES (?, ?, 'dispatch')",
        (topic_id, thread_id),
    )


def test_load_topic_children_resolves_via_bound_running_topic(tmp_path):
    """Thread T is bound (dispatch edge) to a RUNNING graph topic P with 4 tasks —
    load_topic_children(conn, T) must return the 4 tasks, not ()."""
    from dbops.db_topics import create_topic
    from juggle_add_node import add_node

    db = _make_db(tmp_path)
    thread_id = db.create_thread("bound-thread", session_id="")

    with db._connect() as conn:
        create_topic(db, topic_id="topic-P", project_id="INBOX", title="P", conn=conn)
        conn.execute("UPDATE nodes SET state='running' WHERE id='topic-P'")
        _bind_dispatch(conn, "topic-P", thread_id)
        conn.commit()

    task_ids = []
    for i, state in enumerate(["open", "ready", "running", "verified"]):
        r = add_node(db, kind="task", title=f"task {i}", project_id="INBOX", parent_id="topic-P")
        task_ids.append(r["node_id"])
        with db._connect() as conn:
            conn.execute("UPDATE nodes SET state=? WHERE id=?", (state, r["node_id"]))
            conn.commit()

    from juggle_cockpit_tree import load_topic_children

    with db._connect() as conn:
        children = load_topic_children(conn, thread_id)

    assert len(children) == 4
    assert {c.id for c in children} == set(task_ids)
    assert all(c.title.startswith("task ") for c in children)


def test_load_topic_children_skips_verified_bound_topic(tmp_path):
    """A bound topic that is already verified (done) must not nest its tasks —
    only running/dispatching topics attach children (design decision)."""
    from dbops.db_topics import create_topic
    from juggle_add_node import add_node

    db = _make_db(tmp_path)
    thread_id = db.create_thread("bound-thread-2", session_id="")

    with db._connect() as conn:
        create_topic(db, topic_id="topic-Q", project_id="INBOX", title="Q", conn=conn)
        conn.execute("UPDATE nodes SET state='verified' WHERE id='topic-Q'")
        _bind_dispatch(conn, "topic-Q", thread_id)
        conn.commit()

    add_node(db, kind="task", title="orphaned by verified topic", project_id="INBOX", parent_id="topic-Q")

    with db._connect() as conn:
        children = load_topic_children(conn, thread_id)

    assert children == ()


def test_load_topic_children_direct_parent_flow_unchanged(tmp_path):
    """Regression pin: the pre-existing F7 direct-parent flow (tasks parented
    straight to the conversation node) must still work unchanged."""
    from juggle_add_node import add_node

    db = _make_db(tmp_path)
    thread_id = db.create_thread("direct-thread", session_id="")

    add_node(db, kind="task", title="direct task", project_id="INBOX", parent_id=thread_id)

    with db._connect() as conn:
        children = load_topic_children(conn, thread_id)

    assert len(children) == 1
    assert children[0].title == "direct task"
