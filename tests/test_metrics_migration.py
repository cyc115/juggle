"""agent_runs metrics columns (2026-06-30 orchestration-metrics Task 1)."""


def test_agent_runs_has_metric_columns(juggle_db):
    """2026-06-30 orchestration-metrics: agent_runs carries token+prompt columns."""
    with juggle_db._connect() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(agent_runs)")}
    for col in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "session_id", "prompt_fingerprint", "prompt_version", "prompt_bytes",
                "agent_cwd"):
        assert col in cols
