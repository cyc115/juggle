# juggle 2.0 — Roadmap

> **For agentic workers:** each milestone below has its own plan file in this
> directory. Execute ONE milestone per agent (model: sonnet) with
> superpowers:executing-plans. Do not start a milestone whose dependencies are
> not merged into `juggle-2.0`.

**Goal:** the three-layer state model (domain machine + append-only ledgers +
in-memory observer) and pi as a first-class worker harness (persistent RPC
supervisor), per the decision log in
`research/2026-08-07-pi-harness-migration.md` §8.

**Branch policy:** ALL juggle-2.0 work lands on the `juggle-2.0` branch —
never on `main`. Milestone work happens on short-lived branches off
`juggle-2.0` (`j2/<milestone-id>-<slug>`) and merges back into `juggle-2.0`
after the harness gate. `main` continues normal 1.x landings; rebase
`juggle-2.0` onto `main` after each milestone merge.

**Gates (every milestone, from CLAUDE.md):** full pytest green + doctor
`--dry-run` smoke; devil's-advocate critique before implementation; regression
pins rewritten to new seams, never weakened; ≤300-line modules; behavior and
refactor commits separated.

## Milestones

| ID | Plan | Depends on | Risk |
|----|------|-----------|------|
| M0 | `M0-pi-semantics-spike.md` — verify pi RPC/event assumptions by experiment, pin version | — | low |
| M1 | `M1-node-machine-collapse.md` — failure-state merge, `ready`/`dispatching` removal | — | medium |
| M2 | `M2-thread-projection.md` — drop thread status vocabulary, status = projection | M1 | medium |
| M3 | `M3-run-ledger-append-only.md` — drop mutable run status; agent pool derived | M1 | medium |
| M4 | `M4-integration-record.md` — proof states → append-only integration record; 6-state machine complete | M1, M3 | high |
| M5 | `M5-pi-adapter-rpc-supervisor.md` — PiAdapter, supervisor + relay, recovery pin | M0, M3 | high |
| M6 | `M6-bridge-and-gating-extensions.md` — TS hook-event bridge, role gating, completion nudge | M5 | medium |
| M7 | `M7-orchestrator-on-pi.md` — OUTLINE ONLY; planned in detail after M6 retrospective | M6 | high |

Execution order: M0 and M1 in parallel; then M2 ∥ M3; then M4 ∥ M5; then M6;
M7 last. One agent per milestone; do not split a milestone across agents.

## Non-goals (2.0 scope fence)

- No orchestrator migration before M7 — Claude Code remains the orchestrator.
- No removal of the Claude/Codex worker harnesses — dual-harness throughout;
  scraping paths stay behind the adapter capability until explicitly retired.
- No generic event store — typed ledgers only (decision 4).
