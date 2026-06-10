# Topic-as-DAG Autonomous Executor — Verdict & Devil's Advocate

> Executive summary / feasibility verdict for the topic-DAG executor feature.
> Deep technical design (schema DDL, scheduler pseudocode, code-anchored deltas)
> lives in the sibling doc: [`2026-06-05-topic-dag-executor.md`](./2026-06-05-topic-dag-executor.md).

## Short answer

Yes, it's realistic — and it's the natural next form of what Juggle already is.
Right now **you** are the scheduler: you eyeball what's ready, dispatch, wait, merge.
This feature just promotes that loop into code with explicit edges. A topological
executor is the easy 150 lines. The hard parts are elsewhere — the devil's-advocate
pass below is about where it actually bites, because that's what determines feasibility.

## What you already have vs. what's missing

| Have (Juggle today) | Missing for a DAG executor |
|---|---|
| Topics, agents, SQLite store | `topic_dependencies(topic_id, depends_on)` edge table + cycle check |
| Pool with max-concurrency cap | Scheduler tick: compute ready-set → dispatch → recompute |
| Completion signals, watchdog | Topic status FSM: pending→ready→running→done/failed/blocked |
| Projects grouping topics | Failure propagation (block/retry dependents) |

That part is genuinely tractable. Kahn's algorithm + the existing dispatch.

## The four things that will actually hurt

**1. "Independent in the DAG" ≠ "mergeable in parallel."**
You hit this today — the `tui.py` features had to go serial because they share one
file, even though they're logically independent. A DAG of disjoint features can still
collide at the git layer. So the graph must model **integration dependencies**, not
just logical ones — or you accept a serial **merge queue** at the end
(worktree-per-topic, integrator merges one at a time, re-runs tests, *then* marks
done). Build the merge queue; it's exactly what was done by hand this session.

**2. Dependency payload / output contracts.**
When B depends on A, what does B *consume*? In Bazel it's a file at a known path.
Here the artifact is a code change or a finding. Per the SZ research, agents currently
get **ZERO topic context**. So each topic needs a declared output, and a dependent's
prompt must be hydrated with upstream `--retain`/artifacts. That wiring — not the
scheduler — is the real engineering.

**3. Completion must be machine-checkable.**
This whole session was a lesson in "closed ≠ merged ≠ verified." A graph drains on
whatever signal you trust; if that signal is "agent committed," the DAG will march
forward on false green. Each node needs a crisp predicate (**tests pass + merged**),
or autonomous long-horizon execution compounds errors.

**4. Statelessness is mandatory, not optional.**
Hours-long execution means the orchestrator's own context window is the bottleneck
(we've compacted twice today). The DAG + statuses must live in the DB; the
orchestrator becomes a thin restartable loop that reads the ready-set and dispatches.
This is the "code > prompts" principle — the graph must never live in the LLM's memory.

**Decomposition** (the LLM breaking an idea into the DAG) is the riskiest,
least-deterministic piece — LLMs under-specify interfaces and invent false
independence. Don't ship it autonomous on day one; make decomposition itself a
reviewable topic whose output is an editable graph spec with a human gate. The
executor is sound long before the auto-decomposer is.

## Prior art worth stealing from

- **Temporal** — durable, replayable workflow state in a store, workers pull tasks.
  Almost exactly the "stateless restartable scheduler over a DB" model. Most relevant.
- **Dagster / Prefect / Airflow** — DAG orchestration ergonomics, retries,
  `needs:`-style edges.
- **Bazel / Make** — content-hash change detection; could auto-derive file-collision
  edges (solves hard-part #1 mechanically).

## Verdict

Realistic, high-leverage, and incremental — ship the executor over human-authored
DAGs first, add the merge queue, then layer auto-decomposition last. The risk isn't
"can it be built"; it's "does the completion signal lie" and "do parallel branches
collide" — both solvable, both already manually solved this session.
