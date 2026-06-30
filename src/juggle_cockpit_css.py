"""Cockpit stylesheet (Textual CSS).

Extracted from juggle_cockpit.py to keep that module within its LOC budget
(architecture gate). Presentation data; ``CockpitApp`` sets ``CSS = COCKPIT_CSS``.
The single derived value (``_MIN_NOTIF_HEIGHT``, the notification-pane floor) is
substituted into the stylesheet at build time and also threaded into the
HSplitter drag floor so one source of truth governs both.
"""
from __future__ import annotations

# Minimum visible height (terminal cells) of #notif-region: the notifications
# Rich Panel needs the top border (which carries the "Notifications" title),
# ONE content row, and the bottom border to show a single full notification —
# 3 cells (derived from render_notifications, not a magic number). Shared by the
# CSS floor below and threaded into HSplitter's drag floor so neither the
# initial layout, a terminal resize, nor a manual drag can squeeze the pane
# below one visible row.
_MIN_NOTIF_HEIGHT: int = 3

COCKPIT_CSS = """
    Screen {
        layers: base overlay;
    }
    #version-banner {
        display: none;
        height: auto;
        width: 100%;
        background: $warning;
        color: $text;
        text-align: center;
        text-style: bold;
    }
    #layout {
        layout: horizontal;
        height: 1fr;
    }
    #topics {
        height: 100%;
        min-width: 24;
    }
    #right {
        height: 100%;
        layout: vertical;
        min-width: 20;
    }
    #upper {
        layout: horizontal;
        height: 1fr;
    }
    #actions {
        height: 100%;
    }
    #agents {
        height: 100%;
    }
    #notif-region {
        layout: vertical;
        min-height: __MIN_NOTIF_HEIGHT__;
    }
    #notifications {
        height: 100%;
    }
    #graph-scroll {
        height: 100%;
        display: none;
    }
    #graph-body {
        height: auto;
    }
    Footer {
        layer: base;
    }
    #wd-status {
        layer: overlay;
        dock: bottom;
        width: 16;
        height: 1;
        offset: 0 -1;
        background: $panel;
        color: $success;
        content-align: right middle;
    }
    """.replace("__MIN_NOTIF_HEIGHT__", str(_MIN_NOTIF_HEIGHT))
