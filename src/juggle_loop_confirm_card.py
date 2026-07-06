"""juggle_loop_confirm_card — deterministic render of a validated loop template's
decomposed topic-DAG as a pre-create confirm-card (loop-entity V2, spec §6.3).

Code, not prompt (juggle 'code over prompts'): ``schedule:create`` calls
``juggle loop plan`` which renders THIS card from the VALIDATED template so a
legal-but-WRONG partition (the brainstorm merging steps that should have been
separate topics) is caught by a human BEFORE the loop structure is frozen — a loop's
topic-DAG is re-instantiated every fire, so fixing a bad partition means
delete+recreate. Mirrors ``/juggle:delegate``'s plan-card → confirm → fire.

Pure data in / str out — the input is ``validate_loop_template``'s normalised output
(each topic already carries ``role``/``delivery``/``model``/``deps``).
"""
from __future__ import annotations

from juggle_loop_cadence import format_weekly


def _display_cadence(cadence: str) -> str:
    """Card label for the cadence — a weekly day-of-week form renders compactly
    (``'Mon 09:00'``); every other form echoes raw (interval/daily are already
    readable, and the raw echo is the confirm-before-freeze contract)."""
    return format_weekly(cadence) or cadence


def _model_label(model) -> str:
    """Human label for a topic's model — an unset (None) model is best-effort at
    spawn (§2), rendered explicitly so the card never looks like a missing field."""
    return model if model else "default (best-effort)"


def render_topic_dag_card(template: dict, cadence: str) -> str:
    """Render the decomposed topic-DAG confirm-card for a validated ``template``.

    Shows: the topic count, the cadence, each topic with its ``(role, delivery,
    model)`` and member task ids (annotated with its cross-topic deps), and the flat
    list of cross-topic edges (``dep → topic``, i.e. upstream → downstream)."""
    topics = template["topics"]
    n = len(topics)
    lines = [
        f"Loop plan — {n} topic{'s' if n != 1 else ''}",
        f"Cadence:  {_display_cadence(cadence)}",
        "",
        "Topics:",
    ]
    for t in topics:
        deps = t.get("deps") or []
        dep_note = f"   (depends on: {', '.join(deps)})" if deps else ""
        lines.append(f"  [{t['id']}] {t.get('title', t['id'])}{dep_note}")
        lines.append(
            f"      role={t['role']}  delivery={t['delivery']}  "
            f"model={_model_label(t.get('model'))}"
        )
        task_ids = ", ".join(tk["id"] for tk in t.get("tasks", []))
        lines.append(f"      tasks: {task_ids}")

    # Cross-topic edges (dep → topic = upstream produces, downstream consumes).
    edges = [(dep, t["id"]) for t in topics for dep in (t.get("deps") or [])]
    lines.append("")
    if edges:
        lines.append("Cross-topic edges:")
        lines.extend(f"  {src} → {dst}" for src, dst in edges)
    else:
        lines.append("Cross-topic edges: (none)")
    return "\n".join(lines)
