"""juggle_thread_router — SYNC-LOCAL in-hook thread router.

Owns: _route_prompt_to_thread and its pure-local gates/scorer
(_is_trivial_followup, _score, get_classification_candidates).
Must not own: hook wiring, message persistence, or any LLM/network call —
this module is pure-local, zero-cost, called on every UserPromptSubmit.

Extracted from juggle_hooks_prompt.py (2026-07-22, thread-routing fix spec
Q1+Q2) to keep that module under the repo's LOC gate.
"""

import re

# Switch only on a clear win — fail-safe direction is to prefer NOT switching
# (a missed switch is recoverable; a wrong switch splinters a conversation).
SWITCH_THRESHOLD = 0.5

# Small recency prior so ties among near-equal scores break toward the most
# recently active thread (continuity), never large enough to override a real
# overlap difference.
_RECENCY_EPSILON = 0.05

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "to", "of",
    "in", "on", "for", "and", "or", "it", "this", "that", "with", "as", "at",
    "please", "like", "today", "what's", "whats",
}

# Short continuations that carry no new topic signal — never switch on these.
_TRIVIAL_FOLLOWUPS = {
    "try again", "status", "retry", "continue", "go", "yes", "no", "ok",
    "thanks", "?",
}


def get_classification_candidates(threads: list[dict]) -> list[dict]:
    """Return threads eligible for topic classification match.

    Only conversations with state not in ('done', 'archived') are considered
    (node vocab; 'done' covers legacy closed+done). If no match is found among
    these candidates, a new thread should be created — closed threads are never
    resurrected.
    """
    return [t for t in threads if t.get("state") not in ("done", "archived")]


def _is_trivial_followup(prompt: str) -> bool:
    """True when the prompt is a short continuation carrying no topic signal."""
    stripped = prompt.strip()
    if not stripped:
        return True
    if len(stripped.split()) <= 3:
        return True
    normalized = stripped.lower().rstrip("?.!")
    return normalized in _TRIVIAL_FOLLOWUPS


def _tokenize(text: str) -> set[str]:
    return {tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS}


def _score(prompt: str, thread: dict, recency_bonus: float = 0.0) -> float:
    """Local token-overlap score of `prompt` against a thread's topic text.

    Pure function — the thread dict carries whatever text a caller wants
    scored (title + user_label + an optional pre-fetched `_snippet` of recent
    message content); no DB access here, so it needs no DB in tests.
    """
    prompt_tokens = _tokenize(prompt)
    candidate_text = " ".join(
        str(thread.get(key) or "") for key in ("title", "user_label", "_snippet")
    )
    candidate_tokens = _tokenize(candidate_text)
    if not prompt_tokens or not candidate_tokens:
        base = 0.0
    else:
        overlap = len(prompt_tokens & candidate_tokens)
        base = overlap / len(prompt_tokens)
    return base + recency_bonus


def _route_prompt_to_thread(db, prompt: str, current_id: str | None) -> str | None:
    """Route `prompt` to the best-matching open thread, or stay on `current_id`.

    Fail-safe: any ambiguity (no candidates, no confident match, trivial
    follow-up) means stay put. Never auto-creates a thread.
    """
    if _is_trivial_followup(prompt):
        return current_id

    candidates = get_classification_candidates(db.get_all_threads())
    if not candidates:
        return current_id

    ranked_by_recency = sorted(
        candidates, key=lambda t: t.get("last_active_at") or "", reverse=True
    )
    n = len(ranked_by_recency)
    recency_bonus = {
        t["id"]: _RECENCY_EPSILON * (1 - i / n) if n > 1 else 0.0
        for i, t in enumerate(ranked_by_recency)
    }

    scored = []
    for t in candidates:
        exchanges = db.get_recent_exchanges(t["id"], n=2)
        snippet = " ".join(ex["user"] for ex in exchanges if ex.get("user"))
        enriched = {**t, "_snippet": snippet}
        scored.append((t, _score(prompt, enriched, recency_bonus.get(t["id"], 0.0))))

    best, best_score = max(scored, key=lambda pair: pair[1])
    if best_score >= SWITCH_THRESHOLD and best["id"] != current_id:
        return best["id"]
    return current_id
