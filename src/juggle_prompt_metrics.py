"""juggle_prompt_metrics — pure prompt-size/identity helpers for the metrics
layer (2026-06-30 orchestration-metrics). No DB, no I/O."""
from __future__ import annotations

import hashlib

_SEP = "---\n\n"  # send_task_to_agent joins boilerplate + SEP + user prompt


def prompt_fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def prompt_bytes_of(text: str) -> int:
    return len((text or "").encode("utf-8"))


def boilerplate_bytes(input_prompt: str, *, preamble: str) -> int:
    """Bytes of the boilerplate prefix (preamble + worktree/template block up to
    and including the final '---\\n\\n' separator). 0 if the prompt doesn't start
    with the preamble or has no separator."""
    if not input_prompt or not preamble or not input_prompt.startswith(preamble):
        return 0
    idx = input_prompt.find(_SEP)
    if idx < 0:
        return 0
    return len(input_prompt[: idx + len(_SEP)].encode("utf-8"))
