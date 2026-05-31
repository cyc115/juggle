# Harness adapters (pluggable sub-agent CLIs)

Juggle spawns each background agent as a full interactive CLI process inside a
tmux pane. By default that CLI is **Claude Code**, but the launcher is pluggable:
you can point juggle at **Codex**, **reasonix**, or any other harness — usually
with **config only, no code**.

This is implemented by `src/juggle_harness.py` (`HarnessAdapter`) and consumed by
`JuggleTmuxManager.start_agent_in_pane` (`src/juggle_tmux.py`). Everything that
used to be hard-wired to Claude — the binary, flags, per-role tool restriction,
env scrubbing, and the tmux readiness/submission markers — now lives behind the
adapter and is selected from config.

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
    "model_flag": "--model {model}",  // applied only when a model is given
    "restrictions_flag": "",          // (template only) static tool-restriction flag
    "env": {"JUGGLE_IS_AGENT": "1"},  // env vars exported before the command
    "env_unset": ["CLAUDE_PLUGIN_DATA"], // env vars scrubbed via `env -u`
    "readiness_markers": ["..."],     // pane substrings: REPL ready for paste
    "submission_markers": ["..."],    // pane substrings: prompt was submitted
    "supports_hooks": true            // does it run juggle's Claude Code hooks?
  }
}
```

Notes:

- **`type: "claude"`** uses `ClaudeCodeAdapter`, which generates the additive
  per-role `--settings` overlay via `juggle_agent_settings` (your per-role tool
  deny lists). This is the only `type` that needs real Python logic.
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

## When config isn't enough: write a Python adapter

If a harness needs real logic for its tool restriction (e.g. generating a
config file, like Claude's overlay), subclass `HarnessAdapter` in
`src/juggle_harness.py` and override `_restrictions_part` (and, if needed,
`build_launch_command`/`decorate_task`). Register it in the `_ADAPTERS` map
under a new `type`. `ClaudeCodeAdapter` is the reference implementation.
