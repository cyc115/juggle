"""Tests for memory-related CLI subcommands: recall, recall-if-cold, retain."""
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


class MockHindsightHandler(BaseHTTPRequestHandler):
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
            resp = {"text": "Mike prefers TDD"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
        elif "/memories/recall" in self.path:
            resp = {"results": [{"id": "f1", "text": "Mike prefers TDD", "type": "world",
                                 "context": "preferences", "entities": []}]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
        elif "/memories" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def mock_server():
    server = HTTPServer(("127.0.0.1", 0), MockHindsightHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


@pytest.fixture
def env(tmp_path, mock_server):
    """Return env dict with test DB and mock Hindsight config."""
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
    db_path = tmp_path / "test.db"
    return {
        **os.environ,
        "_JUGGLE_TEST_DB": str(db_path),
        "_JUGGLE_CONFIG_PATH": str(config_path),
    }


def run_cli(args, env):
    return subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True, text=True, env=env,
    )


def setup_thread(env):
    """Create a started DB with a thread, return thread label."""
    run_cli(["start"], env)
    r = run_cli(["create-thread", "test topic"], env)
    # Parse label from output like "Created Topic B: test topic. Now in Topic B."
    for word in r.stdout.split():
        if len(word) == 2 and word[:-1].isalpha() and word[-1] in ":.":
            return word[0]
    return "B"  # fallback


def test_recall_stores_memory_context(env):
    label = setup_thread(env)
    r = run_cli(["recall", label, "test query"], env)
    assert r.returncode == 0
    assert "Mike prefers TDD" in r.stdout


def test_recall_if_cold_first_time(env):
    import sqlite3
    label = setup_thread(env)
    # create-thread auto-recalls; reset so we can test recall-if-cold triggers
    db_path = env["_JUGGLE_TEST_DB"]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE threads SET memory_loaded = 0, memory_context = '' WHERE user_label = ?",
            (label.upper(),),
        )
        conn.commit()
    r = run_cli(["recall-if-cold", label, "test query"], env)
    assert r.returncode == 0
    assert "Mike prefers TDD" in r.stdout


def test_recall_if_cold_second_time_is_noop(env):
    label = setup_thread(env)
    # First call triggers recall
    run_cli(["recall", label, "test query"], env)
    # Second call should be no-op
    r = run_cli(["recall-if-cold", label, "same query"], env)
    assert r.returncode == 0
    assert r.stdout.strip() == ""  # no-op, already loaded


def test_retain_success(env):
    label = setup_thread(env)
    r = run_cli(["retain", label, "Task completed successfully"], env)
    assert r.returncode == 0


def test_retain_with_context(env):
    label = setup_thread(env)
    r = run_cli(["retain", label, "User prefers explicit types", "--context", "preferences"], env)
    assert r.returncode == 0


def test_recall_disabled(tmp_path):
    """When hindsight is disabled, recall returns empty."""
    config = {"hindsight": {"enabled": False}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    db_path = tmp_path / "test.db"
    e = {**os.environ, "_JUGGLE_TEST_DB": str(db_path), "_JUGGLE_CONFIG_PATH": str(config_path)}
    run_cli(["start"], e)
    run_cli(["create-thread", "test"], e)
    r = run_cli(["recall", "B", "anything"], e)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_retain_disabled(tmp_path):
    """When hindsight is disabled, retain is a silent no-op."""
    config = {"hindsight": {"enabled": False}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    db_path = tmp_path / "test.db"
    e = {**os.environ, "_JUGGLE_TEST_DB": str(db_path), "_JUGGLE_CONFIG_PATH": str(config_path)}
    run_cli(["start"], e)
    run_cli(["create-thread", "test"], e)
    r = run_cli(["retain", "B", "anything"], e)
    assert r.returncode == 0


def test_create_thread_auto_recalls(env):
    """Creating a thread should auto-recall and store memory_context."""
    run_cli(["start"], env)
    r = run_cli(["create-thread", "fix the send_task bug"], env)
    assert r.returncode == 0
    # Verify memory was loaded by checking recall-if-cold is a no-op
    # The label should be in the create output
    label = None
    for word in r.stdout.split():
        if len(word) == 2 and word[:-1].isalpha() and word[-1] in ":.":
            label = word[0]
            break
    assert label is not None
    time.sleep(1)  # wait for background recall thread
    r2 = run_cli(["recall-if-cold", label, "anything"], env)
    assert r2.stdout.strip() == ""  # already loaded from create-thread
