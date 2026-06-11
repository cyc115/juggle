"""Graph panel shows readable A-Z topic label on in-progress nodes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from juggle_cockpit_graph_layout import GraphNode
from juggle_cockpit_graph_panel import _cell_text


def test_running_node_shows_user_label_not_thread_uuid():
    node = GraphNode(
        id="footer-narrow", title="t", state="running",
        thread_id="ff1e5095-819c-4629", user_label="WR",
    )
    txt = _cell_text(node, inner_w=40, selected=False).plain
    assert "[WR]" in txt, txt
    assert "[ff1e" not in txt, txt


def test_falls_back_to_thread_prefix_when_no_user_label():
    node = GraphNode(
        id="footer-narrow", title="t", state="running",
        thread_id="ff1e5095-819c-4629", user_label=None,
    )
    txt = _cell_text(node, inner_w=40, selected=False).plain
    assert "[ff1e]" in txt, txt
