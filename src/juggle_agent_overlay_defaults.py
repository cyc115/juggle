#!/usr/bin/env python3
"""Agent settings.json overlay defaults — the token-saving permission deny
blocks layered onto every background agent's ``--settings`` file.

Extracted from juggle_settings.py (2026-07-07, architecture-gate: settings.py
exceeded its LOC budget). Single source of truth for
``DEFAULTS["agent"]["settings_overlay_base"]`` /
``["settings_overlay_by_role"]``; imported back so the runtime structure is
unchanged (byte-identical). See juggle_agent_settings.build_agent_overlay for
the composition rules (base merged first, then settings_overlay_by_role[role]
on top; list values union, nested dicts deep-merge, scalars override).
"""

SETTINGS_OVERLAY_BASE: dict = {
    # Force non-vim editor mode for all background agents regardless of
    # the host's global ~/.claude/settings.json (which may set vim mode).
    # Vim mode breaks tmux paste dispatch: send_task pastes into NORMAL
    # mode and the keystrokes are interpreted as editor commands.
    "editorMode": "normal",
    "permissions": {
        "deny": [
            # opentabs browser tools (78 tools) — wildcard collapses to one entry
            "mcp__opentabs__*",
            # GitHub MCP (60+ tools) — the orchestrator owns all GitHub/PR
            # work; agents do code via the git CLI (Bash). Largest single
            # context saving. (Standard `github` MCP namespace.)
            "mcp__github__*",
            # otterai (meeting transcription) — not used by any agent role.
            "mcp__otterai__*",
            # NOTE: the claude.ai Google Workspace connectors (Drive,
            # Calendar, Gmail) are NOT denied universally — researchers
            # need them. They are denied per-role for coder + planner in
            # settings_overlay_by_role below.
            # personal-mcp financial tools (not for agents)
            "mcp__personal-mcp__plaid_get_accounts",
            "mcp__personal-mcp__plaid_get_statements",
            "mcp__personal-mcp__plaid_sync_transactions",
            # meta / orchestrator tools agents don't invoke
            "ScheduleWakeup",
            "CronCreate",
            "CronList",
            "CronDelete",
            "ShareOnboardingGuide",
            "ExitPlanMode",
            "EnterPlanMode",
            "EnterWorktree",
            "ExitWorktree",
            "PushNotification",
            # sub-agent spawning and remote triggers — orchestrator-only
            "Agent",
            "RemoteTrigger",
            # MCP resource browsing — not used by any agent role
            "ListMcpResourcesTool",
            "ReadMcpResourceTool",
        ]
    }
}

# Per-role overlay merged ON TOP of settings_overlay_base. Today only
# adds role-specific denials; a role may also diverge on env / model /
# hooks / sandbox here with no code change.
SETTINGS_OVERLAY_BY_ROLE: dict = {
    "researcher": {
        "permissions": {
            "deny": [
                "Edit",  # researchers don't patch code
                "NotebookEdit",  # no Jupyter in Juggle
            ]
        }
    },
    "coder": {
        "permissions": {
            "deny": [
                "NotebookEdit",  # no Jupyter in Juggle
                "mcp__personal-mcp__extract_text_from_file",  # OCR not needed for coding
                # claude.ai Google Workspace connectors — researchers only.
                # VERIFY these slugs on the host via `/permissions` (add a
                # deny rule, type `mcp__` to autocomplete): the server names
                # contain spaces/dots and Claude Code's slug sanitization
                # for those is undocumented. A wrong slug fails silently.
                "mcp__claude.ai Google Drive__*",
                "mcp__claude.ai Google Calendar__*",
                "mcp__claude.ai Gmail__*",
            ]
        }
    },
    "planner": {
        "permissions": {
            "deny": [
                "Edit",  # planners write plans, not code
                "NotebookEdit",  # no Jupyter in Juggle
                "Monitor",  # planners don't run bg processes
                "TaskOutput",  # no bg tasks to monitor
                "TaskStop",  # no bg tasks to stop
                "mcp__personal-mcp__extract_text_from_file",  # OCR not needed for planning
                # claude.ai Google Workspace connectors — researchers only.
                # (Verify slugs via `/permissions`; see coder note above.)
                "mcp__claude.ai Google Drive__*",
                "mcp__claude.ai Google Calendar__*",
                "mcp__claude.ai Gmail__*",
            ]
        }
    },
}
