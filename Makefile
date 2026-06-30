# Juggle Makefile
# Watchdog is now supervised by the cockpit (not launchd).

.PHONY: test test-fast

# FULL suite (parallel) — the same scope the integrate/CI gate runs. Bare
# `pytest` and this target are ALWAYS the full suite; the speedup-tier `slow`
# marker is NEVER deselected by default addopts (B2, 2026-06-21).
test:
	uv run pytest -n auto --dist loadgroup -m "not watchdog_proc"

# OPT-IN fast inner loop — deselects the heavy `slow` bucket (cockpit render,
# watchdog real-daemon/gap, uv-run shell-outs) for a quick local pass. NEVER
# used by integrate/CI: that would subset the always-full-suite gate.
test-fast:
	uv run pytest -n auto -m "not slow and not watchdog_proc"

# P8 legacy-table-drop per-node acceptance gates (run the committed verify scripts)
p8-verify-%: FORCE
	@bash scripts/p8_verify/$*.sh

# P9 CLI-grammar-migration per-node acceptance gates (run the committed verify scripts).
# The add-task verify_cmd lint forbids shell operators (& ; | > < ` $() and only
# allowlists {make,uv,pytest,python,python3,npm,cargo,go} as the exe — `bash` is
# NOT allowlisted. So each P9 node's verify_cmd is the single operator-free token
# `make p9-verify-<id>`; the compound command lives in scripts/p9_verify/<id>.sh.
p9-verify-%: FORCE
	@bash scripts/p9_verify/$*.sh

FORCE:
