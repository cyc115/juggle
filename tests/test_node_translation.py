"""Pin (2026-06-22): legacy status->state value map is single-source for P8 read-collapse."""
import pytest


def test_status_to_state_full_map():
    from dbops.node_translation import STATUS_TO_STATE
    assert STATUS_TO_STATE == {
        "active": "open", "closed": "done", "background": "running",
        "running": "running", "failed": "failed-exec", "done": "done",
        "archived": "archived",
    }


def test_state_for_status_unknown_fails_loud():
    from dbops.node_translation import state_for_status
    with pytest.raises(KeyError):
        state_for_status("bogus")


def test_state_for_status_known_values():
    from dbops.node_translation import state_for_status
    assert state_for_status("active") == "open"
    assert state_for_status("background") == "running"
    assert state_for_status("failed") == "failed-exec"


def test_column_alias_constants():
    from dbops import node_translation as nt
    assert nt.TOPIC_COL == "title"
    assert nt.PROMPT_COL == "objective"
    assert nt.LAST_ACTIVE_COL == "last_active_at"
    assert nt.TOPIC_ID_COL == "parent_id"
