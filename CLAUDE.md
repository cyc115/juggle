# Project Context

Python CLI project (Claude Code plugin). Source in `src/`.

Required environment variables (no defaults):
- _JUGGLE_TEST_DB, CLAUDE_PLUGIN_DATA (juggle_cli.py)
- JUGGLE_MAX_BACKGROUND_AGENTS, JUGGLE_MAX_THREADS (juggle_db.py)

# Testing

Most tests use an isolated `tmp_path` DB and need no setup. Tests that exercise
the hooks (e.g. `test_juggle_hooks.py`) run against the **shared** DB at
`~/.claude/juggle/juggle.db` (the path `juggle_hooks.DB_PATH` resolves from
`paths.data_dir`, independent of `_JUGGLE_TEST_DB`). Without setup they fail with
`no such table: session`; some also assert active/inactive state.

Set up the shared DB once per fresh checkout/container before running the suite:

```bash
export _JUGGLE_TEST_DB="$HOME/.claude/juggle/juggle.db"   # point CLI at the shared DB
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle"
export JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run python src/juggle_cli.py init-db   # create all tables (fresh DB)
uv run python src/juggle_cli.py start     # activate session (hook tests need active state)
uv run pytest -q
```

Notes:
- `juggle:doctor` only **migrates** an existing/stale DB — it does NOT create a
  fresh one (it prints "will be created on first juggle command"). Use `init-db`
  on a fresh checkout, then `doctor` for schema migrations on an existing DB.
- The hook tests share the active-state of the one DB, so a few that assert
  `juggle inactive` will fail once `start` has activated it — a known
  test-isolation limitation, unrelated to product code.

## Cockpit Development

Use `uv run src/juggle_cli.py cockpit --out` to render the cockpit to stdout for visual inspection and debugging without needing a live tmux session. Always run this after cockpit layout changes to verify rendering.

Use `uv run src/juggle_cli.py cockpit --screenshot /tmp/cockpit.png` to save a PNG image of the cockpit (via Rich SVG + cairosvg). Claude can then `Read /tmp/cockpit.png` for visual debugging. SVG is also supported: `--screenshot /tmp/cockpit.svg`.

## Cockpit Viewport Matrix

After any cockpit layout change, run the smoke harness against all viewports:

```bash
uv run src/juggle_cli.py cockpit --smoke --all-viewports
```

Profiles live in `config/viewports.yaml` (7 profiles: 2k_full 240×67, 2k_half 120×67, 2k_third 80×67, portrait 110×130, custom_1/2/3). All must pass overflow, real-estate, and chrome checks before merging. Frame dumps land in `data/cockpit-viewport-review/` (gitignored except `.gitkeep`).

# Design Philosophy

**Core principle:** juggle is a thin, reliable orchestrator — behaviour lives in deterministic code (one source of truth), not prompts, and is built by reusing existing primitives rather than adding abstractions.

- **Code over prompts.** Logic and behavioral rules go in code or hooks — never prompt-only. Prompts can be forgotten; CLI commands and hooks cannot.
- **Lightweight orchestrator.** Reuse DB tables, CLI patterns, tmux primitives before new abstractions. Cut to simplest viable approach.
- **Simple ≠ MVP.** Production quality, minimal new concepts.
- **Reliability.** Explicit state, deterministic code paths, fail-loud errors.

# Versioning

After every major implementation:
1. Bump `version` in `.claude-plugin/plugin.json` (patch = bug/minor, minor = feature)
2. Commit with `feat:`/`fix:` prefix and version in body
3. Mark done in `TODO.md` (repo root)

# Task Tracking

Track in `TODO.md` (repo root):
- New: `- [ ] <description>`
- In-progress: prefix with `🔄 [IN PLANNING]` or `🔄 [IN PROGRESS]`
- Done: `- [x] <description> ✅ YYYY-MM-DD` (move to Done section)

## Directives
- **Devil's advocate after every implementation:** After any code change is complete, run a critique pass before reporting done.
- **Graphify before grepping:** For any search spanning more than one file or module, prefer graphify over grep. Common subcommands: `graphify query "<question>"` (semantic search), `graphify path "<A>" "<B>"` (trace relationship), `graphify explain "<concept>"` (summarize node). Fall back to grep only for exact symbol lookups in a known file.

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep — these traverse the graph's EXTRACTED + INFERRED edges instead of scanning files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)

## Harness smoke-test gate (mandatory)
Every change/feature MUST be verified with the repo's harness smoke suite before completion or merge:
- full `pytest` green, plus `juggle_cli.py doctor --dry-run` smoke against a tmp DB
- cockpit/TUI changes: run the viewport smoke harness (`--smoke`)
Paste the suite summary line as evidence in the completion result. Completion claims without harness evidence are invalid.

## Architecture gate (mandatory, every iteration)
Act as a senior architect on every build pass:
- **Small, single-purpose files.** Target ≤300 lines/module; an agent should grasp a module without reading the whole file. When a feature touches a file that has outgrown its purpose, EXTRACT first (separate refactor commit, tests green), then add the feature.
- **Refactor pass per iteration:** before completing, scan the files you touched — split mixed-concern modules, extract shared helpers, kill dead code. Pure-mechanical refactor commits are separate from behavior commits.
- Module boundaries follow domain seams (ingest / signals / screening / panels / state), not convenience.

## Regression-pin gate (mandatory)
Every bug/regression fix MUST add a specific pinned test that (a) fails on the pre-fix code (demonstrate RED before fixing), (b) names the incident in its docstring (date + one-line symptom), and (c) lives in the standard suite (not a skipped/optional marker). These pins are the refactor safety net: refactors MUST keep all regression pins green, and a pin may never be deleted or weakened without explicit user approval — if a refactor makes a pin obsolete, rewrite it to assert the same behavior through the new seam.
