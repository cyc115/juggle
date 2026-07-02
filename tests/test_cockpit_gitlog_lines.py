"""Pure git-log line-model tests (cockpit-gitlog-view, part 2 of the 2026-06-30
graph railroad). Seeded DAG, no DB, no Textual."""
from datetime import datetime, timezone

from juggle_cockpit_graph_layout import GraphTask
from juggle_cockpit_graph_dag import GraphDag
from juggle_cockpit_gitlog_lines import gitlog_rows, render_gitlog_line
from juggle_cockpit_gitlog_meta import GitLogMeta

_NOW = datetime(2026, 7, 2, 10, 2, 0, tzinfo=timezone.utc)


def _dag(tasks, edges, pid="p1"):
    return GraphDag(project_id=pid, tasks=tasks, edges=edges, project_name="Proj")


def _t(i, s="open", **kw):
    return GraphTask(i, i.upper(), s, **kw)


def test_newest_active_at_top():
    """diamond DAG: rows come back newest/active-first (reverse topo order)."""
    tasks = [_t("a", "verified"), _t("b", "verified"), _t("c", "running"), _t("d", "ready")]
    edges = [("b", "a"), ("c", "a"), ("d", "b"), ("d", "c")]
    rows = gitlog_rows([_dag(tasks, edges)])
    assert [r.id for r in rows] == ["d", "c", "b", "a"]


def test_full_history_unpruned():
    """Unlike the small panel, the full-screen view shows EVERY node — no
    frontier prune — so an all-verified project still renders every row."""
    tasks = [_t(i, "verified") for i in ("a", "b", "c")]
    edges = [("b", "a"), ("c", "b")]
    rows = gitlog_rows([_dag(tasks, edges)])
    assert len(rows) == 3


def test_glyph_embedded_in_rail_and_rounded_corners():
    """Fan-out/fan-in draws ROUNDED corners (╮╭ / ╯╰), not the square railroad
    connectors (┬┴) — the git-log-styled twin of Surface-B."""
    tasks = [_t("a"), _t("b"), _t("c"), _t("d")]
    edges = [("b", "a"), ("c", "a"), ("d", "b"), ("d", "c")]
    rows = gitlog_rows([_dag(tasks, edges)])
    joined = "".join(r.rail for r in rows)
    assert any(ch in joined for ch in "╮╭") and any(ch in joined for ch in "╯╰")
    assert not any(ch in joined for ch in "┬┴├┤")


def test_glyph_sits_at_the_node_own_lane():
    """The node's state glyph (●) replaces the rail char at its OWN lane column
    (embedded in the spine), matching the approved mockup — not appended after."""
    tasks = [_t("a", "verified"), _t("b", "open")]
    rows = gitlog_rows([_dag(tasks, [("b", "a")])])
    assert "●" in rows[1].rail  # a is row[1] after newest-first reversal


def test_running_row_shows_elapsed_and_agent_model_not_sha():
    tasks = [_t("a", "running", user_label="HW")]
    meta = {"a": GitLogMeta(role="coder", model="sonnet", dispatched_at="2026-07-02T09:50:00+00:00")}
    rows = gitlog_rows([_dag(tasks, [])], meta_by_id=meta, now=_NOW)
    assert rows[0].meta == "running  [HW] 12m  coder·sonnet"


def test_verified_row_shows_finish_sha_and_duration():
    tasks = [_t("a", "verified")]
    meta = {"a": GitLogMeta(
        merged_sha="6db0e75abcdef", dispatched_at="2026-07-02T09:00:00+00:00",
        completed_at="2026-07-02T09:12:00+00:00",
    )}
    rows = gitlog_rows([_dag(tasks, [])], meta_by_id=meta)
    assert rows[0].meta == "verified  09:12  6db0e75  12m"


def test_verified_row_without_meta_renders_em_dash():
    tasks = [_t("a", "verified")]
    rows = gitlog_rows([_dag(tasks, [])])
    assert rows[0].meta == "verified  —  —"


def test_ready_row_shows_ready_marker():
    tasks = [_t("a", "ready")]
    rows = gitlog_rows([_dag(tasks, [])])
    assert "ready" in rows[0].meta and "▸" in rows[0].meta


def test_open_row_shows_dep_wait_suffix():
    tasks = [_t("a", "verified"), _t("b", "open")]
    rows = gitlog_rows([_dag(tasks, [("b", "a")])])
    open_row = next(r for r in rows if r.id == "b")
    assert open_row.meta == "open ⊣1"  # waits on #1 (a)


def test_failed_row_gets_fail_suffix():
    tasks = [_t("a", "failed-exec")]
    rows = gitlog_rows([_dag(tasks, [])])
    assert "✗" in rows[0].meta and rows[0].meta.startswith("failed-exec")


def test_multi_project_dags_each_get_own_lane_layout():
    """Two independent projects concatenate; each keeps its own 1-based idx."""
    d1 = _dag([_t("a", "verified")], [], pid="p1")
    d2 = _dag([_t("x", "verified")], [], pid="p2")
    rows = gitlog_rows([d1, d2])
    assert {r.idx for r in rows} == {1}
    assert {r.project_id for r in rows} == {"p1", "p2"}


def test_selected_row_flag():
    tasks = [_t("a", "verified"), _t("b", "open")]
    rows = gitlog_rows([_dag(tasks, [("b", "a")])], selected_row=1)
    assert [r.selected for r in rows] == [False, True]


def test_render_gitlog_line_returns_rich_text():
    from rich.text import Text
    tasks = [_t("a", "verified")]
    rows = gitlog_rows([_dag(tasks, [])])
    text = render_gitlog_line(rows[0])
    assert isinstance(text, Text)
    assert "a" in text.plain
