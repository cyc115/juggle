"""juggle_brief_diagnose — pure why-stalled verdict for a topic-state dict.

``diagnose(topic_state)`` maps a collected topic-state dict to exactly one
verdict from ``VERDICTS``. PURE (no DB / tmux / git): the collector
(``juggle_brief_collect.collect_topic_state``) does the live IO and hands this a
plain dict, so every branch is table-testable with a synthetic row.

The keys ``diagnose`` reads (all optional — a sparse dict falls through to the
benign default):
  state            node state string (e.g. 'background', 'done', 'failed-exec')
  has_failure_action  True if the topic has an OPEN failure action item
  deps_unmet       True if a graph-task topic is waiting on unfinished deps
  agent            None, or {'idle_at_prompt': bool, 'status': str}
  work_landed      True if the topic's work already merged/landed
  unmerged_commits int commits ahead of trunk on the topic's worktree
"""
from __future__ import annotations

from dbops.terminal_states import FAILURE_TERMINAL_STATES

VERDICTS = frozenset({
    "idle-done-unreleased",
    "dep-wait",
    "failed",
    "unmerged-work",
    "no-agent",
    "healthy-running",
})


def diagnose(topic_state: dict) -> str:
    """Return the single why-stalled verdict for ``topic_state`` (see module doc).

    Precedence (highest first): failed → dep-wait → no-agent → idle-done-unreleased
    → unmerged-work → healthy-running. The default is the benign ``healthy-running``
    so an ordinary open conversation never reads as blocked.
    """
    state = topic_state.get("state") or ""
    agent = topic_state.get("agent")

    if state in FAILURE_TERMINAL_STATES or topic_state.get("has_failure_action"):
        return "failed"
    if topic_state.get("deps_unmet"):
        return "dep-wait"
    if state == "background" and not agent:
        return "no-agent"
    if agent and agent.get("idle_at_prompt") and topic_state.get("work_landed"):
        return "idle-done-unreleased"
    if topic_state.get("unmerged_commits", 0) > 0 and not topic_state.get("work_landed"):
        return "unmerged-work"
    return "healthy-running"
