"""End-to-end test: create thread → auto-recall → do work → retain → recall again."""
import json
import os
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
import time

import pytest

CLI = str(Path(__file__).parent.parent / "src" / "juggle_cli.py")
SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


class StatefulMockHandler(BaseHTTPRequestHandler):
    """Mock that tracks retained content and returns it on recall."""
    retained = []

    def log_message(self, *args):
        pass

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

        if "/reflect" in self.path:
            text = " | ".join(StatefulMockHandler.retained) if StatefulMockHandler.retained else ""
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"text": text}).encode())

        elif "/memories/recall" in self.path:
            results = [{"id": f"f{i}", "text": c, "type": "world",
                        "context": "", "entities": []}
                       for i, c in enumerate(StatefulMockHandler.retained)]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"results": results}).encode())

        elif "/memories" in self.path:
            for item in body.get("items", []):
                StatefulMockHandler.retained.append(item.get("content", ""))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(autouse=True)
def clear_retained():
    StatefulMockHandler.retained = []
    yield


@pytest.fixture(scope="module")
def mock_server():
    server = HTTPServer(("127.0.0.1", 0), StatefulMockHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


def run_cli(args, env):
    return subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True, text=True, env=env,
    )


def test_full_memory_lifecycle(tmp_path, mock_server):
    """
    1. Create thread (auto-recall, empty at first)
    2. Retain learnings
    3. Create new thread (auto-recall should now find retained content)
    """
    config = {
        "hindsight": {
            "enabled": True,
            "api_url": f"http://127.0.0.1:{mock_server}",
            "api_key": "juggle",
            "bank": "juggle",
        }
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    env = {
        **os.environ,
        "_JUGGLE_TEST_DB": str(tmp_path / "test.db"),
        "_JUGGLE_CONFIG_PATH": str(config_path),
    }

    # Start juggle
    run_cli(["start"], env)

    # Create first thread — auto-recall gets empty results
    r = run_cli(["create-thread", "fix send_task"], env)
    assert r.returncode == 0

    # Retain something
    r = run_cli(["retain", "B", "send_task fixed by adding Enter keypress after paste"], env)
    assert r.returncode == 0

    # Create second thread — auto-recall should find the retained content
    r = run_cli(["create-thread", "improve agent dispatch"], env)
    assert r.returncode == 0

    # Explicit recall should contain the retained fact
    r = run_cli(["recall", "C", "send_task"], env)
    assert "send_task fixed" in r.stdout


def test_disabled_memory_is_silent(tmp_path):
    """All memory commands are no-ops when disabled."""
    config = {"hindsight": {"enabled": False}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    env = {
        **os.environ,
        "_JUGGLE_TEST_DB": str(tmp_path / "test.db"),
        "_JUGGLE_CONFIG_PATH": str(config_path),
    }

    run_cli(["start"], env)
    run_cli(["create-thread", "test"], env)

    r = run_cli(["recall", "B", "anything"], env)
    assert r.returncode == 0
    assert r.stdout.strip() == ""

    r = run_cli(["retain", "B", "anything"], env)
    assert r.returncode == 0

    r = run_cli(["recall-if-cold", "B", "anything"], env)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
