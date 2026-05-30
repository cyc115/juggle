# Project Context

Python CLI project (Claude Code plugin). Source in `src/`.

Required environment variables (no defaults):
- _JUGGLE_TEST_DB (src/juggle_cli.py)
- CLAUDE_PLUGIN_DATA (src/juggle_cli.py)
- JUGGLE_MAX_BACKGROUND_AGENTS (src/juggle_db.py)
- JUGGLE_MAX_THREADS (src/juggle_db.py)

Read graphify-out/GRAPH_REPORT.md for orientation — god nodes and community structure show WHERE things live. Then read the actual source files in src/ before implementing. The graph is a navigation aid, not an implementation guide.

See CLAUDE.md for the full design philosophy, versioning, and task-tracking conventions.
