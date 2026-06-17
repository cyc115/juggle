"""Assert commands/init.md frames the OpenRouter key as optional with a claude -p fallback.

Topic of-init-optional-key: the key must be presented as OPTIONAL, with an explicit
skip path and a plain statement of the claude -p / keyword-FTS degradation.
"""

from pathlib import Path

INIT_MD = Path(__file__).parent.parent / "commands" / "init.md"


def _text() -> str:
    return INIT_MD.read_text()


def test_init_md_exists():
    assert INIT_MD.exists(), f"missing {INIT_MD}"


def test_has_skip_option():
    """Q2 must offer an explicit skip path that needs no key."""
    text = _text().lower()
    assert "skip" in text
    # The skip must be tied to the claude -p fallback, not a generic skip.
    assert "claude -p" in text


def test_key_framed_as_optional():
    """The key must be reframed as optional, not required."""
    text = _text().lower()
    assert "optional" in text


def test_states_claude_p_fallback():
    """Plainly state that without a key Juggle falls back to claude -p."""
    text = _text().lower()
    assert "falls back to claude -p" in text or "fall back to claude -p" in text


def test_states_fts_degradation():
    """State that research/search degrade to keyword FTS (no semantic embeddings)."""
    text = _text().lower()
    assert "fts" in text or "keyword" in text
    assert "semantic" in text or "embedding" in text


def test_env_written_without_key_when_skipped():
    """The bootstrap must support omitting the OPENROUTER_KEY line when skipped."""
    text = _text()
    # The .env write block must not unconditionally hardcode the key line; there
    # must be a documented path where the key line is omitted.
    assert "OPENROUTER_KEY" in text  # still referenced for the with-key path
    lower = text.lower()
    assert "without" in lower or "omit" in lower or "no key" in lower
