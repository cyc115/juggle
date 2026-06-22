"""Cockpit stylesheet (Textual CSS).

Extracted from juggle_cockpit.py to keep that module within its LOC budget
(architecture gate). Pure presentation data — no logic — so it moves verbatim;
``CockpitApp`` sets ``CSS = COCKPIT_CSS``.
"""
from __future__ import annotations

COCKPIT_CSS = """
    Screen {
        layers: base overlay;
    }
    #version-banner {
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
    }
    #actions {
        height: 100%;
    }
    #agents {
        height: 100%;
    }
    #notif-region {
        layout: vertical;
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
    """
