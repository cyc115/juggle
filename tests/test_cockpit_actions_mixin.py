"""Structural pin: action_* keybinding handlers live in CockpitActionsMixin
(src/juggle_cockpit_actions.py), mirroring the existing GraphModeMixin
extraction pattern. Slimming pass node N7 (2026-07-08 spec, docs section 8)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest_plugins = []


def test_cockpit_actions_mixin_module_exists():
    from juggle_cockpit_actions import CockpitActionsMixin

    assert CockpitActionsMixin is not None


def test_cockpit_app_bases_include_actions_mixin():
    from juggle_cockpit import CockpitApp
    from juggle_cockpit_actions import CockpitActionsMixin

    assert CockpitActionsMixin in CockpitApp.__mro__


def test_action_handlers_defined_on_mixin_not_cockpit_app():
    import juggle_cockpit
    from juggle_cockpit_actions import CockpitActionsMixin

    for name in (
        "action_switch",
        "action_ack",
        "action_close",
        "action_archive",
        "action_decommission",
        "action_filter",
        "action_focus_pane",
        "action_tail_toggle",
        "action_task_detail",
        "on_key",
    ):
        assert name in CockpitActionsMixin.__dict__, f"{name} missing from CockpitActionsMixin"
        assert name not in juggle_cockpit.CockpitApp.__dict__, (
            f"{name} should be moved off CockpitApp onto CockpitActionsMixin"
        )
