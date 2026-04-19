"""Guard that agent prompt templates reference the new commands."""
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"


def test_no_warning_prefix_in_prompt_text():
    # All src/*.py files should not contain "⚠️ BLOCKER" in OUTWARD-facing prompt strings.
    offenders = []
    for p in SRC.glob("*.py"):
        text = p.read_text()
        # Exempt renderer code that reads legacy data (cockpit) — only flag strings
        # that are OUTPUT to agents (contain "complete-agent" in same file).
        if "⚠️ BLOCKER" in text and "complete-agent" in text:
            offenders.append(str(p))
    assert offenders == [], f"Prompt templates still use ⚠️ prefix: {offenders}"


def test_prompt_templates_mention_three_commands():
    # At least one prompt-carrying module should mention all three commands.
    combined = "".join(p.read_text() for p in SRC.glob("*prompt*.py"))
    if not combined:
        # Fallback: cmd_agents contains dispatch prompts
        combined = (SRC / "juggle_cmd_agents.py").read_text()
    for cmd in ("complete-agent", "request-action", "fail-agent"):
        assert cmd in combined, f"missing mention of {cmd} in prompt templates"
