# P9 verify-harness

One operator-free wrapper per node of the **P9 CLI-grammar-migration** DAG
(spec §6: `docs/2026-06-29-cli-grammar-migration-spec.md` in the vault).

## Why wrappers exist

`juggle graph add-task --verify-cmd` runs a lint (`juggle_graph_upsert.lint_verify_cmd`)
that **forbids shell operators** (`& ; | > < ` `` ` `` `$(`) and only allowlists these
executables as the first token: `make uv pytest python python3 npm cargo go`.
**`bash` is NOT allowlisted.**

Every §6 node's documented `verify_cmd` is a compound command using `&&`, `|`,
`>`, or `!`, so none can load verbatim. The fix (same as `scripts/p8_verify/`):

- Each node's `verify_cmd` is the single, operator-free token **`make p9-verify-<id>`**
  (exe `make` is allowlisted; no operators).
- The Makefile pattern rule `p9-verify-%` runs `bash scripts/p9_verify/$*.sh`.
- The wrapper holds the real compound command (`set -euo pipefail`, exits non-zero on fail).

## Nodes (18)

| id   | wrapper   | node |
|------|-----------|------|
| r1   | `r1.sh`   | R1-spec-scaffold |
| r2   | `r2.sh`   | R2-generic-registrar |
| r3   | `r3.sh`   | R3-port-threads |
| r4   | `r4.sh`   | R4-switch-entrypoint |
| g1   | `g1.sh`   | G1-resource-groups |
| g2   | `g2.sh`   | G2-fold-project-graph |
| g3   | `g3.sh`   | G3-verb-lint |
| a1   | `a1.sh`   | A1-alias-shim |
| a2   | `a2.sh`   | A2-alias-coverage |
| a3   | `a3.sh`   | A3-output-parity |
| d1   | `d1.sh`   | D1-warn-on |
| m1   | `m1.sh`   | M1-templates |
| m2   | `m2.sh`   | M2-commands-md |
| m3   | `m3.sh`   | M3-skills-scripts |
| m4   | `m4.sh`   | M4-tests |
| m5   | `m5.sh`   | M5-docs |
| x1   | `x1.sh`   | X1-regen-units |
| x2   | `x2.sh`   | X2-remove-aliases (manual-gated; OMITTED from the auto-loader) |

## Note on the wrappers' content

The wrappers reference test files and CLI grammar that **do not exist yet** — they
are the deliverables each node produces. A wrapper is expected to FAIL until its
node is implemented; that is exactly what makes it a deterministic done-gate.
