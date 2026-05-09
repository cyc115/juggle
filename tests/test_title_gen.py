"""Tests for title generation fallback chain and settings defaults."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_settings import DEFAULTS


def test_title_gen_defaults_present():
    tg = DEFAULTS.get("title_gen")
    assert tg is not None, "title_gen section missing from DEFAULTS"
    assert tg["openrouter_enabled"] is True
    assert tg["openrouter_model"] == "meta-llama/llama-3.1-8b-instruct:free"
    assert tg["openrouter_api_key"] == ""
    assert tg["haiku_model"] == "claude-haiku-4-5-20251001"
    assert tg["timeout_secs"] == 10
