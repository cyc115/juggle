"""TDD tests for cockpit --profile harness.

Covers:
  1. _parse_psrecord_log: parses psrecord log format into summary dict
  2. _parse_psrecord_log: empty / header-only log returns {}
  3. _parse_psrecord_log: RSS growth > 20 MB is detectable
  4. _parse_psrecord_log: avg CPU > 15% is detectable
  5. _profile_worker_loop: runs exactly N iterations for N-second duration (mocked clock)
  6. _profile_worker_loop: duration=0 → 0 iterations

No real psrecord / uvx required.  time.sleep is mocked to keep tests fast.
"""

import os
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("textual")  # juggle_cockpit has textual at module level

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SAMPLE_LOG = """\
# Elapsed time   CPU (%)     Real (MB)   Virtual (MB)
0.000            5.0         100.0       500.0
0.500            10.0        102.0       502.0
1.000            8.0         105.0       505.0
1.500            6.0         108.0       508.0
2.000            7.0         110.0       510.0
"""


# ---------------------------------------------------------------------------
# _parse_psrecord_log
# ---------------------------------------------------------------------------


def test_parse_psrecord_log_basic():
    """Parses all summary fields from a typical psrecord log."""
    from juggle_cockpit import _parse_psrecord_log

    stats = _parse_psrecord_log(SAMPLE_LOG)

    assert stats["rss_start"] == pytest.approx(100.0)
    assert stats["rss_end"] == pytest.approx(110.0)
    assert stats["rss_growth"] == pytest.approx(10.0)
    assert stats["peak_rss"] == pytest.approx(110.0)
    assert stats["peak_cpu"] == pytest.approx(10.0)
    expected_avg = (5.0 + 10.0 + 8.0 + 6.0 + 7.0) / 5
    assert stats["avg_cpu"] == pytest.approx(expected_avg)


def test_parse_psrecord_log_empty():
    """Header-only log returns empty dict (no data rows)."""
    from juggle_cockpit import _parse_psrecord_log

    log = "# Elapsed time   CPU (%)     Real (MB)   Virtual (MB)\n"
    stats = _parse_psrecord_log(log)
    assert stats == {}


def test_parse_psrecord_log_rss_growth_above_threshold():
    """RSS growth > 20 MB is detectable via rss_growth key."""
    from juggle_cockpit import _parse_psrecord_log

    log = (
        "# header\n"
        "0.0  5.0   50.0  200.0\n"
        "1.0  5.0   75.0  225.0\n"   # 25 MB growth
    )
    stats = _parse_psrecord_log(log)
    assert stats["rss_growth"] == pytest.approx(25.0)
    assert stats["rss_growth"] > 20.0, "rss_growth should exceed 20 MB threshold"


def test_parse_psrecord_log_high_cpu_above_threshold():
    """Avg CPU > 15% is detectable via avg_cpu key."""
    from juggle_cockpit import _parse_psrecord_log

    log = (
        "# header\n"
        "0.0  20.0  100.0  200.0\n"
        "1.0  25.0  100.0  200.0\n"  # avg = 22.5%
    )
    stats = _parse_psrecord_log(log)
    assert stats["avg_cpu"] == pytest.approx(22.5)
    assert stats["avg_cpu"] > 15.0, "avg_cpu should exceed 15% threshold"


# ---------------------------------------------------------------------------
# _profile_worker_loop
# ---------------------------------------------------------------------------


def test_profile_worker_loop_runs_n_iterations():
    """Worker loop calls tick exactly N times for N-second duration (mocked clock).

    Time sequence for duration=3, expecting 3 iterations:
      call  1: end = 0.0 + 3 = 3.0
      call  2: loop check  0.0 < 3.0 → iter 1
      call  3: tick_start = 0.0
      call  4: elapsed = 0.5 − 0.0 = 0.5 → sleep(0.5)
      call  5: loop check  1.0 < 3.0 → iter 2
      call  6: tick_start = 1.0
      call  7: elapsed = 1.5 − 1.0 = 0.5 → sleep(0.5)
      call  8: loop check  2.0 < 3.0 → iter 3
      call  9: tick_start = 2.0
      call 10: elapsed = 2.5 − 2.0 = 0.5 → sleep(0.5)
      call 11: loop check  3.0 < 3.0 → False → exit
    """
    from juggle_cockpit import _profile_worker_loop

    tick_calls = []

    def fake_tick():
        tick_calls.append(True)

    time_sequence = [0.0, 0.0, 0.0, 0.5, 1.0, 1.0, 1.5, 2.0, 2.0, 2.5, 3.0]

    with patch("juggle_cockpit.time") as mock_time:
        mock_time.time.side_effect = time_sequence
        mock_time.sleep = Mock()
        result = _profile_worker_loop(3, db_path=None, _tick_fn=fake_tick)

    assert result == 3
    assert len(tick_calls) == 3


def test_profile_worker_loop_zero_duration():
    """Worker loop with duration=0 runs zero iterations immediately."""
    from juggle_cockpit import _profile_worker_loop

    tick_calls = []

    def fake_tick():
        tick_calls.append(True)

    # duration=0: end = 0.0 + 0 = 0.0; first check 0.0 < 0.0 → False
    time_sequence = [0.0, 0.0]

    with patch("juggle_cockpit.time") as mock_time:
        mock_time.time.side_effect = time_sequence
        mock_time.sleep = Mock()
        result = _profile_worker_loop(0, db_path=None, _tick_fn=fake_tick)

    assert result == 0
    assert len(tick_calls) == 0
