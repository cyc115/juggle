# Juggle Configuration Reference

**Config file:** `~/.juggle/config.json` (JSON)  
**Env override:** `_JUGGLE_CONFIG_PATH=/path/to/config.json`  
**Load order:** built-in DEFAULTS → `~/.juggle/config.json` → env var overrides (deep-merge; omit keys to inherit defaults)

Copy `config.json.example` from the repo root to `~/.juggle/config.json` and edit.

---

## Env-only overrides (not in config.json)

| Env var | Maps to | Default |
|---|---|---|
| `JUGGLE_MAX_THREADS` | `max_threads` | `10` |
| `JUGGLE_MAX_BACKGROUND_AGENTS` | `max_agents` | `20` |
| `JUGGLE_IDLE_THRESHOLD_SECS` | `tmux.agent_idle_detection_secs` | `30` |
| `JUGGLE_READY_POLL_ATTEMPTS` | `tmux.ready_poll_attempts` | `120` |
| `JUGGLE_READY_POLL_INTERVAL_SECS` | `tmux.ready_poll_interval_secs` | `1` |
| `JUGGLE_DB_PATH` | Absolute DB path override | *(computed from `paths.data_dir`)* |

---

## Global Limits

| Key | Type | Default | Description |
|---|---|---|---|
| `max_threads` | int | `10` | Max concurrent threads (also: `JUGGLE_MAX_THREADS`) |
| `max_agents` | int | `20` | Max concurrent background agents (also: `JUGGLE_MAX_BACKGROUND_AGENTS`) |
| `agent_idle_ttl_secs` | int | `43200` | Seconds before an idle agent is reaped (12 h) |
| `agent_boot_grace_secs` | int | `120` | Seconds a newly-spawned agent has before the watchdog considers it stale |
| `message_history_token_budget` | int | `1500` | Token budget for injected message history |
| `context_injection_char_limit` | int | `8000` | Max chars injected into agent context |
| `context_teaser_chars` | int | `80` | Chars shown in thread teasers |
| `stale_summary_message_threshold` | int | `3` | New messages before a summary is considered stale |
| `summary_max_chars` | int | `250` | Max chars in a thread summary |
| `thread_idle_threshold_secs` | int | `1800` | Seconds of inactivity before a thread is "idle" |
| `thread_archive_threshold_secs` | int | `172800` | Seconds of inactivity before a thread is archive-eligible (48 h) |

---

## Paths

| Key | Type | Default | Description |
|---|---|---|---|
| `paths.data_dir` | string | `~/.claude/juggle` | DB, log, and data files |
| `paths.config_dir` | string | `~/.juggle` | Config, `.env`, and flag files |
| `paths.digest_log_dir` | string | `~/.juggle/logs` | Digest log output directory |
| `paths.vault` | string | `/Documents/personal` | Obsidian vault root |
| `paths.vault_name` | string | `""` | Vault display name |

---

## Tmux

| Key | Type | Default | Description |
|---|---|---|---|
| `tmux.session_name` | string | `"juggle"` | tmux session name |
| `tmux.session_width` | int | `220` | Pane width in columns |
| `tmux.session_height` | int | `50` | Pane height in rows |
| `tmux.agent_idle_detection_secs` | int | `30` | Seconds of pane silence before agent is considered idle |
| `tmux.ready_poll_attempts` | int | `120` | Max polls waiting for agent readiness marker |
| `tmux.ready_poll_interval_secs` | float | `1` | Seconds between readiness polls |

---

## Cockpit

| Key | Type | Default | Description |
|---|---|---|---|
| `cockpit.refresh_interval_secs` | float | `1.0` | TUI refresh rate |
| `cockpit.column_ratios` | list[float] | `[0.30, 0.40, 0.30]` | Three-column width ratios (must sum to 1.0) |
| `cockpit.notification_ratio` | int | `30` | Notification panel height as % of screen |
| `cockpit.bell` | bool | `true` | Ring terminal bell on notification |
| `cockpit.desktop_notifications` | bool | `false` | Send OS desktop notifications |

---

## DB Mode (tmpfs)

Opt-in RAM-disk mode to protect against SQLite corruption on btrfs/zfs. Effective on Linux only — macOS falls back to `direct` automatically.

| Key | Type | Default | Allowed | Description |
|---|---|---|---|---|
| `db.mode` | string | `"direct"` | `"direct"`, `"tmpfs"` | `direct` = normal on-disk DB; `tmpfs` = live DB in RAM, flushed to disk periodically |
| `db.tmpfs_dir` | string | `"/dev/shm"` | any writable path | Directory for the live in-RAM DB (Linux `/dev/shm` recommended) |
| `db.flush_interval_s` | int | `10` | any positive int | Seconds between automatic flushes from RAM to durable disk |

To enable: set `db.mode = "tmpfs"` and run `juggle db-flush --install-supervisor` to install the flush daemon.

---

## Hindsight Memory

Long-term memory service (separate Docker container). Get a key at https://openrouter.ai/keys; store it in `~/.juggle/.env` as `OPENROUTER_KEY` — never in `config.json`.

| Key | Type | Default | Description |
|---|---|---|---|
| `hindsight.enabled` | bool | `false` | Enable Hindsight memory integration |
| `hindsight.api_url` | string | `"http://localhost:18888"` | Hindsight service URL |
| `hindsight.api_key` | string | `"juggle"` | Hindsight API key |
| `hindsight.bank` | string | `"juggle"` | Memory bank name |
| `hindsight.timeout_secs` | int | `10` | HTTP timeout for recall/retain calls |
| `hindsight.reflect_timeout_secs` | int | `60` | HTTP timeout for reflect (summarization) calls |

---

## Talkback TTS

| Key | Type | Default | Description |
|---|---|---|---|
| `talkback.enabled` | bool | `false` | Enable text-to-speech read-back of notifications |
| `talkback.port` | int | `18787` | Local TTS service port |

---

## Title Generation

Auto-generates thread titles via LLM. API key stored in `~/.juggle/.env` as `OPENROUTER_KEY`.

| Key | Type | Default | Description |
|---|---|---|---|
| `title_gen.openrouter_enabled` | bool | `true` | Use OpenRouter for title generation |
| `title_gen.openrouter_model` | string | `"google/gemini-2.5-flash-lite"` | OpenRouter model for cheap title generation |
| `title_gen.haiku_model` | string | `"claude-haiku-4-5-20251001"` | Claude Haiku fallback model |
| `title_gen.sonnet_model` | string | `"claude-sonnet-4-6"` | Claude Sonnet model for richer title contexts |
| `title_gen.timeout_secs` | int | `10` | HTTP timeout for title generation calls |

---

## LLM Profiles

Named LLM profiles used by the topic summary and other LLM dispatcher call sites.

| Key | Type | Default | Description |
|---|---|---|---|
| `llm_profiles.cheap.openrouter_model` | string | `"deepseek/deepseek-chat-v3-0324:free"` | OpenRouter model for cheap profile |
| `llm_profiles.cheap.fallback_model` | string | `"claude-haiku-4-5-20251001"` | Claude fallback for cheap profile |
| `llm_profiles.normal.openrouter_model` | string | `"moonshotai/kimi-k2:free"` | OpenRouter model for normal profile |
| `llm_profiles.normal.fallback_model` | string | `"claude-sonnet-4-6"` | Claude fallback for normal profile |

---

## Research Knowledge Base

| Key | Type | Default | Description |
|---|---|---|---|
| `research_kb.db_path` | string | `"~/.juggle/research_kb.db"` | SQLite DB path for research KB |
| `research_kb.embedding_model` | string | `"openai/text-embedding-3-small"` | Embedding model (via OpenRouter) |
| `research_kb.summarization_model` | string | `"~google/gemini-pro-latest"` | Summarization model (via OpenRouter) |
| `research_kb.hn_score_threshold` | int | `100` | Min HN score for ingesting stories |
| `research_kb.web_search_enabled` | bool | `true` | Allow web search in research queries |
| `research_kb.pdf_dirs` | list[string] | `[]` | Directories scanned for PDF ingestion |

---

## Integrate (CI / test runner)

Per-branch integration settings. `repos` maps absolute repo paths to integration config.

| Key | Type | Default | Allowed | Description |
|---|---|---|---|---|
| `integrate.test_scope` | string | `"full"` | `"full"` | INERT (2026-06-20 directive: integrate ALWAYS runs the full suite, never a subset). Retained only so old config.json files don't crash; not read by the code. |
| `integrate.core_tests` | list[string] | `[]` | — | INERT (test scoping removed; not read). |
| `integrate.quarantine_tests` | list[string] | `[]` | — | INERT (no more `--deselect` quarantine; the full suite always runs). Not read. |
| `repos.<path>.push_mode` | string | `"none"` | `"direct"`, `"pr"`, `"none"` | `direct` = ff-merge+push main; `pr` = push branch only; `none` = local merge only |
| `repos.<path>.test_cmd` | string | `""` | any shell cmd | Test command for this repo. Runs VERBATIM at integrate, always the FULL suite. Parallel form: `"uv run pytest -n auto --dist loadgroup -m 'not watchdog_proc'"`. The speedup-tier `slow` marker tiers ONLY the opt-in `make test-fast` inner loop — NEVER subset integrate; a `test_cmd` containing `not slow` / `--deselect` / `--ignore` is REJECTED fail-loud (B2). `-n auto` parallelizes; `--dist loadgroup` honors any `serial`-grouped tests. |

---

## Agent Harness

Controls how background agents are launched, restricted, and configured.

### Top-level agent keys

| Key | Type | Default | Description |
|---|---|---|---|
| `agent.claude_launch_command` | string | `"claude --dangerously-skip-permissions"` | Shell command used to launch Claude Code agents |
| `agent.harness` | string | `"claude"` | Default harness adapter (`"claude"`, `"codex"`, `"reasonix"`, or any custom name) |
| `agent.harness_by_role` | object | `{}` | Per-role harness override, e.g. `{"researcher": "codex"}` |
| `agent.audit_mode` | bool | `false` | When true, drops per-role tool denies so real tool demand is visible for right-sizing |
| `agent.quality_gate_skill` | string | `"mike:pre-pr"` | Skill coder agents invoke before `complete-agent` |

### role_context

Short identity sentences injected into each agent's context anchor.

| Key | Default |
|---|---|
| `agent.role_context.researcher` | `"Produce comprehensive, well-structured, cited reports. Never fabricate URLs."` |
| `agent.role_context.coder` | `"Implement exactly what is specified — no more. Minimal diff."` |
| `agent.role_context.planner` | `"Produce plans a coder can execute without clarification."` |

### task_templates

Markdown role preambles prepended to agent prompts. Override to customize agent instructions.

| Key | Description |
|---|---|
| `agent.task_templates.coder` | Coder role instructions (TDD, completion protocol, scope) |
| `agent.task_templates.planner` | Planner role instructions |
| `agent.task_templates.researcher` | Researcher role instructions |

### Harness adapters (`agent.harnesses.<name>`)

Built-in adapters: `claude`, `codex`, `reasonix`. Add custom adapters here without code changes.

| Key | Type | Description |
|---|---|---|
| `type` | string | Adapter type: `"claude"`, `"codex"`, or `"template"` |
| `command` | string | Launch command (omit for `claude` — uses `claude_launch_command`) |
| `interactive` | bool | `true` = REPL harness (poll for markers); `false` = one-shot |
| `model_flag` | string | CLI flag template for model selection, e.g. `"--model {model}"` |
| `model` | string | Pin a specific model (empty = use per-agent model) |
| `extra_flags` | string | Extra CLI flags appended verbatim |
| `prompt_arg` | string | How to pass the prompt, e.g. `"< {prompt_file}"` |
| `approval_policy` | string | Codex: `"never"` \| `"on-failure"` \| `"always"` |
| `sandbox_by_role` | object | Codex: sandbox mode per role (`"read-only"`, `"workspace-write"`) |
| `restrictions_flag` | string | CLI flag to apply tool restrictions (empty = none) |
| `external_restriction` | bool | `true` = restrictions managed by harness config (e.g. reasonix.toml), not by Juggle flags |
| `env` | object | Extra env vars injected into launched agents |
| `env_unset` | list[string] | Env vars to unset before launching agents |
| `readiness_markers` | list[string] | Strings in pane output that signal agent is ready for input |
| `submission_markers` | list[string] | Strings that indicate the agent finished a response |
| `supports_hooks` | bool | Whether this harness supports Claude Code hooks |

### Settings overlay (tool permissions)

Juggle generates a `--settings` overlay passed to each agent. Layers ADDITIVELY over the host settings (never replaces).

**`agent.settings_overlay_base`** — applied to every agent (universal):
- `editorMode`: `"normal"` — force non-vim mode (vim mode breaks tmux paste dispatch)
- `permissions.deny`: list of tool names/globs denied to all agents

**`agent.settings_overlay_by_role.<role>`** — merged on top for each role:
- Additional `permissions.deny` entries specific to that role

Denied tool examples: `"mcp__github__*"` (all GitHub MCP tools), `"Agent"` (sub-agent spawning), `"mcp__opentabs__*"` (browser tools).

---

## Selfheal

> **TODO:** `selfheal.*` config keys are forthcoming (feature not yet merged). Current selfheal behaviour is hardcoded.

---

## Notes

- `config.json` does **not** store API keys — those live in `~/.juggle/.env` (`OPENROUTER_KEY`, `HINDSIGHT_LLM_MODEL`)
- `autopilot` is toggled via a flag file at `~/.juggle/autopilot` (presence = on), not via config
- All `~` paths in `paths.*` are expanded at load time
- Unknown keys in `config.json` are silently ignored (deep-merge keeps them)
