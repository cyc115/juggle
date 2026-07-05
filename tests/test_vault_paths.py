"""P0a pins — canonical vault-root + loop-output resolver (`juggle_vault_paths`).

Loop Entity V2, plan P0a (2026-07-04): a single `get_vault_root()` replaces the
duplicated inline home-relative normalization at `juggle_cli.py:53-57` and
`juggle_cmd_research.py:52-62`, and `get_loop_output_root()` sources
`paths.loop_output_dir` (config-sourced, never a hardcoded vault literal — spec
§1.2/§1.3/§1.6). These pins guard behavior-equivalence of the extraction and the
no-hardcoded-literal constraint.
"""

import sys
from pathlib import Path
from unittest.mock import patch

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def _legacy_resolve(vault_val: str) -> Path:
    """The EXACT pre-refactor inline formula (juggle_cli.py:54-57) — the oracle."""
    if vault_val.startswith("~"):
        return Path(vault_val).expanduser()
    return Path.home() / vault_val.lstrip("/")


def test_vault_root_matches_legacy_resolution():
    """vault-root-matches-legacy — P0a extraction must be byte-identical to the
    2026-07-04 inline `~`/home-relative resolution for both value shapes."""
    import juggle_vault_paths

    for vault_val in ("/Documents/personal", "~/Documents/personal", "/some/other"):
        with patch(
            "juggle_vault_paths.get_settings",
            return_value={"paths": {"vault": vault_val}},
        ):
            assert juggle_vault_paths.get_vault_root() == _legacy_resolve(vault_val)

    # Default when the key is absent matches the legacy default "/Documents/personal".
    with patch("juggle_vault_paths.get_settings", return_value={"paths": {}}):
        assert juggle_vault_paths.get_vault_root() == _legacy_resolve("/Documents/personal")


def test_both_call_sites_resolve_identically():
    """both-sites-identical — juggle_cli and juggle_cmd_research must resolve the
    vault root through the ONE canonical accessor (P0a, 2026-07-04)."""
    import juggle_cli
    import juggle_cmd_research
    import juggle_vault_paths

    with patch(
        "juggle_vault_paths.get_settings",
        return_value={"paths": {"vault": "/Documents/personal", "vault_name": ""}},
    ):
        canonical = juggle_vault_paths.get_vault_root()
        assert juggle_cli._get_vault_root() == canonical
        # research returns (vault_path, vault_name); the path must be the same root.
        with patch(
            "juggle_cmd_research.get_settings",
            return_value={"paths": {"vault": "/Documents/personal", "vault_name": ""}},
        ):
            vault_path, _name = juggle_cmd_research._get_vault_info()
        assert Path(vault_path) == canonical


def test_loop_output_root_config_sourced_with_fallback():
    """loop-output-config-sourced — get_loop_output_root honors
    paths.loop_output_dir and falls back to the module default when unset
    (spec §1.3, P0a 2026-07-04)."""
    import juggle_vault_paths

    # Config value honored (leading-slash home-relative).
    with patch(
        "juggle_vault_paths.get_settings",
        return_value={"paths": {"loop_output_dir": "/Documents/personal/loops"}},
    ):
        assert juggle_vault_paths.get_loop_output_root() == Path.home() / "Documents/personal/loops"

    # Tilde value honored.
    with patch(
        "juggle_vault_paths.get_settings",
        return_value={"paths": {"loop_output_dir": "~/custom/loops"}},
    ):
        assert juggle_vault_paths.get_loop_output_root() == Path.home() / "custom/loops"

    # Unset → module-constant fallback (NOT juggle_settings.py — it's at LOC budget).
    with patch("juggle_vault_paths.get_settings", return_value={"paths": {}}):
        assert (
            juggle_vault_paths.get_loop_output_root()
            == _legacy_resolve(juggle_vault_paths.LOOP_OUTPUT_DIR_DEFAULT)
        )


def test_loop_run_dir_pure_and_deterministic():
    """loop_run_dir-pure — the cross-topic handoff path helper (spec §1.4) is a PURE
    function of (base, loop_id, run_ts, topic_id): base/loop_id/run_ts/<topic_id>.md,
    with NO I/O and NO get_settings inside (the config resolver is called once at the
    dispatch boundary and passed in as base, §1.6). Deterministic — same inputs →
    same path, so retrieval never parses prose (P4b, 2026-07-05)."""
    import juggle_vault_paths as vp

    base = Path("/vault/loops")
    p1 = vp.loop_run_dir(base, "L3", "0", "L3-research")
    assert p1 == base / "L3" / "0" / "L3-research.md"
    assert vp.loop_run_dir(base, "L3", "0", "L3-research") == p1  # deterministic
    # run_ts (= run_seq) disambiguates per-run dirs under the SAME stable topic (§1.4)
    assert vp.loop_run_dir(base, "L3", "1", "L3-research") != p1
    assert vp.loop_run_dir(base, "L3", "0", "L3-notify").name == "L3-notify.md"

    # PURE: the helper must not read settings (base is passed in, never resolved).
    def _boom():
        raise AssertionError("loop_run_dir must not call get_settings")
    with patch("juggle_vault_paths.get_settings", side_effect=_boom):
        assert vp.loop_run_dir(base, "L3", "0", "L3-research") == p1


def test_no_hardcoded_vault_literal_in_loop_dispatch_modules():
    """no-hardcoded-vault-literal — loop/dispatch code must read the vault/output
    root ONLY through the resolver, never a string literal (spec §1.6, grep-guard,
    2026-07-04). Config-schema homes (settings + the resolver itself) are exempt —
    the default living in config is legitimate."""
    src = Path(__file__).parent.parent / "src"

    # (a) The absolute user-specific literal must appear NOWHERE in src.
    absolute_literal = "Users/mikechen/Documents/personal"
    for py in src.rglob("*.py"):
        assert absolute_literal not in py.read_text(), f"absolute vault literal in {py.name}"

    # (b) Loop/dispatch consumer modules must not hardcode the relative vault
    #     literal — they must go through juggle_vault_paths. (The resolver and the
    #     settings schema legitimately hold the config default and are exempt.)
    loop_dispatch_modules = [
        "juggle_loop_fire.py",
        "juggle_loop_regen.py",
        "juggle_loop_instantiate.py",
        "juggle_loop_dispatch.py",
        "juggle_cmd_loop_create.py",
        "juggle_dispatch_core.py",
        "juggle_graph_dispatch.py",
        "juggle_graph_hydration.py",
        "juggle_prompt_context.py",
        "dbops/loops.py",
    ]
    for rel in loop_dispatch_modules:
        mod = src / rel
        if not mod.exists():
            continue
        assert "Documents/personal" not in mod.read_text(), f"hardcoded vault literal in {rel}"
