"""Tests for talkback /speak logging behavior."""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TALKBACK_PATH = Path(__file__).parent.parent / "scripts" / "talkback"


def _load_talkback():
    """Load talkback script with audio deps mocked out.

    Stubs sounddevice and numpy only for the duration of the import so
    the MagicMock does not persist in sys.modules and contaminate
    pytest.approx (which does isinstance checks against np.bool_) in
    other test files.
    """
    _saved: dict = {}
    for key in ("sounddevice", "numpy"):
        _saved[key] = sys.modules.get(key, _SENTINEL)
        sys.modules[key] = MagicMock()
    try:
        loader = importlib.machinery.SourceFileLoader("talkback", str(TALKBACK_PATH))
        spec = importlib.util.spec_from_loader("talkback", loader)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    finally:
        for key, orig in _saved.items():
            if orig is _SENTINEL:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = orig


_SENTINEL = object()  # distinct from None so we can tell "was absent" vs "was None"


_tb = _load_talkback()


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "talkback.jsonl"


@pytest.fixture
def client(log_path, monkeypatch):
    monkeypatch.setattr(_tb, "_LOG_PATH", log_path)
    monkeypatch.setattr(_tb, "speak", MagicMock())  # don't synthesize audio
    app = _tb.create_app(voice="af_heart", speed=1.0)
    app.config["TESTING"] = True
    return app.test_client(), log_path


def test_speak_logs_received_text(client):
    flask_client, log_path = client
    resp = flask_client.post("/speak", json={"text": "hello world"})
    assert resp.status_code == 200
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["text"] == "hello world"
    assert "ts" in entry
    assert entry["voice"] == "af_heart"
    assert entry["speed"] == 1.0


def test_speak_logs_voice_speed_overrides(client):
    flask_client, log_path = client
    flask_client.post("/speak", json={"text": "testing", "voice": "bm_lewis", "speed": 1.5})
    entry = json.loads(log_path.read_text().strip())
    assert entry["voice"] == "bm_lewis"
    assert entry["speed"] == 1.5


def test_empty_text_still_logs_with_cancelled_flag(client):
    flask_client, log_path = client
    resp = flask_client.post("/speak", json={"text": ""})
    assert resp.status_code == 200
    entry = json.loads(log_path.read_text().strip())
    assert entry["text"] == ""
    assert entry.get("cancelled") is True


def test_log_failure_does_not_break_speech(log_path, monkeypatch):
    """A non-writable log path must not crash the /speak route."""
    bad_path = log_path.parent / "readonly_dir" / "talkback.jsonl"
    bad_path.parent.mkdir()
    bad_path.parent.chmod(0o444)  # read-only directory

    try:
        monkeypatch.setattr(_tb, "_LOG_PATH", bad_path)
        monkeypatch.setattr(_tb, "speak", MagicMock())
        app = _tb.create_app(voice="af_heart", speed=1.0)
        app.config["TESTING"] = True
        with app.test_client() as c:
            resp = c.post("/speak", json={"text": "should not crash"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "speaking"
    finally:
        bad_path.parent.chmod(0o755)
