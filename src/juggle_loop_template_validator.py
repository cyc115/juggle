"""juggle_loop_template_validator — deterministic loop-template partition validator
(loop-entity V2, Phase 4a: multi-topic decomposition).

The partition rule is enforced HERE, in code, at create time — NEVER trusted as raw
LLM output (critique §Axis-4, spec §6):

  > Consecutive steps sharing ``(role, delivery)`` → member tasks of ONE topic.
  > Steps differing in ``role`` OR ``delivery`` → SEPARATE topics joined by a dep
  > edge. **``model`` is NOT a partition key** — same-``(role, delivery)`` steps that
  > differ only in desired model COLLAPSE into one topic and share that topic's single
  > model (model is best-effort at reuse, §2, so it must not force a topic split).

For a template (one or more topics) this validator:

  * permits N topics (the V1 single-topic guard is lifted);
  * asserts every topic has ≥1 member task with a unique, non-empty id;
  * asserts a topic's member tasks agree on their ``(role, delivery)`` signature
    (intra-topic homogeneity — a mixed topic would straddle two agent contracts);
  * requires a UNIFORM model per topic (one topic = one pane = one model, §2.2) — a
    topic pinning two DIFFERENT non-null models is a template error; an unset task
    inherits the topic's single model;
  * validates the cross-topic dep-DAG: every topic-level dep references an EXISTING,
    DISTINCT topic and the topic graph is ACYCLIC;
  * NORMALISES each topic — lifts the shared ``role``/``delivery``/``model`` onto the
    topic and fills each task's ``role``/``model``/``delivery``/``deps``.

Pure data in / data out — no DB, no I/O. The transactional create
(``juggle_cmd_loop_create``) calls this BEFORE opening its write transaction, so a
rejected template never touches the DB.

Template shape (dict, e.g. parsed from the router's JSON):

    {
      "topics": [
        {"id": "research", "title": "…", "objective": "…", "delivery": "deliver",
         "deps": [],                          # cross-topic deps (other topic ids)
         "tasks": [
           {"id": "gather", "title": "…", "prompt": "…", "role": "researcher",
            "model": "sonnet", "verify_cmd": null, "deps": []}   # deps: sibling tasks
         ]},
        {"id": "notify", "title": "…", "delivery": "merge", "deps": ["research"],
         "tasks": [ … ]}
      ]
    }

Two dep grains: a TASK ``deps`` references sibling member tasks (within-topic); a
TOPIC ``deps`` references other topics (cross-topic edges, the handoff seam, §1).
"""
from __future__ import annotations

_VALID_ROLES = frozenset({"coder", "planner", "researcher"})
_VALID_DELIVERY = frozenset({"merge", "deliver"})


class LoopTemplateError(ValueError):
    """A loop template violates the deterministic partition/scope rules."""


def _validate_topic(topic: dict) -> dict:
    """Validate + normalise ONE topic. Returns a normalised topic dict carrying the
    shared ``role``/``delivery``/``model``, its cross-topic ``deps``, and normalised
    member tasks. Raises ``LoopTemplateError`` on any intra-topic violation."""
    if not isinstance(topic, dict) or not topic.get("id"):
        raise LoopTemplateError("topic must be a dict with a non-empty 'id'")
    tid = topic["id"]
    if not topic.get("title"):
        raise LoopTemplateError(f"topic {tid!r} must have a 'title'")

    topic_delivery = topic.get("delivery") or "merge"
    if topic_delivery not in _VALID_DELIVERY:
        raise LoopTemplateError(
            f"topic {tid!r} delivery {topic_delivery!r} invalid "
            f"(expected one of {sorted(_VALID_DELIVERY)})"
        )

    tasks = topic.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise LoopTemplateError(f"topic {tid!r} has no member tasks")

    sigs: set = set()      # (role, delivery) — the partition signature
    models: set = set()    # distinct NON-NULL models pinned by member tasks
    for task in tasks:
        if not isinstance(task, dict) or not task.get("id"):
            raise LoopTemplateError(f"topic {tid!r} has a task with no 'id'")
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
        sigs.add((role, delivery))
        if task.get("model") is not None:
            models.add(task["model"])

    if len(sigs) != 1:
        raise LoopTemplateError(
            f"topic {tid!r} mixes (role, delivery) across its member tasks: "
            f"{sorted(sigs)} — steps sharing (role, delivery) belong in ONE topic; "
            f"differing (role, delivery) require SEPARATE topics joined by a dep edge."
        )
    if len(models) > 1:
        raise LoopTemplateError(
            f"topic {tid!r} mixes model across its member tasks: {sorted(models)} — "
            f"one topic = one pane = one model (model is stored at topic grain, §2.2). "
            f"Same-(role, delivery) steps differing only in model collapse to one "
            f"model; a genuinely different model needs a different role/delivery."
        )
    role, delivery = next(iter(sigs))
    model = next(iter(models)) if models else None  # unset tasks inherit (collapse)

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

    # Intra-topic dep closure: every TASK dep must reference a sibling member task
    # (never a foreign id, never itself). replace_edges uses INSERT OR IGNORE with no
    # FK check, so a dangling dep edge would silently wedge a task at never-'deps_ready'.
    member_ids = {tk["id"] for tk in norm_tasks}
    for tk in norm_tasks:
        for dep in tk["deps"]:
            if dep == tk["id"]:
                raise LoopTemplateError(f"task {tk['id']!r} depends on itself")
            if dep not in member_ids:
                raise LoopTemplateError(
                    f"task {tk['id']!r} dep {dep!r} is not a member task of topic "
                    f"{tid!r}"
                )

    return {
        "id": tid,
        "title": topic["title"],
        "objective": topic.get("objective") or "",
        "role": role,
        "delivery": delivery,
        "model": model,
        "deps": list(topic.get("deps") or []),
        "tasks": norm_tasks,
    }


def _validate_cross_topic_dag(norm_topics: list) -> None:
    """Validate the cross-topic dep graph: unique topic ids, every ``deps`` entry an
    EXISTING DISTINCT topic, and the graph ACYCLIC. Raises ``LoopTemplateError``.

    A dangling / self / cyclic topic edge would wedge a downstream generation at
    never-dep-ready (or loop forever), so it is refused deterministically here — the
    validator is the gate, not run time."""
    ids = [t["id"] for t in norm_topics]
    idset = set(ids)
    if len(idset) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise LoopTemplateError(f"duplicate topic id(s): {dupes}")

    adj: dict = {}
    for t in norm_topics:
        for dep in t["deps"]:
            if dep == t["id"]:
                raise LoopTemplateError(
                    f"topic {t['id']!r} depends on itself — a cross-topic edge must "
                    f"connect DISTINCT topics"
                )
            if dep not in idset:
                raise LoopTemplateError(
                    f"topic {t['id']!r} dep {dep!r} is not a topic in this template"
                )
        adj[t["id"]] = list(t["deps"])

    # Acyclicity via 3-colour DFS (white=unseen, grey=on-stack, black=done).
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {i: WHITE for i in ids}

    def _visit(node: str) -> None:
        colour[node] = GREY
        for nxt in adj[node]:
            if colour[nxt] == GREY:
                raise LoopTemplateError(
                    f"cross-topic dep cycle detected at topic {nxt!r} — the topic-DAG "
                    f"must be acyclic"
                )
            if colour[nxt] == WHITE:
                _visit(nxt)
        colour[node] = BLACK

    for i in ids:
        if colour[i] == WHITE:
            _visit(i)


def validate_loop_template(template: dict) -> dict:
    """Validate + normalise a loop template (one or more topics, spec §6).

    Returns a normalised ``{"topics": [<topic>, …]}`` where each topic carries the
    shared ``role``/``delivery``/``model``, its cross-topic ``deps``, and tasks with
    explicit ``role``/``model``/``delivery``/``deps``. Raises ``LoopTemplateError``
    (never a bare ValueError from deep inside) on any partition/scope/DAG violation.
    """
    if not isinstance(template, dict):
        raise LoopTemplateError("template must be a dict")
    topics = template.get("topics")
    if not isinstance(topics, list) or not topics:
        raise LoopTemplateError("template must contain a non-empty 'topics' list")

    norm_topics = [_validate_topic(t) for t in topics]
    _validate_cross_topic_dag(norm_topics)
    _validate_globally_unique_task_ids(norm_topics)
    return {"topics": norm_topics}


def _validate_globally_unique_task_ids(norm_topics: list) -> None:
    """Task ids must be unique ACROSS topics, not just within one (P4b, spec §6/§1).

    A generation materializes each task as ``<L#>-r<seq>-<task_id>``
    (juggle_loop_instantiate); two topics sharing a base task id collide on that node
    id — ``create_task`` INSERT OR IGNORE silently drops the second, and a crossing
    edge to ``<gen><task_id>`` becomes ambiguous. Gate it deterministically at the
    validator so a mis-decomposition is refused at create, never wedged at
    instantiation."""
    seen: dict = {}
    for t in norm_topics:
        for tk in t["tasks"]:
            prior = seen.get(tk["id"])
            if prior is not None:
                raise LoopTemplateError(
                    f"task id {tk['id']!r} is not unique across topics (in both "
                    f"{prior!r} and {t['id']!r}) — task ids must be globally unique so "
                    f"a generation's node ids (<L#>-r<seq>-<task_id>) are unambiguous."
                )
            seen[tk["id"]] = t["id"]
