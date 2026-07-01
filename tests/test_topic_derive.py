"""Pure-fn truth table for derive_topic_state (2026-06-30 topic-graph-state-unify R2)."""
import pytest

from juggle_topic_derive import derive_topic_state


@pytest.mark.parametrize("states,mins,expect", [
    ([], 40, None),
    (["running"], 40, "open"),
    (["verified"], None, "done"),
    (["verified", "done"], 40, "done"),
    (["verified"], 5, "open"),               # idle guard
    (["failed-exec"], 40, "open"),           # needs attention
    (["verified", "running"], 40, "open"),   # active wins
])
def test_derive_topic_state_table(states, mins, expect):
    assert derive_topic_state(
        states, minutes_since_human_msg=mins, close_idle_min=30
    ) == expect
