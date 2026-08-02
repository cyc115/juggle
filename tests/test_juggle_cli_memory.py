"""Tests for the `memory` CLI resource group: `memory recall`, `memory retain`."""

import argparse
import json
import os
import socket
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread

import pytest

SRC = str(Path(__file__).parent.parent / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

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
        if content_len:
            self.rfile.read(content_len)
        if "/reflect" in self.path:
            resp = {"text": "Mike prefers TDD"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
        elif "/memories/recall" in self.path:
            resp = {
                "results": [
                    {
                        "id": "f1",
                        "text": "Mike prefers TDD",
                        "type": "world",
                        "context": "preferences",
                        "entities": [],
                    }
                ]
            }
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
        capture_output=True,
        text=True,
        env=env,
    )


def setup_thread(env):
    """Create a started DB with a thread, return thread label."""
    run_cli(["start"], env)
    r = run_cli(["thread", "create", "test topic"], env)
    # Parse label from output like "Created Topic B: test topic. Now in Topic B."
    for word in r.stdout.split():
        if len(word) == 2 and word[:-1].isalpha() and word[-1] in ":.":
            return word[0]
    return "B"  # fallback



def test_retain_success(env):
    label = setup_thread(env)
    r = run_cli(["memory", "retain", label, "Task completed successfully"], env)
    assert r.returncode == 0


def test_retain_with_context(env):
    label = setup_thread(env)
    r = run_cli(
        ["memory", "retain", label, "User prefers explicit types", "--context", "preferences"],
        env,
    )
    assert r.returncode == 0



def test_retain_disabled(tmp_path):
    """When hindsight is disabled, retain is a silent no-op."""
    config = {"hindsight": {"enabled": False}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    db_path = tmp_path / "test.db"
    e = {
        **os.environ,
        "_JUGGLE_TEST_DB": str(db_path),
        "_JUGGLE_CONFIG_PATH": str(config_path),
    }
    run_cli(["start"], e)
    run_cli(["thread", "create", "test"], e)
    r = run_cli(["memory", "retain", "B", "anything"], e)
    assert r.returncode == 0


# ── `memory recall` — restored read verb ─────────────────────────────────────

def _disabled_env(tmp_path, hindsight: dict):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"hindsight": hindsight}))
    return {
        **os.environ,
        "_JUGGLE_TEST_DB": str(tmp_path / "test.db"),
        "_JUGGLE_CONFIG_PATH": str(config_path),
    }


def test_memory_recall_returns_memories(env):
    """REGRESSION PIN (2026-08-02): commands/start.md mandated a `recall` verb
    deleted by the grammar migration; personal-question recall silently no-op'd.

    start.md's "Personal questions — recall first" rule shells out to this verb
    before answering anything personal. With the verb missing, every one of those
    calls exited 2 on an argparse usage error — several piped through `| head`,
    which hid the non-zero exit — so the "never answer from training data alone"
    rule was violated silently. `memory recall` must resolve AND print what the
    memory service returned.
    """
    label = setup_thread(env)
    r = run_cli(["memory", "recall", label, "what does Mike prefer?"], env)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "Mike prefers TDD" in r.stdout


def test_memory_recall_reports_empty_result_explicitly(env):
    """An empty recall must SAY it found nothing — never print nothing at all.

    start.md: "If Hindsight returns nothing, say so explicitly." Blank stdout is
    indistinguishable from a crash, which is how the incident stayed invisible.
    """
    label = setup_thread(env)
    r = run_cli(["memory", "recall", label, "   "], env)
    assert r.returncode == 0
    assert "no memories" in r.stdout.lower()


def test_memory_recall_disabled_degrades_clearly(tmp_path):
    """Memory switched OFF in config is a config state, not an error: exit 0,
    but the orchestrator must be told no memory was consulted."""
    e = _disabled_env(tmp_path, {"enabled": False})
    run_cli(["start"], e)
    run_cli(["thread", "create", "test"], e)
    r = run_cli(["memory", "recall", "B", "anything"], e)
    assert r.returncode == 0
    assert "no memory was consulted" in r.stdout.lower()


def test_memory_recall_service_down_fails_loud(tmp_path):
    """Enabled-but-unreachable must fail FAST and LOUD, never look like a hit.

    The marker goes to stdout so it survives the `2>&1 | head -100` pipes that
    masked the original failure, and the exit code is non-zero so `&&` chains
    stop.
    """
    # Bind then close a port so the address is known-dead (connection refused).
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()

    e = _disabled_env(tmp_path, {
        "enabled": True,
        "api_url": f"http://127.0.0.1:{dead_port}",
        "api_key": "juggle",
        "bank": "juggle",
        "timeout_secs": 2,
    })
    run_cli(["start"], e)
    run_cli(["thread", "create", "test"], e)
    r = run_cli(["memory", "recall", "B", "how much did I pay for rent?"], e)
    assert r.returncode != 0, f"stdout={r.stdout!r}"
    assert "no memory was consulted" in r.stdout.lower()


def test_memory_recall_call_is_time_bounded():
    """The blocking call must carry an explicit, capped timeout.

    `HindsightClient.recall` auto-restarts docker and retries on failure (~37s
    worst case) and then returns "" — a hang followed by a silent empty answer.
    The CLI path must not inherit that: it passes its own ceiling.
    """
    import juggle_cmd_memory as m

    seen = {}

    class FakeClient:
        timeout = 600  # a hostile config value

        def recall_bounded(self, query, timeout=None):
            seen["timeout"] = timeout
            return "ok"

    r = m._recall(FakeClient(), "q")
    assert r == "ok"
    assert 0 < seen["timeout"] <= m.RECALL_TIMEOUT_SECS


def test_recall_bounded_raises_instead_of_swallowing():
    """`recall_bounded` must propagate a dead service, unlike `recall`."""
    from juggle_hindsight import HindsightClient, HindsightError

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()

    client = HindsightClient(api_url=f"http://127.0.0.1:{dead_port}", timeout=2)
    with pytest.raises(HindsightError):
        client.recall_bounded("anything", timeout=2)


def test_cmd_recall_prints_marker_when_client_missing(monkeypatch, capsys):
    """No configured client → explicit marker, exit 0, no Hindsight call."""
    import juggle_cmd_memory as m

    monkeypatch.setattr(m, "_get_hindsight_client", lambda: None)
    m.cmd_recall(argparse.Namespace(thread_id="A", query="q"))
    assert "no memory was consulted" in capsys.readouterr().out.lower()


