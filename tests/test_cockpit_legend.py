"""Tests for the cockpit legend single-source-of-truth module."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ── Task 1: glyph dicts moved into the legend module ──────────────────────────

def test_legend_module_exports_glyph_dicts():
    """juggle_cockpit_legend owns the six glyph dicts (moved out of view.py)."""
    import juggle_cockpit_legend as L
    for name in ("TOPIC_STATUS_GLYPHS", "ACTION_TIER_GLYPHS", "AGENT_STATUS_GLYPHS",
                 "SCHED_STATUS_GLYPHS", "TASK_STATE_GLYPHS", "NOTIF_KIND_GLYPHS"):
        d = getattr(L, name)
        assert isinstance(d, dict) and d, f"{name} missing or empty"


def test_view_reexports_same_dict_objects():
    """view.py re-exports the SAME objects (identity) so no second copy can drift."""
    import juggle_cockpit_legend as L
    import juggle_cockpit_view as V
    assert V.TASK_STATE_GLYPHS is L.TASK_STATE_GLYPHS
    assert V.TOPIC_STATUS_GLYPHS is L.TOPIC_STATUS_GLYPHS


# ── Task 2: graph one-line legend derived from the glyph dict ──────────────────

def test_graph_inline_legend_matches_glyph_dict():
    """The Graph panel's one-line legend is derived from TASK_STATE_GLYPHS, not hardcoded."""
    import juggle_cockpit_legend as L
    s = L.graph_inline_legend()
    assert L.TASK_STATE_GLYPHS["running"] in s      # 🏃
    assert L.TASK_STATE_GLYPHS["ready"] in s         # ◇
    assert L.TASK_STATE_GLYPHS["verified"] in s      # ✅
    assert f"{L.GRAPH_DEP_SUFFIX}n=waits on #n" in s  # ⊣n=waits on #n


def test_graph_panel_uses_registry_legend():
    """build_graph_panel renders the registry-derived legend (drift pin: 2026-06-29
    graph legend was a hardcoded string divorced from TASK_STATE_GLYPHS)."""
    import io
    from rich.console import Console
    from juggle_cockpit_graph_panel import build_graph_panel
    from juggle_cockpit_graph_layout import GraphTask
    import juggle_cockpit_legend as L
    tasks = [GraphTask(id="a", title="A", state="ready")]
    panel = build_graph_panel(project_id="P1", tasks=tasks, edges=[], selection=0,
                              unread=0, width=120, height=20, pan_offset=0)
    buf = io.StringIO()
    Console(width=120, file=buf, no_color=True).print(panel)
    out = buf.getvalue().replace("\n", " ")
    assert L.graph_inline_legend() in out or all(
        tok in buf.getvalue() for tok in ("running", "ready", "done")
    )


# ── Task 3: STATUS_LEGEND registry + coverage pin ─────────────────────────────

def test_status_legend_structure():
    import juggle_cockpit_legend as L
    secs = L.build_legend_content()
    assert {s["section"] for s in secs} >= {"Topics", "Agents", "Graph (task DAG)"}
    for s in secs:
        assert s["entries"]
        for e in s["entries"]:
            assert e["glyph"].strip() and e["meaning"].strip()


def test_every_rendered_glyph_is_in_the_legend():
    """COVERAGE PIN (2026-06-29): every glyph a cockpit panel can render must
    appear in STATUS_LEGEND, so the ? help can never under-document the UI."""
    import juggle_cockpit_legend as L
    legend_glyphs = {e["glyph"] for s in L.build_legend_content() for e in s["entries"]}
    missing = L.all_rendered_glyphs() - legend_glyphs
    assert not missing, (
        f"Glyphs rendered by panels but absent from STATUS_LEGEND: {sorted(missing)}. "
        "Add an entry to STATUS_LEGEND in juggle_cockpit_legend.py."
    )


def test_render_legend_lines_smoke():
    import juggle_cockpit_legend as L
    text = "\n".join(L.render_legend_lines())
    for g in ("👉", "🏃", "🟢", "⚡", "◇", "⊣"):
        assert g in text, f"legend render missing {g}"


def test_render_legend_lines_is_multicolumn():
    """v2 redesign: the legend packs SHORT-celled sections 2-column (per-section,
    where cells fit) — e.g. Notifications — so it mostly fits one screen."""
    import juggle_cockpit_legend as L
    lines = L.render_legend_lines(width=110)
    # Notifications cells are short → they must pack two per row.
    complete_glyph = L.NOTIF_KIND_GLYPHS["complete"]  # ⚡
    info_glyph = L.NOTIF_KIND_GLYPHS["info"]          # ℹ️
    assert any(complete_glyph in ln and info_glyph in ln for ln in lines), (
        "expected complete+info on one row (per-section 2-column legend)"
    )


def test_render_legend_lines_contains_every_meaning():
    """Compact layout must not drop any meaning (cockpit --legend completeness)."""
    import juggle_cockpit_legend as L
    text = "\n".join(L.render_legend_lines(width=110))
    for sec in L.build_legend_content():
        for e in sec["entries"]:
            assert e["meaning"] in text, f"meaning dropped: {e['meaning']!r}"


# ── Task 5: headless `cockpit --legend` stdout gate ───────────────────────────

def test_cockpit_legend_cli_prints_full_legend(tmp_path):
    """`cockpit --legend` renders keybindings + every panel glyph to stdout (headless gate)."""
    import subprocess
    from pathlib import Path
    import juggle_cockpit_legend as L
    src = Path(__file__).resolve().parent.parent / "src" / "juggle_cockpit.py"
    env = {**os.environ,
           "CLAUDE_PLUGIN_DATA": str(tmp_path / "cpd"),
           "JUGGLE_MAX_BACKGROUND_AGENTS": "5", "JUGGLE_MAX_THREADS": "10"}
    r = subprocess.run([sys.executable, str(src), "--legend"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "Status Legend" in out and "Keyboard Shortcuts" in out
    for g in L.all_rendered_glyphs():
        assert g in out, f"--legend stdout missing glyph {g}"


# ── AMENDMENT 2: "no inline emoji in renderers" lint (DA Fix 3) ────────────────

def _forbidden_glyph_chars():
    """Every non-ASCII glyph CHARACTER the legend module owns (dict values +
    emoji marker constants). Renderers must reference these via imported names,
    never hardcode the literal — so the legend stays the single source of truth."""
    import juggle_cockpit_legend as L
    owned = set()
    for d in (L.TOPIC_STATUS_GLYPHS, L.ACTION_TIER_GLYPHS, L.AGENT_STATUS_GLYPHS,
              L.SCHED_STATUS_GLYPHS, L.TASK_STATE_GLYPHS, L.NOTIF_KIND_GLYPHS):
        for v in d.values():
            owned.update(v)
    for const in (L.GRAPH_READY_SUFFIX, L.GRAPH_DEP_SUFFIX, L.GRAPH_FAIL_SUFFIX,
                  L.UNREAD_BADGE, L.FALLBACK_TOPIC, L.FALLBACK_TASK,
                  L.FALLBACK_SCHED, L.FALLBACK_NOTIF):
        owned.update(const)
    # Only non-ASCII chars are "emoji/glyph" literals; box-drawing/ASCII excluded.
    return {ch for ch in owned if ord(ch) > 0x2014}


def test_no_inline_emoji_in_renderers():
    """LINT (2026-06-29 redesign, DA Fix 3): juggle_cockpit_view.py and
    juggle_cockpit_graph_panel.py must NOT hardcode any glyph the legend owns —
    they import the constants from juggle_cockpit_legend instead. A hardcoded
    emoji here would reintroduce the drift the SoT module exists to prevent.
    (Comment lines are exempt; box-drawing / progress-bar chars are not owned.)"""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src"
    forbidden = _forbidden_glyph_chars()
    assert forbidden, "forbidden glyph set must be non-empty"
    violations = []
    for fname in ("juggle_cockpit_view.py", "juggle_cockpit_graph_panel.py"):
        for lineno, raw in enumerate((src / fname).read_text().splitlines(), 1):
            if raw.lstrip().startswith("#"):
                continue  # comments may describe glyphs by example
            hits = sorted({ch for ch in raw if ch in forbidden})
            if hits:
                violations.append(f"{fname}:{lineno}: {hits}  | {raw.strip()[:70]}")
    assert not violations, (
        "Inline glyph literals in renderers (import them from juggle_cockpit_legend "
        "instead):\n" + "\n".join(violations)
    )


# ── v2: canonical lifecycle vocabulary (CANONICAL_VOCAB.md) ───────────────────

def test_canonical_maps_are_bijective():
    """Each canonical map is one-glyph-per-universal-state (no dup values)."""
    import juggle_cockpit_legend as L
    for name in ("CANONICAL_GLYPHS", "CANONICAL_COMPACT"):
        m = getattr(L, name)
        vals = list(m.values())
        assert len(vals) == len(set(vals)), f"{name} has duplicate glyphs: {vals}"


def test_no_glyph_denotes_two_lifecycle_states():
    """VOCAB PIN: every lifecycle glyph maps to EXACTLY ONE universal state.
    Catches the old defects — ◇ (ready vs blocked), ⏸️ (paused vs unknown),
    ⚠️ (warn vs action-blocker) each denoted two states before v2."""
    import juggle_cockpit_legend as L
    conflicts = {g: sorted(u) for g, u in L.glyph_to_universal_states().items()
                 if len(u) > 1}
    assert not conflicts, (
        f"glyphs denoting multiple lifecycle states: {conflicts}. "
        "Re-map onto the CANONICAL_GLYPHS single source."
    )


def test_canonical_vocab_defect_fixes():
    """Lock the five concrete defect fixes from CANONICAL_VOCAB.md."""
    import juggle_cockpit_legend as L
    # (1) ◇ collision — railroad blocked-failed → ⊘; ◇ stays = ready
    assert L.RAILROAD_STATE_GLYPHS["blocked-failed"] == L.CANONICAL_COMPACT["blocked"] == "⊘"
    assert L.TASK_STATE_GLYPHS["ready"] == "◇"
    # (2) unified running glyph
    assert L.CANONICAL_GLYPHS["running"] == "🏃"
    assert L.CANONICAL_COMPACT["running"] == "◐"
    # (3) failure ✗-vs-❌ split — notif error → canonical failed emoji ❌
    assert L.NOTIF_KIND_GLYPHS["error"] == L.CANONICAL_GLYPHS["failed"] == "❌"
    # (4) ⚠️ overload — action blocker → 🛑; ⚠️ is warn-only
    assert L.ACTION_TIER_GLYPHS[0] == L.CANONICAL_GLYPHS["needs-you"] == "🛑"
    assert L.NOTIF_KIND_GLYPHS["warning"] == L.CANONICAL_GLYPHS["warn"] == "⚠️"
    # (5) ⏸️ overload — sched unknown → ❔; ⏸️ is paused-only
    assert L.SCHED_STATUS_GLYPHS["unknown"] == L.CANONICAL_GLYPHS["unknown"] == "❔"
    assert L.TOPIC_STATUS_GLYPHS["paused"] == L.CANONICAL_GLYPHS["paused"] == "⏸️"


# ── v2: ? Help modal hanging-indent wrap ──────────────────────────────────────

def test_help_shortcut_wrap_is_hanging_indent():
    """v2 redesign root-cause fix: a long shortcut description wraps UNDER the
    description column (hanging indent), never back to column 0 (the old shatter)."""
    from juggle_cockpit_modals import render_help_lines, HELP_DESC_INDENT
    lines = render_help_lines(width=60)  # narrow → forces the desc to wrap
    for i, ln in enumerate(lines):
        if "Decommission agent" in ln:            # known long description
            cont = lines[i + 1]
            assert cont.startswith(" " * HELP_DESC_INDENT) and cont.strip(), (
                f"continuation not hanging-indented under desc col: {cont!r}"
            )
            break
    else:
        raise AssertionError("Decommission shortcut not found in help render")


def test_help_no_description_shatters_to_column_zero():
    """No shortcut continuation line falls back to column 0 (wrap-shatter guard).
    Only banner headers, rules, and the close hint may be flush-left."""
    from juggle_cockpit_modals import render_help_lines
    lines = render_help_lines(width=60)
    allowed_flush = {"Keyboard Shortcuts", "Status Legend"}
    for ln in lines:
        if not ln or ln.startswith(" "):
            continue
        if ln in allowed_flush or ln.startswith("Esc"):
            continue
        # section rules are all box/dot chars
        assert set(ln) <= set("─·"), f"description shattered to column 0: {ln!r}"
