---
description: Create a scheduled task — routes OS-schedule (script on a timer) vs loop (a recurring Juggle work topic), confirms the type, then creates it
allowed-tools: Bash, Read, Write
---

# /juggle:schedule:create

Unified schedule-create router. From a natural-language requirement it decides
between the **two** kinds of recurring thing Juggle can schedule, confirms the
choice with a plan card, then creates it. V1 loops are **single-topic** only.

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

- **LOOP** — a recurring **unit of agent work** (a topic) that Juggle dispatches,
  drives to completion, and re-fires each cadence. Keywords: "research/summarize/
  draft/review …", "every day/week produce …", anything that needs an agent rather
  than a fixed script. → build a single-topic loop template (Step 2).

If ambiguous, ASK the user which they mean — misroute is cheap to avoid here and
expensive to unwind later.

## Step 2 (LOOP only) — Build the single-topic template

Compose ONE topic with one or more member tasks that **all share the same
`(role, model, delivery)`**. The deterministic validator
(`juggle_loop_template_validator`) REJECTS a mixed-signature or multi-topic
template — do not try to work around it; a differing signature means it is not a V1
loop.

- **role** — `researcher` (read-only, no worktree), `coder`, or `planner`.
- **delivery** — `deliver` (non-merge work: a digest/report/notification whose proof
  is a `verify_cmd`/attestation, NO merge) or `merge` (lands to main, verified⟺merged).
  A "write me a digest / notify me" loop is almost always `deliver`.
- **cadence** — `every 15m` / `every 6h` / `daily at 08:00`.

Write the template to a temp JSON file:

```json
{
  "topics": [
    {
      "id": "overnight-ai-digest",
      "title": "Overnight AI news digest",
      "objective": "Research overnight AI news and write a digest",
      "delivery": "deliver",
      "tasks": [
        {"id": "research", "title": "Research + write digest",
         "prompt": "Search for AI news from the last 24h and write a concise digest.",
         "role": "researcher", "model": null, "verify_cmd": null, "deps": []}
      ]
    }
  ]
}
```

## Step 3 — Confirm the plan card (states the chosen type EXPLICITLY)

Before creating anything, show the user a plan card that NAMES the routed type so a
misroute is overridable:

```
Type:     LOOP  (recurring agent work topic)   ← or:  OS SCHEDULE (script on a timer)
Cadence:  daily at 08:00
Topic:    overnight-ai-digest — Overnight AI news digest
Role:     researcher      Delivery: deliver (no merge)
```

Wait for confirmation. If the user says it's the other type, re-route.

## Step 4 — Create

- **OS SCHEDULE** → follow the OS-schedule backend reference at
  `${CLAUDE_PLUGIN_ROOT}/docs/schedule-os-backends.md` (launchd/systemd/cron).
- **LOOP** → transactional, atomic create:

```bash
juggle loop create --template /tmp/loop-template.json --cadence "daily at 08:00" \
  --name "Overnight AI digest"
```

The create is ONE DB transaction — the `kind='loop'` project, the topic graph, and
the loop row (with `next_run`) are written all-or-nothing. If any step fails the
whole create rolls back (no orphan project/loop/nodes). The loop project is
excluded from P-slots/arming; the watchdog fires it on cadence (Phase 5).
