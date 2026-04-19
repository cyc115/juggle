"""Hindsight HTTP API client for Juggle memory integration.

All Hindsight communication goes through this module.
No dependency on the hindsight CLI binary.
"""

import json
import logging
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from juggle_settings import get_settings as _get_settings

_log = logging.getLogger(__name__)

def _hs() -> dict:
    """Shortcut: return the hindsight settings section."""
    return _get_settings()["hindsight"]

def _paths() -> dict:
    """Shortcut: return the paths settings section."""
    return _get_settings()["paths"]

# Kept as module-level constants for backward compat (used as __init__ defaults below)
DEFAULT_API_URL: str = _hs()["api_url"]
DEFAULT_API_KEY: str = _hs()["api_key"]
DEFAULT_BANK: str = _hs()["bank"]
DEFAULT_TIMEOUT: int = _hs()["timeout_secs"]

JUGGLE_CONFIG_DIR = Path(_paths()["config_dir"])
JUGGLE_CONFIG_PATH = JUGGLE_CONFIG_DIR / "config.json"
JUGGLE_LOG_DIR = Path(_paths()["digest_log_dir"])


class HindsightError(Exception):
    pass


class HindsightClient:
    """HTTP client for Hindsight recall/retain API."""

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        api_key: str = DEFAULT_API_KEY,
        bank: str = DEFAULT_BANK,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.bank = bank
        self.timeout = timeout

    @classmethod
    def from_config(cls, config_path: str | None = None) -> "HindsightClient | None":
        """Load client from config. If config_path provided and exists, reads it directly.

        Falls back to get_settings() (reads _JUGGLE_CONFIG_PATH or ~/.juggle/config.json).
        """
        if config_path:
            import json as _json
            p = Path(config_path)
            if not p.exists():
                return None
            try:
                raw = _json.loads(p.read_text())
            except (OSError, _json.JSONDecodeError):
                return None
            hs = raw.get("hindsight", {})
        else:
            hs = _hs()
        if not hs.get("enabled", False):
            return None
        return cls(
            api_url=hs.get("api_url", DEFAULT_API_URL),
            api_key=hs.get("api_key", DEFAULT_API_KEY),
            bank=hs.get("bank", DEFAULT_BANK),
            timeout=hs.get("timeout_secs", DEFAULT_TIMEOUT),
        )

    def _request(self, method: str, path: str, body: dict | None = None, timeout: int | None = None) -> dict:
        """Make HTTP request to Hindsight API. Returns parsed JSON or empty dict."""
        url = f"{self.api_url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as e:
            _log.debug("Hindsight API error: %s %s — %s", method, path, e)
            raise HindsightError(str(e)) from e

    def _request_with_retry(self, method: str, path: str, body: dict | None = None, timeout: int | None = None) -> dict:
        """Request with one retry after auto-restart on failure."""
        try:
            return self._request(method, path, body, timeout=timeout)
        except HindsightError:
            _log.info("Hindsight unreachable, attempting auto-restart...")
            self._restart_service()
            try:
                return self._request(method, path, body, timeout=timeout)
            except HindsightError as e:
                self._log_error(f"Failed after restart: {e}")
                return {}

    def _restart_service(self):
        """Attempt to restart the juggle-hindsight Docker service."""
        compose_path = Path(__file__).parent.parent / "docker" / "docker-compose.yml"
        env_file = JUGGLE_CONFIG_DIR / ".env"
        cmd = ["docker", "compose"]
        if env_file.exists():
            cmd += ["--env-file", str(env_file)]
        cmd += ["-f", str(compose_path), "up", "-d"]
        try:
            subprocess.run(cmd, capture_output=True, timeout=15)
            import time
            time.sleep(2)  # wait for service to start
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            _log.warning("Failed to restart hindsight: %s", e)

    def _log_error(self, msg: str):
        """Append error to log file."""
        try:
            JUGGLE_LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = JUGGLE_LOG_DIR / "memory-errors.log"
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).isoformat()
            with open(log_path, "a") as f:
                f.write(f"[{ts}] {msg}\n")
        except OSError:
            pass

    def health_check(self) -> bool:
        """Check if Hindsight service is healthy."""
        try:
            result = self._request("GET", "/health")
            return result.get("status") == "healthy"
        except HindsightError:
            return False

    def recall(self, query: str, max_tokens: int = 4096) -> str:
        """Recall memories matching query via vector search."""
        if not query.strip():
            return ""
        body = {"query": query, "max_tokens": max_tokens}
        result = self._request_with_retry(
            "POST",
            f"/v1/default/banks/{self.bank}/memories/recall",
            body,
        )
        results = result.get("results", [])
        if not results:
            return ""
        lines = []
        for r in results:
            text = r.get("text", "")
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)

    def reflect(self, query: str, timeout: int = 60) -> str:
        """Reflect on memories matching query. Returns LLM-synthesized answer or empty string."""
        if not query.strip():
            return ""
        body = {"query": query}
        result = self._request_with_retry(
            "POST",
            f"/v1/default/banks/{self.bank}/reflect",
            body,
            timeout=min(timeout, _hs()["reflect_timeout_secs"]),
        )
        return result.get("text", "")

    def retain(self, content: str, context: str | None = None) -> None:
        """Retain content as memory. Non-blocking — failures are logged, not raised."""
        if not content.strip():
            return
        item: dict = {"content": content}
        if context:
            item["context"] = context
        body = {"items": [item]}
        try:
            self._request_with_retry(
                "POST",
                f"/v1/default/banks/{self.bank}/memories",
                body,
            )
        except Exception as e:
            self._log_error(f"retain failed: {e}")
