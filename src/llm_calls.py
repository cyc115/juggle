"""llm_calls — single source of truth for headless LLM subprocess calls.

Owns: the one `claude -p` subprocess core (run_claude_p) and the profile-based
LLM dispatcher (llm_call, OpenRouter primary → claude fallback). Must not own:
prompt construction or domain logic — callers keep their own wrappers/seams
(juggle_schedule_common.claude_p, juggle_project_summary._claude_sonnet,
juggle_cli_common.llm_call re-export).

Extracted in Phase 1.2 of the 2026-06-10 refactor plan, consolidating four
divergent `claude -p` wrappers.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.request


def run_claude_p(
    prompt: str,
    *,
    model: str,
    timeout: int = 120,
    output_format: str | None = None,
    cost_tracker=None,
    log: logging.Logger | None = None,
) -> str | None:
    """Run `claude -p <prompt> --model <model>` and return its text output.

    Returns None when claude exits non-zero (after an optional log warning).
    Exceptions (timeout, missing binary) propagate — callers keep their own
    try/except policy, preserving each historical call site's behavior.

    output_format="json": passes --output-format json, parses the envelope,
    updates cost_tracker (if given) from usage tokens, and returns the
    "result" (or "content") field; on parse failure falls back to raw stdout.
    """
    cmd = ["claude", "-p", prompt, "--model", model]
    if output_format:
        cmd += ["--output-format", output_format]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        if log is not None:
            log.warning("claude -p failed: %s", result.stderr[:200])
        return None
    if output_format == "json":
        try:
            data = json.loads(result.stdout)
            if cost_tracker and isinstance(data, dict):
                usage = data.get("usage", {})
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                cost = cost_tracker.estimate_from_tokens(in_tok, out_tok, model)
                cost_tracker.add(cost)
            if isinstance(data, dict):
                return data.get("result", data.get("content", str(data)))
            return str(data)
        except Exception:
            # Fallback: treat as plain text
            return result.stdout.strip()
    return result.stdout.strip()


def llm_call(prompt: str, profile: str = "cheap", timeout: int = 10) -> str | None:
    """Profile-based LLM dispatcher.

    Profiles defined in settings.llm_profiles (cheap / normal).
    Flow: OpenRouter primary -> Claude subprocess fallback -> None.
    """
    from juggle_settings import get_settings
    profiles = get_settings().get("llm_profiles", {})
    if profile not in profiles:
        raise ValueError(f"Unknown LLM profile: {profile!r}. Valid: {list(profiles)}")
    cfg = profiles[profile]
    api_key = os.environ.get("OPENROUTER_KEY", "")
    import time as _time
    if api_key:
        try:
            t0 = _time.monotonic()
            body = json.dumps({
                "model": cfg["openrouter_model"],
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
            }).encode()
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                body,
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            text = (data["choices"][0]["message"].get("content") or "").strip()
            if text:
                logging.info(
                    "llm_call(%s): provider=openrouter model=%s elapsed=%dms len=%d preview=%r",
                    profile, cfg["openrouter_model"], elapsed_ms, len(text), text[:60],
                )
                return text
        except Exception as e:
            logging.warning("llm_call(%s): openrouter failed: %s", profile, e)
    try:
        t0 = _time.monotonic()
        out = run_claude_p(prompt, model=cfg["fallback_model"], timeout=timeout)
        elapsed_ms = int((_time.monotonic() - t0) * 1000)
        if out:
            logging.info(
                "llm_call(%s): provider=claude-code model=%s elapsed=%dms len=%d preview=%r",
                profile, cfg["fallback_model"], elapsed_ms, len(out), out[:60],
            )
            return out
    except Exception as e:
        logging.warning("llm_call(%s): fallback failed: %s", profile, e)
    return None
