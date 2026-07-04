"""juggle_loop_template_validator — deterministic loop-template partition validator
(loop-entity V1, Phase 4).

The partition rule ("same (role, model) → ONE topic; differing → SEPARATE topics",
critique §Axis-4) is enforced HERE, in code, at create time — NEVER trusted as raw
LLM output. For V1 single-topic loops this validator:

  * asserts the template is exactly ONE topic (V1 scope guard — multi-topic is V2),
  * asserts the topic has ≥1 member task,
  * asserts every member task agrees on its ``(role, model, delivery)`` signature
    (intra-topic homogeneity — a mixed-role/-model/-delivery topic is rejected so
    a later per-node dispatch never straddles two contracts inside one topic),
  * NORMALISES the topic: lifts the shared ``role``/``delivery`` onto the topic and
    fills each task's ``role``/``delivery`` from the shared signature.

Pure data in / data out — no DB, no I/O. The transactional create
(``juggle_cmd_loop_create``) calls this BEFORE opening its write transaction, so a
rejected template never touches the DB.

Template shape (dict, e.g. parsed from the router's JSON):

    {
      "topics": [
        {
          "id": "daily-digest",
          "title": "Daily digest",
          "objective": "…",                 # optional
          "delivery": "deliver",            # topic-level default; 'merge' if absent
          "tasks": [
            {"id": "gen", "title": "…", "prompt": "…",
             "role": "researcher", "model": "sonnet",
             "verify_cmd": null, "deps": []}
          ]
        }
      ]
    }
"""
from __future__ import annotations

_VALID_ROLES = frozenset({"coder", "planner", "researcher"})
_VALID_DELIVERY = frozenset({"merge", "deliver"})


class LoopTemplateError(ValueError):
    """A loop template violates the deterministic partition/scope rules."""


def _task_signature(task: dict, topic_delivery: str) -> tuple:
    """The (role, model, delivery) tuple a member task belongs to. ``delivery``
    falls back to the topic-level default when the task omits it."""
    role = task.get("role") or "coder"
    model = task.get("model")  # None is a legitimate 'unpinned' value in V1
    delivery = task.get("delivery") or topic_delivery
    return (role, model, delivery)


def validate_loop_template(template: dict) -> dict:
    """Validate + normalise a loop template for V1 (single-topic loops).

    Returns a normalised template ``{"topics": [<one topic>]}`` where the topic
    carries the shared ``role``/``delivery`` and every task carries an explicit
    ``role``/``model``/``delivery``/``deps``. Raises ``LoopTemplateError`` (never
    a bare ValueError from deep inside) on any partition/scope violation.
    """
    if not isinstance(template, dict):
        raise LoopTemplateError("template must be a dict")
    topics = template.get("topics")
    if not isinstance(topics, list) or not topics:
        raise LoopTemplateError("template must contain a non-empty 'topics' list")
    if len(topics) != 1:
        raise LoopTemplateError(
            f"V1 supports single-topic loops only; got {len(topics)} topics "
            f"(multi-topic decomposition is a V2 concern)"
        )
    topic = topics[0]
    if not isinstance(topic, dict) or not topic.get("id"):
        raise LoopTemplateError("topic must be a dict with a non-empty 'id'")
    if not topic.get("title"):
        raise LoopTemplateError(f"topic {topic['id']!r} must have a 'title'")

    topic_delivery = topic.get("delivery") or "merge"
    if topic_delivery not in _VALID_DELIVERY:
        raise LoopTemplateError(
            f"topic {topic['id']!r} delivery {topic_delivery!r} invalid "
            f"(expected one of {sorted(_VALID_DELIVERY)})"
        )

    tasks = topic.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise LoopTemplateError(f"topic {topic['id']!r} has no member tasks")

    sigs = set()
    for task in tasks:
        if not isinstance(task, dict) or not task.get("id"):
            raise LoopTemplateError(
                f"topic {topic['id']!r} has a task with no 'id'"
            )
        role = task.get("role") or "coder"
        if role not in _VALID_ROLES:
            raise LoopTemplateError(
                f"task {task['id']!r} role {role!r} invalid "
                f"(expected one of {sorted(_VALID_ROLES)})"
            )
        delivery = task.get("delivery") or topic_delivery
        if delivery not in _VALID_DELIVERY:
            raise LoopTemplateError(
                f"task {task['id']!r} delivery {delivery!r} invalid "
                f"(expected one of {sorted(_VALID_DELIVERY)})"
            )
        sigs.add(_task_signature(task, topic_delivery))

    if len(sigs) != 1:
        raise LoopTemplateError(
            f"topic {topic['id']!r} mixes (role, model, delivery) across its "
            f"member tasks: {sorted(sigs)} — same (role, model) belongs in ONE "
            f"topic; differing signatures require SEPARATE topics (V2)."
        )
    role, model, delivery = next(iter(sigs))

    norm_tasks = []
    for task in tasks:
        norm_tasks.append({
            "id": task["id"],
            "title": task.get("title") or task["id"],
            "prompt": task.get("prompt") or "",
            "role": role,
            "model": model,
            "delivery": delivery,
            "verify_cmd": task.get("verify_cmd"),
            "deps": list(task.get("deps") or []),
        })

    # Dep closure: every dep must reference a sibling member task (never a foreign
    # id, never itself). replace_edges uses INSERT OR IGNORE with no FK check, so a
    # dangling dep edge would silently wedge a task at never-'deps_ready'. The
    # validator is the deterministic gate — reject it here, not at run time.
    member_ids = {tk["id"] for tk in norm_tasks}
    for tk in norm_tasks:
        for dep in tk["deps"]:
            if dep == tk["id"]:
                raise LoopTemplateError(f"task {tk['id']!r} depends on itself")
            if dep not in member_ids:
                raise LoopTemplateError(
                    f"task {tk['id']!r} dep {dep!r} is not a member task of topic "
                    f"{topic['id']!r}"
                )
    norm_topic = {
        "id": topic["id"],
        "title": topic["title"],
        "objective": topic.get("objective") or "",
        "role": role,
        "delivery": delivery,
        "tasks": norm_tasks,
    }
    return {"topics": [norm_topic]}
