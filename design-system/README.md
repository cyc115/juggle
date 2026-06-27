# Juggle Cockpit — Design System (HTML preview cards)

A **local** component library of self-contained static HTML+CSS cards that
faithfully reproduce the Juggle Cockpit TUI's visual components. These are
synced to a Claude Design project by the orchestrator — **do not push from here**.

Each card lives at `components/<name>/index.html` and is fully self-contained
(inline `<style>`, no shared assets). The **first line** of every card is a
machine-readable marker:

```html
<!-- @dsCard group="<Group>" -->
```

Run the checker to validate every card (line-1 marker, HTML parses, balanced
inline CSS):

```bash
python3 scripts/check_design_cards.py          # human output, exit 1 on any failure
python3 scripts/check_design_cards.py --json    # machine-readable
```

## Fidelity — where the palette comes from

Colors were **extracted from source**, not guessed:

- **Terminal content** (the panes/rows/cells) renders through Rich's
  `SVG_EXPORT_THEME` — captured by rendering the *real* `render_topics` /
  `render_actions` / `render_agents` / `render_notifications` /
  `build_graph_panel` functions to SVG and reading the emitted hex:

  | role | named color | hex |
  |------|-------------|-----|
  | background | — | `#292929` |
  | foreground | default | `#c5c8c6` |
  | dim | dim fg | `#868887` |
  | blocker / failed | red | `#cc555a` |
  | busy / complete / verified | green | `#98a84b` |
  | review / stale / warning / running | yellow | `#d0b344` |
  | graph border / ready | cyan | `#68a0b3` |
  | pending / mirror | grey50 | `#808080` |
  | project header strip | grey23 | `#3a3a3a` |

- **Chrome** (borders/banners driven by Textual CSS variables in
  `juggle_cockpit_css.py`) uses the Textual `textual-dark` theme:
  `$accent`/`$warning` `#ffa62b`, `$error` `#ba3c5b`, `$success` `#4EBF71`,
  `$primary` `#0178D4`. Active pane border = `bright_blue` `#1984e9`; inactive =
  dim grey.

Terminal content is **monospace**; per house rule, the cards' own designer
chrome (labels, group tags, notes) uses a **sans** font. Glyphs are copied
verbatim from the glyph tables in `juggle_cockpit_view.py`.

## Components

| Card | Group | Represents | Source |
|------|-------|-----------|--------|
| `topics-panel` | Topics | Topic rows (current/running/paused/done) + project-grouped variant with aggregate progress | `render_topics` |
| `agents-panel` | Agents | Active + Pool sections; busy/stale/idle states, harness/model badge, scheduled `(L)` rows | `render_agents` |
| `notifications-panel` | Notifications | Notification lines by kind: complete/warning/error/failed/info | `render_notifications` |
| `action-items-panel` | Actions | Action-item chips by priority tier: blocker/review/open-question/nudge | `render_actions` |
| `graph-node-states` | Graph | Every DAG node state cell: pending/ready/dispatching/running/integrating/verified/failed/blocked/mirror/selected | `TASK_STATE_GLYPHS` + `_cell_text` |
| `graph-panel` | Graph | Full graph-mode panel: header progress bar, column-major node grid, legend, unread badge | `build_graph_panel` |
| `footer-hotkeys` | Chrome | Compact footer hotkey bar (visible bindings) | `BINDINGS` + `Footer` |
| `header-bar` | Chrome | Title + `Cockpit v2 · v<version>` subtitle + clock | `Header` + `_cockpit_subtitle` |
| `version-banner` | Chrome | Version-drift restart banner (warning background) | `_drift_banner` + `#version-banner` CSS |
| `watchdog-status` | Chrome | Watchdog overlay chip: `● wd <pid>` alive / `○ wd` dead | `#wd-status` CSS + `_refresh` |
| `budget-meters` | Chrome | 5h / 7d token budget meters **(PROPOSED — not yet in source)** | floor item; styled to `_progress_bar` |
| `help-modal` | Modals | Keyboard-shortcuts overlay (grouped key/short/desc) | `render_help_lines` |
| `task-detail-modal` | Modals | Graph-node detail: title/state/deps/thread/verify + child tasks + prompt excerpt | `_GraphTaskModal` |
| `topic-detail-modal` | Modals | Thread detail: header + LLM Context/Why/What/Result + recent activity | `_TopicDetailModal` |
| `tail-drawer` | Modals | Live tmux-pane tail overlay (scrollable, auto-follow) | `_TailModal` |
| `project-arm-panel` | Modals | Project arm/disarm overlay: global flag + per-project rows | `_ProjectArmModal` |
| `confirm-modal` | Modals | Single-key y/N confirm gate (warning border) | `_ConfirmModal` |
| `prompt-modal` | Modals | One-line input modal (switch/filter) | `_PromptModal` |

**18 cards across 7 groups:** Topics, Agents, Notifications, Actions, Graph,
Chrome, Modals.

> Note: `budget-meters` has no rendering in the cockpit source yet — it is on the
> inventory floor and is included as a **proposed** chrome element, clearly badged
> as such and styled to match the existing progress-bar primitive and palette.
