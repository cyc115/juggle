---
name: vcs-backend-init
description: Make juggle work on this repo's VCS — detect, bind/generate a backend, validate via the conformance kit
allowed-tools: Bash
---

# /juggle:vcs-backend-init — Set up a VCS backend for a repo

Run once on a repo to make juggle drive its VCS + landing workflow: detect the
VCS, bind the git builtin / scaffold a bundled recipe / (for an unknown VCS) fall
back to a seeded coder agent, write the juggle config, and validate against the
conformance kit — printing a green readiness checklist or failing loud.

All logic lives in the deterministic `juggle vcs` CLI (code-over-prompts); this
command is thin orchestration over it. Optional args: `--repo PATH` (default cwd),
`--recipe NAME` / `--vcs NAME` to override detection.

## Execution

Run the full flow (detect → scaffold → configure → conformance → readiness):

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py vcs init $ARGUMENTS
```

- Exit 0 with "✅ juggle is configured for this repo." → done.
- Exit 1 → resolve the ❌ items and re-run. The command NEVER reports ready on a
  red conformance run.

## Unknown VCS (agentic fallback)

If `init` reports the VCS is unknown (no builtin, no bundled recipe), implement a
plugin from the durable contract, then re-run with the new recipe:

1. Read `${CLAUDE_PLUGIN_ROOT}/docs/create-your-own-vcs-backend.md` (the 15-method
   contract) and inspect the target VCS CLI (`<vcs> --help`).
2. Write `~/.juggle/vcs_plugins/<name>.py` implementing every method; iterate it
   against the conformance kit until green (that doc's §4 is the done-gate).
3. `uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py vcs init --repo PATH --vcs <name>`.

Escalate to the user only for facts you cannot infer (e.g. the review-submit
command).

## Individual steps (debugging)

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py vcs detect --repo PATH
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py vcs scaffold sapling
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py vcs configure --repo PATH --vcs sapling --trunk remote/main --async-land true
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py vcs conformance --backend git   # or: toy
```

## After init

Report to the user: the detected VCS, the readiness checklist result, and — for a
recipe/agentic backend — that behavioral conformance must be run against the real
VCS in a plugin repo (juggle CI ships harnesses only for `git` and `toy`).
