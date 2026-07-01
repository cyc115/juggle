"""Cost/perf/quality compute (2026-06-30 orchestration-metrics Task 7)."""
import juggle_metrics as m


def _run(**kw):
    base = dict(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
                status="completed", dispatched_at="2026-06-30T12:00:00",
                completed_at="2026-06-30T12:01:00", agent_id="a", task_id="t", input_prompt="")
    base.update(kw)
    return base


def test_tokens_per_dispatch():
    runs = [_run(input_tokens=100, output_tokens=10), _run(input_tokens=200, output_tokens=20)]
    c = m.cost(runs)
    assert c["tokens_per_dispatch"] == 165.0  # (110+220)/2


def test_wall_per_dispatch():
    p = m.performance([_run()])  # 60s window
    assert p["wall_per_dispatch_secs"] == 60.0


def test_queue_wait_deferred_null():
    assert m.performance([_run()])["queue_wait_secs"] is None


def test_completion_pct(juggle_db):
    runs = [_run(status="completed"), _run(status="failed")]
    q = m.quality(juggle_db, runs)
    assert q["completion_pct"] == 50.0


def test_token_coverage_flags_broken_pipeline():
    """2026-06-30 orchestration-metrics: token_coverage = % completed runs with
    nonzero tokens — makes a silently-broken token pipeline VISIBLE."""
    runs = [_run(input_tokens=100), _run(input_tokens=0, output_tokens=0)]
    assert m.cost(runs)["token_coverage"] == 50.0


def test_compute_metrics_shape(juggle_db):
    out = m.compute_metrics(juggle_db, [_run()])
    assert set(out) == {"cost", "performance", "quality"}
