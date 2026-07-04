# Spool dead-letter RCA — A5/A6 follow-up (2026-07-04)

**Author:** coder agent AR (thread `cyc_AR`), **INVESTIGATE + PLAN ONLY — no fix implemented**.
**Repo:** `/Users/mikechen/github/juggle` worktree `/tmp/juggle-juggle-AR` @ `bf933e0` (v1.110.0, branch `cyc_AR`).
**Scope:** follow-up on spool dead-letter defects **A5** (test-isolation leak) and **A6** (writer/applier shape drift) from `research/2026-07-02-juggle-rca.md`. Establish current state on HEAD: what dead-letters events, whether the A5/A6 isolation defects still exist, what replay/recovery exists, and whether dead-letters are surfaced or rot silently. Primary sources cited at `file:line`; the 2026-07-03 10:28–11:28 incident is reconstructed from the persisted `~/.juggle/spool/dead/` payloads.

---

## Executive summary

- **A5 (isolation leak) and A6 (shape drift) are both fixed and present on HEAD** — verified by code and by commits `2129b5d`/`f0dbe11`/`be11936`/`117e09a`. The `JUGGLE_SPOOL_DIR` override (`juggle_spool_paths.py:19`) + the fail-closed conftest guard (`tests/conftest.py:119`) close A5; `_NS_DEFAULTS` (`juggle_spool_apply.py:55`) + the superseded-replay guard (`juggle_spool_apply.py:118`) close A6 and its 07-03 successor.
- **But the *dead-letter* class is NOT resolved — it is unmanaged.** Events still dead-letter for three live reasons (handler `sys.exit(1)`, an interrupted `applying` journal state, and a supersede/advance **state-set gap**), and once a file lands in `spool/dead/` it is **surfaced exactly once** (a drain-time action item) and then **rots silently forever** — there is **no replay/recovery command, no CLI to list dead-letters, and the cockpit does not count them**.
- **The 2026-07-03 10:28–11:28 window is real and reconstructable.** `~/.juggle/spool/dead/` holds 13 dead events dated 2026-07-03; two land inside the window: `10:28:08` `agent_complete` (`SystemExit code=1`) and two `applying`-state refusals at `10:49:08` and `11:24:00`. The 10:27:41 `graph_mark_task` for `T-fix-spool-terminal-mark-noop` dead-lettered *while spooling the very fix meant to prevent it*.
- **Forensics are now partially impossible by design:** the `spool_journal` (the only per-event apply-outcome record) lives in the **tmpfs** DB (`~/.juggle/config.json` → `db.mode: "tmpfs"`) and is lost on restart, while the dead-letter *files* persist on disk — so a dead file can no longer be correlated to its journal outcome after a reboot. The disk `~/.juggle/juggle.db` has **no `spool_journal` table at all** (migration 58 never ran against it).

Highest-leverage fixes (detail in §7): (1) a `juggle spool` CLI with `list-dead` / `replay <uuid>` / `purge`; (2) surface a persistent dead-letter count in the cockpit; (3) close the advance/supersede **state-set gap** so a legitimately-terminal task never dead-letters; (4) give the `applying`-crash path a triage/replay route instead of a permanent dead-letter.

---

## 1. Current spool subsystem shape (HEAD)

Five modules, one drain path:

| Module | Role | Key symbols |
|---|---|---|
| `dbops/spool.py` | pure-FS event store | `write_event` (`:32`), `read_pending` (`:51`), `move_to_dead` (`:73`) |
| `juggle_spool_paths.py` | dir resolution | `spool_dir` (`:10`), `spool_dead_dir` (`:29`) |
| `juggle_spool_cli_common.py` | agent-context write seam | `should_spool` / `spool_event_if_agent` |
| `juggle_spool_apply.py` | watchdog-side drain/replay | `apply_event` (`:177`), `drain_spool` (`:243`) |
| `juggle_cockpit_spool_status.py` | cockpit chrome | `get_spool_status_line` (`:13`) |

Write side (agent context): a CLI write (`graph mark-task`, `agent complete/fail`, `action …`) detects agent context and **spools a JSON event instead of touching the DB** — e.g. `juggle_cmd_graph_ops.py:157` early-returns after `spool_event_if_agent("graph_mark_task", …)`.

Read/apply side (watchdog only): `drain_spool(db)` is called every tick and at watchdog startup — `juggle_watchdog_daemon.py:197` and `:435`, both wrapped so a drain bug never downs the tick. **`drain_spool` is the ONLY caller of `apply_event`; there is no other entry point** (no CLI, no manual drain, no replay). Confirmed: `grep -rn "spool" src/juggle_cli.py` → no matches; there is no `spool` subcommand registered anywhere.

---

## 2. What causes an event to dead-letter (exhaustive, cited)

A pending file leaves the spool one of four ways; only paths **(B)** move it to `spool/dead/`:

**(A) Skipped as unreadable — NOT dead-lettered.** `read_pending` swallows `JSONDecodeError/KeyError/OSError` and `continue`s (`dbops/spool.py:68`). A malformed/half-written/permission-denied file is **silently skipped every tick and never dead-lettered** — it accumulates in the pending dir forever (a separate latent gap: it inflates the cockpit `spool: N` count but nothing ever clears it).

**(B) Applied unsuccessfully → `move_to_dead`.** `drain_spool` calls `apply_event`; on `ok == False` it `move_to_dead(spool_dir(), event.path, msg)` (`juggle_spool_apply.py:262-266`). `apply_event` returns `(False, …)` in exactly two ways:

- **(B1) `applying`-state refusal** — the journal already shows `applying` for this uuid, meaning a prior apply started and the process died mid-flight; `apply_event` **refuses to blind-retry** and returns `False` (`juggle_spool_apply.py:187-193`). *This dead-letters a genuine, not-yet-applied completion.* Both 07-03 window `applying` dead-letters (10:49, 11:24) are this path.
- **(B2) Handler raised** — `_dispatch` calls the real `cmd_*` handler, which uses `sys.exit(1)` for validation; the `except BaseException` boundary (`juggle_spool_apply.py:232-240`) catches `SystemExit`/`Exception`, journals `failed`, and returns `False` with `f"{kind} during replay of {event.type}: {detail}"`. This is the `SystemExit … code=1` string on 95 of the 100 dead files.

**(C) Applied OK → unlink.** `ok == True` → `event.path.unlink` (`juggle_spool_apply.py:260-261`). Includes the `applied`, `already applied` (dup), and `superseded` sub-cases.

**Root of the `SystemExit code=1` (B2), traced:** for `graph_mark_task`, `cmd_graph_mark_task` exits 1 on either **task-not-found** (`juggle_cmd_graph_ops.py:162-164`) or **`mark_completion` raising `ValueError`** (`:171-173`). `mark_completion` raises iff the task's state is **not in `_ADVANCE_TO_INTEGRATING`** = `{open, ready, dispatching, running, integrating}` (`dbops/db_graph_marking.py:44-48`). So a `graph_mark_task` replayed against a task in **any other state** raises → `sys.exit(1)` → dead-letter — *unless* the supersede guard intercepts it first (§4).

---

## 3. A5 — test-isolation leak: **FIXED on HEAD** (verified)

A5 was: tests resolved `spool_dir()` to the real `~/.juggle/spool` and consumed/dead-lettered ~79 live prod events. On HEAD the fix is present and layered:

1. **Env override** — `spool_dir()` returns `JUGGLE_SPOOL_DIR` when set, before any config lookup (`juggle_spool_paths.py:19-21`). The env var is read at **call time**, so it defeats a forgotten monkeypatch of `spool_dir` itself.
2. **Fail-closed conftest guard** — the autouse `_isolate_spool_from_prod` fixture points every test's `JUGGLE_SPOOL_DIR` at a per-test tmp dir *and* wraps `dbops.spool.write_event/read_pending/move_to_dead` with `assert_not_prod_spool`, and **re-patches the frozen `from dbops.spool import …` bindings inside `juggle_spool_apply`** (`tests/conftest.py:119-155`; note the module-namespace subtlety at `:147-149`).
3. **Regression pins** — `tests/test_spool_isolation.py` documents the incident and pins both the override and the fail-closed raise.

Commit: `2129b5d` (`fix(spool): fail-closed test isolation for the real ~/.juggle/spool`). **Conclusion: the A5 isolation defect does not exist on HEAD.** The 86 dead files dated 2026-07-02 are the *residue* of A5 (pre-fix); they were never cleaned up (see §6/D1).

---

## 4. A6 — writer/applier shape drift: **FIXED on HEAD** (verified), plus its 07-03 successor

A6 was: replay built an `argparse.Namespace` missing attrs the handler read → `AttributeError` before the handler could validate. On HEAD:

- **`_NS_DEFAULTS`** seeds every attribute any replayed handler reads (`juggle_spool_apply.py:55-61`); `_ns` layers defaults → top-level `thread_id` → event args (`:64-70`) so a partial event degrades to the handler's *own* `sys.exit(1)` validation (a clean dead-letter) instead of a pre-validation `AttributeError`. Pinned by `test_spool_apply_event_shape` (writer-args ⊆ defaults ∪ `{thread_id}`).
- Commits `f0dbe11` + `be11936`.

**07-03 successor defect (also fixed):** the first live drains after A6 produced a *new* dead-letter class — mark/complete replays whose target had **already** been reconciled to the same-sign terminal state hit `mark_completion`'s `ValueError` guard and dead-lettered as false alarms. Fixed by `_superseded_replay` (`juggle_spool_apply.py:118-158`) + `apply_event`'s pre-dispatch check (`:195-200`): if the target task/topic already sits in the intended terminal set, journal `superseded`, emit a watchdog-only row, **no dead-letter**. Commit `117e09a`. Terminal sets: `_SUCCESS_TERMINAL = {verified, integrated-unlanded}`, `_FAILURE_TERMINAL = {failed-exec, failed-integration, failed-verify, blocked-failed}` (`:112-115`).

**Residual gap in the supersede fix — see D3 below.** The supersede sets and the advance set are **not complementary**, so a state gap remains where a legitimate replay still dead-letters.

---

## 5. Replay / recovery: **none exists** (root finding)

- **No dead-letter replay.** Nothing reads `spool/dead/`. `grep -rn "dead" src/` shows only *writers* (`move_to_dead`) and doc comments — no reader, no requeue, no `recover`. A dead file is terminal.
- **No manual drain / no CLI.** `drain_spool`/`apply_event` are reachable only from the watchdog tick (`juggle_watchdog_daemon.py:197,435`). There is no `juggle spool …` command of any kind.
- **`applying`-crash has no exit.** (B1) is designed to refuse blind-retry because some handlers have non-transactional side effects (git integrate/push). Correct as a safety stance — but the event is then **dead-lettered with no triage/replay path**, so a *real completion whose apply was merely interrupted* (not actually side-effected) is lost and must be hand-reconciled from git, exactly as the 07-02 RCA describes.
- **The idempotency backstop is the journal, which is ephemeral.** `apply_event` keys idempotency on `spool_journal` (`juggle_spool_apply.py:184`). With `db.mode: "tmpfs"` that table lives in RAM; on restart the journal is empty, so a persisted-but-not-yet-unlinked file could in principle re-apply. More importantly for *this* RCA: after a restart you **cannot** look up what outcome a given dead file got (§6).

---

## 6. The 2026-07-03 10:28–11:28 incident — reconstruction

`~/.juggle/spool/dead/` holds **100** dead files: **86** dated `20260702` (A5 residue), **13** dated `20260703`, 1 test-fixture (`irrelevant.json`). Reason tally: **95 `SystemExit`**, 2 `applying`, 2 `AttributeError` (the original A6 pair), 1 `boom` (a test fixture).

The 13 dead events of 2026-07-03, by event `created_at`:

| created_at | type | dead_reason (class) |
|---|---|---|
| 05:06:58 | graph_mark_task | SystemExit code=1 |
| 05:22:26 | graph_mark_task | SystemExit code=1 |
| 05:24:32 | agent_complete | SystemExit code=1 |
| 06:10:59 | agent_complete | SystemExit code=1 |
| 07:33:23 | agent_complete | SystemExit code=1 |
| 07:49:49 | graph_mark_task | SystemExit code=1 |
| 07:57:25 | agent_complete | SystemExit code=1 |
| 09:09:17 | graph_mark_task | SystemExit code=1 |
| 09:09:17 | graph_mark_task | SystemExit code=1 |
| **10:27:41** | **graph_mark_task** | **SystemExit code=1** |
| **10:28:08** | **agent_complete** | **SystemExit code=1** |
| **10:49:08** | **agent_complete** | **`applying`-state refusal** |
| **11:24:00** | **agent_complete** | **`applying`-state refusal** |

The bold four fall in/adjacent to the reported window. Notable: the `10:27:41` `graph_mark_task` + `10:28:08` `agent_complete` are the **same agent finishing `T-fix-spool-terminal-mark-noop`** (the supersede fix itself) — its `result_summary` and `handoff` are intact in the payloads. They dead-lettered because at replay the target was in a state outside `_ADVANCE_TO_INTEGRATING` yet not caught by the supersede guard (the D3 gap), or the topic/task had already been hand-reconciled. The `10:49` + `11:24` `applying` refusals are the (B1) crash path — two real completions whose apply was interrupted mid-flight and then permanently dead-lettered.

**Why the journal can't confirm the exact per-event outcome:** `~/.juggle/config.json` has `db.mode: "tmpfs"`, and the on-disk `~/.juggle/juggle.db` has **no `spool_journal` table** (`.tables` shows none; `PRAGMA user_version = 0`; no `schema_migrations`). Migration 58 that installs `spool_journal` (`dbops/migrations_tail.py:17-23`) never ran against the disk DB. So the authoritative apply-outcome ledger for the window is gone — only the dead **files** survive. That mismatch (ephemeral journal, persistent dead files) is itself a defect (D4).

---

## 7. Root-cause defects still open on HEAD + fix plan (DO NOT IMPLEMENT)

Ordered by leverage. Each names the primary source and a minimal fix.

### D1 — Dead-letters rot silently (no recovery, no persistent surfacing) — **HIGH**
**Root cause:** dead files are terminal. Surfaced *once* at drain via `_file_dead_letter_action_items` (`juggle_spool_apply.py:278-299`, capped at 3 items + 1 grouped overflow), then never again. The cockpit's `get_spool_status_line` counts only `read_pending(spool_dir)` = pending `*.json` in the top dir, **excluding the `dead/` subdir** (`juggle_cockpit_spool_status.py:23`; `read_pending` is non-recursive glob `*.json`, `dbops/spool.py:57`). 100 dead events sat invisibly for two days.
**Fix plan:**
1. Add a `juggle spool` command group: `list-dead [--json]` (read `spool_dead_dir()`, print uuid/type/created_at/dead_reason), `replay <uuid|--all>` (re-inject a dead file into the pending dir *after* clearing its `failed` journal row, or call `apply_event` directly with a `--force` that overrides the `applying` guard), and `purge [--before DATE]`.
2. Extend the cockpit status to `spool: P (dead: D)` — count `spool_dead_dir()/*.json`; alert when `D > 0`.
3. Re-file a low-frequency (e.g. daily) reminder action item while `dead/` is non-empty, so a silent backlog can't persist.

### D2 — `applying`-crash permanently dead-letters real completions — **HIGH**
**Root cause:** (B1) at `juggle_spool_apply.py:187-193` refuses retry and the event dead-letters with no route back. Correct to refuse *blind* retry (non-transactional handlers), but wrong to make it terminal — the 10:49/11:24 window events were genuine completions.
**Fix plan:** on an `applying`-state event, do **not** silently dead-letter as normal noise; instead move it to a distinct `spool/needs-triage/` (or tag `dead_reason` with a machine-readable `code=applying-interrupted`) and file a **dedicated HIGH** action item that names the thread/task, since these are almost always recoverable by hand or by a guarded replay. Pair with the D1 `replay --force` path so triage is one command. Consider making the side-effecting handlers idempotent enough that a guarded retry is safe (longer-term).

### D3 — Supersede/advance **state-set gap** → spurious `SystemExit` dead-letters — **MED**
**Root cause:** `mark_completion` accepts only `_ADVANCE_TO_INTEGRATING = {open, ready, dispatching, running, integrating}` (`dbops/db_graph_marking.py:18-25,44-48`); the supersede guard catches only `_SUCCESS_TERMINAL ∪ _FAILURE_TERMINAL` (`juggle_spool_apply.py:112-115`). These two sets are **not complementary** — any task state that is neither advanceable nor in a supersede set (e.g. an intermediate/other terminal string the watchdog reconciled the task into, or a same-sign state whose *string* differs from the frozenset literals) makes a legitimate replay raise `ValueError` → `sys.exit(1)` → dead-letter. This is the likely mechanism behind several of the 07-03 `SystemExit` dead-letters, including 10:27/10:28.
**Fix plan:** enumerate the *complete* task state set (single source, `dbops/db_graph` state machine) and assert at test time that `advanceable ∪ success_terminal ∪ failure_terminal == all_states`; add a pin so a new state string can never silently fall into the gap. For the mark/complete replay specifically, treat "target in *any* terminal state that agrees in sign with the event" as superseded, driven off the state machine's terminal classification rather than a hand-maintained frozenset literal.

### D4 — Ephemeral journal vs persistent dead files → no post-mortem correlation — **MED**
**Root cause:** `spool_journal` lives in the tmpfs DB (`db.mode: "tmpfs"`); dead files persist on disk. After a restart a dead file's journal outcome/timestamps are unrecoverable, and idempotency resets (a persisted-but-un-unlinked file could re-apply). The disk DB never ran migration 58.
**Fix plan:** stamp each dead file with the full failure context at `move_to_dead` time — already writes `dead_reason` (`dbops/spool.py:80`); also write `journal_outcome`, `applied_at`, and the drain's boot-HEAD so the file is self-describing without the journal. Separately, evaluate whether the spool journal should live in a **durable** (non-tmpfs) sidecar DB so the at-least-once idempotency guarantee survives a restart; today a tmpfs journal + persistent files is the weakest combination.

### D5 — Unreadable pending files never clear — **LOW**
**Root cause:** `read_pending` `continue`s past `JSONDecodeError/KeyError/OSError` (`dbops/spool.py:68`) — a corrupt/half-written pending file is skipped every tick forever, inflating `spool: N` and never dead-lettering.
**Fix plan:** in `drain_spool`, after the pending pass, sweep the dir for `*.json` that `read_pending` could not parse (age > N ticks) and `move_to_dead` them with `dead_reason="unparseable pending file"` so they leave the pending count and become visible via D1.

---

## 8. Verification notes

- A5/A6 fixes confirmed by reading HEAD source (`juggle_spool_paths.py`, `tests/conftest.py`, `juggle_spool_apply.py`) and `git log` (`2129b5d`, `f0dbe11`, `be11936`, `117e09a`).
- Dead-letter forensics read **read-only** from `~/.juggle/spool/dead/` (100 files) and `~/.juggle/config.json`; no prod state was modified, no DB opened read-write, no migration run.
- The live `spool_journal` for the window is unrecoverable (tmpfs, cleared); findings about the window rely on the persisted dead-file payloads (`created_at`, `type`, `dead_reason`, `args`) and the code paths that produce each `dead_reason` string.
- **No code was changed** — this task is investigate + plan only.
