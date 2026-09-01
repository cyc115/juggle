# M0 — pi semantics spike

> **For agentic workers:** execute with superpowers:executing-plans. This is a
> RESEARCH milestone: deliverables are a findings doc + a version pin + go/no-go
> flags. No production code changes.

**Goal:** replace the unverified assumptions about pi (repo:
`badlogic/pi-mono`, npm `@earendil-works/pi-coding-agent`) with experimental
evidence, so M5/M6 build on facts.

**Why:** decision-log risk item — `agent_end` abort/crash semantics, event
contract stability, and extension loading in headless modes are all
documented-but-unverified (`research/2026-08-07-pi-harness-migration.md` §7).

## Files Touched

| File | Action |
|---|---|
| `research/2026-08-XX-pi-semantics-findings.md` | Create — one section per experiment, verbatim observed output |
| `config/pi-version.lock` | Create — the exact pi version all 2.0 work targets |
| `scripts/spike/pi_*.sh` (throwaway, gitignored ok) | Create as needed |

## Tasks (sequential)

- [ ] **1. Install + pin.** `npm i -g @earendil-works/pi-coding-agent@<latest>`;
  record exact version in `config/pi-version.lock`. Configure a cheap provider
  (OpenRouter key from `~/.juggle/.env`; smallest usable model). Acceptance:
  `pi -p "say ok"` returns.
- [ ] **2. RPC lifecycle map.** Drive `pi --mode rpc` over stdio with a Python
  script: send `prompt`, capture EVERY event JSONL until settle. Repeat for:
  normal completion; `abort` mid-turn; SIGKILL of the pi process; SIGKILL of
  the *parent* script (does pi survive? does it exit?). Acceptance: findings
  doc table = event sequence per scenario, incl. whether `agent_end` fires on
  abort/crash.
- [ ] **3. steer / follow_up semantics.** While a long task runs, send `steer`
  and `follow_up`; document delivery timing (immediate interrupt vs
  next-turn) and event ordering. Acceptance: documented with captured output.
- [ ] **4. Reattach probe.** Start `pi --mode rpc` with stdin/stdout bound to
  named FIFOs; kill and restart the reader side; verify the pi process
  survives and the session can be resumed via `get_entries` cursor +
  `--session <id>`. This validates the M5 relay design. Acceptance: explicit
  YES/NO + exact mechanics that worked.
- [ ] **5. get_session_stats / session file.** Confirm token-usage fields,
  session file location + header version for this pinned build; parse one
  session JSONL end-to-end. Acceptance: field list in findings doc.
- [ ] **6. Extensions in headless modes.** Minimal extension (log
  `before_agent_start`, `tool_call`, `agent_end`): confirm whether it loads
  under `-p`, `--mode json`, `--mode rpc`. Acceptance: 3-row matrix.
- [ ] **7. Go/no-go summary.** End findings doc with: assumptions CONFIRMED /
  REFUTED / CHANGED, and required amendments to M5/M6 plans. If the FIFO
  reattach (task 4) is NO, flag loudly — M5's relay design must be redesigned
  before M5 starts.

## Definition of done

Findings doc committed; version pinned; go/no-go section present; no prod code
touched. Harness gate: full pytest green (should be untouched) — paste summary
line.
