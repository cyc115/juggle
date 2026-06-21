# OpenRouter Fallback — Facts & Implementation Spec

Date: 2026-06-17 · Scope: read-only investigation of `/Users/mikechen/github/juggle` · No source modified.

## Summary

**Verdict: PARTIALLY graceful.** Juggle has a clean fallback only inside `llm_calls.llm_call`
(OpenRouter → `claude -p` → None) and everything routed through it degrades correctly when the
OpenRouter key is unset: title generation, topic summaries, project-create coach, project
classification/inference. These keep working via `claude -p`.

**But the entire research/search subsystem bypasses `llm_call` and calls OpenRouter directly with
NO `claude -p` fallback.** With the key unset, those commands either `sys.exit(1)` with an error, print
a warning and skip, or raise (embeddings). Concretely degraded/broken: `research`, `research-ingest`,
`search` (KB half), and offline semantic search.

**Env-var bug: CONFIRMED but benign in practice.** Every *functional read* uses `OPENROUTER_KEY`
(7 sites, consistent). Only two *comments* and the `init.md`/reasonix docs mention `OPENROUTER_API_KEY`
— and one of those (settings.py:239) is a real instruction telling users to export `OPENROUTER_API_KEY`
for the reasonix harness, which **nothing reads**. So the name is consistent in code, but the docs/comments
are inconsistent and one comment instructs the wrong variable. See below.

## Env var inconsistency

Canonical functional name: **`OPENROUTER_KEY`** (read in 7 places, written by `init.md` into `~/.juggle/.env`,
loaded by `juggle_cli.py:29-35` via `os.environ.setdefault`).

| Site | file:line | Name | Kind |
|---|---|---|---|
| llm_call dispatcher | `src/llm_calls.py:77` | `OPENROUTER_KEY` | functional read |
| research-ingest main | `src/juggle_research_ingest.py:214,216` | `OPENROUTER_KEY` | functional read + error msg |
| cmd_search main | `src/juggle_cmd_search.py:99,107` | `OPENROUTER_KEY` | functional read + warning |
| cmd_research run | `src/juggle_cmd_research.py:271,273` | `OPENROUTER_KEY` | functional read + error msg |
| offline search | `src/juggle_search_offline.py:94,97` | `OPENROUTER_KEY` | functional read + error msg |
| cli env-load comment | `src/juggle_cli.py:28` | `OPENROUTER_KEY` | comment (correct) |
| settings title_gen comment | `src/juggle_settings.py:379` | `OPENROUTER_KEY` | comment (correct) |
| settings reasonix doc | `src/juggle_settings.py:239` | **`OPENROUTER_API_KEY`** | comment/instruction (mismatched) |
| init.md reasonix example | `docs/reasonix.toml.example` (referenced) | `OPENROUTER_API_KEY` | doc |

**Conclusion:** Code is internally consistent on `OPENROUTER_KEY`; the key is NOT silently undetected in any
functional path. The bug is documentation drift: `settings.py:239` tells the user to export `OPENROUTER_API_KEY`
for reasonix-launched agents, but `juggle_cli.py` only loads `OPENROUTER_KEY` from `.env`, and `init.md` only
writes `OPENROUTER_KEY`. A user following the reasonix comment would set a variable nothing consumes. Fix =
standardize the comment to `OPENROUTER_KEY` (or make the reasonix env explicitly inherit `OPENROUTER_KEY`).

## Call-site inventory

| Site | file:line | Routed via llm_call? | Behavior when key unset | Verdict |
|---|---|---|---|---|
| `llm_call` dispatcher | `src/llm_calls.py:66-119` | n/a (is the dispatcher) | Skips OpenRouter (`if api_key:`), uses `run_claude_p(fallback_model)` | **SAFE** |
| Title generation | `juggle_cli_common.py:274` → `_cheap_llm_call` (165) → `llm_call` | Yes (cheap) | claude -p; plus deterministic word-slice fallback (`fallback` at :256) | **SAFE** |
| Topic summary | `juggle_topic_summary.py:133-137` | Yes (cheap) | claude -p | **SAFE** |
| Project summary | `juggle_project_summary.py:15-18` | Yes (`run_claude_p` direct, model=sonnet) | Always claude -p (never uses OpenRouter) | **SAFE** |
| Project classify | `juggle_cmd_projects.py:234` | Yes (cheap) | claude -p | **SAFE** |
| Project infer | `juggle_cmd_projects.py:385` | Yes (cheap) | claude -p | **SAFE** |
| Project coach (create) | `juggle_cmd_projects.py:468-471, 703-705` | `run_claude_p` direct (model=sonnet) | Always claude -p | **SAFE** |
| **Research synthesis** | `juggle_cmd_research.py:186-197` (`synthesize` via httpx) | **No** | `run()` at :271-274 does `sys.exit(1)` "OPENROUTER_KEY not set" before reaching synthesis | **GAP** |
| **Research KB embed** | `juggle_cmd_research.py:79` (`get_embedding`/`search_kb`) | **No** | guarded by same `sys.exit(1)` at :272-274 | **GAP** |
| **Search — KB embed** | `juggle_cmd_search.py:46-67` (`get_embedding`,`search_kb`) | **No** | `main` :106-107 prints warning, **skips KB**, web still returned | **GAP (degraded, not crash)** |
| **Search — Haiku filter** | `juggle_cmd_search.py:70-91` (`haiku_filter`) | **No** | only runs `if ... api_key` (:114), silently skipped → unfiltered output | **GAP (degraded)** |
| **Research-ingest embed** | `juggle_research_ingest.py:58-122` (`embed_batch` etc.) | **No** | `main` :214-217 `sys.exit(1)` "OPENROUTER_KEY not set" | **GAP** |
| **Offline search embed** | `juggle_search_offline.py:101` (`_get_embedding`) | **No** | :94-100 `sys.exit(1)` unless `--fts` given | **GAP (has --fts escape hatch)** |

User-facing impact when key unset:
- `project create` coach, cockpit titles, project/thread/topic summaries, project classification → **work** (claude -p).
- `research <topic>` → hard exit, no output.
- `research-ingest` → hard exit, KB never populated.
- `search <q>` → web-only, KB skipped, no Haiku dedup/filter (lower quality but non-fatal).
- offline `search-offline-db` → hard exit unless `--fts` (FTS keyword search works fully offline).

**Note on embeddings:** `llm_calls.py` has NO embedding helper — `claude -p` cannot produce embedding vectors.
So KB semantic search/ingest CANNOT fall back to claude -p; they hard-require an embeddings provider. Only the
*text-generation* sub-steps (synthesis, Haiku filter) can route through `llm_call`. This is the key design
constraint for the spec.

## Settings

`src/juggle_settings.py` `DEFAULTS`:
- **Key is env-only.** No `openrouter_key`/`api_key` field in `DEFAULTS`. The key lives solely in
  `~/.juggle/.env` as `OPENROUTER_KEY`, loaded into the process env by `juggle_cli.py:29-35`
  (`setdefault`, so a real env var wins over `.env`). Default when unset = empty string at every read site.
- **`llm_profiles`** (`:387-396`): `cheap` = `{openrouter_model: deepseek/deepseek-chat-v3-0324:free,
  fallback_model: claude-haiku-4-5-20251001}`; `normal` = `{openrouter_model: moonshotai/kimi-k2:free,
  fallback_model: claude-sonnet-4-6}`. `llm_call` hardcodes `max_tokens: 200` (`llm_calls.py:85`) — too small
  for synthesis migration.
- **`research_kb`** (`:371-378`): `embedding_model: openai/text-embedding-3-small`,
  `summarization_model: ~google/gemini-pro-latest`. Both OpenRouter-only.
- **`title_gen`** (`:380-385`): legacy block; the functional title path now goes through `llm_call`/cheap.
- Config file: `~/.juggle/config.json`, `_deep_merge(DEFAULTS, user)`; override path `_JUGGLE_CONFIG_PATH`.

## juggle:init today

`commands/init.md` (handler is the markdown command itself — there is no `init` subcommand in `juggle_cli.py`;
it shells out via embedded python heredocs against `juggle_settings.DEFAULTS`/`_deep_merge`).

What it does:
1. Detects existing `~/.juggle/.env` and `config.json` (idempotent guards).
2. Checks Docker (for Hindsight).
3. AskUserQuestion: Hindsight yes/no.
4. **Q2 already prompts for the OpenRouter API key**, validates it via `curl .../v1/models`, and stores it in
   `~/.juggle/.env` as `OPENROUTER_KEY` (chmod 600). Q3 picks the model → `HINDSIGHT_LLM_MODEL`.
5. Writes/merges `config.json`, starts Hindsight via docker compose, health-checks, adds shell alias.

**Gap in init:** Q2 is framed as effectively required ("Hindsight uses OpenRouter…", validation retries 3×
then "gives up with a clear error"). It does **not** communicate that the key is OPTIONAL and that Juggle falls
back to `claude -p`. There is no skip path — a user with no OpenRouter account is steered to a dead end even
though core Juggle works without it.

Where the change slots in cleanly: **Q2**. Add a third option "Skip — use claude -p fallback (no research KB
semantic search)" that bypasses key entry, writes `.env` without `OPENROUTER_KEY`, and reframes the preamble as
optional. The validation/retry block already exists; just make it skippable.

## Recommended implementation + refactor

### 1. Standardize the env var name
Keep `OPENROUTER_KEY` (already canonical in code). Only fix the doc drift:
- `src/juggle_settings.py:239` — change reasonix comment from `OPENROUTER_API_KEY` to `OPENROUTER_KEY`
  (or make reasonix `env` explicitly pass `OPENROUTER_KEY`). Update `docs/reasonix.toml.example` to match.

### 2. Make `llm_call` the SOLE OpenRouter text-generation entry point
Migrate the *generation* call sites (NOT embeddings — those can't fall back):
- `juggle_cmd_research.py:178-197` `synthesize` → `llm_call(prompt, profile="normal")`. Needs a **new/edited
  profile** with larger `max_tokens` (current hardcoded 200 is fatal for synthesis). Recommend adding a
  `max_tokens` field to `llm_profiles` and reading it in `llm_calls.py:85` (default 200), or a dedicated
  `synthesis` profile with `max_tokens: 2048`, openrouter_model = `summarization_model`, fallback = sonnet.
- `juggle_cmd_search.py:70-91` `haiku_filter` → `llm_call(profile="cheap")` returning JSON. Needs the prompt to
  tolerate claude -p JSON; keep the existing markdown-fence strip. Consider a `json_mode` profile flag.
- After migration, synthesis/filter degrade to claude -p instead of being skipped/exiting.

**Embeddings stay direct** (`get_embedding`, `embed_batch`, `_get_embedding`) — no claude equivalent. These
should DEGRADE gracefully instead of `sys.exit(1)`: when key unset, fall back to FTS keyword search
(`kb.fts_search`, already used by offline `--fts`) and skip ingest with a warning rather than hard-exit.

### 3. Hard-require vs degrade
- **Degrade (claude -p):** all generation (synthesis, filter) — already SAFE once migrated.
- **Degrade (FTS, no semantics):** KB search/offline search → fall back to `fts_search` when key unset.
- **Skip with warning:** `research-ingest` embedding step (can't embed without provider) — but still allow PDF
  text ingest into FTS table. No hard-require anywhere; nothing should `sys.exit(1)` solely on missing key.

### 4. juggle:init change
Edit `commands/init.md` Q2: add "Skip (use claude -p fallback)" option; reframe preamble as
"**Optional.** Without a key, Juggle uses `claude -p` for all generation; only research-KB *semantic* search is
unavailable (keyword/FTS search still works)." Store key in `~/.juggle/.env` as `OPENROUTER_KEY` only when
provided; otherwise omit the line.

### 5. Agent-first verification (per fix)

Graceful-fallback proof (run after env-unset):
```
env -u OPENROUTER_KEY -u OPENROUTER_API_KEY python3 src/juggle_cmd_research.py "test topic" --no-web
# EXPECT: synthesized text via claude -p, exit 0 (currently: exit 1)

env -u OPENROUTER_KEY -u OPENROUTER_API_KEY python3 src/juggle_cmd_search.py "test" --no-web
# EXPECT: warning + (post-fix) FTS KB results, exit 0

env -u OPENROUTER_KEY -u OPENROUTER_API_KEY python3 src/juggle_search_offline.py "test"
# EXPECT (post-fix): auto-falls-back to FTS, exit 0 (currently: exit 1 unless --fts)
```

Deterministic unit tests (monkeypatch `OPENROUTER_KEY=""`, patch `subprocess.run`/`run_claude_p`):
- `test_research_synthesis_falls_back_to_claude` — patch urlopen→raise, assert claude -p text returned.
- `test_search_kb_falls_back_to_fts_when_key_unset` — assert `fts_search` called, no exit.
- `test_offline_search_auto_fts_when_key_unset` — assert exit 0, mode=fts.
- `test_llm_profile_respects_max_tokens` — assert synthesis profile passes >200 max_tokens.
- Pattern already exists: `tests/test_cheap_llm_call.py` (`patch.dict OPENROUTER_KEY=""`),
  `tests/test_llm_dispatch.py::test_llm_call_openrouter_failure_falls_back_to_claude`.

## Verification commands (existing baseline)

```
cd /Users/mikechen/github/juggle && python -m pytest tests/test_llm_dispatch.py tests/test_cheap_llm_call.py -q
```
These prove the `llm_call` core fallback already works. New tests above extend coverage to the GAP sites.

## Proposed task breakdown (TDD)

1. **Doc/env standardization.** Fix `OPENROUTER_API_KEY` comment at `juggle_settings.py:239` +
   `docs/reasonix.toml.example` → `OPENROUTER_KEY`. Verify: `grep -rn OPENROUTER_API_KEY src/ docs/` returns
   only intentional references (ideally none).
2. **Profile max_tokens + JSON support in `llm_call`.** Add `max_tokens` (default 200) read from profile in
   `llm_calls.py:85`; add `synthesis` profile (max_tokens 2048, fallback sonnet). Verify:
   `pytest tests/test_llm_dispatch.py -q` + new `test_llm_profile_respects_max_tokens`.
3. **Migrate research synthesis + Haiku filter onto `llm_call`.** Replace direct httpx generation calls in
   `juggle_cmd_research.py` synthesize and `juggle_cmd_search.py` haiku_filter; remove their `sys.exit(1)`
   on missing key (generation now always degrades). Verify:
   `env -u OPENROUTER_KEY python3 src/juggle_cmd_research.py "x" --no-web` exits 0 + new fallback test.
4. **Graceful embeddings degradation → FTS.** When key unset, `juggle_cmd_search.py`/`juggle_search_offline.py`
   auto-route to `kb.fts_search` instead of warning-skip/exit; `research-ingest` skips embed step with warning
   (still ingests PDF text to FTS). Verify: `env -u OPENROUTER_KEY ... search-offline` exits 0, mode=fts +
   new tests.
5. **init.md optional-key UX.** Add Q2 "Skip — claude -p fallback" option, reframe preamble as optional, omit
   `OPENROUTER_KEY` line when skipped. Verify: manual `/juggle:init` dry-run reads "optional" + skip path
   writes `.env` without the key line.

## Test coverage gaps

Covered today:
- `llm_call` core: profile selection, unknown-profile raise, OpenRouter-failure → claude fallback, None on all
  failures (`test_llm_dispatch.py`, `test_cheap_llm_call.py`).
- `_cheap_llm_call` shim delegation; project classify/infer mock `llm_call` (`test_projects.py`).

NOT covered (the actual gaps):
- No test asserts `research`, `search`, `research-ingest`, or `search-offline` behave gracefully with
  `OPENROUTER_KEY=""` — these GAP paths are entirely untested for the key-unset case.
- No test for synthesis/Haiku-filter fallback (they don't route through `llm_call` yet, so they can't).
- No test that embeddings degrade to FTS rather than crashing.
- No test guarding env-var name consistency (`OPENROUTER_API_KEY` could re-creep in).
