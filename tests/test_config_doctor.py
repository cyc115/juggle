"""Tests for juggle_config_doctor — stale/inert/unknown config detection.

Doctor's stale-config check runs on EVERY invocation. It must:
  - flag inert keys (code no longer honors them) — names the deprecation version,
  - flag unknown keys (not in DEFAULTS schema) — report-only by default,
  - never false-positive on a clean config,
  - prune inert keys in non-dry-run, leaving valid keys intact,
  - never modify the config file in --dry-run.

NEVER touches the real ~/.juggle/config.json — all tests use tmp config files.
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import juggle_config_doctor as cdoc  # noqa: E402
from juggle_settings import DEFAULTS  # noqa: E402


# ── inert registry ────────────────────────────────────────────────────────────


def test_inert_registry_seeded_with_integrate_keys():
    """The motivating bug: integrate.test_scope / quarantine_tests are inert
    since the full-suite directive. They MUST be in the registry."""
    assert "integrate.test_scope" in cdoc.INERT_KEYS
    assert "integrate.quarantine_tests" in cdoc.INERT_KEYS
    # Notes must name the version so users understand why.
    assert "1.80.0" in cdoc.INERT_KEYS["integrate.quarantine_tests"]
    assert "1.80.0" in cdoc.INERT_KEYS["integrate.test_scope"]


def test_find_inert_keys_flags_present_quarantine():
    """REGRESSION PIN (2026-06-20): live config.json had integrate.quarantine_tests
    set (6 entries) + integrate.test_scope=null while integrate IGNORES them
    entirely since v1.80.0 — silently masking tests. Doctor must flag this."""
    cfg = {
        "integrate": {
            "test_scope": None,
            "quarantine_tests": [
                "tests/test_loc_gate.py",
                "tests/test_data_migration.py",
            ],
        }
    }
    found = dict(cdoc.find_inert_keys(cfg))
    assert "integrate.quarantine_tests" in found
    assert "integrate.test_scope" in found
    assert "1.80.0" in found["integrate.quarantine_tests"]


def test_find_inert_keys_clean_config_empty():
    """A config that sets no inert keys yields no inert findings."""
    cfg = {"max_threads": 12, "cockpit": {"bell": False}}
    assert cdoc.find_inert_keys(cfg) == []


# ── unknown keys ──────────────────────────────────────────────────────────────


def test_find_unknown_keys_flags_typo():
    """A misspelled/removed top-level key is unknown to DEFAULTS."""
    cfg = {"max_threds": 12}  # typo of max_threads
    unknown = cdoc.find_unknown_keys(cfg, DEFAULTS)
    assert "max_threds" in unknown


def test_find_unknown_keys_flags_nested_typo():
    """An unknown nested key is reported with its full dotted path."""
    cfg = {"cockpit": {"refresh_interval_secs": 2.0, "bogus_knob": 1}}
    unknown = cdoc.find_unknown_keys(cfg, DEFAULTS)
    assert "cockpit.bogus_knob" in unknown
    # valid sibling not flagged
    assert "cockpit.refresh_interval_secs" not in unknown


def test_find_unknown_keys_clean_config_no_false_positives():
    """A subset of the real DEFAULTS schema must yield ZERO unknown keys."""
    cfg = {
        "max_threads": 15,
        "integrate": {"test_scope": "full", "core_tests": [], "quarantine_tests": []},
        "cockpit": {"refresh_interval_secs": 1.5, "bell": False},
        "paths": {"vault": "/Documents/personal"},
    }
    assert cdoc.find_unknown_keys(cfg, DEFAULTS) == []


def test_find_unknown_keys_ignores_freeform_repos():
    """`repos` is keyed by arbitrary absolute repo path — user keys are valid,
    must NOT be flagged as unknown."""
    cfg = {"repos": {"/Users/me/myrepo": {"push_mode": "pr", "test_cmd": "pytest"}}}
    assert cdoc.find_unknown_keys(cfg, DEFAULTS) == []


def test_find_unknown_keys_ignores_freeform_harnesses():
    """agent.harnesses / harness_by_role are user-extensible registries — a
    custom harness or per-role mapping is legitimate, not unknown."""
    cfg = {
        "agent": {
            "harness_by_role": {"researcher": "codex"},
            "harnesses": {"myharness": {"type": "template", "command": "foo"}},
        }
    }
    assert cdoc.find_unknown_keys(cfg, DEFAULTS) == []


def test_find_unknown_keys_skips_inert_paths():
    """Inert keys live IN DEFAULTS, so the unknown scan must not see them; they
    are reported via the inert channel only (no double counting)."""
    cfg = {"integrate": {"quarantine_tests": ["tests/x.py"], "test_scope": None}}
    assert cdoc.find_unknown_keys(cfg, DEFAULTS) == []


# ── analyze + format ──────────────────────────────────────────────────────────


def test_analyze_config_separates_channels():
    cfg = {
        "integrate": {"quarantine_tests": ["tests/x.py"]},
        "totally_made_up": 1,
    }
    report = cdoc.analyze_config(cfg, DEFAULTS)
    assert "integrate.quarantine_tests" in dict(report.inert)
    assert "totally_made_up" in report.unknown
    assert report.has_findings


def test_analyze_clean_config_no_findings():
    cfg = {"max_threads": 9, "cockpit": {"bell": True}}
    report = cdoc.analyze_config(cfg, DEFAULTS)
    assert not report.has_findings
    assert report.inert == []
    assert report.unknown == []


def test_format_report_mentions_reasons():
    cfg = {"integrate": {"quarantine_tests": ["tests/x.py"]}, "nope_key": 1}
    report = cdoc.analyze_config(cfg, DEFAULTS)
    lines = cdoc.format_report(report)
    text = "\n".join(lines)
    assert "integrate.quarantine_tests" in text
    assert "1.80.0" in text
    assert "nope_key" in text


# ── prune ─────────────────────────────────────────────────────────────────────


def test_prune_removes_inert_leaves_valid():
    cfg = {
        "max_threads": 12,
        "integrate": {
            "test_scope": None,
            "quarantine_tests": ["tests/x.py"],
            "core_tests": [],
        },
        "cockpit": {"bell": False},
    }
    new_cfg, removed = cdoc.prune_config(
        cfg, ["integrate.test_scope", "integrate.quarantine_tests"]
    )
    assert "test_scope" not in new_cfg["integrate"]
    assert "quarantine_tests" not in new_cfg["integrate"]
    # valid keys untouched
    assert new_cfg["max_threads"] == 12
    assert new_cfg["cockpit"]["bell"] is False
    assert new_cfg["integrate"]["core_tests"] == []
    assert set(removed) == {"integrate.test_scope", "integrate.quarantine_tests"}


def test_prune_does_not_mutate_input():
    cfg = {"integrate": {"test_scope": None}}
    new_cfg, _ = cdoc.prune_config(cfg, ["integrate.test_scope"])
    assert "test_scope" in cfg["integrate"], "input dict must not be mutated"
    assert "test_scope" not in new_cfg["integrate"]


def test_prune_missing_path_is_noop():
    """Pruning a path that isn't present must not error and must not be in removed."""
    cfg = {"integrate": {"core_tests": []}}
    new_cfg, removed = cdoc.prune_config(cfg, ["integrate.quarantine_tests"])
    assert removed == []
    assert new_cfg == cfg


# ── doctor CLI integration (tmp config + fake DB) ──────────────────────────────
#
# These drive cmd_doctor end-to-end against a tmp config.json + a no-op fake DB
# (the _FakeDB pattern mirrors tests/test_doctor.py so the DB section does no
# real work and the tests stay focused on the config-doctor behavior).

import json  # noqa: E402

import juggle_cmd_doctor as doc  # noqa: E402
import juggle_db  # noqa: E402


class _FakeDB:
    """No-op DB so doctor's DB/reconcile/backfill sections do nothing."""

    def __init__(self, path):
        self._path = path

    def init_db(self, *, require_migrate=False):
        pass

    def list_projects(self, include_archived=False):
        return []


def _make_current_db(db_path):
    """Minimal DB file so Path(DB_PATH).exists() is True; _FakeDB does the rest."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, status TEXT)")
    conn.commit()
    conn.close()


def _wire_fake_db(tmp_path, monkeypatch):
    """Point doctor at a fake DB so only the config section does real work."""
    db_path = tmp_path / "current.db"
    _make_current_db(db_path)
    monkeypatch.setattr(juggle_db, "DB_PATH", str(db_path))
    monkeypatch.setattr(juggle_db, "JuggleDB", _FakeDB)


class _Args:
    def __init__(self, dry_run):
        self.dry_run = dry_run


def test_doctor_dry_run_does_not_modify_config(tmp_path, monkeypatch, capsys):
    """Dry-run reports the inert key but must NOT touch the config file bytes."""
    cfg_path = tmp_path / "config.json"
    bak_path = tmp_path / "config.json.bak"
    cfg_path.write_text(
        json.dumps(
            {
                "max_threads": 11,
                "integrate": {"quarantine_tests": ["tests/test_loc_gate.py"]},
            },
            indent=2,
        )
    )
    before = cfg_path.read_bytes()

    monkeypatch.setattr(doc, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(doc, "BACKUP_PATH", bak_path)
    _wire_fake_db(tmp_path, monkeypatch)

    assert doc.cmd_doctor(_Args(dry_run=True)) == 0

    assert cfg_path.read_bytes() == before, "dry-run must not modify the config file"
    assert not bak_path.exists(), "dry-run must not write a backup"
    out = capsys.readouterr().out
    assert "integrate.quarantine_tests" in out
    assert "would prune" in out.lower() or "dry" in out.lower()


def test_doctor_non_dry_run_prunes_inert_keeps_valid(tmp_path, monkeypatch, capsys):
    """Non-dry-run prunes the inert integrate.* keys, leaves valid keys intact,
    and writes a backup."""
    cfg_path = tmp_path / "config.json"
    bak_path = tmp_path / "config.json.bak"
    cfg_path.write_text(
        json.dumps(
            {
                "max_threads": 13,
                "integrate": {
                    "test_scope": None,
                    "quarantine_tests": ["tests/test_loc_gate.py"],
                    "core_tests": [],
                },
                "cockpit": {"bell": False},
            },
            indent=2,
        )
    )

    monkeypatch.setattr(doc, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(doc, "BACKUP_PATH", bak_path)
    _wire_fake_db(tmp_path, monkeypatch)

    assert doc.cmd_doctor(_Args(dry_run=False)) == 0

    after = json.loads(cfg_path.read_text())
    assert "quarantine_tests" not in after["integrate"], "inert key must be pruned"
    assert "test_scope" not in after["integrate"], "inert key must be pruned"
    # valid keys survive
    assert after["max_threads"] == 13
    assert after["cockpit"]["bell"] is False
    assert bak_path.exists(), "non-dry-run must write a backup before pruning"
    out = capsys.readouterr().out
    assert "pruned" in out.lower()


def test_doctor_unknown_key_reported_not_pruned(tmp_path, monkeypatch, capsys):
    """Unknown keys are REPORT-ONLY: flagged in output but never auto-removed."""
    cfg_path = tmp_path / "config.json"
    bak_path = tmp_path / "config.json.bak"
    cfg_path.write_text(json.dumps({"bogus_top": 1, "max_threads": 7}, indent=2))

    monkeypatch.setattr(doc, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(doc, "BACKUP_PATH", bak_path)
    _wire_fake_db(tmp_path, monkeypatch)

    assert doc.cmd_doctor(_Args(dry_run=False)) == 0

    after = json.loads(cfg_path.read_text())
    assert after["bogus_top"] == 1, "unknown key must NOT be auto-pruned"
    out = capsys.readouterr().out
    assert "bogus_top" in out
    assert "UNKNOWN" in out


# ── _JUGGLE_CONFIG_PATH parity (Codex review, 2026-06-20) ──────────────────────


def test_doctor_honors_juggle_config_path_override(tmp_path, monkeypatch, capsys):
    """REGRESSION PIN (2026-06-20, Codex review): doctor read/backed-up/pruned the
    hardcoded module-level CONFIG_PATH (~/.juggle/config.json) and ignored the
    _JUGGLE_CONFIG_PATH override that juggle_settings.get_settings() honors. In a
    deployment whose runtime config comes from _JUGGLE_CONFIG_PATH, doctor would
    prune the WRONG file and could mutate the home config. Doctor must resolve the
    SAME effective path as the loader.

    This test does NOT monkeypatch doc.CONFIG_PATH — it sets _JUGGLE_CONFIG_PATH to
    a tmp config and asserts (a) the tmp file's inert key was pruned, (b) the backup
    landed NEXT TO that tmp file (not in $HOME), and (c) the real home backup was not
    newly created (proving the home config was never touched).

    Pre-fix this FAILS: doctor operates on ~/.juggle/config.json, so the tmp file is
    never pruned (no .bak created next to it) and the assertions below trip.
    """
    # Tmp config carrying an inert key (integrate.quarantine_tests is inert since
    # the v1.80.0 full-suite directive) plus a valid key that must survive.
    env_cfg = tmp_path / "isolated_config.json"
    env_cfg.write_text(
        json.dumps(
            {
                "max_threads": 14,
                "integrate": {"quarantine_tests": ["tests/test_loc_gate.py"]},
            },
            indent=2,
        )
    )
    env_backup = env_cfg.with_name(env_cfg.name + ".bak-pre-1.21")

    # Point the loader (and now doctor) at the tmp config via the env override.
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(env_cfg))
    # Do NOT patch doc.CONFIG_PATH/BACKUP_PATH: leaving them at the home default is
    # exactly how cmd_doctor detects "no test override" and falls through to the
    # _JUGGLE_CONFIG_PATH-aware resolver. Assert that precondition explicitly.
    assert doc.CONFIG_PATH == doc._HOME_CONFIG_DEFAULT
    assert doc.BACKUP_PATH == doc._HOME_BACKUP_DEFAULT

    # Safety net: the real home backup must not exist before the run (and we assert
    # below that doctor did not create it). Guard so this test never writes to $HOME.
    home_backup = doc._HOME_BACKUP_DEFAULT
    home_backup_existed = home_backup.exists()

    # DB section: point at a nonexistent path so cmd_doctor returns right after the
    # config section (it prints "will be created" and returns 0 before any DB work).
    monkeypatch.setattr(juggle_db, "DB_PATH", str(tmp_path / "no_such.db"))

    assert doc.cmd_doctor(_Args(dry_run=False)) == 0

    # (a) the _JUGGLE_CONFIG_PATH file had its inert key pruned, valid key intact.
    after = json.loads(env_cfg.read_text())
    assert "quarantine_tests" not in after["integrate"], (
        "inert key in the _JUGGLE_CONFIG_PATH file must be pruned"
    )
    assert after["max_threads"] == 14, "valid key must survive"

    # (b) the backup landed NEXT TO the tmp config (not in $HOME).
    assert env_backup.exists(), "backup must be written next to the overridden config"

    # (c) isolation: the real home backup was NOT newly created by this run.
    assert home_backup.exists() == home_backup_existed, (
        "doctor must not create a backup in $HOME when _JUGGLE_CONFIG_PATH is set"
    )
    assert env_backup.parent == tmp_path, "backup must live under tmp_path, never $HOME"

    out = capsys.readouterr().out
    assert "pruned" in out.lower()
