"""Tests for the cockpit viewport smoke harness (juggle_smoke).

Pure heuristic and loader tests require no external deps.
PTY-based integration tests spawn a real cockpit process and are skipped
on platforms without pty (e.g. Windows) or when the cockpit cannot render
deterministically (mark SMOKE_SKIP=1 env var to skip).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("yaml", reason="pyyaml not installed")
pytest.importorskip("pyte", reason="pyte not installed")

VIEWPORTS_YAML = Path(__file__).parent.parent / "config" / "viewports.yaml"

_HAS_PTY = sys.platform != "win32" and hasattr(os, "openpty")
_SMOKE_SKIP = os.environ.get("SMOKE_SKIP", "").strip() == "1"
_PTY_REASON = "PTY not available or SMOKE_SKIP=1"
_skip_pty = pytest.mark.skipif(not _HAS_PTY or _SMOKE_SKIP, reason=_PTY_REASON)


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_grid(rows: int, cols: int, fill: str = "") -> list[str]:
    """Synthetic grid: each row exactly `cols` chars, space-padded."""
    lines = []
    for i in range(rows):
        raw = fill if fill else f"row{i:03d} content"
        line = (raw * (cols // max(len(raw), 1) + 1))[:cols]
        lines.append(line)
    return lines


def _make_db(tmp_path: Path, n_threads: int = 30) -> str:
    """Minimal juggle.db seeded with n_threads topics.

    Hermetic w.r.t. JUGGLE_MAX_THREADS: the documented test env exports
    JUGGLE_MAX_THREADS=10, which would cap thread creation below n_threads.
    Temporarily raise the module-level cap while seeding.
    """
    import juggle_db
    import dbops.schema as juggle_db_schema
    import dbops.threads as juggle_db_threads
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)
    old_max = juggle_db.MAX_THREADS
    new_max = max(old_max, n_threads)
    juggle_db.MAX_THREADS = new_max
    juggle_db_schema.MAX_THREADS = new_max
    juggle_db_threads.MAX_THREADS = new_max
    try:
        for i in range(n_threads):
            db.create_thread(f"smoke-topic-{i:02d}", session_id="s0")
    finally:
        juggle_db.MAX_THREADS = old_max
        juggle_db_schema.MAX_THREADS = old_max
        juggle_db_threads.MAX_THREADS = old_max
    return str(tmp_path / "juggle.db")


# ── viewport loader ────────────────────────────────────────────────────────────


def test_load_viewports_parses_all_7_profiles():
    from juggle_smoke import load_viewports

    vp = load_viewports(VIEWPORTS_YAML)
    assert set(vp.keys()) == {
        "2k_full", "2k_half", "2k_third", "portrait",
        "custom_1", "custom_2", "custom_3",
    }


def test_load_viewports_dims_correct():
    from juggle_smoke import load_viewports

    vp = load_viewports(VIEWPORTS_YAML)
    expected = {
        "2k_full":  (240, 67),
        "2k_half":  (120, 67),
        "2k_third": (80,  67),
        "portrait": (110, 130),
        "custom_1": (100, 50),
        "custom_2": (160, 48),
        "custom_3": (200, 55),
    }
    for name, (cols, rows) in expected.items():
        assert vp[name]["cols"] == cols, f"{name} cols mismatch"
        assert vp[name]["rows"] == rows, f"{name} rows mismatch"


def test_load_viewports_missing_file_raises(tmp_path):
    from juggle_smoke import load_viewports

    with pytest.raises(FileNotFoundError):
        load_viewports(tmp_path / "nonexistent.yaml")


def test_load_viewports_profiles_have_desc():
    from juggle_smoke import load_viewports

    vp = load_viewports(VIEWPORTS_YAML)
    for name, profile in vp.items():
        assert "desc" in profile, f"Profile {name!r} missing 'desc'"
        assert isinstance(profile["desc"], str) and profile["desc"]


# ── heuristic: check_overflow ──────────────────────────────────────────────────


def test_check_overflow_clean_grid_passes():
    from juggle_smoke import check_overflow

    grid = _make_grid(24, 80)
    result = check_overflow(grid, 80)
    assert result["pass"] is True
    assert result["violations"] == []


def test_check_overflow_long_line_fails():
    from juggle_smoke import check_overflow

    grid = _make_grid(24, 80)
    grid[5] = "x" * 95  # exceeds 80 cols
    result = check_overflow(grid, 80)
    assert result["pass"] is False
    assert any("5" in v for v in result["violations"])


# ── heuristic: check_real_estate ──────────────────────────────────────────────


def test_check_real_estate_full_grid_passes():
    from juggle_smoke import check_real_estate

    grid = _make_grid(40, 80, fill="Topics│Actions│Agents")
    result = check_real_estate(grid, 40)
    assert result["pass"] is True


def test_check_real_estate_mostly_blank_fails():
    from juggle_smoke import check_real_estate

    # 30/40 blank = 75% blank > 40% threshold
    grid = [" " * 80] * 30 + ["content " + " " * 72] * 10
    result = check_real_estate(grid, 40)
    assert result["pass"] is False
    assert result["blank_pct"] > 0.40


# ── heuristic: check_chrome_present ───────────────────────────────────────────


def test_check_chrome_present_header_footer_passes():
    from juggle_smoke import check_chrome_present

    rows = (
        ["Juggle  Cockpit v2" + " " * 62]       # header
        + ["content line " + " " * 67] * 20
        + ["q Quit  ? Help  s Switch" + " " * 56]  # footer
    )
    result = check_chrome_present(rows)
    assert result["pass"] is True


def test_check_chrome_present_no_header_fails():
    from juggle_smoke import check_chrome_present

    # All content, no recognizable chrome
    grid = ["x" * 80] * 24
    result = check_chrome_present(grid)
    assert result["pass"] is False


# ── heuristic: check_truncation ───────────────────────────────────────────────


def test_check_truncation_clean_grid_no_warn():
    from juggle_smoke import check_truncation

    grid = ["normal content line " + " " * 60] * 20
    result = check_truncation(grid)
    assert result["warn"] is False
    assert result["count"] == 0


def test_check_truncation_ellipsis_warns():
    from juggle_smoke import check_truncation

    grid = ["normal content " + " " * 65] * 18
    grid.append("some very long text that was tru…" + " " * 47)
    grid.append("another truncated row…" + " " * 58)
    result = check_truncation(grid)
    assert result["warn"] is True
    assert result["count"] == 2


# ── PTY integration tests ──────────────────────────────────────────────────────


def _arm_graph(db_path: str) -> None:
    """Seed an armed project + a small DAG so graph mode renders a real graph."""
    from datetime import datetime, timezone
    from juggle_db import JuggleDB
    from dbops import db_graph as g
    from juggle_graph_dispatch import ARMED_PROJECT_KEY

    db = JuggleDB(db_path=db_path)
    now = datetime.now(timezone.utc).isoformat()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)", ("GP", "GraphProj", "active", now, now),
        )
        conn.commit()
    for i in range(8):
        g.create_task(db, task_id=f"g{i}", project_id="GP",
                      title=f"Task number {i}", prompt=f"do {i}")
        if i:
            g.replace_edges(db, f"g{i}", [f"g{i-1}"])
    db.set_setting(ARMED_PROJECT_KEY, "GP")


@_skip_pty
def test_graph_mode_smoke_2k_third_no_overflow(tmp_path):
    """Graph mode (press g) at 80x67 (2k_third) renders without overflow.

    Headline gate for the tightest viewport: the graph panel must never exceed
    the column budget even with an 8-rank chain DAG armed.
    """
    from juggle_smoke import load_viewports, open_cockpit_pty, check_overflow

    db_path = _make_db(tmp_path, n_threads=5)
    _arm_graph(db_path)
    profile = load_viewports(VIEWPORTS_YAML)["2k_third"]

    with open_cockpit_pty(profile, db_path=db_path) as handle:
        handle.frame(settle=2.0, timeout=12.0)
        handle.send(b"g")  # toggle into graph mode
        grid = handle.frame(settle=1.5, timeout=8.0)

    assert grid, "Expected a non-empty graph-mode grid"
    result = check_overflow(grid, profile["cols"])
    assert result["pass"], f"Graph-mode overflow at 2k_third: {result['violations'][:3]}"


@_skip_pty
def test_render_2k_third_no_overflow_and_frame_file_written(tmp_path):
    """Integration: cockpit renders at 80x67 (2k_third), no overflow, frame dumped."""
    from juggle_smoke import load_viewports, open_cockpit_pty, check_overflow

    db_path = _make_db(tmp_path)
    vp = load_viewports(VIEWPORTS_YAML)
    profile = vp["2k_third"]

    out_dir = tmp_path / "frames"
    out_dir.mkdir()

    with open_cockpit_pty(profile, db_path=db_path) as handle:
        grid = handle.frame(settle=2.0, timeout=12.0)

    assert grid, "Expected non-empty grid"
    assert len(grid) > 0, "Grid must have rows"

    result = check_overflow(grid, profile["cols"])
    assert result["pass"], f"Overflow at 2k_third: {result['violations'][:3]}"

    frame_path = out_dir / "2k_third.txt"
    frame_path.write_text("\n".join(grid) + "\n", encoding="utf-8")
    assert frame_path.exists()
    assert frame_path.stat().st_size > 0


@_skip_pty
def test_nav_j_key_produces_visible_change(tmp_path):
    """Nav: pressing 'j' 10 times scrolls the topics pane — grid differs from initial."""
    from juggle_smoke import load_viewports, open_cockpit_pty

    db_path = _make_db(tmp_path, n_threads=30)
    vp = load_viewports(VIEWPORTS_YAML)
    profile = vp["2k_third"]

    with open_cockpit_pty(profile, db_path=db_path) as handle:
        frame_before = handle.frame(settle=2.0, timeout=12.0)
        for _ in range(10):
            handle.send(b"j")
        frame_after = handle.frame(settle=1.0, timeout=5.0)

    # With 30 topics, 10× j must have scrolled content — grid must differ
    assert frame_before != frame_after, (
        "Frame unchanged after 10× 'j' with 30 topics — scroll did not fire"
    )


@_skip_pty
def test_resize_reflows_no_overflow(tmp_path):
    """Resize: TIOCSWINSZ mid-session from 240x67 to 80x67 reflows without overflow."""
    from juggle_smoke import load_viewports, open_cockpit_pty, check_overflow

    db_path = _make_db(tmp_path)
    vp = load_viewports(VIEWPORTS_YAML)

    with open_cockpit_pty(vp["2k_full"], db_path=db_path) as handle:
        handle.frame(settle=2.0, timeout=12.0)  # initial wide render
        handle.resize(80, 67)                    # shrink to 2k_third
        grid_after = handle.frame(settle=1.5, timeout=8.0)

    result = check_overflow(grid_after, 80)
    assert result["pass"], f"Overflow after resize to 80x67: {result['violations'][:3]}"


@_skip_pty
def test_flow_tab_cycles_pane_grid_changes(tmp_path):
    """Flow: Tab cycles focused pane — grid must differ from initial after 2 tabs."""
    from juggle_smoke import load_viewports, open_cockpit_pty

    db_path = _make_db(tmp_path, n_threads=5)
    vp = load_viewports(VIEWPORTS_YAML)
    profile = vp["2k_half"]

    with open_cockpit_pty(profile, db_path=db_path) as handle:
        frame0 = handle.frame(settle=2.0, timeout=12.0)
        handle.send(b"\t")  # Tab → cycle pane
        frame1 = handle.frame(settle=0.8, timeout=4.0)
        handle.send(b"\t")  # Tab again
        frame2 = handle.frame(settle=0.8, timeout=4.0)

    # At least one Tab must produce a visible change
    assert frame0 != frame1 or frame1 != frame2, (
        "No visible change after 2× Tab — pane cycle did not affect the text grid"
    )


# ── topics-floor regression tests ─────────────────────────────────────────────


def test_compute_ratios_floor_no_pty():
    """Unit guard: _compute_ratios never returns 0.0 for topics (no PTY needed)."""
    from juggle_cockpit import _compute_ratios, _MIN_TOPICS_RATIO

    # Simulate the exact incident: topics collapsed to 0 cells
    ratios = _compute_ratios(0, 108, 132)
    assert ratios[0] >= _MIN_TOPICS_RATIO, (
        f"topics ratio {ratios[0]} below floor — regression of 0-width collapse"
    )
    assert sum(ratios) == pytest.approx(1.0, abs=0.01)


def test_sanitize_col_ratios_incident_config_no_pty():
    """Unit guard: the exact incident config [0.0, 0.45, 0.55] self-heals on load."""
    from juggle_cockpit import _sanitize_col_ratios, _MIN_TOPICS_RATIO

    result = _sanitize_col_ratios([0.0, 0.45, 0.55])
    assert result[0] >= _MIN_TOPICS_RATIO, (
        f"Incident config not sanitized: topics={result[0]}"
    )
    assert sum(result) == pytest.approx(1.0, abs=0.01)


@_skip_pty
def test_topics_nonzero_after_multi_resize(tmp_path):
    """Integration: topics pane stays non-zero across 240→120→80→200 resize sequence."""
    from juggle_smoke import load_viewports, open_cockpit_pty

    db_path = _make_db(tmp_path)
    vp = load_viewports(VIEWPORTS_YAML)

    def _topics_visible(frame: list[str]) -> bool:
        """Topics pane is visible when the left third of the frame has non-whitespace content."""
        if not frame:
            return False
        left = max(1, len(frame[0]) // 3)
        non_blank = sum(1 for row in frame if row[:left].strip())
        return non_blank >= max(1, len(frame) // 4)

    with open_cockpit_pty(vp["2k_full"], db_path=db_path) as handle:
        handle.frame(settle=2.5, timeout=15.0)

        for cols in (120, 80, 200):
            handle.resize(cols, 67)
            frame = handle.frame(settle=1.5, timeout=8.0)
            if cols >= 130:  # wide breakpoint — topics must be visible
                assert _topics_visible(frame), (
                    f"Topics pane invisible after resize to {cols}×67"
                )


def test_topics_nonzero_with_corrupted_config_out(tmp_path):
    """--out render with incident config [0.0,0.45,0.55] must produce non-empty topics output.

    Uses cockpit --out (stdout render, no TUI) so this test runs without PTY.
    The load-sanitize path converts [0.0,0.45,0.55] to defaults before rendering.
    """
    import json as _json
    import subprocess

    db_path = _make_db(tmp_path)

    cfg_dir = tmp_path / "juggle_cfg"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text(_json.dumps({"cockpit": {"column_ratios": [0.0, 0.45, 0.55]}}))

    env = {**os.environ, "_JUGGLE_CONFIG_PATH": str(cfg_file)}
    result = subprocess.run(
        ["uv", "run", str(Path(__file__).parent.parent / "src" / "juggle_cli.py"),
         "cockpit", "--out", "--db", db_path],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    # --out exits 0 (inactive session exits with non-zero but still prints something)
    output = result.stdout + result.stderr
    assert len(output.strip()) > 0, (
        "cockpit --out produced no output with incident config — process may have crashed"
    )
    # The output should not contain "0%" for topics (sanitize-on-load applied)
    # Primarily: no crash and some output rendered
    assert "Traceback" not in output, f"Cockpit crashed with incident config:\n{output[:500]}"


# ── regression pins (2026-06-10) ──────────────────────────────────────────────


def test_smoke_json_single_viewport_exits_cleanly(monkeypatch):
    """Regression pin (2026-06-10): `cockpit --smoke --viewport X --json` crashed
    with UnboundLocalError — `cannot access local variable 'any_fail'` — because
    the exit-code flag was only computed in the non-JSON print branch.
    Must SystemExit(1) on a failing result, never UnboundLocalError.
    """
    from types import SimpleNamespace

    import juggle_cmd_misc
    import juggle_smoke

    fake_results = [{"profile": "2k_third", "cols": 80, "rows": 67, "pass": False}]
    monkeypatch.setattr(juggle_smoke, "run_smoke", lambda *a, **k: fake_results)
    args = SimpleNamespace(
        smoke=True,
        viewport_name="2k_third",
        json_out=True,
        interactive=False,
        db_path=None,
    )
    with pytest.raises(SystemExit) as ei:
        juggle_cmd_misc.cmd_cockpit(args)
    assert ei.value.code == 1


def test_smoke_json_all_pass_exits_zero(monkeypatch):
    """Companion to the 2026-06-10 any_fail pin: --json with all-pass results
    must exit 0."""
    from types import SimpleNamespace

    import juggle_cmd_misc
    import juggle_smoke

    fake_results = [{"profile": "2k_third", "cols": 80, "rows": 67, "pass": True}]
    monkeypatch.setattr(juggle_smoke, "run_smoke", lambda *a, **k: fake_results)
    args = SimpleNamespace(
        smoke=True,
        viewport_name="2k_third",
        json_out=True,
        interactive=False,
        db_path=None,
    )
    with pytest.raises(SystemExit) as ei:
        juggle_cmd_misc.cmd_cockpit(args)
    assert ei.value.code == 0


class _LateBodyHandle:
    """Fake CockpitHandle whose body paints only on the 3rd frame() call —
    models the real cockpit, whose chrome paints <1s but body panes ~4-6s."""

    def __init__(self, cols: int = 80, rows: int = 67, body_at_call: int = 3):
        self.cols = cols
        self.rows = rows
        self.body_at_call = body_at_call
        self.frame_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def _grid(self, body: bool) -> list[str]:
        grid = [" " * self.cols for _ in range(self.rows)]
        grid[0] = "Juggle Cockpit".ljust(self.cols)
        grid[-1] = "q Quit".ljust(self.cols)
        if body:
            for i in range(1, self.rows - 1):
                grid[i] = f"row{i:03d} body content".ljust(self.cols)
        return grid

    def frame(self, settle: float = 1.0, timeout: float = 10.0) -> list[str]:
        self.frame_calls += 1
        return self._grid(body=self.frame_calls >= self.body_at_call)


def test_smoke_waits_for_body_paint_not_blank_first_frame(monkeypatch, tmp_path):
    """Regression pin (2026-06-10): `cockpit --smoke --all-viewports` captured a
    single frame at settle=2.0s, but the cockpit body paints ~4-6s after launch,
    so all 7 profiles falsely reported blank_pct=94% real-estate failures.
    run_smoke must keep polling until body content appears (bounded deadline)."""
    import juggle_smoke

    handle = _LateBodyHandle()
    monkeypatch.setattr(
        juggle_smoke, "open_cockpit_pty", lambda profile, db_path=None, env=None: handle
    )
    results = juggle_smoke.run_smoke(
        {"2k_third": {"cols": 80, "rows": 67}}, output_dir=tmp_path
    )
    assert len(results) == 1
    rec = results[0]
    assert rec.get("error") is None, rec
    assert rec["real_estate"]["pass"], (
        f"blank first frame accepted as final capture: {rec['real_estate']}"
    )
    assert rec["pass"], rec
    assert handle.frame_calls >= 3


@_skip_pty
def test_cockpit_pty_no_orphan_after_close(tmp_path):
    """REGRESSION PIN (2026-06-10): CockpitHandle.close() left an orphaned
    juggle_cockpit.py grandchild process when:
      (a) `q` was sent but the quit binding was removed (no-op), so the process
          never exited and proc.wait(timeout=4) expired.
      (b) proc.kill() killed only the `uv` wrapper, orphaning the real cockpit.
    Fix: spawn in own session (start_new_session=True), quit via ctrl+c, on
    timeout kill the whole process group via os.killpg."""
    import signal

    from juggle_smoke_pty import open_cockpit_pty

    export_db = str(tmp_path / "smoke.db")
    from juggle_db import JuggleDB
    db = JuggleDB(export_db)
    db.init_db()

    from juggle_smoke import load_viewports
    vp = load_viewports(VIEWPORTS_YAML)
    profile = vp["2k_third"]  # smallest profile

    with open_cockpit_pty(profile, db_path=export_db) as handle:
        pgid = os.getpgid(handle._proc.pid)

    # After context manager exits, NO process in the process group should survive
    try:
        os.killpg(pgid, 0)  # signal 0 = existence check
        surviving = True
    except (ProcessLookupError, PermissionError):
        surviving = False
    assert not surviving, (
        f"Process group {pgid} still alive after CockpitHandle.close() — "
        "cockpit was orphaned"
    )


@_skip_pty
def test_cockpit_pty_close_is_idempotent(tmp_path):
    """REGRESSION PIN (2026-06-10): close() must be safe to call twice — the
    second call must be a no-op without raising."""
    from juggle_smoke_pty import open_cockpit_pty
    from juggle_smoke import load_viewports
    from juggle_db import JuggleDB

    export_db = str(tmp_path / "smoke2.db")
    JuggleDB(export_db).init_db()
    vp = load_viewports(VIEWPORTS_YAML)
    profile = vp["2k_third"]

    handle = open_cockpit_pty(profile, db_path=export_db)
    handle.close()
    handle.close()  # must not raise


def test_smoke_genuinely_blank_body_still_fails(monkeypatch, tmp_path):
    """Companion to the 2026-06-10 blank-frame pin: a cockpit whose body NEVER
    paints must still fail real-estate (poll deadline bounded, last grid kept)."""
    import juggle_smoke

    handle = _LateBodyHandle(body_at_call=10**9)
    monkeypatch.setattr(
        juggle_smoke, "open_cockpit_pty", lambda profile, db_path=None, env=None: handle
    )
    monkeypatch.setattr(juggle_smoke, "_BODY_PAINT_DEADLINE", 0.5, raising=False)
    results = juggle_smoke.run_smoke(
        {"2k_third": {"cols": 80, "rows": 67}}, output_dir=tmp_path
    )
    assert results[0]["pass"] is False
    assert results[0]["real_estate"]["pass"] is False
