# Project Context

Python CLI project (Claude Code plugin). Source in `src/`.

Required environment variables (no defaults):
- _JUGGLE_TEST_DB, CLAUDE_PLUGIN_DATA (juggle_cli.py)
- JUGGLE_MAX_BACKGROUND_AGENTS, JUGGLE_MAX_THREADS (juggle_db.py)

# Design Philosophy

- **Code over prompts.** Logic and behavioral rules go in code or hooks — never prompt-only. Prompts can be forgotten; CLI commands and hooks cannot.
- **Lightweight orchestrator.** Reuse DB tables, CLI patterns, tmux primitives before new abstractions. Cut to simplest viable approach.
- **Simple ≠ MVP.** Production quality, minimal new concepts.
- **Reliability.** Explicit state, deterministic code paths, fail-loud errors.

# Versioning

After every major implementation:
1. Bump `version` in `.claude-plugin/plugin.json` (patch = bug/minor, minor = feature)
2. Commit with `feat:`/`fix:` prefix and version in body
3. Mark done in `/Users/mikechen/Documents/personal/projects/juggle/TODO.md`

# Task Tracking

Track in `/Users/mikechen/Documents/personal/projects/juggle/TODO.md`:
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
