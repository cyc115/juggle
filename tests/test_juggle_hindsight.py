"""Tests for juggle_hindsight.py — Hindsight HTTP API client."""
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from juggle_hindsight import HindsightClient, HindsightError


class MockHindsightHandler(BaseHTTPRequestHandler):
    """Mock Hindsight API server for testing."""

    def log_message(self, format, *args):
        pass  # suppress logs

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_len)) if content_len else {}

        if "/memories/recall" in self.path:
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer juggle":
                self.send_response(401)
                self.end_headers()
                return
            response = {
                "results": [
                    {"id": "f1", "text": "Test recalled fact", "type": "world",
                     "context": "test", "entities": []}
                ]
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        elif "/memories" in self.path and "recall" not in self.path:
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer juggle":
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","items_queued":1}')

        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def mock_server():
    """Start a mock Hindsight server on a random port."""
    server = HTTPServer(("127.0.0.1", 0), MockHindsightHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def client(mock_server):
    return HindsightClient(
        api_url=mock_server,
        api_key="juggle",
        bank="juggle",
    )


def test_health_check(client):
    assert client.health_check() is True


def test_health_check_bad_url():
    c = HindsightClient(api_url="http://127.0.0.1:1", api_key="x", bank="x")
    assert c.health_check() is False


def test_recall_returns_text(client):
    result = client.recall("test query")
    assert "Test recalled fact" in result


def test_recall_empty_query(client):
    result = client.recall("")
    assert result == ""


def test_retain_success(client):
    # Should not raise
    client.retain("some content", context="learnings")


def test_retain_empty_content(client):
    # Empty content should be a no-op
    client.retain("")


def test_recall_with_bad_auth(mock_server):
    c = HindsightClient(api_url=mock_server, api_key="wrong", bank="juggle")
    result = c.recall("test")
    assert result == ""  # graceful failure


def test_retain_with_bad_auth(mock_server):
    c = HindsightClient(api_url=mock_server, api_key="wrong", bank="juggle")
    # Should not raise — retain is non-blocking
    c.retain("content")


def test_recall_timeout():
    """Recall against unreachable host should timeout and return empty."""
    c = HindsightClient(
        api_url="http://192.0.2.1:9999",  # non-routable
        api_key="juggle",
        bank="juggle",
        timeout=1,
    )
    result = c.recall("test")
    assert result == ""


class TestConfigFromFile:
    """Test loading config from ~/.juggle/config.json."""

    def test_from_config_enabled(self, tmp_path):
        config = {
            "hindsight": {
                "enabled": True,
                "api_url": "http://localhost:18888",
                "api_key": "juggle",
                "bank": "juggle",
            }
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))
        c = HindsightClient.from_config(str(config_path))
        assert c is not None
        assert c.api_url == "http://localhost:18888"

    def test_from_config_disabled(self, tmp_path):
        config = {"hindsight": {"enabled": False}}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))
        c = HindsightClient.from_config(str(config_path))
        assert c is None

    def test_from_config_missing_file(self, tmp_path):
        c = HindsightClient.from_config(str(tmp_path / "nonexistent.json"))
        assert c is None
