# Brief — Multi-Project Parallel Autopilot

**Status:** Requirements brief (input to brainstorm/spec/plan). 2026-06-10.
**Author:** orchestrator (for Fable spec agent)

## Use case
Mike runs ONE Juggle Claude Code session but actively works across multiple
repos at once — e.g. **juggle**, **lifeos**, **trading-edge**. He wants each
project to have its OWN task-graph driven autonomously by autopilot, with agents
executing against all of them **concurrently** in the same session. Today he can
only arm ONE project's graph at a time; arming a second silently replaces the
first.

Target experience: arm 3 projects → 3 independent task graphs → the watchdog
tick drives ready nodes across ALL of them in parallel (subject to a global
agent budget) → cockpit shows all 3 graphs' progress → disarming one leaves the
others running.

## Current limitation (ground truth — verify in code)
- Armed project is a SCALAR settings key `autopilot_armed_project`
  (`juggle_cmd_autopilot.py:54` `set_setting(KEY, project_id)`); arming
  overwrites.
- Tick is single-project: `armed = get_armed_project(db); if not armed: return`
  (`juggle_graph_dispatch.py:197`). It only ticks that one project.
- Within ONE graph, multiple ready nodes already dispatch in parallel
  (`for node in ready:`), capped by `MAX_THREADS` / `MAX_BACKGROUND_AGENTS`.
- Stale-claim sweep, ready recompute, claim_node atomicity are all scoped to a
  single `armed` project today.

## Requirements
R1. Arm MULTIPLE projects: `autopilot arm P2`, then `arm P3` ADDS (does not
    replace). `autopilot off P2` disarms just P2; `autopilot off` disarms all.
    `autopilot status` lists every armed project + each graph's done/ready/failed.
R2. The tick iterates EVERY armed project's graph each cycle, claiming/dispatching
    ready nodes across all of them.
R3. A shared global agent budget (MAX_THREADS / MAX_BACKGROUND_AGENTS) is
    respected ACROSS projects — no single project may starve the others. Define a
    fair policy (round-robin across armed projects? per-project soft cap? weighted?).
R4. Per-project isolation preserved: claim/sweep/ready-recompute must remain
    correct per graph; a failure or disarm in one project must not affect others.
R5. Cockpit shows all armed graphs (the DAG/graph panel currently assumes one).
R6. Backward compatible: single-armed-project behavior unchanged; existing
    settings key migrates cleanly (scalar -> set/list).
R7. Hooks (`juggle_hooks_autopilot.py`) that re-inject the armed-project directive
    must reflect the multi-project set.

## Constraints / non-goals
- Agent-first: correctness must be verifiable by an agent without a human —
  deterministic CLI/JSON, unit-testable tick over a fake multi-graph DB.
- Do NOT regress single-project autopilot or the dispatch path currently being
  fixed in thread WL (cross-connection thread visibility). Assume WL's fix is
  merged; build on top.
- Keep it lean (CLAUDE.md: simplest robust solution). No distributed scheduler.

## Open questions for the spec agent to resolve via Devil's Advocate
- Fair-scheduling policy: round-robin vs per-project cap vs weighted — pick one
  with justification and a failure-mode analysis.
- Settings migration: list in the existing key vs a new `autopilot_armed_projects`
  key; how the CLI authority + cockpit read it.
- MAX_THREADS semantics: global only, or global + per-project ceiling?
- What does `arm` with no graph loaded do per-project (decompose gate UX × N)?
- Starvation/fairness edge cases when one graph has 50 ready nodes and another 2.

## Deliverables expected from the Fable agent
1. Brainstorm notes (intent/options) — superpowers:brainstorming.
2. Spec at `docs/specs/2026-06-10-multi-project-autopilot.md` WITH an explicit
   Devil's Advocate section (challenge each assumption, weakest-link, simpler
   alternative, edge cases).
3. Implementation plan at `plan/2026-06-10-multi-project-autopilot.md`
   (superpowers:writing-plans) — agent-verifiable acceptance criteria per task,
   sized for a Sonnet coder to execute via TDD.

## R8 (added 2026-06-10) — Armed graph routes ALL project work through the graph
When a project's graph is armed, any NEW work related to that project must be added as a graph node (tick-owned), never dispatched ad-hoc via send-task. Provide a CODE-ENFORCED guard: `send-task` (or get-agent dispatch) to a thread auto-assigned to an armed project should refuse/warn and point to `juggle graph add-node`, unless `--force-node`/explicit override. Narrow exceptions: (a) fixes to the graph/dispatch machinery itself; (b) pure planning/spec/research whose output IS the nodes. Include this guard in the spec + plan.
