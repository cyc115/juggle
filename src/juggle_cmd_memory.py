#!/usr/bin/env python3
"""juggle_cmd_memory — handlers for the `memory` resource group.

``cmd_retain`` was extracted here from ``juggle_cmd_context.py`` (a grab-bag
sitting at the 300-line LOC gate); the ``memory`` resource now has a module of
its own, matching the ``juggle_cmd_<domain>.py`` convention.

``cmd_recall`` (2026-08-02) restores the READ side. The flat ``recall`` verb was
dropped by the resource-verb grammar migration (spec §7) for having no
resource-verb home — but ``commands/start.md`` kept mandating it, so every
personal-question recall exited 2 on an argparse usage error and the "never
answer from training data alone" rule silently stopped holding. Under ``memory``
the verb now HAS a home, next to its ``retain`` sibling, so this honours the
migration rather than reverting it.
"""

from __future__ import annotations

import sys

from juggle_cli_common import _get_hindsight_client

# Hard ceiling on the blocking recall. The orchestrator runs this in the
# foreground before answering, so it must fail fast: HindsightClient.recall's
# retry path (timeout → `docker compose up` → sleep → timeout) can burn ~37s and
# then return "" anyway. The client's own configured timeout is honoured only
# when it is SMALLER than this.
RECALL_TIMEOUT_SECS = 8

# Degradation markers. Every no-result path says, in words, that memory was NOT
# consulted — a blank stdout is what let the original failure go unnoticed. They
# go to stdout on purpose: the incident's invocations ran under
# `2>&1 | head -100`, where the pipeline's exit status is head's, so the failing
# exit code was invisible and only the TEXT survived to be read.
NO_MEMORY_DISABLED = (
    "[recall: memory service is OFF — no memory was consulted. "
    "Start it with /juggle:memory-start, or answer without memory and SAY SO.]"
)
NO_MEMORY_UNREACHABLE = (
    "[recall: memory service UNREACHABLE ({err}) — no memory was consulted. "
    "Start it with /juggle:memory-start, or answer without memory and SAY SO.]"
)
NO_MEMORIES_FOUND = "[recall: no memories found for this query]"


def cmd_retain(args):
    """Retain content as memory in Hindsight."""
    client = _get_hindsight_client()
    if client is None:
        return  # disabled or unconfigured

    context = getattr(args, "context", None)
    client.retain(args.content, context=context)


def _recall(client, query: str) -> str:
    """Time-bounded recall against ``client`` (raises HindsightError on failure)."""
    configured = getattr(client, "timeout", None) or RECALL_TIMEOUT_SECS
    return client.recall_bounded(query, timeout=min(configured, RECALL_TIMEOUT_SECS))


def cmd_recall(args):
    """Recall memories from Hindsight for a topic (blocking, time-bounded).

    Exit codes are the contract: 0 when memory was consulted (hit or miss) or is
    switched off by config; 1 when memory is switched ON but unreachable — an
    anomaly the orchestrator must not paper over.
    """
    from juggle_hindsight import HindsightError

    client = _get_hindsight_client()
    if client is None:
        print(NO_MEMORY_DISABLED)
        return

    try:
        result = _recall(client, args.query or "")
    except HindsightError as e:
        print(NO_MEMORY_UNREACHABLE.format(err=e))
        sys.exit(1)

    print(result or NO_MEMORIES_FOUND)
