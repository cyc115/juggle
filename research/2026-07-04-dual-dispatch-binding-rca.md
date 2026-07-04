# RCA: dual-dispatch label-binding collision (topic T-rca-dual-dispatch-binding)

**Date:** 2026-07-04
**Author:** coder agent AB (INVESTIGATE + PLAN ONLY — no fix implemented)
**Incident date:** 2026-07-03
**Symptom:** three graph-topics (`T-gp-spine`, `T-gp-refactor`, `T-gp-edit`) all
bound their `kind='dispatch'` node_edge to the **same** conversation thread
`b28610a6` (user_label `GP`). At dispatch, `graph_tick` reused that one thread for
each topic, so multiple coders were dispatched into the **same worktree**
`/tmp/juggle-juggle-GP` on branch `cyc_GP` — a concurrent-write corruption risk
(one coder aborted + stashed). `df-atomic` (79f65f3) prevents the *follow-on*
no-worktree wedge but does **not** prevent the shared binding that causes the
collision.

---

## TL;DR — root cause

The dispatch binding is **many-topics → one-thread capable by construction**.
Two independent gaps compose:

1. **Binding fan-in has no reverse-uniqueness guard (PRIMARY).** A conversation
   named repeatedly via `add-task --topic GP` is bound as the dispatch thread of
   *every* synthetic `T-<task-id>` topic it seeds. `bind_dispatch_thread` enforces
   uniqueness only on `node_id` (one topic → one thread) and **never** on
   `depends_on_id` (the thread), so N distinct topics can point their dispatch
   edge at the same conversation. **The collision is created at `add-task` time —
   not at graph load, not at dispatch.**

2. **`graph_tick` reuses a bound thread unconditionally (SECONDARY).** When a
   topic carries a live `thread_id`, the tick reuses it with no check that the
   same thread is already the dispatch target of another *dispatching/running*
   topic. The reuse path also bypasses the `MAX_THREADS` cap (that gate only
   guards `create_thread`). The worktree is keyed by the thread's `user_label`
   and persisted on the thread row, so every reuse resolves to the identical
   `/tmp/juggle-juggle-GP` directory.

Answer to the framing question *(label derivation? reuse by prefix? binding at
load vs dispatch?)*: **binding created at `add-task` time.** The `GP` label /
`cyc_GP` branch are downstream cosmetics derived from the one shared thread's
`user_label` — not a prefix-matching or label-collision bug.

---

## The binding chain (file:line)

### Step 1 — `add-task --topic GP` synthesizes a fresh topic per task
`resolve_dispatch_topic` — `src/juggle_graph_add_surfacing.py:15-34`

```python
if requested_topic and db_topics.get_topic(db, requested_topic) is not None:
    return requested_topic, False          # a REAL graph-topic → verbatim
return f"T-{task_id}", True                # else synthesize T-<task-id>, auto_create
```

`GP` is a **conversation label**, not a graph-topic, so `get_topic(db, "GP")` is
`None`. Each call therefore synthesizes a *distinct* topic id `T-<task-id>`:
`add-task gp-spine   --topic GP` → `T-gp-spine`;
`add-task gp-refactor --topic GP` → `T-gp-refactor`;
`add-task gp-edit     --topic GP` → `T-gp-edit`.

### Step 2 — each synthetic topic binds the SAME conversation as its surfacing thread
CLI call site — `src/juggle_cmd_graph_ops.py:102-103`

```python
if auto_topic:
    record_surfacing_conversation(db, topic_id, getattr(args, "topic", None))
```

`record_surfacing_conversation` — `src/juggle_graph_add_surfacing.py:37-54`

```python
conv = db.get_thread(requested_topic) or db.get_thread_by_user_label(requested_topic)
if conv is not None:
    db_topics.set_topic_thread(db, topic_id, conv["id"])
```

`get_thread_by_user_label("GP")` is a **newest-wins single-chokepoint** resolver
(`src/dbops/threads.py:214-241`) — every `--topic GP` collapses to the one live
conversation `b28610a6`. So all three synthetic topics call
`set_topic_thread(topic_id, b28610a6)`.

### Step 3 — the bind writes a dispatch edge with NO reverse-uniqueness check
`set_topic_thread` → `bind_dispatch_thread` — `src/dbops/db_topics.py:143-156`,
`src/dbops/dispatch_edge.py:20-33`

```python
def bind_dispatch_thread(conn, node_id, thread_id):
    # A node binds exactly one thread: the prior dispatch edge is replaced.
    conn.execute("DELETE FROM node_edges WHERE node_id=? AND kind=?", (node_id, DISPATCH))
    if thread_id is not None:
        conn.execute("INSERT OR IGNORE INTO node_edges (node_id, depends_on_id, kind) "
                     "VALUES (?,?,?)", (node_id, thread_id, DISPATCH))
```

The `DELETE` is keyed by `node_id`; the `INSERT OR IGNORE` dedups the triple
`(node_id, depends_on_id, kind)`. **Nothing forbids two different `node_id`s from
sharing the same `depends_on_id`.** After the three adds, `node_edges` holds:

| node_id (topic) | depends_on_id (thread) | kind |
|---|---|---|
| `T-gp-spine`    | `b28610a6` | dispatch |
| `T-gp-refactor` | `b28610a6` | dispatch |
| `T-gp-edit`     | `b28610a6` | dispatch |

The invariant is one-directional (topic → thread). The reverse (thread → topic)
is unconstrained — **this is the defect**.

### Step 4 — `graph_tick` reuses the shared thread per topic, unconditionally
`graph_tick` — `src/juggle_graph_dispatch.py:230-267`

```python
reuse_tid = topic.get("thread_id")                       # ← from the dispatch edge
thread_id = reuse_tid if (reuse_tid and db.get_thread(reuse_tid) is not None) else None
...
if thread_id is None:
    thread_id = db.create_thread(...)                    # MAX_THREADS cap only here
...
_label = (_t.get("user_label") or thread_id[:6])         # → "GP"
db.update_thread(thread_id, worktree_branch=f"cyc_{_label}")   # → cyc_GP
db_topics.set_topic_thread(db, tid, thread_id)           # re-binds b28610a6 (idempotent)
dispatch(db, thread_id, hydrate_for_topic(db, pid, topic), topic)
```

`topic["thread_id"]` is derived directly from the dispatch edge
(`_TOPIC_SELECT`, `src/dbops/db_topics.py:40-45`:
`(SELECT depends_on_id FROM node_edges WHERE node_id=nodes.id AND kind='dispatch' LIMIT 1)`).
For every one of the three ready topics that value is `b28610a6`, the thread is
alive, so **each topic takes the reuse branch** — skipping `create_thread` (and
its `MAX_THREADS` cap). There is **no guard** that `b28610a6` is already the
dispatch thread of another `dispatching`/`running` topic.

### Step 5 — reused thread → identical worktree → concurrent coders
`build_worktree_context` — `src/juggle_dispatch_worktree_context.py:34,51-66`

```python
thread_label_wt = thread_wt.get("user_label") or thread_wt["id"][:6]   # "GP"
...
existing_wt = (thread_wt.get("worktree_path") or "").strip()           # persisted on the THREAD
if not existing_wt and repo_path_wt and not allow_main:
    ok_wt, wt_path_new, branch_new, _ = _com._create_worktree(
        repo_path_wt, thread_label_wt, default_worktree_root, ...)     # /tmp/juggle-juggle-GP
    db.update_thread(thread_id, worktree_path=wt_path_new, ...)        # sticks to the thread
```

The worktree path is keyed by the thread label (`GP`) and stored on the thread
row. Because all three topics share thread `b28610a6`, they all resolve to the
identical `/tmp/juggle-juggle-GP` / `cyc_GP` — multiple coders in one worktree.

---

## Timeline (source: `~/.claude/juggle/watchdog-spawn.log`)

All events 2026-07-03. Line numbers are log lines.

| time | log ln | event |
|---|---|---|
| 19:51:16 | 24287 | `Worktree created: /tmp/juggle-juggle-GP on branch cyc_GP` — dispatch #1 into GP |
| (after)  | 24482 | `task gp-spine → verified` |
| 19:52:54 | —     | watchdog exits for respawn (plugin code advanced) |
| 21:36:44 | 26240 | `Worktree created: /tmp/juggle-juggle-GP on branch cyc_GP` — **same path re-created**, dispatch #2 |
| (after)  | 25714 | `task gp-refactor → verified` |
| 22:15:32 | 26781 | `Worktree created: /tmp/juggle-juggle-GP on branch cyc_GP` — **same path re-created**, dispatch #3 |
| (after)  | 26913 | `task gp-edit → verified` |
| 23:14:45 | 27213 | `orphaned thread b28610a6 (GP, 60 min no agent)` — the shared thread wedges |
| 23:14:51 | 27217-9 | reintegrate spawned for wedged topics `T-gp-cancel`, `T-gp-retry`, `T-gp-edit` (attempt 1) |

Corroboration: **the same worktree path `/tmp/juggle-juggle-GP` is created three
separate times** — the signature of three topics driving dispatch through one
shared thread/worktree. The log also shows further `T-gp-*` topics
(`T-gp-cancel`, `T-gp-retry`) beyond the three named in the incident, consistent
with many `add-task --topic GP` calls all fanning into `b28610a6`.

> Note: the three dispatches are serialized across watchdog respawns in this log
> excerpt rather than caught in a single 30 s tick, but the corruption class is
> the same — **the shared worktree is reused by successive/overlapping coders**.
> The incident report's "two coders concurrently / one aborted+stashed" is the
> intra-tick manifestation of the identical binding defect (two ready topics
> sharing `b28610a6` claimed in one `graph_tick` pass at `graph_dispatch.py:220`).

---

## Why `df-atomic` (79f65f3) does not cover this

`df-atomic` (`build_worktree_context` refuse-loud, worktree_context.py:71-86)
fixes a *different* failure: a coder that gets **no** worktree must not silently
reach `running`. Here every coder gets a worktree — the **same** one — so the
refuse-guard never fires. `df-atomic` bounds the follow-on wedge; the shared
binding upstream is untouched.

---

## Fix plan (DO NOT IMPLEMENT — investigation topic)

Three candidate layers, ordered by preference. A durable fix should apply **F1
(prevent)** plus **F2 (defend)**; F3 is a schema-level backstop.

### F1 — refuse a fan-in bind at the source (PRIMARY, add-task time)
In `record_surfacing_conversation` (`juggle_graph_add_surfacing.py:37-54`),
before `set_topic_thread`, reject binding a conversation that is **already** the
dispatch thread of a *different* topic. Reuse `get_topic_by_thread`
(`db_topics.py:117-124`) — it maps thread → owning topic:

```python
owner = db_topics.get_topic_by_thread(db, conv["id"])
if owner is not None and owner["id"] != topic_id:
    # a second task named the same --topic conversation → give THIS topic its own
    # surfacing thread (or leave it unbound so graph_tick mints a fresh [T-<id>]).
    return
db_topics.set_topic_thread(db, topic_id, conv["id"])
```

Design note: this preserves "dedup defect F" (one conversation ↔ its *first*
topic) while refusing the many-to-one fan-in. Decide the exact policy with the
user — options: (a) silently leave later topics unbound (fresh mirror thread at
dispatch), or (b) fail the `add-task` loudly so the operator picks distinct
`--topic` names. (a) is the lower-friction default.

### F2 — guard the reuse in `graph_tick` (SECONDARY, dispatch time)
At `juggle_graph_dispatch.py:230-233`, before reusing `reuse_tid`, verify the
thread is not already the dispatch thread of another **active**
(`dispatching`/`running`/`integrating`) topic. If it is, treat `thread_id` as
`None` (mint a fresh thread) — or defer this topic to a later tick. This makes
the tick defensive even if a stale/legacy DB already contains a fan-in binding
that F1 would only prevent going forward.

### F3 — schema backstop (optional, strongest)
Add a **partial unique index** on the dispatch relation so the DB itself refuses
a second topic pointing at a bound thread:

```sql
CREATE UNIQUE INDEX ix_dispatch_thread_unique
  ON node_edges(depends_on_id) WHERE kind = 'dispatch';
```

Caveats: (a) this is a **migration** — out of scope for an agent context per
guardrails; must be reserved via `juggle migration next` and reviewed by a human
(migration/security surface → PR). (b) Requires auditing every existing
`bind_dispatch_thread` caller so a legitimate rebind (unbind-then-rebind, e.g.
CapacityError rollback at `graph_dispatch.py:271-273`) never trips the index
mid-transaction. (c) `INSERT OR IGNORE` at `dispatch_edge.py:29` would need to
become a loud failure for the guard to be meaningful.

### Regression pin (when a fix is implemented)
Add `tests/test_dual_dispatch_binding.py` asserting: two `add-task --topic GP`
calls produce two topics whose dispatch edges point at **different** threads (or
the second is unbound), and that `graph_tick` never dispatches two topics into
one thread/worktree. Must fail RED on pre-fix code (two edges → one `b28610a6`),
name this incident in its docstring, live in the standard suite.

---

## Cited sources

- `src/juggle_graph_add_surfacing.py:15-54` — resolve_dispatch_topic + record_surfacing_conversation
- `src/juggle_cmd_graph_ops.py:74-103` — add-task CLI wiring of the two above
- `src/dbops/dispatch_edge.py:20-33` — `bind_dispatch_thread` (node_id-only uniqueness)
- `src/dbops/db_topics.py:40-45,117-124,143-156` — `_TOPIC_SELECT` thread_id derivation, `get_topic_by_thread`, `set_topic_thread`
- `src/dbops/threads.py:214-241` — `get_thread_by_user_label` newest-wins chokepoint
- `src/juggle_graph_dispatch.py:220,230-267` — `graph_tick` claim + unconditional thread reuse + label derivation
- `src/juggle_dispatch_worktree_context.py:34,51-86` — worktree keyed by thread label, persisted on thread; df-atomic refuse-guard
- `~/.claude/juggle/watchdog-spawn.log:24287,25714,26240,26781,26913,27213,27217-27219` — timeline evidence
- commit `79f65f3` (df-atomic-dispatch) — the adjacent fix that does NOT cover this
