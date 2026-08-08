# M5 — PiAdapter + RPC supervisor

> **For agentic workers:** execute with superpowers:executing-plans. Depends on
> M0 (semantics confirmed, version pinned) + M3 (ledger, lease table) merged
> into `juggle-2.0`. If M0 refuted the FIFO-reattach assumption, STOP and
> escalate — the relay design below assumes it holds.

**Goal:** pi becomes a first-class worker harness: `src/harnesses/pi.py`
adapter + a persistent-RPC supervisor (`src/juggle_pi_supervisor.py`) that owns
`pi --mode rpc` children through a FIFO relay, translates pi events into
domain transitions + ledger appends, and replaces ALL pane-scraping for pi
agents. Decision: persistent RPC (decision 2) with its required elements —
relay, sole-dispatcher lease, recovery-first.

**Why:** decision log §8.2. pi RPC is stdio-bound; without the relay a
supervisor crash kills every worker (weakest-item finding).

## Files Touched

| File | Action |
|---|---|
| `src/harnesses/pi.py` | Create — adapter: launch via supervisor, capabilities (`supports_hooks:false` until M6, `structured_state:true` NEW capability), model namespace validation |
| `src/juggle_harness.py`, `juggle_harness_defaults.py` | Modify — `structured_state` capability seam; pi defaults (pinned version check) |
| `src/juggle_pi_supervisor.py` | Create — child lifecycle: spawn `pi --mode rpc` with stdin/stdout on named FIFOs under `~/.juggle/pi/<agent>/`; event pump; dispatch (`prompt`), stall nudge (`steer`, backoff [0,5m,15m,30m]), bounded completion follow-up |
| `src/juggle_pi_recovery.py` | Create — restart reconcile: process table + FIFO reopen + `get_entries` cursor replay + open-run ledger diff |
| `src/juggle_pi_events.py` | Create — pi event → {domain event, ledger append} translation (pure) |
| `src/juggle_watchdog_daemon.py` | Modify — pi agents: consume supervisor state, SKIP pane classification entirely |
| `src/dbops/runs.py` | Modify — close via `agent_end` + `get_session_stats` tokens into `session_ref` path |
| `tests/test_harness_conformance.py` | Modify — pi passes C1–C9 (markers = sentinels; C7 satisfied via declared `external_restriction` UNTIL M6 gating lands — explicit, never silent) |
| `tests/test_pi_supervisor_recovery.py` | Create — THE kill-and-restart pin |

## Tasks (sequential)

- [ ] **1. Recovery pin FIRST (RED).** Test: seed 2 fake RPC children (mock pi
  emitting scripted JSONL over FIFOs), kill supervisor mid-task, restart →
  full state reconstructed, zero duplicate domain events, zero lost
  completions. This pin gates everything else in M5.
- [ ] **2. Lease enforcement.** Supervisor start = CAS on `dispatcher_lease`
  (M3 table) + heartbeat; stale lease (dead pid) is claimable. RED test: second
  supervisor refuses to start.
- [ ] **3. Relay + child lifecycle.** Spawn under `setsid` with FIFO-bound
  stdio (per M0 task-4 mechanics); supervisor death leaves children running;
  reopen on restart. Env: standard juggle identity vars via `_env_prefix`.
- [ ] **4. Event translation (pure, test-first).** `agent_end` + completion
  recorded → close run + domain event; `agent_end` without completion →
  follow_up nudge ×N → `exec_fail`; no-event-for-T while running → `steer`
  nudge (derived condition, never persisted); child exit code ≠ 0 →
  `spawn_fail`/`exec_fail` per phase. Loop guard = explicit counter.
- [ ] **5. Adapter + dispatch integration.** `get_adapter("pi")` routes
  dispatch through the supervisor instead of tmux paste;
  `tmux` pane (optional) only ever *views* (`pi --session <id>` attach is out
  of scope; a log-tail pane is fine). Conformance C1–C9 green.
- [ ] **6. Tokens.** On close, `get_session_stats` → run ledger; delete no
  Claude-path code (dual-harness).
- [ ] **7. Watchdog integration.** Pi agents: state from supervisor; assert
  paneparse modules are UNREACHED for pi (test with a spy).

## Enforcement

Recovery = pinned test (code). Lease = schema + CAS (code). Nudge bounds =
code counters. Restriction gap = DECLARED `external_restriction` in config
until M6 (visible in `juggle agent tools` output) — flagged, never silent.

## Definition of done

Full pytest green incl. recovery pin; conformance green with pi discovered;
`doctor --dry-run` smoke; a live smoke script dispatching one real pi task
end-to-end against the pinned version (manual evidence in PR); paste suite
summary line.
