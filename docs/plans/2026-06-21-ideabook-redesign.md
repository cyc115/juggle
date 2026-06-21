# Idea Book Redesign — TUI Hero View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote the Idea Book from a hidden `F5` DataTable modal to the home/hero view — a full-screen Textual surface with a pipeline spine, three body layouts (BOARD kanban / TABLE / SPLIT), the 3-part thesis triad cards, a live distance-to-entry badge, and a deep inspector — recreating `design_handoff_ideabook/Idea Book.dc.html` as Python/Textual/Rich.

**Architecture:** The redesign ships as a **new, isolated full-screen Textual `Screen` (`IdeaBookHomeScreen`)** that the app auto-pushes at boot (the hero) and that `F5` toggles. The existing news-feed `NewsTuiApp` base screen is left **100% untouched** — every existing regression pin stays green, and the new surface gets its own geometry pins, pilot tests, and tty canary. All view logic (distance, triad, lifecycle, counts, sort, filters) lives in **pure functions** seeded directly from the HTML's JS so an agent can verify each in isolation. Status/spine/toolbar/footer are pure markup builders; BOARD/TABLE/inspector are small region widgets. Per-session UI state lives in an `IdeaBookState` object on the screen instance and persists through the existing `ui_state.json`.

**Tech Stack:** Python 3 (`uv run`), Textual (App/Screen/Widget/DataTable/TextArea), Rich (Table/Text/Panel renderables), SQLite (`user_state.db` `ideas` table + `news.db` `quote_cache`), pytest (`-m pilot`, `-m tty`).

---

## Global Constraints

Copied verbatim from the design handoff (`design_handoff_ideabook/README.md`) and the project gates (`CLAUDE.md`). Every task's requirements implicitly include this section.

- **Color palette (24-bit, exact hex).** `bg #0a0c0e` · `bg-panel #0c0e11` · `bg-panel-2 #0b0e11` · `bg-card #0f1318` · `bg-card-sel #15191f` · `bg-inset #0d1116` · `line #1c2128` · `line-dim #161b21` · `txt #aab4bf` · `txt-bright #e9edf1` · `txt-dim #7a838d` · `txt-fade #5b636d` · `txt-faint #3a424b`.
- **Status / semantic colors (LOAD-BEARING — drive column accent + card left-border + chips).** open/bullish/value-good/LOW = `#3fb950` green · triggered/IN-ZONE/MED/brand = `#e8b13d` amber (brand accent `#f2a93b`) · entered = `#4cc2d6` cyan · closed = `#6b7480` gray · rejected/drawdown/bearish/HIGH/loss = `#f0524a` red · activist/smart-money-here = `#9d8cf5` violet. Rule of thumb: **drawdown = red, smart-money = amber, valuation = green, status drives the column accent + card left-border.**
- **Translate px → cells** per the README mapping table: pixel widths → fixed cell widths / `fr`; `border-radius`/`box-shadow`/`hover` → drop; font-size hierarchy → weight + color + caps + spacing. Square terminal cells; use reverse-video / brighter bg for selection.
- **Glyphs only (no assets):** `● ▸ ✕ ⚑ ⚠ › ▾ ★ ✓ −`.
- **Density:** card padding ≈ 1 cell horizontal / 1 row between groups; column gap ≈ 1 cell. Maximum density (user's explicit choice).
- **Footer cheatsheet (verbatim):** `j/k move · v layout · e advance · x reject · i inspector · / search · : cmd · F5 ideas · ? help`.
- **Gates (mandatory, all must stay green at each milestone):**
  - **Harness smoke:** `uv run pytest -m pilot` green + full `uv run pytest` green. Paste the summary line as completion evidence.
  - **UI-parity:** every signal/field/filter reachable in the TUI, each with explicit pilot tests. A CLI-only feature is incomplete.
  - **Geometry:** any layout/composition change adds pilot GEOMETRY tests — every primary region mounted AND visible AND `region.width > 0` after boot, after layout toggle, and after each panel open/close. State-only assertions are insufficient.
  - **Real-TTY (`-m tty`):** keep minimal — boot canaries only at 2 viewports; NO per-feature tty cases.
  - **Env-parity:** at least one test runs the real `scripts/news-tui` launcher as a subprocess and asserts on rendered output (the tty canary), with mocks only at the network boundary; silent error→placeholder conversions are forbidden (errors render loudly).
  - **Regression-pin:** every bug/regression fix adds a pinned test that fails RED pre-fix, names the incident (date + symptom) in its docstring, and lives in the standard suite.
  - **Architecture:** small single-purpose modules (≤300 LOC); EXTRACT before adding when a touched file has outgrown its purpose; refactor commits separate from behavior commits.
  - **Plain-English guide:** ship `docs/guide/` coverage for a NON-finance audience (define short interest, 13D, drawdown, fwd PE, EV/EBITDA, insider cluster on first use).
  - **TDD:** write the failing test before implementation for every behavior.

---

## File Structure

**New modules (all small, single-purpose, ≤300 LOC each):**

| File | Responsibility | LOC budget |
|---|---|---|
| `news/ideabook_logic.py` | Pure view logic: `distance_to_entry`, `thesis_sentence`, `smart_money_sentiment`, `lifecycle_advance`, `lifecycle_reject`, `pipeline_counts`, `sort_ideas`, `filter_visible`, `risk_rank`. No DB, no Textual. | ≤150 |
| `news/ideabook_data.py` | `IdeaVM` (display view-model) + `load_ideabook(user_conn, news_conn) -> list[IdeaVM]`: reads `ideas`, joins live `px` from `quote_cache`, computes distance/triad. | ≤180 |
| `news/app/ideabook/__init__.py` | Package marker. | ≤5 |
| `news/app/ideabook/theme.py` | `PALETTE` (hex dict), `STATUS_COLOR`, `RISK_COLOR`, `SMART_MONEY_SENTIMENT`, `IDEABOOK_CSS` (Textual CSS string). | ≤120 |
| `news/app/ideabook/state.py` | `IdeaBookState` dataclass (session UI state) + `from_persist`/`to_persist`. | ≤90 |
| `news/app/ideabook/region_spine.py` | `spine_markup(counts, stage_filter)` → Rich markup for the pipeline tiles. | ≤90 |
| `news/app/ideabook/region_toolbar.py` | `toolbar_markup(ib_state, view_counts, n_shown)` → Rich markup for LAYOUT segmented control + tabs + sort/risk/inspector. | ≤90 |
| `news/app/ideabook/region_board.py` | `build_board(vms, ib_state) -> Rich renderable` (kanban columns of cards). | ≤160 |
| `news/app/ideabook/region_table.py` | `build_table(vms, ib_state) -> Rich Table` (dense single table). | ≤140 |
| `news/app/ideabook/inspector.py` | `render_inspector(vm) -> Rich renderable` (header/thesis/stepper/triad/entry-zone bar/downside/tiles/ticker-live). | ≤220 |
| `news/app/ideabook/screen.py` | `IdeaBookHomeScreen(Screen)`: compose() of the 5 regions + inspector, key handling, mutations, persistence. | ≤300 |
| `news/tui_render/render_idea_card.py` | `render_idea_card(vm, selected) -> Rich renderable` + `distance_badge(vm)`. | ≤140 |

**New tests:** `tests/test_ideabook_logic.py`, `tests/test_ideabook_data.py`, `tests/test_ideabook_theme.py`, `tests/test_render_idea_card.py`, `tests/test_pilot_ideabook_geometry.py`, `tests/test_pilot_ideabook_board.py`, `tests/test_pilot_ideabook_table.py`, `tests/test_pilot_ideabook_split.py`, `tests/test_pilot_ideabook_inspector.py`, `tests/test_pilot_ideabook_keys.py`, `tests/test_pilot_ideabook_nav.py`.

**Modified files:** `news/ideas_db.py` (additive schema migration), `news/ideas_cli_lib.py` (backfill enrich + JSON), `news/tui_state_persist.py` (new persisted fields), `news/tui_app.py` (boot auto-push + restore), `news/app/commands.py` (`_on_ideas_command` → push full-screen home), `news/app/region_header.py` (`status_markup` gets `app_badge` param), `news/app/widgets.py` (`_HELP_TEXT` Idea Book section + footer markup), `conftest.py` (`make_pilot_app` `home_view` kwarg; legacy fixtures pin `home_view="news"`), `tests/test_tty_smoke.py` (pin existing canaries to news + add Idea Book canaries), `tests/test_script_launcher.py` (env-parity snapshot note), `docs/guide/idea-book.md` (+ README link).

---

## Decomposition & Ordering (milestone map)

Each milestone leaves the app bootable and all gates green. M0–M2 are pure/data/renderable (no app wiring — zero geometry risk). M3 introduces the screen shell (first geometry pins). M4–M6 add the three body layouts + inspector. M7 adds mutations + filters. M8 carries over nav + help. M9 nails persistence + env-parity + tty. M10 ships the guide.

| M | Title | Ships | Gate focus |
|---|---|---|---|
| M0 | Pure logic + theme tokens | `ideabook_logic.py`, `theme.py` | unit |
| M1 | Schema extension + data layer + CLI | `ideas_db` cols, `ideabook_data.py`, `te-ideas` JSON | unit + full suite |
| M2 | Idea card + distance badge renderables | `render_idea_card.py` | unit/golden |
| M3 | `IdeaBookHomeScreen` shell + boot/F5 wiring | status/spine/toolbar/footer | **geometry** |
| M4 | BOARD kanban body | `region_board.py`, cards, j/k | **geometry** |
| M5 | TABLE layout + `v` cycle | `region_table.py` | **geometry** |
| M6 | Inspector + SPLIT layout + `i` | `inspector.py` | **geometry** |
| M7 | Mutations + filters (`e`/`x`/stage/sort/risk) | watchlist sync, live counts | pilot + regression-pin |
| M8 | Carry-over nav + help + footer | gg/G, Ctrl+D/U, /:f o F2 D ? | pilot |
| M9 | Persistence + env-parity + tty canary | `ui_state.json` fields | env-parity + tty |
| M10 | Plain-English guide | `docs/guide/idea-book.md` | guide |

---

## Reference: the design's exact logic (transcribed from `Idea Book.dc.html`)

These are the ground-truth formulas every pure function must match (lines cited from the HTML script block).

- **Distance** (`calcDist`, L427-431): `px∈[lo,hi]` → `("IN ZONE", amber)`; `px>hi` → `("+{((px-hi)/hi*100):.1f}% to zone", red)`; else → `("−{((lo-px)/lo*100):.1f}% below", green)`.
- **Thesis** (L440-441): `valTail = m if ms in {"n/a","clin"} else "cheap at " + m`; sentence = `f"{sm} on a {abs(dd)}% drawdown — {valTail}."`.
- **Triad cells** (decorate + README §Region4): `−{abs(dd)}% / DRAWDOWN` (red) · `{tag} / SMART $` (amber) · `{mult_short} / VALUE` (green).
- **Smart-money sentiment** (`sMap`, L506-507): `13d` → `("ACTIVIST", violet)`; `cluster`/`ceo` → `("INSIDER+", green)`; else → `("NEUTRAL", txt-dim)`.
- **Lifecycle** (`advance`/`reject`, L582-588): advance `open→triggered→entered→closed` (closed terminal, no-op); reject → `rejected`.
- **Pipeline counts** (`buildFunnel`, L467-485): `NEWS` (static `1,284`, gray) · `SCREENED` (static `86`, violet) — context, not clickable. `OPEN/TRIGGERED/ENTERED/CLOSED` = `len(ideas where status==k)`, clickable filters.
- **Sort** (`sortVms`, L458-465): `dd` → ascending by raw dd (deepest, most-negative first); `dist` → ascending by `abs(px-(lo+hi)/2)/((lo+hi)/2)`; `ticker` → alphabetical; `risk` → order `HIGH<MED<LOW`.
- **Risk cycle** (`cycleRisk`, L580): `None → LOW → MED → HIGH → None`. **Sort cycle** (`cycleSort`, L579): `dd → dist → ticker → risk → dd`. Sort labels: `dd=DRAWDOWN, dist=DISTANCE, ticker=TICKER, risk=RISK`.
- **Visible filter** (`renderVals`, L527-535): drop `rejected` unless `showRejected`; then `stageFilter` (status==k), then `riskFilter`. When a stage filter is active, **only that one column** is shown (BOARD), and Table/Split hard-filter too.
- **Closed badge** (L437): distance shows realized `result` (`+22% · 14mo`), green if leading `+` else red. **Rejected badge** (L438): `REJECTED` red.
- **Entry-zone bar** (`buildInspector`, L489-493): synthetic `hi52 = round(hi*1.7, 2)`, `lo52 = round(lo*0.78, 2)`, positions `pos(x)=clamp((x-lo52)/span*100, 0, 100)` for entry band `[lo,hi]` and px marker.
- **Ticker-live drawer** (L503-515): prototype seeds `5D`, `RSI`, `sent` from ticker char codes; **production wires real quotes/RSI**. RSI amber when `<40 or >70`.
- **Column header labels** (`SM.label2`): `OPEN · watching` · `TRIGGERED · at entry` · `ENTERED · in position` · `CLOSED` · `REJECTED`. Stepper labels (`SM.label`): `OPEN · TRIG · ENTRD · CLOSED`.
- **Empty-column copy** (L542): entered → `"No open positions yet.\nAdvance a TRIGGERED idea with [e] to enter."` · open → `"Nothing waiting."` · closed → `"No closed trades."` · rejected → `"No rejected ideas."`.

---

## Task M0.1: Theme tokens module

**Files:**
- Create: `news/app/ideabook/__init__.py`
- Create: `news/app/ideabook/theme.py`
- Test: `tests/test_ideabook_theme.py`

**Interfaces:**
- Produces: `PALETTE: dict[str,str]`, `STATUS_COLOR: dict[str,str]` (keys `open/triggered/entered/closed/rejected`), `RISK_COLOR: dict[str,str]` (keys `LOW/MED/HIGH`), `SMART_MONEY_SENTIMENT: dict[str, tuple[str,str]]` (keys `13d/cluster/ceo`), `IDEABOOK_CSS: str`.

- [ ] **Step 1: Write the failing test** — `tests/test_ideabook_theme.py`

```python
from news.app.ideabook.theme import (
    PALETTE, STATUS_COLOR, RISK_COLOR, SMART_MONEY_SENTIMENT, IDEABOOK_CSS,
)

def test_palette_exact_hex():
    assert PALETTE["bg"] == "#0a0c0e"
    assert PALETTE["bg-card"] == "#0f1318"
    assert PALETTE["bg-card-sel"] == "#15191f"
    assert PALETTE["line"] == "#1c2128"
    assert PALETTE["txt"] == "#aab4bf"
    assert PALETTE["txt-faint"] == "#3a424b"

def test_status_colors_load_bearing():
    assert STATUS_COLOR["open"] == "#3fb950"
    assert STATUS_COLOR["triggered"] == "#e8b13d"
    assert STATUS_COLOR["entered"] == "#4cc2d6"
    assert STATUS_COLOR["closed"] == "#6b7480"
    assert STATUS_COLOR["rejected"] == "#f0524a"

def test_risk_and_smart_money():
    assert RISK_COLOR == {"LOW": "#3fb950", "MED": "#e8b13d", "HIGH": "#f0524a"}
    assert SMART_MONEY_SENTIMENT["13d"] == ("ACTIVIST", "#9d8cf5")
    assert SMART_MONEY_SENTIMENT["cluster"] == ("INSIDER+", "#3fb950")
    assert SMART_MONEY_SENTIMENT["ceo"] == ("INSIDER+", "#3fb950")

def test_css_is_nonempty_str():
    assert isinstance(IDEABOOK_CSS, str) and "#ib-body" in IDEABOOK_CSS
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/test_ideabook_theme.py -q` → FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement** `news/app/ideabook/theme.py` with the four dicts (exact hex from Global Constraints) and an `IDEABOOK_CSS` string laying out the screen regions. CSS skeleton (region IDs are contract for M3):

```python
"""news/app/ideabook/theme.py — design tokens + screen CSS for the Idea Book home."""
from __future__ import annotations

PALETTE = {
    "bg": "#0a0c0e", "bg-panel": "#0c0e11", "bg-panel-2": "#0b0e11",
    "bg-card": "#0f1318", "bg-card-sel": "#15191f", "bg-inset": "#0d1116",
    "line": "#1c2128", "line-dim": "#161b21",
    "txt": "#aab4bf", "txt-bright": "#e9edf1", "txt-dim": "#7a838d",
    "txt-fade": "#5b636d", "txt-faint": "#3a424b", "brand": "#f2a93b",
}
STATUS_COLOR = {"open": "#3fb950", "triggered": "#e8b13d", "entered": "#4cc2d6",
                "closed": "#6b7480", "rejected": "#f0524a"}
RISK_COLOR = {"LOW": "#3fb950", "MED": "#e8b13d", "HIGH": "#f0524a"}
SMART_MONEY_SENTIMENT = {"13d": ("ACTIVIST", "#9d8cf5"),
                         "cluster": ("INSIDER+", "#3fb950"),
                         "ceo": ("INSIDER+", "#3fb950")}

IDEABOOK_CSS = """
IdeaBookHomeScreen { background: #0a0c0e; layout: vertical; }
#ib-status  { height: 1; background: #0c0e11; }
#ib-spine   { height: 3; background: #0a0c0e; }
#ib-toolbar { height: 1; background: #0c0e11; }
#ib-body    { height: 1fr; }
#ib-inspector { width: 46; display: none; border-left: solid #1c2128; }
#ib-inspector.open { display: block; }
#ib-footer  { height: 1; background: #0c0e11; }
"""
```

- [ ] **Step 4: Run** `uv run pytest tests/test_ideabook_theme.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): design-token theme module"`

---

## Task M0.2: Pure view logic

**Files:**
- Create: `news/ideabook_logic.py`
- Test: `tests/test_ideabook_logic.py`

**Interfaces:**
- Produces (exact signatures — later tasks rely on these names/types):
  - `distance_to_entry(px: float | None, lo: float | None, hi: float | None) -> tuple[str, str]` → `(text, kind)` where `kind ∈ {"in_zone","above","below","na"}`. Returns `("px …","na")` when `px is None`.
  - `thesis_sentence(smart_money: str, drawdown_pct: float, multiple: str, multiple_state: str) -> str`.
  - `smart_money_sentiment(smt: str) -> tuple[str, str]` → `(label, hex)`; unknown → `("NEUTRAL", "#7a838d")`.
  - `lifecycle_advance(status: str) -> str` (closed/rejected unchanged).
  - `lifecycle_reject(status: str) -> str` (always `"rejected"`).
  - `pipeline_counts(ideas: list[dict]) -> dict[str,int]` (keys open/triggered/entered/closed/rejected).
  - `risk_rank(risk: str) -> int` (`HIGH=0,MED=1,LOW=2`, unknown=3).
  - `sort_ideas(vms: list, key: str) -> list` (stable; `key ∈ {dd,dist,ticker,risk}`; sorts by `vm.drawdown_pct`/`vm.dist_pct`/`vm.ticker`/`risk_rank(vm.risk)`).
  - `filter_visible(vms: list, *, stage: str | None, risk: str | None, show_rejected: bool) -> list`.

- [ ] **Step 1: Write the failing test** — `tests/test_ideabook_logic.py` (mirrors the HTML formulas exactly)

```python
import pytest
from news.ideabook_logic import (
    distance_to_entry, thesis_sentence, smart_money_sentiment,
    lifecycle_advance, lifecycle_reject, pipeline_counts, risk_rank, sort_ideas,
)

def test_distance_in_zone():
    assert distance_to_entry(452, 430, 470) == ("IN ZONE", "in_zone")

def test_distance_above_zone_needs_to_fall():
    text, kind = distance_to_entry(110.0, 90.0, 100.0)
    assert kind == "above" and text == "+10.0% to zone"

def test_distance_below_zone():
    text, kind = distance_to_entry(56.20, 48.0, 52.0)  # RYAN seed
    assert kind == "below" and text.startswith("−") and text.endswith("% below")

def test_distance_no_quote_is_loud_na():
    assert distance_to_entry(None, 10.0, 12.0) == ("px …", "na")

def test_thesis_sentence_with_multiple():
    s = thesis_sentence("13D 6.1% activist (Prescott)", -36, "8.4x", "8.4x")
    assert s == "13D 6.1% activist (Prescott) on a 36% drawdown — cheap at 8.4x."

def test_thesis_sentence_clinical_drops_cheap_at():
    s = thesis_sentence("Insider open-market buys", -40, "clinical-stage", "clin")
    assert s == "Insider open-market buys on a 40% drawdown — clinical-stage."

def test_smart_money_sentiment_map():
    assert smart_money_sentiment("13d") == ("ACTIVIST", "#9d8cf5")
    assert smart_money_sentiment("cluster") == ("INSIDER+", "#3fb950")
    assert smart_money_sentiment("zzz") == ("NEUTRAL", "#7a838d")

def test_lifecycle_advance_chain_and_terminal():
    assert lifecycle_advance("open") == "triggered"
    assert lifecycle_advance("triggered") == "entered"
    assert lifecycle_advance("entered") == "closed"
    assert lifecycle_advance("closed") == "closed"      # terminal no-op
    assert lifecycle_advance("rejected") == "rejected"  # terminal no-op

def test_lifecycle_reject():
    assert lifecycle_reject("triggered") == "rejected"

def test_pipeline_counts():
    ideas = [{"status": "open"}, {"status": "triggered"}, {"status": "triggered"},
             {"status": "closed"}, {"status": "rejected"}]
    c = pipeline_counts(ideas)
    assert c["open"] == 1 and c["triggered"] == 2 and c["closed"] == 1 and c["entered"] == 0

def test_risk_rank_orders_high_first():
    assert risk_rank("HIGH") < risk_rank("MED") < risk_rank("LOW")

def test_sort_dd_deepest_first():
    class V:  # minimal stand-in
        def __init__(self, dd, t): self.drawdown_pct, self.ticker = dd, t
        def __repr__(self): return self.ticker
    out = sort_ideas([V(-22, "A"), V(-71, "B"), V(-44, "C")], "dd")
    assert [v.ticker for v in out] == ["B", "C", "A"]  # -71,-44,-22
```

- [ ] **Step 2: Run** `uv run pytest tests/test_ideabook_logic.py -q` → FAIL.
- [ ] **Step 3: Implement** `news/ideabook_logic.py`. Key bodies (use the HTML formulas verbatim; note `−` is U+2212 minus to match the design, and `risk` normalizes uppercase):

```python
def distance_to_entry(px, lo, hi):
    if px is None or lo is None or hi is None:
        return ("px …", "na")
    if lo <= px <= hi:
        return ("IN ZONE", "in_zone")
    if px > hi:
        return (f"+{(px - hi) / hi * 100:.1f}% to zone", "above")
    return (f"−{(lo - px) / lo * 100:.1f}% below", "below")

def thesis_sentence(smart_money, drawdown_pct, multiple, multiple_state):
    tail = multiple if multiple_state in ("n/a", "clin") else f"cheap at {multiple}"
    return f"{smart_money} on a {abs(int(drawdown_pct))}% drawdown — {tail}."

_ADVANCE = {"open": "triggered", "triggered": "entered", "entered": "closed"}
def lifecycle_advance(status): return _ADVANCE.get(status, status)
def lifecycle_reject(status): return "rejected"
```

`sort_ideas` uses `sorted(..., key=...)` with stable Python sort; `dist` key = `abs(px-(lo+hi)/2)/((lo+hi)/2)` precomputed on the VM as `dist_pct` (see M1). `filter_visible` drops rejected unless `show_rejected`, then stage, then risk.

- [ ] **Step 4: Run** `uv run pytest tests/test_ideabook_logic.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): pure view logic (distance/thesis/lifecycle/sort/filter)"`

---

## Task M1.1: Additive `ideas` schema extension + migration

**Files:**
- Modify: `news/ideas_db.py:29-35` (`ensure_ideas_table`)
- Test: `tests/test_ideas_db.py` (append)

**Interfaces:**
- New nullable columns on `ideas`: `company_name TEXT`, `sector TEXT`, `drawdown_pct REAL`, `smart_money_tag TEXT`, `smart_money_type TEXT`, `valuation_mult TEXT`, `valuation_short TEXT`, `realized_result TEXT`. Migrated in place (mirror the existing `note` ALTER pattern) so pre-existing DBs and the test DDLs upgrade transparently.

- [ ] **Step 1: Write the failing test**

```python
def test_ensure_ideas_table_adds_display_columns(tmp_path):
    import sqlite3
    from news.ideas_db import ensure_ideas_table
    conn = sqlite3.connect(":memory:")
    # legacy table without display columns (matches conftest _PILOT_DDL)
    conn.executescript(
        "CREATE TABLE ideas (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,"
        " thesis_short TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',"
        " created_ts TEXT NOT NULL, updated_ts TEXT NOT NULL);")
    ensure_ideas_table(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ideas)")}
    for c in ("company_name", "sector", "drawdown_pct", "smart_money_tag",
              "smart_money_type", "valuation_mult", "valuation_short",
              "realized_result", "note", "entry_low", "entry_high"):
        assert c in cols, f"missing migrated column {c}"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_ideas_db.py -q -k display_columns` → FAIL.
- [ ] **Step 3: Implement** — extend `ensure_ideas_table` to add each missing column idempotently:

```python
_DISPLAY_COLUMNS = {
    "company_name": "TEXT", "sector": "TEXT", "drawdown_pct": "REAL",
    "smart_money_tag": "TEXT", "smart_money_type": "TEXT",
    "valuation_mult": "TEXT", "valuation_short": "TEXT", "realized_result": "TEXT",
    "note": "TEXT", "entry_low": "REAL", "entry_high": "REAL", "downside": "TEXT",
    "risk_level": "TEXT", "horizon": "TEXT", "source_report": "TEXT",
}
def ensure_ideas_table(conn):
    conn.executescript(_IDEAS_DDL)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(ideas)")}
    for col, typ in _DISPLAY_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE ideas ADD COLUMN {col} {typ}")
    conn.commit()
```

- [ ] **Step 4: Run** `uv run pytest tests/test_ideas_db.py -q` → PASS (and existing ideas_db tests stay green).
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): additive ideas schema (triad/name/sector display cols)"`

---

## Task M1.2: `IdeaVM` + `load_ideabook` data layer

**Files:**
- Create: `news/ideabook_data.py`
- Test: `tests/test_ideabook_data.py`

**Interfaces:**
- Consumes: `news.ideas_db.list_ideas`, `news.quote_cache.read_cached_quote`, `news.ideabook_logic.{distance_to_entry, thesis_sentence}`.
- Produces:
  - `IdeaVM` dataclass: `id:int, ticker:str, company:str, sector:str, status:str, drawdown_pct:float, smart_money:str, smart_money_tag:str, smart_money_type:str, multiple:str, multiple_short:str, multiple_state:str, entry_low:float|None, entry_high:float|None, px:float|None, risk:str, horizon:str, source:str, downside:str, note:str, realized_result:str|None, thesis:str, dist_text:str, dist_kind:str, dist_pct:float`.
  - `load_ideabook(user_conn, news_conn=None) -> list[IdeaVM]` — reads all ideas, resolves `px` from `read_cached_quote(news_conn or user_conn, ticker)` (None on miss → loud `px …` distance), normalizes `risk` to `LOW/MED/HIGH`, computes `thesis`/`dist_*`/`dist_pct`.
  - `_norm_risk(risk_level: str | None) -> str` (`Low*→LOW`, `Med*/Medium*→MED`, `High*→HIGH`, default `MED`).

- [ ] **Step 1: Write the failing test**

```python
import sqlite3
import pytest
from news.ideabook_data import load_ideabook, IdeaVM, _norm_risk

@pytest.fixture
def seeded(tmp_path):
    from news.ideas_db import ensure_ideas_table, add_idea, set_idea_status
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    ensure_ideas_table(conn)
    iid = add_idea(conn, ticker="CACC", thesis_short="activist 13D",
                   entry_low=430.0, entry_high=470.0, risk_level="Medium-High",
                   horizon="12–24mo", status="triggered",
                   source_report="VIC #500")
    conn.execute(
        "UPDATE ideas SET company_name='Credit Acceptance', sector='Financials',"
        " drawdown_pct=-36, smart_money='13D 6.1% activist', smart_money_tag='13D 6%',"
        " smart_money_type='13d', valuation_mult='8.4x fwd PE', valuation_short='8.4x'"
        " WHERE id=?", (iid,))
    conn.commit()
    return conn, iid

def test_norm_risk():
    assert _norm_risk("Low-Medium") == "LOW"
    assert _norm_risk("Medium-High") == "MED"
    assert _norm_risk("High") == "HIGH"
    assert _norm_risk(None) == "MED"

def test_load_ideabook_shapes_vm_and_distance_loud_without_quote(seeded):
    conn, iid = seeded
    vms = load_ideabook(conn, news_conn=conn)
    vm = next(v for v in vms if v.id == iid)
    assert isinstance(vm, IdeaVM)
    assert vm.ticker == "CACC" and vm.company == "Credit Acceptance"
    assert vm.status == "triggered" and vm.risk == "MED"
    assert vm.dist_kind == "na" and vm.dist_text == "px …"  # no cached quote → loud
    assert vm.thesis  # non-empty thesis sentence

def test_load_ideabook_distance_from_cached_quote(seeded):
    from news.quotes import Quote
    from news.quote_cache import ensure_quote_cache_table, write_cached_quote
    conn, iid = seeded
    ensure_quote_cache_table(conn)
    write_cached_quote(conn, "CACC", Quote(ticker="CACC", price=452.0, prev_close=450.0))
    vm = next(v for v in load_ideabook(conn, news_conn=conn) if v.id == iid)
    assert vm.px == 452.0 and vm.dist_kind == "in_zone" and vm.dist_text == "IN ZONE"
```

> Impl note: `smart_money` (full text), `smart_money_tag` (short) and `smart_money_type` are distinct columns — the test seeds all three. Construct `Quote` with whatever the real constructor requires — confirm against `news/quotes.py`; if `Quote` is a dataclass with required fields, pass them. If construction is awkward in a unit test, monkeypatch `news.ideabook_data.read_cached_quote` to return `(Quote-like, None)` with a `price` attr.

- [ ] **Step 2: Run** `uv run pytest tests/test_ideabook_data.py -q` → FAIL.
- [ ] **Step 3: Implement** `news/ideabook_data.py`. For each idea row: `px = getattr(read_cached_quote(conn, ticker)[0], "price", None)`; `dist_text, dist_kind = distance_to_entry(px, lo, hi)`; for closed ideas with `realized_result`, override `dist_text=realized_result, dist_kind="result"`; for rejected, `dist_text="REJECTED", dist_kind="rejected"`; `dist_pct = abs(px-(lo+hi)/2)/((lo+hi)/2)` when px and lo,hi present else `inf`. `thesis = thesis_sentence(smart_money_full, drawdown_pct, valuation_mult, valuation_state)` where `valuation_state` is derived (`"clin"` if `valuation_short=="clin"`, `"n/a"` if `=="n/a"`, else `""`). Fall back gracefully (empty strings) for unenriched rows — **never silently fabricate** triad numbers; an unenriched idea shows `—` cells.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): IdeaVM + load_ideabook data layer"`

---

## Task M1.3: Backfill enrichment for the 43 real seed ideas + CLI JSON

**Files:**
- Modify: `news/ideas_cli_lib.py` (`_BACKFILL_IDEAS` entries gain triad fields; new `cmd_enrich` or extend `cmd_backfill`)
- Test: `tests/test_idea_ledger.py` (append) or new `tests/test_ideas_enrich.py`

**Interfaces:**
- The 43 real ideas (3 open / 31 triggered / 9 closed; see brief §5.5 and the HTML seed L353-399) get `company_name, sector, drawdown_pct, smart_money_tag, smart_money_type, valuation_mult, valuation_short, realized_result` populated. Source of truth: the HTML `buildData()` seed (it carries the exact `n/se/dd/smt/sm/tag/m/ms/result` per ticker). Map HTML `smt`→`smart_money_type`, `tag`→`smart_money_tag`, `m`→`valuation_mult`/`valuation_short`, `ms`→`valuation_state`, `result`→`realized_result`.
- New CLI seam: `te-ideas enrich [--db PATH] [--json]` updates existing rows by ticker+status from the seed map; idempotent; emits JSON of `{updated: N}`.

- [ ] **Step 1: Write the failing test**

```python
def test_enrich_populates_triad_for_seeded_idea(tmp_path):
    import sqlite3
    from news.ideas_db import ensure_ideas_table, add_idea
    from news.ideas_cli_lib import enrich_ideas, IDEA_ENRICHMENT
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    ensure_ideas_table(conn)
    add_idea(conn, ticker="CHTR", thesis_short="cable at 3x", status="triggered")
    n = enrich_ideas(conn)
    assert n >= 1
    row = conn.execute("SELECT * FROM ideas WHERE ticker='CHTR'").fetchone()
    assert row["smart_money_type"] in ("cluster", "13d", "ceo")
    assert row["drawdown_pct"] is not None
    assert "CHTR" in IDEA_ENRICHMENT  # seed map keyed by ticker
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — add `IDEA_ENRICHMENT: dict[str, dict]` keyed by ticker (transcribe the HTML seed's `n/se/dd/smt/tag/m/ms` for all 43) and `enrich_ideas(conn) -> int` that updates each matching row. Wire `cmd_enrich(args, db)` and register the `enrich` subcommand in `scripts/te_ideas.py` (`--json` honored). Keep `enrich_ideas` pure-ish (conn in, count out).
- [ ] **Step 4: Run** `uv run pytest tests/test_ideas_enrich.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): triad enrichment backfill + te-ideas enrich"`

> Operational note (for the human running it once on the real DB): `uv run scripts/te-ideas enrich` then `uv run scripts/te-ideas list --json | head` to confirm the 43 rows carry triad fields. Not part of the test suite.

---

## Task M2.1: Idea card + distance badge renderables

**Files:**
- Create: `news/tui_render/render_idea_card.py`
- Test: `tests/test_render_idea_card.py`

**Interfaces:**
- Consumes: `IdeaVM`, `news.app.ideabook.theme.{STATUS_COLOR,RISK_COLOR}`.
- Produces:
  - `distance_badge(vm) -> rich.text.Text` — colored per `dist_kind` (in_zone=amber, above=red, below=green, na=dim, result=green/red by sign, rejected=red).
  - `render_idea_card(vm, *, selected: bool, width: int) -> rich.panel.Panel | rich.console.Group` — 5-row layout (ticker+name+badge / thesis 2-line clamp / triad three inset cells / `ENTRY lo–hi` + `$px` / risk chip + horizon + source). Left border = `STATUS_COLOR[vm.status]`; selected → full border + `bg-card-sel`.

- [ ] **Step 1: Write the failing test** (render to a fixed-width console, assert plain text + that styles carry the status hex)

```python
from rich.console import Console
from news.tui_render.render_idea_card import render_idea_card, distance_badge
from news.ideabook_data import IdeaVM

def _vm(**kw):
    base = dict(id=1, ticker="CACC", company="Credit Acceptance", sector="Financials",
        status="triggered", drawdown_pct=-36, smart_money="13D 6.1% activist",
        smart_money_tag="13D 6%", smart_money_type="13d", multiple="8.4x fwd PE",
        multiple_short="8.4x", multiple_state="", entry_low=430.0, entry_high=470.0,
        px=452.0, risk="MED", horizon="12–24mo", source="VIC #500", downside="x",
        note="", realized_result=None, thesis="13D 6.1% activist on a 36% drawdown — cheap at 8.4x.",
        dist_text="IN ZONE", dist_kind="in_zone", dist_pct=0.0)
    base.update(kw); return IdeaVM(**base)

def _plain(renderable, width=44):
    con = Console(width=width, no_color=True, force_terminal=True)
    with con.capture() as cap: con.print(renderable)
    return cap.get()

def test_card_shows_ticker_triad_entry_and_badge():
    out = _plain(render_idea_card(_vm(), selected=False, width=44))
    assert "CACC" in out
    assert "DRAWDOWN" in out and "SMART $" in out and "VALUE" in out
    assert "−36%" in out and "8.4x" in out and "13D 6%" in out
    assert "ENTRY" in out and "430" in out and "470" in out
    assert "452" in out and "IN ZONE" in out
    assert "MED" in out and "VIC #500" in out

def test_distance_badge_color_in_zone_is_amber():
    t = distance_badge(_vm())
    assert "IN ZONE" in t.plain
    # amber hex appears in the badge's style spans
    assert any("#e8b13d" in str(sp.style) for sp in t.spans) or t.style == "#e8b13d"

def test_distance_badge_closed_result_sign_colors():
    pos = distance_badge(_vm(status="closed", dist_kind="result", dist_text="+22% · 14mo"))
    neg = distance_badge(_vm(status="closed", dist_kind="result", dist_text="−12% · 5mo"))
    assert "+22%" in pos.plain and "−12%" in neg.plain
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `render_idea_card.py`. Use `rich.table.Table.grid` for the triad row (3 equal columns), `rich.panel.Panel` with `border_style=STATUS_COLOR[status]` (selected uses `box.HEAVY` + `style="on #15191f"`). Thesis clamped to 2 lines via `Text(...,overflow="ellipsis")` truncation helper. Badge built by `distance_badge`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): idea card + distance badge renderables"`

---

## Task M3.1: `IdeaBookState` session state + persistence fields

**Files:**
- Create: `news/app/ideabook/state.py`
- Modify: `news/tui_state_persist.py` (`_V2_DEFAULTS` / `default_ui_state`)
- Test: `tests/test_ideabook_state.py`, `tests/test_tui_state_persist.py` (append)

**Interfaces:**
- Produces `IdeaBookState` dataclass with fields + defaults: `selected_id: int | None = None`, `layout: str = "board"` (∈ board/table/split), `stage_filter: str | None = None`, `risk_filter: str | None = None`, `sort_key: str = "dd"`, `inspector_open: bool = True`, `show_rejected: bool = False`. Methods `to_persist() -> dict` / `from_persist(d) -> IdeaBookState`.
- New persisted keys in `ui_state.json`: `home_view` (`"ideabook"` default), `ib_layout`, `ib_sort_key`, `ib_risk_filter`, `ib_stage_filter`, `ib_inspector_open`, `ib_show_rejected`, `ib_selected_id`.

- [ ] **Step 1: Write the failing test**

```python
from news.app.ideabook.state import IdeaBookState
from news.tui_state_persist import default_ui_state

def test_default_layout_is_board_inspector_open():
    s = IdeaBookState()
    assert s.layout == "board" and s.inspector_open is True and s.sort_key == "dd"

def test_roundtrip_persist():
    s = IdeaBookState(layout="split", sort_key="risk", risk_filter="HIGH",
                      stage_filter="triggered", show_rejected=True, selected_id=7)
    assert IdeaBookState.from_persist(s.to_persist()) == s

def test_default_ui_state_boots_to_ideabook():
    d = default_ui_state()
    assert d["home_view"] == "ideabook"
    assert d["ib_layout"] == "board"
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `IdeaBookState` (frozen=False dataclass with `==`). Add the `ib_*` + `home_view` keys to `_V2_DEFAULTS` and `default_ui_state()` in `tui_state_persist.py` (do NOT bump `STATE_VERSION`; they're forward-compat fields filled by `setdefault`, matching the existing v1→v2 migration pattern).
- [ ] **Step 4: Run** `uv run pytest tests/test_ideabook_state.py tests/test_tui_state_persist.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): session state + persisted ui_state fields"`

---

## Task M3.2: Region markup builders (status / spine / toolbar)

**Files:**
- Create: `news/app/ideabook/region_spine.py`, `news/app/ideabook/region_toolbar.py`
- Modify: `news/app/region_header.py` (`status_markup` gains `app_badge="news-tui"` param)
- Test: `tests/test_ideabook_regions.py`, `tests/test_pilot_frame_v2.py`/`tests/test_*` (status_markup default unchanged)

**Interfaces:**
- `spine_markup(counts: dict[str,int], stage_filter: str | None) -> str` — `PIPELINE` label + `news → idea → entry` + six tiles `NEWS 1,284 › SCREENED 86 › OPEN {n} › TRIGGERED {n} › ENTERED {n} › CLOSED {n}`, each tile colored by stage; active stage filter tile bracketed/tinted.
- `toolbar_markup(ib_state: IdeaBookState, view_counts: dict, n_shown: int) -> str` — `LAYOUT [BOARD][TABLE][SPLIT] [v]` (active = amber-fill) · tabs `All N · My positions N · High-conv N · Watchlist N · Earnings N` · right `sort {KEY} ▾` · `risk {ALL|LOW|MED|HIGH} ▾` · `{n} shown` · `inspector [i]` (amber when open).
- `status_markup(market, width=None, app_badge="news-tui")` — when `app_badge="TE"` it renders `TE`(brand bold) + `IDEABOOK`(faint caps) in place of the `news-tui` badge. Default keeps the exact current output (existing status tests untouched).

- [ ] **Step 1: Write the failing test**

```python
from news.app.ideabook.region_spine import spine_markup
from news.app.ideabook.region_toolbar import toolbar_markup
from news.app.ideabook.state import IdeaBookState
from news.app.region_header import status_markup

def _plain(markup):
    from rich.text import Text
    return Text.from_markup(markup).plain

def test_spine_lists_all_six_stages_with_counts():
    p = _plain(spine_markup({"open": 3, "triggered": 31, "entered": 0, "closed": 9}, None))
    for label in ("NEWS", "SCREENED", "OPEN", "TRIGGERED", "ENTERED", "CLOSED"):
        assert label in p
    assert "1,284" in p and "31" in p and "9" in p
    assert "PIPELINE" in p

def test_toolbar_active_layout_and_sort_risk():
    s = IdeaBookState(layout="board", sort_key="dd", risk_filter=None, inspector_open=True)
    p = _plain(toolbar_markup(s, {"All": 43, "Watchlist": 34}, n_shown=43))
    assert "BOARD" in p and "TABLE" in p and "SPLIT" in p
    assert "DRAWDOWN" in p and "ALL" in p and "43 shown" in p and "inspector" in p

def test_status_markup_app_badge_te_ideabook():
    market = {"session": {"badge": "OPEN", "clock_et": "10:00:00", "countdown": "",
              "boundary_label": ""}, "spy_pct": 0.42, "breadth": (312, 188, 500),
              "universe_size": 500, "book_pct": 1.18, "alerts_unacked": 2}
    p = _plain(status_markup(market, app_badge="TE"))
    assert "TE" in p and "IDEABOOK" in p and "news-tui" not in p

def test_status_markup_default_unchanged():
    market = {"session": {"badge": "OPEN", "clock_et": "10:00:00", "countdown": "",
              "boundary_label": ""}, "spy_pct": None, "breadth": (0, 0, 0),
              "universe_size": 0, "book_pct": None, "alerts_unacked": 0}
    assert "news-tui" in _plain(status_markup(market))
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the two builders + the `app_badge` param. Stage tile colors from `STATUS_COLOR`; NEWS gray `#6b7480`, SCREENED violet `#9d8cf5`. Sort label map `{dd:"DRAWDOWN",dist:"DISTANCE",ticker:"TICKER",risk:"RISK"}`. Active layout segment uses `[reverse]`/brand-fill markup.
- [ ] **Step 4: Run** `uv run pytest tests/test_ideabook_regions.py -q` plus the existing status-bar tests → all PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): spine + toolbar markup; status_markup app_badge"`

---

## Task M3.3: `IdeaBookHomeScreen` shell + boot/F5 wiring + GEOMETRY pins

**Files:**
- Create: `news/app/ideabook/screen.py`
- Modify: `news/app/commands.py` (`_on_ideas_command` pushes the full-screen home), `news/tui_app.py` (`on_mount` auto-push when `home_view=="ideabook"` + restore `ib_*`), `news/app/widgets.py` (footer cheatsheet for the home)
- Modify: `conftest.py` (`make_pilot_app` gains `home_view="news"` default kwarg; `pilot_app` + `_legacy_flat_boot` write `home_view="news"`)
- Test: `tests/test_pilot_ideabook_geometry.py`

**Interfaces:**
- Consumes: `theme.IDEABOOK_CSS`, `region_spine.spine_markup`, `region_toolbar.toolbar_markup`, `region_header.status_markup`, `ideabook_data.load_ideabook`, `IdeaBookState`.
- Produces: `IdeaBookHomeScreen(Screen)` with `compose()` mounting region IDs `#ib-status` (Static), `#ib-spine` (Static), `#ib-toolbar` (Static), `#ib-body` (Container — empty stub this milestone), `#ib-inspector` (Container, `display:none`), `#ib-footer` (Static). `BINDINGS = []` (keys routed in `on_key`). Constructor `IdeaBookHomeScreen(user_conn, news_conn, ib_state)`. `_refresh()` repaints all regions from `load_ideabook` + `ib_state`. `app._ideabook_home_open() -> bool`.

This milestone proves the shell boots with healthy geometry; the body is an empty container (filled in M4).

- [ ] **Step 1: Write the failing GEOMETRY test** — `tests/test_pilot_ideabook_geometry.py`

```python
from __future__ import annotations
import pytest
pytestmark = pytest.mark.pilot

IB_REGIONS = ("#ib-status", "#ib-spine", "#ib-toolbar", "#ib-body", "#ib-footer")

async def _open_home(make_pilot_app):
    cm = make_pilot_app(size=(120, 40), with_user_db=True, home_view="news")
    return cm  # caller uses `async with`

def _assert_regions_healthy(screen, *, label=""):
    pfx = f"[{label}] " if label else ""
    for sel in IB_REGIONS:
        w = screen.query_one(sel)
        assert w.is_attached, f"{pfx}{sel} not attached"
        assert w.region.width > 0, f"{pfx}{sel} width {w.region.width} ≤ 0"
        assert w.region.height > 0, f"{pfx}{sel} height {w.region.height} ≤ 0"

async def test_ideabook_home_regions_healthy_after_boot(make_pilot_app):
    async with make_pilot_app(size=(120, 40), with_user_db=True, home_view="news") as (app, pilot):
        await pilot.press("f5")            # open the Idea Book home
        await pilot.pause(); await pilot.pause()
        assert app._ideabook_home_open()
        _assert_regions_healthy(app.screen, label="boot")

async def test_ideabook_home_regions_sum_to_terminal_width(make_pilot_app):
    async with make_pilot_app(size=(120, 40), with_user_db=True, home_view="news") as (app, pilot):
        await pilot.press("f5"); await pilot.pause(); await pilot.pause()
        for sel in ("#ib-status", "#ib-spine", "#ib-toolbar", "#ib-footer"):
            assert app.screen.query_one(sel).region.width == 120

async def test_f5_toggles_home_closed(make_pilot_app):
    async with make_pilot_app(size=(120, 40), with_user_db=True, home_view="news") as (app, pilot):
        await pilot.press("f5"); await pilot.pause(); await pilot.pause()
        assert app._ideabook_home_open()
        await pilot.press("escape"); await pilot.pause()
        assert not app._ideabook_home_open()

async def test_auto_boot_home_when_home_view_ideabook(make_pilot_app):
    # Out-of-box hero behavior: with home_view='ideabook' the app pushes the home at boot.
    async with make_pilot_app(size=(120, 40), with_user_db=True, home_view="ideabook") as (app, pilot):
        await pilot.pause(); await pilot.pause()
        assert app._ideabook_home_open()
        _assert_regions_healthy(app.screen, label="auto-boot")
```

- [ ] **Step 2: Run** `uv run pytest tests/test_pilot_ideabook_geometry.py -q` → FAIL (`home_view` kwarg + screen absent).

- [ ] **Step 3a: conftest** — add `home_view="news"` param to `make_pilot_app._factory`; when provided, write a state file (`{**default_ui_state(), "home_view": home_view, "cluster_mode": "off", "default_sort": "date", "sort_spec": [["date","desc"]]}`) before constructing the app, so existing v2 tests (which call `make_pilot_app(...)` without the kwarg → default `"news"`) keep landing on the news feed. Add `home_view="news"` to the `pilot_app` fixture's state write and to `_legacy_flat_boot`.

- [ ] **Step 3b: app wiring** — `news/tui_app.py on_mount`: after building `tui_state`, read `home_view = boot.get("home_view", "ideabook")` and restore `ib_*` into `self._ib_state = IdeaBookState.from_persist(boot)`; if `home_view == "ideabook"`, `self.call_after_refresh(lambda: self._on_ideas_command(None))`. `news/app/commands.py _on_ideas_command`: build `IdeaBookState` from `self._ib_state`, `self.push_screen(IdeaBookHomeScreen(user_conn=uconn, news_conn=self._conn, ib_state=self._ib_state), _on_dismiss)`. Add `_ideabook_home_open()` (isinstance check) and keep `_idea_book_is_open` as an alias for the F5 toggle in `keys.py` (so the existing F5 branch pops/pushes the new home).

- [ ] **Step 3c: screen** — implement `IdeaBookHomeScreen.compose()` mounting the 5 region widgets + inspector container per the CSS IDs; `CSS = IDEABOOK_CSS`; `on_mount` → `self._refresh()`; `_refresh()` sets `#ib-status` via `status_markup(self._market(), app_badge="TE")`, `#ib-spine` via `spine_markup(pipeline_counts(rows), ib.stage_filter)`, `#ib-toolbar` via `toolbar_markup(...)`, `#ib-footer` via the verbatim cheatsheet, leaves `#ib-body` empty. `BINDINGS=[("escape","dismiss")]`; `on_key` handles `f5`→dismiss (toggle). Market dict: reuse a minimal snapshot (session_info + zeros) — the live market wiring is the same `_market_snapshot` shape; acceptable to reuse `status_markup` with `spy_pct=None` etc. at this milestone (loud `…`).

- [ ] **Step 4: Run** `uv run pytest tests/test_pilot_ideabook_geometry.py -q` → PASS, then `uv run pytest -m pilot -q` → confirm existing pilot suite still green (legacy fixtures pinned to news).

- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): full-screen home shell + boot/F5 wiring + geometry pins"`

---

## Task M4.1: BOARD kanban body

**Files:**
- Create: `news/app/ideabook/region_board.py`
- Modify: `news/app/ideabook/screen.py` (mount board into `#ib-body`; j/k selection)
- Test: `tests/test_pilot_ideabook_board.py` (+ geometry assertions appended to `test_pilot_ideabook_geometry.py`)

**Interfaces:**
- `build_board(vms: list[IdeaVM], ib_state, *, total_width: int) -> rich.console.RenderableType` — one column per status in order `open · triggered · entered · closed (+rejected when present/shown)`; column header = status dot + `LABEL · sub` + count, top-border in status color; body = stacked `render_idea_card`. When `ib_state.stage_filter` set → only that column. Empty column → dashed placeholder with the exact empty-copy. Selection: the card whose `vm.id == ib_state.selected_id` renders selected.
- Screen mounts the board inside a horizontally-scrolling `#ib-body` container; `j/k` move `selected_id` through the current filtered+sorted visible order (`filter_visible` → `sort_ideas`), repaint.

- [ ] **Step 1: Write the failing test**

```python
import pytest
pytestmark = pytest.mark.pilot

async def test_board_renders_status_columns(ideabook_home):  # fixture defined in test file
    app, pilot = ideabook_home
    body = app.screen.query_one("#ib-body")
    text = _captured_text(app)  # helper: render screen, return plain text
    assert "OPEN · watching" in text and "TRIGGERED · at entry" in text

async def test_board_empty_entered_column_placeholder(ideabook_home):
    app, pilot = ideabook_home
    text = _captured_text(app)
    assert "No open positions yet." in text  # entered empty copy

async def test_jk_moves_selection_through_visible_order(ideabook_home):
    app, pilot = ideabook_home
    first = app.screen._ib.selected_id
    await pilot.press("j"); await pilot.pause()
    assert app.screen._ib.selected_id != first

async def test_board_columns_visible_geometry(ideabook_home):
    app, pilot = ideabook_home
    assert app.screen.query_one("#ib-body").region.width > 0
    assert app.screen.query_one("#ib-body").region.height > 0
```

> The test file defines an `ideabook_home` fixture: seed several ideas across statuses via `ensure_ideas_table`+`add_idea`+`enrich_ideas`, boot `make_pilot_app(with_user_db=True, home_view="news")`, press `f5`, pause. `_captured_text` uses `app.export_screenshot()`/pilot screen capture or queries the body widget's rendered Rich content.

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `build_board` + mount + j/k. Use a `Horizontal`/`HorizontalScroll` of per-column `VerticalScroll` widgets, OR render the whole board as one Rich renderable in a single scrollable `Static` (simpler, matches the read-only nature). Recommended: single `Static#ib-board` inside a `HorizontalScroll` showing a `rich.table.Table.grid` of fixed-width (≈34-cell) columns. Selection cursor handled in `screen.on_key` j/k by recomputing the visible order.
- [ ] **Step 4: Run** `uv run pytest tests/test_pilot_ideabook_board.py -q` → PASS; re-run `-m pilot`.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): BOARD kanban body + j/k selection"`

---

## Task M5.1: TABLE layout + `v` layout cycle

**Files:**
- Create: `news/app/ideabook/region_table.py`
- Modify: `news/app/ideabook/screen.py` (`v` cycles board→table→split; mount table)
- Test: `tests/test_pilot_ideabook_table.py` (+ geometry per layout)

**Interfaces:**
- `build_table(vms, ib_state) -> rich.table.Table` — sticky header columns `STATUS · TICKER · WHY NOW (dd / smart$ / value / name) · ENTRY · PX · DIST · RISK · HORIZON · SOURCE`; selected row = status-colored left border + brighter bg.
- Screen: `v` → `IdeaBookState.layout` cycles `board→table→split→board`; repaint swaps the body renderable. Stage/risk/sort filters apply identically across layouts.

- [ ] **Step 1: Write the failing test**

```python
import pytest
pytestmark = pytest.mark.pilot

async def test_v_cycles_board_table_split(ideabook_home):
    app, pilot = ideabook_home
    assert app.screen._ib.layout == "board"
    await pilot.press("v"); await pilot.pause(); assert app.screen._ib.layout == "table"
    await pilot.press("v"); await pilot.pause(); assert app.screen._ib.layout == "split"
    await pilot.press("v"); await pilot.pause(); assert app.screen._ib.layout == "board"

async def test_table_layout_headers_present(ideabook_home):
    app, pilot = ideabook_home
    await pilot.press("v"); await pilot.pause()  # → table
    text = _captured_text(app)
    for h in ("STATUS", "TICKER", "WHY NOW", "ENTRY", "PX", "DIST", "RISK", "HORIZON", "SOURCE"):
        assert h in text

async def test_table_body_geometry_healthy(ideabook_home):
    app, pilot = ideabook_home
    await pilot.press("v"); await pilot.pause()
    assert app.screen.query_one("#ib-body").region.width > 0
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `build_table` + `v` cycle. Reuse the distance badge + status colors. Add a per-layout geometry assertion to `test_pilot_ideabook_geometry.py` (`#ib-body` width>0 in board/table/split).
- [ ] **Step 4: Run** → PASS; re-run `-m pilot`.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): TABLE layout + v layout cycle"`

---

## Task M6.1: Inspector + SPLIT layout + `i` toggle

**Files:**
- Create: `news/app/ideabook/inspector.py`
- Modify: `news/app/ideabook/screen.py` (mount `#ib-inspector`, `i` toggles, SPLIT shows list+inspector)
- Test: `tests/test_pilot_ideabook_inspector.py`, `tests/test_pilot_ideabook_split.py` (+ inspector geometry)

**Interfaces:**
- `render_inspector(vm, *, market=None) -> rich.console.RenderableType` — sections per README §4b: (1) header big TICKER + status chip · company · sector · `$px` + distance; (2) THESIS block (amber left rule); (3) LIFECYCLE stepper `OPEN→TRIG→ENTRD→CLOSED` (done=dim-filled, current=status border+tint+bold, future=faint) + `ADVANCE ▸ [e]` (green) / `REJECT ✕ [x]` (red outline); (4) TRIAD rows (DRAWDOWN red / SMART $ amber / VALUATION green); (5) ENTRY ZONE bar (synthetic `lo52=round(lo*0.78,2)`, `hi52=round(hi*1.7,2)`, entry band + px marker, labels `52w {lo52}`, `px ${px}`, `{hi52}`); (6) DOWNSIDE (red label); (7) tiles RISK/HORIZON/SOURCE(violet); (8) NOTE (editable `TextArea#ib-note`); (9) TICKER · LIVE drawer `5D {chg} · RSI {n} · SENT {…}` (RSI amber when `<40 or >70`).
- Screen: `#ib-inspector` toggled by `i` (adds/removes `.open` class). In SPLIT, body = left list (status dot · ticker · name · distance) + inspector always shown (`i` is a no-op visual in split).

- [ ] **Step 1: Write the failing test**

```python
import pytest
from textual.widgets import TextArea
pytestmark = pytest.mark.pilot

async def test_i_toggles_inspector_visibility_and_geometry(ideabook_home):
    app, pilot = ideabook_home  # boots board, inspector default open
    insp = app.screen.query_one("#ib-inspector")
    assert insp.region.width > 0           # open by default (ib.inspector_open=True)
    await pilot.press("i"); await pilot.pause()
    assert app.screen._ib.inspector_open is False
    await pilot.press("i"); await pilot.pause()
    assert app.screen._ib.inspector_open is True
    assert app.screen.query_one("#ib-inspector").region.width > 0

async def test_inspector_sections_present(ideabook_home):
    app, pilot = ideabook_home
    text = _captured_text(app)
    for s in ("THESIS", "LIFECYCLE", "ADVANCE", "REJECT", "DRAWDOWN", "ENTRY ZONE",
              "DOWNSIDE", "RISK", "HORIZON", "SOURCE", "NOTE", "RSI"):
        assert s in text

async def test_split_layout_shows_list_and_inspector(ideabook_home):
    app, pilot = ideabook_home
    await pilot.press("v"); await pilot.press("v"); await pilot.pause()  # → split
    assert app.screen._ib.layout == "split"
    assert app.screen.query_one("#ib-inspector").region.width > 0
    assert app.screen.query_one("#ib-body").region.width > 0

async def test_note_textarea_persists(ideabook_home):
    from news.ideas_db import get_idea_note
    app, pilot = ideabook_home
    ta = app.screen.query_one("#ib-note", TextArea)
    ta.load_text("watch earnings")
    await pilot.pause()
    app.screen._save_note()   # explicit save seam (also on blur / ctrl+s)
    assert get_idea_note(app.screen._user_conn, app.screen._ib.selected_id) == "watch earnings"
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `render_inspector` + mount/toggle. Inspector is a `VerticalScroll#ib-inspector` containing a `Static#ib-inspector-body` (the Rich render) + a `TextArea#ib-note`. `i` flips `ib.inspector_open` and the `.open` class; in `split`, force-open. Note save seam: `ctrl+s` in the textarea + on inspector rebuild persists via `set_idea_note`. The ENTRY ZONE bar is a Rich `Text` of block chars (`█`/`░`) positioned by the synthetic 52w math; px marker `▎`/white cell.
- [ ] **Step 4: Run** `uv run pytest tests/test_pilot_ideabook_inspector.py tests/test_pilot_ideabook_split.py -q` → PASS; re-run `-m pilot`.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): inspector + SPLIT layout + i toggle + note editor"`

---

## Task M7.1: Lifecycle mutations (`e` advance / `x` reject) + watchlist sync

**Files:**
- Modify: `news/app/ideabook/screen.py` (key handlers + DB mutation + repaint)
- Reuse: `news/ideas_db.set_idea_status`, `news/ideas_cli_lib._sync_watchlist`
- Test: `tests/test_pilot_ideabook_keys.py`

**Interfaces:**
- `e` → `set_idea_status(uconn, selected_id, lifecycle_advance(cur))`; on reaching `closed` → `_sync_watchlist(uconn, ticker, entry_high, remove_trigger=True)`. `x` → `set_idea_status(uconn, selected_id, "rejected")` + remove watchlist trigger; rejected hidden from default view. Advancing into `open/triggered` keeps/adds the watchlist trigger (`_sync_watchlist(uconn, ticker, entry_high)`). After each mutation: reload `load_ideabook`, recompute pipeline counts (spine repaints), keep selection.

- [ ] **Step 1: Write the failing tests (regression pins; name the behavior)**

```python
import pytest
pytestmark = pytest.mark.pilot

async def test_e_advances_status_and_updates_spine(ideabook_home):
    """e advances open→triggered and the pipeline spine count moves. (2026-06-21 ideabook-advance)"""
    from news.ideas_db import get_idea
    app, pilot = ideabook_home
    # select a known 'open' idea
    app.screen._select_first_with_status("open"); await pilot.pause()
    iid = app.screen._ib.selected_id
    await pilot.press("e"); await pilot.pause()
    assert get_idea(app.screen._user_conn, iid)["status"] == "triggered"
    assert "TRIGGERED" in _captured_text(app)

async def test_e_on_closed_is_terminal_noop(ideabook_home):
    from news.ideas_db import get_idea
    app, pilot = ideabook_home
    app.screen._select_first_with_status("closed"); await pilot.pause()
    iid = app.screen._ib.selected_id
    await pilot.press("e"); await pilot.pause()
    assert get_idea(app.screen._user_conn, iid)["status"] == "closed"

async def test_x_rejects_and_hides_and_clears_trigger(ideabook_home):
    """x rejects, the row leaves the default view, watchlist trigger cleared. (2026-06-21 ideabook-reject)"""
    from news.ideas_db import get_idea
    from news.watchlist import list_tickers
    app, pilot = ideabook_home
    app.screen._select_first_with_status("triggered"); await pilot.pause()
    iid = app.screen._ib.selected_id
    tkr = get_idea(app.screen._user_conn, iid)["ticker"]
    await pilot.press("x"); await pilot.pause()
    assert get_idea(app.screen._user_conn, iid)["status"] == "rejected"
    # trigger removed (price_below cleared) — assert via watchlist row
    wl = {r["ticker"]: r for r in list_tickers(app.screen._user_conn)}
    assert tkr not in wl or wl[tkr].get("price_below") in (None, "")
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the `e`/`x` handlers + `_select_first_with_status` test seam + repaint. Mutations call `set_idea_status` then `self._refresh()`.
- [ ] **Step 4: Run** → PASS; re-run `-m pilot`.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): e advance / x reject lifecycle mutations + watchlist sync"`

---

## Task M7.2: Pipeline stage filter + sort cycle + risk cycle + show-rejected

**Files:**
- Modify: `news/app/ideabook/screen.py` (stage filter via click + keyed, sort/risk cycles)
- Test: `tests/test_pilot_ideabook_keys.py` (append)

**Interfaces:**
- Clicking a pipeline stage tile (OPEN/TRIGGERED/ENTERED/CLOSED) → `ib.stage_filter = k` (click again → `None`); filter is global across all three layouts (BOARD shows only that column; TABLE/SPLIT hard-filter). NEWS/SCREENED tiles are non-clickable context.
- Sort cycle key (`s` reuse, or toolbar click) → `dd→dist→ticker→risk→dd`. Risk cycle key (`r`, or toolbar click) → `None→LOW→MED→HIGH→None`. `X`/a binding toggles `show_rejected` (rejected column appears).
- Each cycle repaints body + toolbar; `n_shown` reflects `len(filter_visible(...))`.

> Stage clicking: bind `on_click` on the spine Static and map the click x-offset → stage (or render each tile as a small clickable `Static`/`Button`). Simplest agent-verifiable seam: also expose keyboard stage cycling so a pilot test can drive it without mouse — bind `[` `]` (already free on this screen) to cycle stage filter, AND keep mouse-click. Tests drive the keyboard seam.

- [ ] **Step 1: Write the failing tests**

```python
async def test_stage_filter_shows_single_column(ideabook_home):
    app, pilot = ideabook_home
    app.screen._set_stage_filter("triggered"); await pilot.pause()
    text = _captured_text(app)
    assert "TRIGGERED · at entry" in text and "OPEN · watching" not in text

async def test_sort_cycle_dd_dist_ticker_risk(ideabook_home):
    app, pilot = ideabook_home
    seq = []
    for _ in range(4):
        seq.append(app.screen._ib.sort_key); app.screen._cycle_sort(); await pilot.pause()
    assert seq == ["dd", "dist", "ticker", "risk"]

async def test_risk_cycle(ideabook_home):
    app, pilot = ideabook_home
    seq = []
    for _ in range(4):
        seq.append(app.screen._ib.risk_filter); app.screen._cycle_risk(); await pilot.pause()
    assert seq == [None, "LOW", "MED", "HIGH"]

async def test_show_rejected_reveals_rejected_column(ideabook_home):
    app, pilot = ideabook_home
    app.screen._select_first_with_status("triggered"); await pilot.pause()
    await pilot.press("x"); await pilot.pause()          # reject one
    assert "REJECTED" not in _captured_text(app)          # hidden by default
    app.screen._toggle_show_rejected(); await pilot.pause()
    assert "REJECTED" in _captured_text(app)
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `_set_stage_filter`, `_cycle_sort`, `_cycle_risk`, `_toggle_show_rejected` + their key/click bindings + repaint. The body builders already accept `ib_state` (filters applied via `filter_visible`+`sort_ideas`).
- [ ] **Step 4: Run** → PASS; re-run `-m pilot`.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): stage filter + sort/risk cycles + show-rejected"`

---

## Task M8.1: Carry-over navigation, cross-screen keys, help, footer

**Files:**
- Modify: `news/app/ideabook/screen.py` (gg/G, Ctrl+D/U, `/` `:` `f` `o` `F2` `D` `?`)
- Modify: `news/app/widgets.py` (`_HELP_TEXT` Idea Book section)
- Test: `tests/test_pilot_ideabook_nav.py`, `tests/test_pilot_help.py` (append)

**Interfaces:**
- On the home: `gg`/`G` jump selection to first/last visible; `Ctrl+D`/`Ctrl+U` half-page move; `?` opens `HelpScreen`; `/` search (filter visible by ticker/thesis substring → next match), `:` cmdline (`:q` closes home), `f` palette (ticker jump), `o` open selected idea's source/ticker URL, `F2` Screener, `D` Dossier for selected ticker. Footer cheatsheet verbatim (Global Constraints). Help text gains an Idea Book block listing `v e x i` + stage/sort/risk.

- [ ] **Step 1: Write the failing tests**

```python
import pytest
pytestmark = pytest.mark.pilot

async def test_gg_G_jump_selection(ideabook_home):
    app, pilot = ideabook_home
    await pilot.press("G"); await pilot.pause()
    last = app.screen._visible_order()[-1]
    assert app.screen._ib.selected_id == last
    await pilot.press("g"); await pilot.press("g"); await pilot.pause()
    assert app.screen._ib.selected_id == app.screen._visible_order()[0]

async def test_question_opens_help(ideabook_home):
    app, pilot = ideabook_home
    await pilot.press("?"); await pilot.pause()
    from news.app.widgets import HelpScreen
    assert isinstance(app.screen, HelpScreen)

async def test_f2_opens_screener_from_home(ideabook_home):
    app, pilot = ideabook_home
    await pilot.press("f2"); await pilot.pause()
    from news.app.screener import ScreenerScreen
    assert isinstance(app.screen, ScreenerScreen)

def test_help_text_has_ideabook_section():
    from news.app.widgets import _HELP_TEXT
    assert "Idea Book" in _HELP_TEXT and "v layout" in _HELP_TEXT and "e advance" in _HELP_TEXT
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the nav/cross-screen handlers (push the same `ScreenerScreen`/`DossierScreen`/`HelpScreen` the app uses; for cross-screen, the home can call `self.app._on_screener_command()` etc.). Add the Idea Book help block.
- [ ] **Step 4: Run** → PASS; re-run `-m pilot`.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): carry-over nav + cross-screen keys + help + footer"`

---

## Task M9.1: Persistence round-trip + env-parity + tty canary

**Files:**
- Modify: `news/app/commands.py` (`_build_persist_state` writes `ib_*`; `on_dismiss` syncs `self._ib_state` from the screen), `news/app/ideabook/screen.py` (`to_persist` on unmount/dismiss)
- Modify: `tests/test_tty_smoke.py` (pin existing canaries to news; add Idea Book canaries), `news/tui_smoke.py` (Idea Book chrome markers)
- Modify: `tests/test_script_launcher.py` (snapshot path note)
- Test: `tests/test_pilot_ideabook_persist.py`

**Interfaces:**
- `_build_persist_state()` includes `home_view` + `ib_layout/ib_sort_key/ib_risk_filter/ib_stage_filter/ib_inspector_open/ib_show_rejected/ib_selected_id` (read from `self._ib_state`). On home dismiss, `self._ib_state = screen._ib`. `on_mount` restores them (M3.3 already wired the read).
- tty: existing `test_tty_boot_paints_cells_*` pin `home_view="news"` via the `tty_state_dir` state file so they keep asserting the news panes (the layout regression they uniquely guard). NEW `test_tty_boot_paints_ideabook_*` (medium + wide) boot out-of-box (`home_view="ideabook"`), capture the real screen, and assert the Idea Book hero painted: chrome present + spine + at least one board column painted non-empty cells with width>0. Add Idea-Book markers to `news/tui_smoke.py` (`_IB_HEADER_MARKERS=("IDEABOOK","PIPELINE")`, `_IB_FOOTER_MARKERS=("advance","layout","inspector")`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pilot_ideabook_persist.py
import pytest
pytestmark = pytest.mark.pilot

async def test_layout_and_filters_persist_across_reboot(make_pilot_app, tmp_path):
    from news.tui_state_persist import get_state_path, load_ui_state
    async with make_pilot_app(size=(120, 40), with_user_db=True, home_view="news") as (app, pilot):
        await pilot.press("f5"); await pilot.pause()
        await pilot.press("v"); await pilot.pause()   # board→table
        app.screen._cycle_risk(); await pilot.pause() # → LOW
        await pilot.press("escape"); await pilot.pause()
    saved = load_ui_state(get_state_path())
    assert saved["ib_layout"] == "table" and saved["ib_risk_filter"] == "LOW"
```

```python
# tests/test_tty_smoke.py  (new canaries)
def test_tty_boot_paints_ideabook_medium(tty_db, tty_state_dir_ideabook):
    from news.tui_smoke import open_newstui_pty, assert_chrome_present_ideabook
    with open_newstui_pty({"cols":120,"rows":40}, db_path=tty_db,
                          env={"XDG_STATE_HOME": tty_state_dir_ideabook}) as h:
        grid = h.frame(settle=2.0, timeout=15.0)
    assert any(r.strip() for r in grid), "ideabook painted NO cells"
    assert_chrome_present_ideabook(grid, label="ideabook-120x40")  # IDEABOOK + PIPELINE + footer
```

- [ ] **Step 2: Run** `uv run pytest tests/test_pilot_ideabook_persist.py -q` and `uv run pytest -m tty -q` → FAIL.
- [ ] **Step 3: Implement** persistence writes/reads; add `assert_chrome_present_ideabook` to `news/tui_smoke.py`; add the `tty_state_dir_ideabook` fixture (empty dir → out-of-box hero) and pin the existing news canaries (`tty_state_dir` writes a `home_view="news"` ui_state.json). Verify the real launcher boots to the hero by capturing the pty screen (env-parity: real `scripts/news-tui` subprocess, rendered output asserted, network mocked only at the quote boundary — the screen shows loud `px …`/`SPY …`, never fabricated).
- [ ] **Step 4: Run** `uv run pytest -m tty -q` and `uv run pytest -m pilot -q` and full `uv run pytest -q` → all PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(ideabook): persist ib_* state + tty hero canary + env-parity"`

---

## Task M10.1: Plain-English user guide

**Files:**
- Create: `docs/guide/idea-book.md`
- Modify: `README.md` (link), `docs/guide/ui-v2.md` (cross-link if it indexes guides)
- Test: `tests/test_docs_guide.py` (append, if a guide-presence test pattern exists) or a simple existence assertion.

**Interfaces:** A guide for a smart engineer with ZERO finance background: define drawdown, 13D, insider/cluster buy, fwd PE, EV/EBITDA, short interest, RSI on first use; explain what the Idea Book is and why a trader cares; a worked example using the TUI (hotkeys first: `F5` to open, `j/k` to move, `v` to switch BOARD/TABLE/SPLIT, `e` advance, `x` reject, `i` inspector, click a pipeline stage to filter), then the `te-ideas` CLI second.

- [ ] **Step 1: Write a presence test**

```python
from pathlib import Path
def test_idea_book_guide_exists_and_defines_jargon():
    p = Path(__file__).parent.parent / "docs" / "guide" / "idea-book.md"
    body = p.read_text().lower()
    for term in ("drawdown", "13d", "fwd pe", "ev/ebitda", "insider", "cluster"):
        assert term in body, f"guide must define {term}"
    assert "f5" in body and "[e]" in body or "advance" in body
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Write** `docs/guide/idea-book.md` (region-by-region tour + worked example) and add the README link.
- [ ] **Step 4: Run** → PASS; final full `uv run pytest -q`.
- [ ] **Step 5: Commit** — `git commit -m "docs(ideabook): plain-English Idea Book guide + README link"`

---

## Architecture-gate extractions (when/why)

- **No forced pre-extraction** of existing files: the redesign is an isolated new screen, so `state_bridge.py` (265), `keys.py` (407, already over gate — NOT grown here), and `dispatch.py` stay untouched. The single small edits to `tui_app.py` (+~12 LOC, stays <300), `commands.py` (+~10), `tui_state_persist.py` (+fields), `ideas_db.py` (+migration), `region_header.py` (+`app_badge` param), `widgets.py` (+help block) are all additive and keep each file under the gate.
- **New modules are pre-split by region** so none exceeds its LOC budget (table above). If `screen.py` approaches 300 during M6–M8, EXTRACT the key-handling into `news/app/ideabook/keys.py` as a **separate refactor commit** (tests green before/after) before adding more handlers.

## Per-milestone test list (summary)

| M | Unit | Pilot | Geometry | tty / env-parity |
|---|---|---|---|---|
| M0 | theme hex, logic formulas | — | — | — |
| M1 | schema migration, IdeaVM/load, enrich | — | — | — |
| M2 | card text + badge colors | — | — | — |
| M3 | state roundtrip, region markup | F5 open/close, auto-boot | **regions width>0, sum=term width** | — |
| M4 | — | columns, empty copy, j/k | **#ib-body width/height>0** | — |
| M5 | — | v cycle, headers | **per-layout #ib-body>0** | — |
| M6 | — | i toggle, sections, split, note | **inspector width>0 open/split** | — |
| M7 | — | e/x mutations, filters, cycles (regression pins) | — | — |
| M8 | help text | gg/G, ?, F2, D | — | — |
| M9 | — | persist roundtrip | — | **tty hero canary + real launcher** |
| M10 | guide presence | — | — | — |

---

## Devil's Advocate — weakest assumption per milestone, failure mode, mitigation

1. **M3 (boot/F5 fork).** *Weakest assumption:* making the Idea Book the auto-boot hero won't break the ~15 existing `make_pilot_app` v2 tests that expect the news feed. *Failure mode:* flipping `default_ui_state["home_view"]="ideabook"` silently auto-pushes the home in every out-of-box pilot test → mass red. *Mitigation:* `make_pilot_app` gains an explicit `home_view="news"` default (additive kwarg, FROZEN-contract-safe) that writes a pinning state file — same "legacy tests pin explicit state" pattern conftest already uses for `_LEGACY_FLAT_BOOT_MODULES`. Only the real launcher (no state file) and the new Idea Book fixtures land on the hero. RED-verify: run `-m pilot` after the conftest edit, before the screen exists, to prove no existing test regressed.

2. **M1 (triad data sourcing fork).** *Weakest assumption:* the 43 real ideas can be enriched with drawdown/smart-money/valuation deterministically. *Failure mode:* live computation needs `yfinance` (52w high) + signal rows many ideas lack → blank or fabricated triads (violates "errors render loudly"). *Mitigation:* store the triad as **nullable display columns** backfilled from the HTML seed (the design's own ground truth), refresh only `px`/distance live from `quote_cache`; unenriched rows render `—` cells, never invented numbers. Agent-verifiable via the enrich unit test.

3. **M4/M6 (board/inspector as one Rich Static vs many widgets).** *Weakest assumption:* rendering the whole board/inspector as single scrollable `Static` renderables (not nested widgets) still satisfies the geometry gate. *Failure mode:* the gate demands every *primary pane* width>0; a single Static could collapse unseen. *Mitigation:* the geometry pins assert `#ib-body` and `#ib-inspector` `region.width/height>0` in every layout AND that the captured text contains column/section markers — a collapsed Static fails both. The 2026-06-06 zero-width incident is the precedent; this mirrors `_assert_panes_healthy`.

4. **M9 (env-parity for a pushed Screen).** *Weakest assumption:* the `--snapshot` Rich `build_layout` path need not render the Idea Book home. *Failure mode:* the snapshot test (`test_script_launcher`) keeps proving the *news* layout, so a hero-boot regression could ship unseen by snapshot. *Mitigation:* the **tty pty canary** boots the real `scripts/news-tui` out-of-box and asserts the Idea Book hero paints from the captured screen — the exact-artifact + rendered-output proof the env-parity gate requires; `--snapshot` stays news (documented). If the team wants snapshot parity, add `--snapshot-ideabook` (flagged as optional).

5. **M5/M7 (filter consistency across layouts).** *Weakest assumption:* `filter_visible`+`sort_ideas` applied uniformly keeps BOARD's "one column when stage-filtered" consistent with TABLE/SPLIT. *Failure mode:* BOARD filters columns while TABLE forgets to filter rows → divergent `n_shown`. *Mitigation:* all three body builders consume the SAME pre-filtered/sorted VM list from one `_visible_order()` seam; a pilot test asserts `n_shown` equals row/card count in each layout under an active filter.

6. **Status-bar marker collision (tty).** *Weakest assumption:* changing the home's status badge to `TE IDEABOOK` won't break the existing tty `assert_chrome_present` (which greps `"news-tui"`). *Failure mode:* out-of-box tty boot now shows the Idea Book home → `"news-tui"`/`"F2"` markers absent → existing canary red. *Mitigation:* pin the existing tty canaries to `home_view="news"` and add separate Idea Book canaries with their own markers; `status_markup` default output is byte-unchanged (param-gated).

---

## Open Questions (batched — design forks needing the owner's call; defaults chosen so the plan is executable as-is)

1. **Replace vs parallel hero.** Plan builds the Idea Book as a **parallel full-screen `Screen` that auto-boots as the hero and toggles via F5**, leaving the news feed untouched. Alternative: fully replace the news-list home (higher risk, breaks every news-feed pin at once). *Default: parallel hero.*
2. **Triad data for the 43 real ideas.** Plan **extends the `ideas` schema with nullable display columns + a seed backfill** (`te-ideas enrich`). Alternatives: derive live from signals/fundamentals (needs network + missing for many tickers) or parse from `thesis_short` (lossy). *Default: schema + backfill.*
3. **SPLIT semantics.** Plan makes SPLIT a **third internal layout of the Idea Book home** (idea list + inspector), distinct from the news feed's list+detail. Alternative: reuse the existing news list+detail widgets. *Default: internal third layout.*
4. **Entry-zone bar + ticker-live data.** Plan uses the design's **synthetic 52w range** (`lo*0.78`/`hi*1.7`) and seeds 5D/RSI/sent, **upgrading to real `quote_cache`/fundamentals/RSI when cached** (loud `…` when missing). Confirm whether real 52w low/high + RSI must be wired in this pass or deferred. *Default: synthetic now, real-when-cached, loud-when-missing.*
5. **Snapshot parity.** Plan proves the hero via the **tty pty canary** and leaves `--snapshot` rendering the news layout. Confirm whether `--snapshot` must also render the Idea Book home (would need a second Rich `build_layout`-style path). *Default: tty-only proof.*
6. **Stage-filter input.** Plan adds a **keyboard seam** (cycle stage filter) alongside mouse-click on spine tiles so pilot tests can drive it headless. Confirm the exact key (plan uses `[`/`]`, free on this screen). *Default: `[`/`]` + click.*
