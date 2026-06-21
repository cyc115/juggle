# PLAN: LLM Gateway — incremental, agent-verifiable

Date: 2026-06-17 · Spec: `specs/2026-06-17-llm-gateway.md`
Repo: `/Users/mikechen/github/juggle` · Runner: `uv run pytest` (testpaths=`tests`)
Import seam: `tests/conftest.py:21` puts `src/` on path; modules imported by bare name.

Ordering principle: low-risk, behavior-preserving extractions FIRST; the bypass-guard test LAST
(after every call site is clean, so it can pass). File overlap is serialized — only one task touches
`schedules/*` model literals, only one touches embeddings.

All verify commands are headless and deterministic (monkeypatch / key-unset / patched subprocess),
no network, explicit exit codes. Each ends with a green pytest or a non-zero failure on regression.

---

## Task 1 — Close the last direct text bypass (`dogfood.py`)

**Why first:** it's the only direct `claude -p` outside the gateway; the CI guard (Task 6) cannot pass
until it's gone. Pure behavior-preserving swap.

**Scope:** `src/schedules/dogfood.py:130-146` only. Replace the inline
`subprocess.run(["claude","-p",...,"--output-format","json"])` + manual cost block with
`run_claude_p(task_prompt, model=<from map or existing literal>, output_format="json", cost_tracker=ct, timeout=...)`.
Keep the same model id and cost accumulation.

**TDD:**
1. Add `tests/test_dogfood_routes_through_gateway.py`: patch `llm_calls.run_claude_p` (or `subprocess.run`),
   call the dogfood research helper with a fake `CostTracker`, assert `run_claude_p` was invoked with
   `output_format="json"` and the cost tracker received the same delta as the old inline path.
2. Implement the swap. No new domain logic.

**Verify (agent, no human):**
```
cd /Users/mikechen/github/juggle && uv run pytest tests/test_dogfood_routes_through_gateway.py -q
grep -rn '"claude"\s*,\s*"-p"' src/schedules/dogfood.py    # EXPECT: no matches (exit 1 from grep == pass)
```

---

## Task 2 — Centralize the intent → model map; delete inline model literals

**Why second:** removes scattered literals so later tasks and the guard reference one place. No call-site
behavior changes (same model ids, just sourced from the map).

**Scope:** `src/juggle_settings.py` (add `classify` alias + ensure `embed` model is discoverable via the map;
keep existing `llm_profiles`+`research_kb`), and replace inline literals with map lookups in:
`juggle_project_summary.py:18`, `juggle_cmd_projects.py:465,696`, `schedules/common.py:97,99,181`,
`schedules/reflect.py:79,130,180,250,295,357`, `schedules/autofix.py:198,306,354,428`.
Add a tiny accessor in `llm_calls.py` (e.g. `model_for(intent)`) reading settings, so non-`llm_call`
callers (project summary, schedules) resolve model ids through one function.

**TDD:**
1. `tests/test_model_map.py`: assert `model_for("normal")=="claude-sonnet-4-6"`,
   `model_for("cheap")=="claude-haiku-4-5-20251001"`, unknown intent raises `ValueError`.
2. Replace literals. Existing tests for projects/schedules must still pass.

**Verify:**
```
cd /Users/mikechen/github/juggle && uv run pytest tests/test_model_map.py tests/test_projects.py -q
# regression net: literals gone from schedules
grep -rnE 'claude-(sonnet|haiku|opus)-' src/schedules/ src/juggle_project_summary.py | grep -v model_for
# EXPECT: no bare literals (only map/accessor refs remain)
```

---

## Task 3 — Add error taxonomy + bounded retry + circuit breaker to `llm_call`

**Why third:** hardens the existing primary path before more callers depend on it. Internal to the gateway;
no caller API change.

**Scope:** `src/llm_calls.py` only. Add pure helpers:
`classify_openrouter_error(exc_or_status) -> "retryable"|"terminal"`,
`breaker_is_open(state, provider, now) -> bool`, and a module-level breaker dict + `_reset_breaker()`.
Wrap the OpenRouter attempt with: retry-once on retryable, skip-to-claude on terminal, open breaker on
auth-terminal / repeated retryable.

**TDD (all monkeypatch, no network):**
1. `tests/test_llm_breaker.py`:
   - `test_classify_timeout_is_retryable`, `test_classify_401_is_terminal` (pure fn).
   - `test_breaker_opens_after_auth_failure_then_skips_openrouter`: patch `urlopen`→401, assert second
     call does NOT hit `urlopen` (breaker open) and goes straight to `run_claude_p`.
   - `test_breaker_expires_after_ttl`: advance a injected `now`, assert OpenRouter retried.
   - `test_retryable_retries_once_then_falls_back`: `urlopen`→timeout twice, assert claude fallback used.
2. Implement. Keep `_reset_breaker()` called by an autouse fixture in the test.

**Verify:**
```
cd /Users/mikechen/github/juggle && uv run pytest tests/test_llm_breaker.py tests/test_llm_dispatch.py -q
```

---

## Task 4 — Introduce `embed()` and migrate the 4 direct embedding sites

**Why fourth:** the embeddings capability is the largest remaining direct surface. Must land before the guard
tightens to cover the embeddings URL. Behavior-preserving (same endpoint/model/dims, same FTS degrade).

**Scope:** add `embed(inputs, *, timeout=30) -> list[list[float]] | None` to `src/llm_calls.py`
(single `httpx`→`openrouter.ai/api/v1/embeddings` request, `research_kb.embedding_model`, returns `None`
when key unset or request fails, emits the embed log line). Replace the 4 duplicated request bodies:
`juggle_cmd_search.py:46-55`, `juggle_cmd_research.py:76-88`, `juggle_research_ingest.py:58-70`,
`juggle_search_offline.py:28-40`. Callers keep their existing `None→FTS` degrade
(`fts_search` at `juggle_cmd_search.py:74`, `juggle_search_offline.py:98`, `juggle_cmd_research.py:287`).

**TDD:**
1. `tests/test_embed_seam.py`:
   - `test_embed_returns_none_when_key_unset` (`OPENROUTER_KEY=""` → `None`, no exception).
   - `test_embed_returns_vectors` (patch `httpx` response → list of 1536-float vectors).
   - `test_search_kb_still_falls_back_to_fts_when_key_unset` (extends existing
     `test_openrouter_fallback.py:159` behavior post-migration).
2. Migrate callers to call `embed()`; delete their inline request code.

**Verify:**
```
cd /Users/mikechen/github/juggle && uv run pytest tests/test_embed_seam.py tests/test_openrouter_fallback.py tests/test_research_cmd.py -q
env -u OPENROUTER_KEY uv run python src/juggle_search_offline.py "test" ; echo "exit=$?"
# EXPECT: exit=0, mode=fts (no crash)
```

---

## Task 5 — Wire OpenRouter cost into CostTracker; add intent-named facades

**Why fifth:** closes the cost-observability gap on the primary path and adds the `complete/complete_json`
public names. Depends on Tasks 2–4 (map + breaker + embed exist).

**Scope:** `src/llm_calls.py`. (a) On a successful OpenRouter completion, read `data["usage"]` and call
`cost_tracker.estimate_from_tokens(prompt_tokens, completion_tokens, model) + .add()` when a `cost_tracker`
is supplied (add optional `cost_tracker=` param to `llm_call`/`complete`). (b) Add thin facades
`complete(prompt,*,intent="cheap",...)` and `complete_json(...)` delegating to `llm_call`. Keep `llm_call`
+ `run_claude_p` as back-compat (re-exported).

**TDD:**
1. `tests/test_cost_and_facade.py`:
   - `test_openrouter_cost_recorded`: patch `urlopen`→response with `usage`, pass a fake tracker, assert
     `.add()` called with the estimated cost (>0).
   - `test_complete_delegates_to_llm_call`: patch `llm_call`, assert `complete(intent="normal")` forwards
     `profile="normal"`.
   - `test_complete_json_sets_json_mode`.
2. Implement.

**Verify:**
```
cd /Users/mikechen/github/juggle && uv run pytest tests/test_cost_and_facade.py tests/test_cheap_llm_call.py -q
```

---

## Task 6 — CI bypass-guard test (LAST)

**Why last:** only passes once Tasks 1 & 4 have removed every out-of-gateway `claude -p` / completions call.

**Scope:** add `tests/test_no_direct_llm_calls.py`. Walk `src/**/*.py` recursively. Fail on, outside the
allowlist:
- `openrouter\.ai/api/v1/chat/completions`
- a `["claude","-p",...]` argv (`["']claude["']\s*,\s*["']-p["']`)
- `openrouter\.ai/api/v1/embeddings` (now that `embed()` exists)

**Allowlist:** `src/llm_calls.py` only. Report every offending `file:line` in the assert message. Model the
tree walk on `tests/test_openrouter_fallback.py:177` (existing `OPENROUTER_API_KEY` scan) — reuse its walk,
swap patterns + allowlist.

**TDD / self-proving the guard actually guards:**
1. Write the test; run it — must be GREEN (all callers already migrated).
2. **Prove it bites:** temporarily inject a `["claude","-p","x"]` line into a throwaway module
   `src/_guard_probe.py`, run the guard — must FAIL and name `src/_guard_probe.py:<line>`. Delete the probe;
   guard goes green again.

**Verify:**
```
cd /Users/mikechen/github/juggle && uv run pytest tests/test_no_direct_llm_calls.py -q          # GREEN
printf 'x=["claude","-p","probe"]\n' > src/_guard_probe.py
uv run pytest tests/test_no_direct_llm_calls.py -q ; test $? -ne 0 && echo "GUARD BITES OK"      # must FAIL
rm src/_guard_probe.py
uv run pytest tests/test_no_direct_llm_calls.py -q                                               # GREEN again
```

---

## Final gate

```
cd /Users/mikechen/github/juggle && uv run pytest -q
```
Whole suite green. No file outside `src/llm_calls.py` references `claude -p` or `openrouter.ai` completions/
embeddings. Model ids live in one map. OpenRouter cost is tracked.

## Dependency / overlap notes

- T1 → T6 (dogfood must be clean before guard).
- T2 touches `schedules/*` + project modules (model literals); no other task touches those, no overlap.
- T3 & T5 both edit `llm_calls.py` but different regions (T3 retry/breaker around the OpenRouter try-block;
  T5 cost-on-success + facades). Run T3 before T5 to avoid conflict; both internal-only.
- T4 touches the 4 embedding caller modules + adds `embed()` to `llm_calls.py`; independent of T2/T3 regions.
- T6 is additive (new test file only).
