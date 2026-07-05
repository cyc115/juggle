---
description: Create a scheduled task — routes OS-schedule (script on a timer) vs loop (a recurring Juggle work topic), confirms the type, then creates it
allowed-tools: Bash, Read, Write
---

# /juggle:schedule:create

Unified schedule-create router. From a natural-language requirement it decides
between the **two** kinds of recurring thing Juggle can schedule, confirms the
choice with a plan card, then creates it. A loop may decompose into **one or more
topics** joined by cross-topic dep edges (partition rule in Step 2).

## Arguments

`$ARGUMENTS` — natural language, e.g.:
- `run ~/github/trading-edge/scripts/news-ingest every 15 minutes` → **OS schedule**
- `every morning at 8, research overnight AI news and write me a digest` → **loop**

## Step 1 — Understand intent, then ROUTE (OS-schedule XOR loop)

Classify the requirement into exactly ONE type:

- **OS SCHEDULE** — runs an existing **script/binary** on a timer. No Juggle agent,
  no work topic, no completion contract. Keywords: a concrete file path, "run
  <script>", "cron", ingestion/backup/sync jobs. → follow the OS-schedule backend
  reference at `${CLAUDE_PLUGIN_ROOT}/docs/schedule-os-backends.md`
  (launchd/systemd/cron — the backend playbook; do not reimplement it here).

- **LOOP** — a recurring **unit of agent work** (one or more topics) that Juggle
  dispatches, drives to completion, and re-fires each cadence. Keywords: "research/
  summarize/draft/review …", "every day/week produce …", anything that needs an agent
  rather than a fixed script. → build the loop's topic-DAG template (Step 2).

If ambiguous, ASK the user which they mean — misroute is cheap to avoid here and
expensive to unwind later.

## Step 2 (LOOP only) — Build the topic-DAG template

Decompose the requirement into a small **graph of topics** joined by cross-topic dep
edges, following the rule (enforced in CODE by `juggle_loop_template_validator`, NOT
trusted from you):

> Consecutive steps sharing `(role, delivery)` → member tasks of **ONE** topic.
> Steps differing in **`role` OR `delivery`** → **SEPARATE** topics joined by a dep
> edge (`deps` on the downstream topic naming the upstream topic id).

- **`model` is NOT a partition key.** Same-`(role, delivery)` steps that differ only
  in desired model **collapse into one topic** and share that topic's single model —
  do NOT split on model. Each topic must carry a UNIFORM model (one topic = one pane
  = one model); a topic pinning two different non-null models is rejected.
- **role** — `researcher` (read-only, no worktree), `coder`, or `planner`.
- **delivery** — `deliver` (non-merge work: a digest/report/notification whose proof
  is a `verify_cmd`/attestation, NO merge) or `merge` (lands to main, verified⟺merged).
  A "write me a digest / notify me" loop is almost always `deliver`.
- **cadence** — `every 15m` / `every 6h` / `daily at 08:00`.

The validator REJECTS a topic mixing `(role, delivery)` or `model`, a cross-topic
edge that is cyclic / self-referential / points at an unknown topic — do not try to
work around it. A single-topic loop is just a one-topic graph with no `deps`.

Write the template to a temp JSON file. The shape `loop create` instantiates today is
a **single topic** (one-topic graph, no cross-topic `deps`):

```json
{
  "topics": [
    {
      "id": "overnight-ai-digest",
      "title": "Overnight AI news digest",
      "objective": "Research overnight AI news and write a digest",
      "delivery": "deliver",
      "deps": [],
      "tasks": [
        {"id": "research", "title": "Research + write digest",
         "prompt": "Search for AI news from the last 24h and write a concise digest.",
         "role": "researcher", "model": null, "verify_cmd": null, "deps": []}
      ]
    }
  ]
}
```

If the requirement genuinely spans differing `(role, delivery)` steps, emit MULTIPLE
topics joined by cross-topic `deps` — e.g. a `researcher`/`deliver` topic feeding a
`coder`/`merge` topic:

```json
{"topics": [
  {"id": "research", "title": "Research news", "delivery": "deliver", "deps": [],
   "tasks": [{"id": "gather", "title": "Research + draft", "prompt": "…",
              "role": "researcher", "model": null, "verify_cmd": null, "deps": []}]},
  {"id": "notify", "title": "Send the digest", "delivery": "merge", "deps": ["research"],
   "tasks": [{"id": "send", "title": "Send", "prompt": "…",
              "role": "coder", "model": null, "verify_cmd": null, "deps": []}]}
]}
```

The validator + the `loop plan` confirm-card accept a multi-topic decomposition
**today**, but **`loop create` currently instantiates a SINGLE topic** — multi-topic
cross-topic instantiation (the handoff seam) lands in a later phase, and create
REFUSES a >1-topic template loudly until then.

## Step 3 — Confirm the decomposed topic-DAG (states the chosen type EXPLICITLY)

Before creating anything, render the **code-backed confirm-card** of the decomposed
topic-DAG (topics · `(role, delivery, model)` · cross-topic edges · cadence) so a
legal-but-WRONG partition is caught BEFORE the loop is frozen (its structure is
re-instantiated every fire — fixing a bad partition means delete+recreate). Mirrors
`/juggle:delegate`'s plan-card → confirm → fire:

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py loop plan \
  --template /tmp/loop-template.json --cadence "daily at 08:00"
```

Show the rendered card, NAMING the routed type (LOOP) so a misroute is overridable,
then wait for confirmation. If the user says it's the other type, re-route; if the
partition is wrong (topics that should be split/merged), fix the template and
re-render.

## Step 4 — Create

- **OS SCHEDULE** → follow the OS-schedule backend reference at
  `${CLAUDE_PLUGIN_ROOT}/docs/schedule-os-backends.md` (launchd/systemd/cron).
- **LOOP** → transactional, atomic create (single-topic template — a >1-topic
  template is refused loudly until multi-topic instantiation lands):

```bash
juggle loop create --template /tmp/loop-template.json --cadence "daily at 08:00" \
  --name "Overnight AI digest"
```

The create is ONE DB transaction — the `kind='loop'` project, the topic graph, and
the loop row (with `next_run`) are written all-or-nothing. If any step fails the
whole create rolls back (no orphan project/loop/nodes). The loop project is
excluded from P-slots/arming; the watchdog fires it on cadence (Phase 5).
