"""juggle_brief_render — pure human formatters for the brief command.

Each ``render_*_human(state)`` is a PURE fn of a collected state dict (built by
``juggle_brief_collect``): no DB / tmux / git, so budget + content invariants are
unit-testable on synthetic state. The ``--json`` path never comes here — it dumps
the collected dict verbatim (``juggle_cmd_brief``). Formatting only lives here.

Session snapshot is deliberately terse (≤~1500 chars, see the plan's measured
19.9k-char baseline it replaces): active topics only, agents reconciled, one
health line — no archived topics, no raw self-heal dump.
"""
from __future__ import annotations

_ARCHIVED = {"archived"}


def _topic_line(t: dict) -> str:
    """`[ID] badge label · openQ:n · agent(role/status)` — one active topic."""
    badge = t.get("state_badge") or ""
    label = t.get("label") or t.get("id")
    openq = t.get("open_questions") or 0
    parts = [f"[{t.get('id')}]"]
    if badge:
        parts.append(badge)
    parts.append(str(label))
    parts.append(f"· {t.get('state')}")
    if openq:
        parts.append(f"· openQ:{openq}")
    agent = t.get("agent")
    if agent:
        parts.append(f"· agent({agent.get('role')}/{agent.get('status')})")
    return " ".join(parts)


def render_session_human(state: dict) -> str:
    """The no-arg session snapshot (≤~1500 chars). Archived topics are dropped
    defensively even if handed one, so the NO-archived contract never depends on
    the collector's filter alone."""
    lines: list[str] = []

    wd = state.get("watchdog") or {}
    wd_txt = f"watchdog {'✓' if wd.get('alive') else '✗'}"
    if wd.get("pid"):
        wd_txt += f"(pid {wd['pid']})"
    ap = "autopilot ON" if state.get("autopilot") else "autopilot off"
    notifs = state.get("unconsumed_notifs") or 0
    lines.append(
        f"Juggle v{state.get('version', '?')} · {wd_txt} · {ap} · notif:{notifs}"
    )

    topics = [
        t for t in (state.get("topics") or [])
        if (t.get("state") not in _ARCHIVED and t.get("state_badge") != "🗄️")
    ]
    lines.append("")
    lines.append(f"Active topics ({len(topics)}):")
    for t in topics:
        lines.append(f"  {_topic_line(t)}")

    agents = state.get("agents") or {}
    lines.append("")
    lines.append(f"Agents: {agents.get('busy', 0)} busy / {agents.get('idle', 0)} idle")
    for a in agents.get("busy_list") or []:
        recon = a.get("reconciled")
        tag = f" ⚠{recon}" if recon and recon != "busy" else ""
        lines.append(
            f"  {a.get('id')} {a.get('role')} [{a.get('topic')}] {a.get('age')}{tag}"
        )

    actions = state.get("actions") or []
    if actions:
        lines.append("")
        lines.append(f"Open actions ({len(actions)}):")
        for it in actions:
            lines.append(
                f"  ⚡[{it.get('id')}] {str(it.get('priority', '')).upper()} {it.get('message')}"
            )

    h = state.get("health") or {}
    wt = h.get("worktrees") or {}
    sh = h.get("selfheal") or {}
    dirty = " *dirty*" if h.get("dirty") else ""
    lines.append("")
    lines.append(
        f"Health: git {h.get('git_branch', '?')}{dirty} · "
        f"worktrees {wt.get('clean', 0)} clean/{wt.get('unmerged', 0)} unmerged/"
        f"{wt.get('orphan', 0)} orphan · self-heal {sh.get('count', 0)}"
    )
    for sig in (sh.get("top_sigs") or [])[:3]:
        lines.append(f"  ↳ {sig}")

    return "\n".join(lines)


def render_topic_human(state: dict) -> str:
    """Topic probe: state, summary, open-Qs, agent, actions, notifications,
    worktree, and the computed why-stalled verdict."""
    lines: list[str] = []
    lines.append(
        f"[{state.get('id')}] {state.get('title', '')}  ({state.get('state')})"
    )
    verdict = state.get("verdict")
    if verdict:
        lines.append(f"verdict: {verdict}")
    if state.get("summary"):
        lines.append(f"summary: {state['summary']}")

    for q in state.get("open_questions") or []:
        lines.append(f"  ❓ {q}")

    agent = state.get("agent")
    if agent:
        idle = " (idle-at-prompt)" if agent.get("idle_at_prompt") else ""
        lines.append(
            f"agent: {agent.get('id')} {agent.get('role')}/{agent.get('status')} "
            f"age {agent.get('age')}{idle}"
        )
        if agent.get("last_line"):
            lines.append(f"  pane: {agent['last_line']}")
    else:
        lines.append("agent: (none)")

    for it in state.get("actions") or []:
        lines.append(
            f"  ⚡[{it.get('id')}] {str(it.get('priority', '')).upper()} {it.get('message')}"
        )

    notifs = state.get("notifications") or []
    if notifs:
        lines.append("recent:")
        for n in notifs[:3]:
            lines.append(f"  · {n.get('message')}")

    wt = state.get("worktree") or {}
    if wt:
        tg = wt.get("tests_green")
        tg_txt = "?" if tg is None else ("green" if tg else "red")
        lines.append(
            f"worktree: {wt.get('branch', '-')} "
            f"exists={wt.get('exists')} dirty={wt.get('dirty')} "
            f"unmerged={wt.get('unmerged_commits', 0)} tests={tg_txt}"
        )
    return "\n".join(lines)


def render_project_human(state: dict) -> str:
    """Project probe: graph rollup counts, armed flag, next actionable node,
    failing node ids."""
    lines: list[str] = []
    armed = "armed" if state.get("armed") else "disarmed"
    lines.append(f"[{state.get('id')}] {state.get('name', '')}  ({armed})")

    c = state.get("counts") or {}
    if c:
        seg = [f"{c.get('verified', 0)}/{c.get('total', 0)} done"]
        for key in ("failed", "blocked", "ready", "running", "open"):
            if c.get(key):
                seg.append(f"{c[key]} {key}")
        lines.append("graph: " + ", ".join(seg))
    else:
        lines.append("graph: (no tasks)")

    nxt = state.get("next_node")
    if nxt:
        lines.append(f"next: {nxt.get('id')} ({nxt.get('title')})")

    failing = state.get("failing_nodes") or []
    if failing:
        lines.append("failing: " + ", ".join(failing))
    return "\n".join(lines)
