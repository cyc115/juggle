"""juggle_model_registry — canonical valid Claude Code --model values.

Pure, no DB/I/O (bug KB, 2026-07-19: an invalid --model string reached the
Claude Code harness at boot, silently failed, and left the agent stuck
status='busy' forever). Config-driven: DEFAULT_VALID_MODELS is overridable via
~/.juggle/config.json -> agents.valid_models, same cascade as every other
agents.* knob resolved in juggle_agent_runtime.

Two accepted shapes:
  - a short alias, e.g. "opus", "sonnet", "haiku-4-5" (DEFAULT_VALID_MODELS)
  - a full "claude-<...>" model id, e.g. "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001" (_FULL_MODEL_ID_RE)
The bug's actual malformed strings ("claude/opus", "claude/sonnet") use a
'/' provider-prefix Claude Code doesn't understand — normalize_model strips
a redundant leading "claude/" (an unambiguous typo of the bare alias); a
model that still contains '/' after that, or doesn't match either accepted
shape, is rejected.
"""
from __future__ import annotations

import re

DEFAULT_VALID_MODELS: tuple[str, ...] = (
    "opus", "sonnet", "haiku",
    "opus-4-8", "sonnet-4-5", "haiku-4-5",
)

_FULL_MODEL_ID_RE = re.compile(r"^claude-[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalize_model(model: str) -> str:
    """Strip a redundant leading 'claude/' provider prefix — an unambiguous
    typo of the bare alias (e.g. 'claude/opus' -> 'opus')."""
    if model.startswith("claude/"):
        return model[len("claude/"):]
    return model


def resolve_claude_model(model: str, *, settings: dict | None = None) -> str:
    """Normalize + validate a --model string against the canonical Claude set.

    Raises ValueError on a still-unrecognized model — callers must fail loud
    rather than spawn/reuse an agent that will jam at boot."""
    normalized = normalize_model(model)
    valid = tuple((settings or {}).get("agents", {}).get("valid_models") or DEFAULT_VALID_MODELS)
    if normalized in valid or _FULL_MODEL_ID_RE.match(normalized):
        return normalized
    raise ValueError(
        f"invalid model {model!r} (normalized {normalized!r}) — must be one "
        f"of: {', '.join(valid)}, or a full 'claude-...' model id"
    )


def is_poisoned_claude_model(model: str | None, harness: str | None, *,
                             settings: dict | None = None) -> bool:
    """True if ``model`` is a stored "claude"-harness model that no longer
    validates — legacy data predating spawn-time validation. Used to guard
    idle-pool checkout (a poisoned agent must be decommissioned, never reused)."""
    if harness != "claude" or not model:
        return False
    try:
        resolve_claude_model(model, settings=settings)
        return False
    except ValueError:
        return True
