# Harness adapters (pluggable sub-agent CLIs)

Juggle spawns each background agent as a full interactive CLI process inside a
tmux pane. By default that CLI is **Claude Code**, but the launcher is pluggable:
you can point juggle at **Codex**, **reasonix**, or any other harness — usually
with **config only, no code**.

`src/juggle_harness.py` is the **framework** (the `HarnessAdapter` contract, the
registry, and `get_adapter`), consumed by `JuggleTmuxManager.start_agent_in_pane`
(`src/juggle_tmux.py`). Each concrete harness is **self-contained in its own
module** under `src/harnesses/` and owns, in one place: launch (binary/flags/env),
restriction materialization, context delivery, and capabilities. Shipped:

| Harness | Type | Restriction strategy | Context delivery |
|--------|------|----------------------|------------------|
| `harnesses/claude.py` | `claude` | per-role `permissions.deny` written to a JSON `--settings` overlay | juggle hooks (`UserPromptSubmit`) |
| `harnesses/codex.py` | `codex` | per-role sandbox/approval **modes** (`-a/-s` flags) — Codex has no tool-deny list | anchor **inlined** into the prompt (Codex hooks are version-skewed) |
| `reasonix` (config-only) | `template` | delegated to the harness's own `reasonix.toml` (`external_restriction:true`) — no per-call flags | anchor **inlined**; Reasonix reads `AGENTS.md` |
| (built-in) | `template` | static `restrictions_flag` from config | inlined if `supports_hooks:false` |

A harness needing custom logic = drop `src/harnesses/<name>.py` that subclasses
`HarnessAdapter` and calls `register_adapter(...)` at import; add it to
`harnesses/__init__.py`. A harness expressible in config (like **Reasonix**, a
one-shot `reasonix run` reading the prompt from stdin) needs **no Python** — just
a `"type": "template"` entry under `agent.harnesses` (ships inactive in DEFAULTS;
select it with `agent.harness` / `agent.harness_by_role`).
It is then auto-discovered and **gated by the conformance suite** (below).

## How selection works

Three keys under `agent` in `~/.juggle/config.json`:

| Key                 | Meaning                                                       |
|---------------------|--------------------------------------------------------------|
| `harness`           | Global default harness id (default `"claude"`).              |
| `harness_by_role`   | Optional per-role override, e.g. `{"researcher": "codex"}`.  |
| `harnesses`         | The harness definitions, keyed by id.                        |

Resolution precedence for a role: `harness_by_role[role]` → `harness` →
`"claude"`. If the selected id has no definition, juggle falls back to the
built-in Claude harness, so **older configs with no `harnesses` block keep
working unchanged.**

## Harness definition schema

```jsonc
"harnesses": {
  "<id>": {
    "type": "claude" | "template",   // "claude" = built-in overlay logic
    "command": "claude ...",          // launch command (claude falls back to
                                       //   agent.claude_launch_command)
    "model_flag": "--model {model}",  // the model flag template
    "model": "",                      // pin a model for this harness, overriding
                                       //   the per-agent model (e.g. Codex "gpt-5");
                                       //   empty = use the agent's model
    "extra_flags": "",                // arbitrary extra CLI flags appended verbatim
    "restrictions_flag": "",          // (template only) static tool-restriction flag
    "external_restriction": false,    // true = restriction lives in the harness's
                                       //   own config file (e.g. reasonix.toml), not
                                       //   juggle-managed flags/overlay
    "env": {},                        // per-harness env: set/override ANY var for the
                                       //   process (overrides inherited env). JUGGLE_IS_AGENT/
                                       //   ROLE/AUDIT are injected automatically.
    "env_unset": ["CLAUDE_PLUGIN_DATA"], // env vars scrubbed via `env -u`
    "interactive": true,              // true = warm REPL pane (default);
                                       //   false = one-shot process per task
    "prompt_arg": "< {prompt_file}",  // (one-shot) how the prompt FILE is fed to
                                       //   the process; default redirects to stdin
                                       //   (Codex uses "- < {prompt_file}")
    "readiness_markers": ["..."],     // (interactive) pane substrings: REPL ready
    "submission_markers": ["..."],    // (interactive) pane substrings: submitted
    "supports_hooks": true            // does it run juggle's Claude Code hooks?
  }
}
```

### Interactive vs one-shot

- **`interactive: true`** (default, Claude Code): the REPL is launched once and
  each task is pasted into the warm pane; juggle polls the `readiness_markers` /
  `submission_markers` to drive paste-and-submit.
- **`interactive: false`** (one-shot, e.g. `codex exec`): each task spawns a
  fresh process that runs to completion and exits. No warm-pane reuse and **no
  marker polling** — simpler and more robust for non-interactive CLIs. The
  prompt is passed **by file**: juggle writes it to `/tmp` and the process reads
  it via the `prompt_arg` (default `< {prompt_file}` → stdin; Codex uses `- <
  {prompt_file}`). Passing by file (not inlined on the command line) avoids the
  OS `ARG_MAX` limit on large prompts and needs no escaping. The prompt file is
  left in `/tmp` (the OS reaper collects it; keeping it makes the run auditable).
  Markers are only used in interactive mode (the conformance suite still requires
  non-empty values, so keep a sentinel).

Notes:

- **`type: "claude"`** uses `ClaudeCodeAdapter` (`harnesses/claude.py`), which
  generates the additive per-role `--settings` overlay via `juggle_agent_settings`.
- **`type: "codex"`** uses `CodexAdapter` (`harnesses/codex.py`), and runs
  **one-shot** (`command: "codex exec"`, `interactive: false`) — each task is a
  fresh process, no warm REPL. Codex restricts via sandbox + approval **modes**,
  not a tool list, so its config keys differ: `approval_policy`, `sandbox_by_role`
  (e.g. `{"coder":"workspace-write","researcher":"read-only"}`), `sandbox_default`,
  `sandbox_audit`. It materializes these as `-a <approval> -s <sandbox>` flags.
  Codex auto-reads `AGENTS.md` for context and the role anchor is inlined into the
  prompt (`supports_hooks:false`). Confirm `command`/flags against your installed
  `codex` and override in config — no code change.
- **`type: "template"`** uses the fully config-driven `TemplateHarnessAdapter`.
  This is the "bring your own harness" path — no Python required.
- **`supports_hooks: false`** means the harness does **not** run juggle's
  Claude Code hooks. Juggle compensates: the role anchor (role identity +
  `complete-agent` completion command) is **inlined into the task prompt**
  instead of injected via the `UserPromptSubmit` hook. Per-role tool telemetry
  (`juggle agent-tools`) only has data for hook-capable harnesses.
- `JUGGLE_IS_AGENT=1`, `JUGGLE_AGENT_ROLE=<role>` and (in audit mode)
  `JUGGLE_AGENT_AUDIT=1` are exported for **every** harness so juggle can still
  identify the agent process; you don't need to list them in `env`.
- The tmux marker resolution is taken from the **global default** harness
  (`agent.harness`). In a mixed per-role setup, panes don't carry their harness
  id, so the readiness/submission markers used for paste-and-submit follow the
  global default. Keep per-role harnesses' TUIs marker-compatible, or set the
  global default to the one whose markers you rely on.

## Example: add Codex as a config-only harness

This is a **starting point** — verify your installed Codex CLI's real flags and
TUI strings (`codex --help`, and watch a live pane) before relying on it.

```jsonc
{
  "agent": {
    "harness": "claude",
    "harness_by_role": { "researcher": "codex" },
    "harnesses": {
      "codex": {
        "type": "template",
        "command": "codex",
        "model_flag": "--model {model}",
        "restrictions_flag": "--sandbox workspace-write",
        "env": { "JUGGLE_IS_AGENT": "1" },
        "env_unset": [],
        "readiness_markers": ["» "],
        "submission_markers": ["Esc to interrupt"],
        "supports_hooks": false
      }
    }
  }
}
```

With the above, `researcher` agents launch Codex while every other role still
launches Claude Code.

## Example: a brand-new harness ("reasonix")

```jsonc
"harnesses": {
  "reasonix": {
    "type": "template",
    "command": "reasonix chat",
    "model_flag": "-m {model}",
    "restrictions_flag": "",
    "env": { "JUGGLE_IS_AGENT": "1" },
    "env_unset": [],
    "readiness_markers": ["ready>"],
    "submission_markers": ["thinking", "cancel"],
    "supports_hooks": false
  }
}
```
Set `"harness": "reasonix"` to make it the default for all roles.

## Conformance: every harness must pass the contract suite

`tests/test_harness_conformance.py` is an **executable contract** that runs
against *every* harness juggle knows about — auto-discovered from both the
registered adapter types (`juggle_harness._ADAPTERS`) and the shipped
`DEFAULTS["agent"]["harnesses"]`. A new harness (config-only or a new Python
adapter) is picked up automatically and **must pass it, or CI is red.** There is
no opt-out.

The contract each harness must satisfy:

| ID | Behaviour | Why juggle needs it |
|----|-----------|---------------------|
| C1 | Constructs via `get_adapter`; exposes id + capability/marker API | basic wiring |
| C2 | Launch command exports `JUGGLE_IS_AGENT=1` and `JUGGLE_AGENT_ROLE=<role>` | hooks/telemetry/watchdog identify the agent process |
| C3 | `audit=True` ⇒ `JUGGLE_AGENT_AUDIT=1`; `audit=False` ⇒ absent | tool-usage telemetry tagging |
| C4 | Model appears when given; no dangling flag when omitted | model selection |
| C5 | Launch command is a single shell line | it is pasted into a tmux pane |
| C6 | Readiness + submission markers are non-empty strings | the paste/submit poll loop needs them |
| C7 | Per-role restriction is materialized (inline **or** in a written artifact); audit relaxes per-role denies | the token-saving deny guarantee |
| C8 | Role anchor reaches the agent exactly once (hook harnesses keep the prompt clean; non-hook harnesses inline it) | the agent must learn its role without double injection |
| C9 | Repeated identical builds are stable (modulo per-run temp paths) | no hidden global state |

To add a new **required** behaviour for all harnesses, add one test to that
file — it instantly applies to every present and future harness. Run it with:

```bash
uv run pytest -q tests/test_harness_conformance.py
```

## When config isn't enough: write a Python adapter

If a harness needs real logic for its tool restriction (e.g. generating a
config file, like Claude's overlay), subclass `HarnessAdapter` in
`src/juggle_harness.py` and override `_restrictions_part` (and, if needed,
`build_launch_command`/`decorate_task`). Register it in the `_ADAPTERS` map
under a new `type`. `ClaudeCodeAdapter` is the reference implementation.
