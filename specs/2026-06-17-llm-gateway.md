# SPEC: LLM Gateway — single seam for all external-LLM usage

Date: 2026-06-17 · Status: Draft · Scope: `/Users/mikechen/github/juggle`
Companion plan: `plan/2026-06-17-llm-gateway.md`

## Problem

External-LLM usage in Juggle is *almost* consolidated but not enforced. The nucleus
`src/llm_calls.py` already owns the one `claude -p` subprocess (`run_claude_p`) and the
OpenRouter→claude text dispatcher (`llm_call`), and the recent merge routed research
synthesis and the search Haiku filter through it. But:

1. **One direct text-generation bypass remains.** `src/schedules/dogfood.py:130-146`
   calls `subprocess.run(["claude","-p",task_prompt,"--model","claude-sonnet-4-6","--output-format","json"])`
   with its own cost-tracker block — duplicating exactly what `run_claude_p(output_format="json", cost_tracker=...)`
   already provides.
2. **Nothing prevents drift.** There is no CI guard. A future call site can re-introduce a
   raw `claude -p` or `openrouter.ai` call and tests stay green. The consolidation is convention-only today.
3. **Model ids are scattered.** ~20 hardcoded model-id literals across `juggle_settings.py`
   DEFAULTS *and* inline in `schedules/*.py`, `juggle_project_summary.py`, `juggle_cmd_projects.py`,
   `schedules/common.py`. Bumping Opus/Sonnet/Haiku means editing many files.
4. **Cost/observability is uneven.** `CostTracker` (`src/schedules/common.py:76-104`) is only
   wired into `run_claude_p` json-mode and the dogfood bypass. The OpenRouter path in `llm_call`
   logs latency but tracks **no cost**. Embeddings track no cost at all.
5. **Capabilities are conflated.** Text generation can degrade claude→FTS is meaningless;
   embeddings (4 direct `openrouter.ai/api/v1/embeddings` sites) have *no* claude fallback and must
   degrade to FTS keyword search instead. Today this distinction lives implicitly across call sites.

### Current-state verdict

**~95% already routes through the gateway for text generation.** Every chat/completions touchpoint
goes through `llm_call`/`run_claude_p` EXCEPT `schedules/dogfood.py:130-146`. All 4 embedding sites
are direct *by necessity* (claude cannot embed). No Anthropic SDK is used anywhere — all Claude access
is `claude -p` subprocess. So this is a **consolidation-completion + enforcement** effort, not a rewrite.

## Goals

- **One public surface** for external LLMs: `complete()` (text), `complete_json()` (JSON text),
  `embed()` (vectors). Callers ask for an **intent** (cheap / normal / synthesis / classify / embed),
  never a model id.
- **Capability-aware routing.** Text intents fall back OpenRouter→`claude -p`→None. The `embed`
  capability has no claude path; it degrades to FTS at the caller (gateway returns a typed
  "embeddings unavailable" signal, never silently routes embed→claude).
- **One model-id + intent map.** A single table (in `juggle_settings.py` DEFAULTS, surfaced via the
  gateway) is the only place to bump model ids. Eliminate inline literals in `schedules/*.py` etc.
- **Robust fallback semantics.** Distinguish *retryable* (timeout, 5xx, 429) from *terminal* (auth 401/403,
  400 bad request) OpenRouter errors. Bounded retries on retryable; short-TTL circuit breaker so a dead
  provider is skipped for the next N seconds instead of re-timing-out every call.
- **Centralized cost + observability.** Every text call (OpenRouter and claude) updates `CostTracker`
  via a single code path and emits the existing structured `logging.info` line. OpenRouter responses
  carry `usage`; wire it in.
- **Enforcement in code, not convention.** A CI guard test fails the build if any module outside the
  gateway references `openrouter.ai/.../chat/completions` or spawns `claude -p`.
- **Behavior-preserving.** No prompt text changes, no new domain logic. Existing tests
  (`test_llm_dispatch.py`, `test_cheap_llm_call.py`, `test_openrouter_fallback.py`, `test_research_cmd.py`)
  keep passing throughout.

## Non-goals

- **No new providers now.** Anthropic-direct API and others are *interface-shaped-for* but not built.
  Don't build LangChain for 2 providers.
- **No prompt construction / domain logic in the gateway.** Preserve the `llm_calls.py` docstring
  contract: callers keep their own wrappers and seams.
- **No streaming, no async text** (current calls are sync; embeddings stay `httpx.AsyncClient` at the
  4 caller sites — the gateway exposes a sync `embed()` that runs the request, callers keep their await shape).
- **No change to KB schema** (`articles_vec FLOAT[1536]`, FTS5 — `juggle_research_kb.py`) or `~/.juggle` config layout.
- **Not standardizing the agent-harness model flag** (`juggle_cli_parsers_agents.py` `default="sonnet"`) —
  that's the sub-agent harness, not external-LLM-from-Juggle.

## Design

### Module shape — RECOMMENDATION: keep ONE module `src/llm_calls.py`, do NOT create an `llm/` package

Rationale:
- The repo is **flat top-level modules** (no `src/__init__.py`; `tests/conftest.py:21` puts `src/` on path
  and imports happen by bare name `from llm_calls import ...`). A package would be the only nested package
  among ~100 sibling modules and would force import-path churn at every call site.
- The gateway is small: provider abstraction (2 providers), an intent map, a fallback policy, a breaker,
  cost wiring. This is ~200 lines, not a subsystem. **Simplest solution first.**
- A package buys nothing here except the *appearance* of architecture. The single-module seam is already
  proven (every test patches `llm_calls.llm_call` / `llm_calls.run_claude_p`).

So: evolve `src/llm_calls.py` in place. Internally organize into clearly-named sections
(provider functions, intent/model map accessor, policy, public API). If it ever grows a 3rd provider or
streaming, *then* split — not before.

### Public API surface (the only entry points callers use)

```
complete(prompt, *, intent="cheap", timeout=10, max_tokens=None) -> str | None
complete_json(prompt, *, intent="cheap", timeout=10, max_tokens=None) -> str | None   # sets response_format + json fallback contract
embed(inputs: list[str], *, timeout=30) -> list[list[float]] | None   # None == embeddings unavailable (caller degrades to FTS)
```

- `llm_call(...)` and `run_claude_p(...)` **remain** as the underlying implementations / back-compat
  shims (re-exported), so the existing ~15 call sites and all tests keep working. The new names are thin
  intent-named facades over `llm_call`. (No big-bang rename; migrate opportunistically.)
- `complete_json` is `llm_call(..., json_mode=True)` with the existing markdown-fence-strip contract left
  to callers (unchanged).
- `embed` is the NEW seam that pulls the 4 duplicated `httpx` embedding requests into one place. It returns
  `None` when `OPENROUTER_KEY` is unset or the request fails — callers already have the FTS fallback
  (`kb.fts_search`, `juggle_cmd_search.py:74`, `juggle_search_offline.py:98`, `juggle_cmd_research.py:287`).

### Intent → model map (single source of truth)

One table in `juggle_settings.py` DEFAULTS, read only by the gateway. Intents map to a profile
(openrouter_model + fallback_model + optional max_tokens). Current latest model ids — **bump here only**:

| Intent | openrouter_model | fallback (claude) | max_tokens | Today's source |
|---|---|---|---|---|
| `cheap` | `deepseek/deepseek-chat-v3-0324:free` | `claude-haiku-4-5-20251001` | 200 | `llm_profiles.cheap` |
| `normal` | `moonshotai/kimi-k2:free` | `claude-sonnet-4-6` | 200 | `llm_profiles.normal` |
| `synthesis` | `google/gemini-2.5-flash` | `claude-sonnet-4-6` | 2048 | `llm_profiles.synthesis` |
| `classify` | (alias of `cheap`) | `claude-haiku-4-5-20251001` | 200 | new alias for project classify/infer |
| `embed` | `openai/text-embedding-3-small` (dim 1536) | — (no claude) | — | `research_kb.embedding_model` |

Latest ids for reference (bump the literals above when models advance):
Opus 4.8 = `claude-opus-4-8`, Sonnet 4.6 = `claude-sonnet-4-6`, Haiku 4.5 = `claude-haiku-4-5-20251001`.

Inline literals to eliminate (route through the map): `juggle_project_summary.py:18` (`model="sonnet"`),
`juggle_cmd_projects.py:465,696`, `schedules/common.py:97,99,181`, `schedules/dogfood.py:130`,
`schedules/reflect.py:79,130,180,250,295,357`, `schedules/autofix.py:198,306,354,428`.

### Fallback chain + error taxonomy

Per text call, ordered chain: **[OpenRouter(intent.openrouter_model)] → [claude -p(intent.fallback_model)] → None**.

OpenRouter error classification (decides retry vs skip-to-next-provider):
- **Retryable:** `urllib`/socket timeout, HTTP 429, HTTP 5xx, connection reset. → bounded retry
  (max 2 attempts, no backoff sleep beyond a tiny jitter; total budget stays under `timeout`).
- **Terminal:** HTTP 401/403 (auth), HTTP 400 (bad request/model). → do **not** retry OpenRouter; go straight
  to claude fallback. Auth-terminal also trips the breaker.
- **Key unset:** treated as "provider absent" — skip OpenRouter entirely (existing `if api_key:` behavior).

### Circuit breaker (short-TTL)

Process-local. On a terminal-auth error or N consecutive retryable failures for OpenRouter, mark the provider
"open" for a short TTL (e.g. 60s). While open, `complete()` skips OpenRouter and goes directly to claude,
avoiding repeated timeouts. State is a module-level dict `{provider: open_until_ts}`; reset on a success.
Deliberately tiny — no external store, no half-open complexity beyond "TTL expired → try again".

### Cost + observability

- `CostTracker` stays where it is (`schedules/common.py`). The gateway gains an optional
  `cost_tracker=` param on the public API (already on `run_claude_p`). Wire OpenRouter `usage`
  (`data["usage"]["prompt_tokens"]/["completion_tokens"]`) into `estimate_from_tokens` + `.add()` —
  closing the gap where the OpenRouter path tracks no cost today.
- Keep the existing structured log line (`llm_call(%s): provider=... model=... elapsed=%dms len=%d preview=%r`)
  and emit one for embeddings too (`provider=openrouter-embed model=... elapsed=%dms n=%d`).
- `estimate_from_tokens` (`common.py:96`) needs an embedding rate branch only if we choose to cost embeddings;
  for v1, log embedding token counts but mark cost as 0 (non-goal to price embeddings precisely).

### Enforcement — CI guard test (the anti-drift mechanism)

`tests/test_no_direct_llm_calls.py`. Pure-stdlib, deterministic, no network. It walks the source tree and
fails on forbidden patterns outside the gateway.

- **Scan set:** every `*.py` under `src/` recursively (flat modules + `src/schedules/`, `src/harnesses/`,
  `src/dbops/`).
- **Forbidden patterns (regex, on file text):**
  - chat/completions HTTP: `openrouter\.ai/api/v1/chat/completions`
  - claude subprocess: a `claude` + `-p` argv list — match `["']claude["']\s*,\s*["']-p["']`
  - (optional) any `urllib.request`/`httpx` POST whose URL string contains `chat/completions`
- **Allowlist (exempt files):** `src/llm_calls.py` only (the gateway). The embeddings endpoint
  (`openrouter.ai/api/v1/embeddings`) is intentionally direct at the 4 caller sites **until** they migrate
  to `embed()`; the guard scopes the *completions/claude* rule separately so embeddings don't trip it.
  After the `embed()` migration (Plan task 4), tighten: allowlist `src/llm_calls.py` for the embeddings
  URL too and remove the 4 callers from any embed allowlist.
- **Implementation note:** model the scanner on the existing
  `tests/test_openrouter_fallback.py:177 test_no_openrouter_api_key_in_code_or_docs`, which already walks
  `src/`+`docs/` for a forbidden string. Reuse that walk; just change the patterns and allowlist. Report
  every offending `file:line` in the assertion message so failures are actionable.
- **Why a test, not a hook/grep:** it runs in the same `uv run pytest` gate everything else uses, fails
  the build deterministically, and needs no settings.json wiring. Code > convention.

## Devil's Advocate

- **Over-abstraction risk.** With exactly 2 providers and no Anthropic SDK, a "provider plugin registry"
  is gold-plating. *Mitigation:* keep providers as two plain functions inside one module; the "abstraction"
  is just an ordered list `[openrouter_fn, claude_fn]` per intent. No base classes, no entry points.
  Reject any design that adds a package or a registry before a 3rd provider actually lands.

- **Embeddings-as-separate-capability.** The tempting symmetry is "everything goes through one router."
  That is wrong: `claude -p` cannot emit a 1536-dim vector, so an `embed→complete` fallback would silently
  return prose where a vector is expected and corrupt KB writes. *Mitigation:* `embed()` is a distinct
  method returning `list[vector] | None`; `None` means "no embeddings provider," and callers degrade to
  **FTS** (which already exists). The breaker/retry policy for embeddings is independent of text.

- **Subprocess-vs-HTTP latency asymmetry in selection.** OpenRouter HTTP is ~hundreds of ms; `claude -p`
  spawns a process (seconds). The fallback ordering (OpenRouter first) is correct for cost+latency *when the
  key is present*, but the breaker is what prevents the pathological case (key present but provider dead →
  every call eats the full HTTP `timeout` *then* spawns claude). *Mitigation:* short-TTL breaker skips a
  known-dead OpenRouter, so latency degrades to "claude only," not "timeout + claude." Keep `timeout` small
  (default 10s) so a single miss is bounded.

- **Single-point-of-failure / god-module.** Routing 100% of LLM traffic through one module means a bug
  there breaks everything. *Mitigations:* (1) the module is a *thin router* — no prompt/domain logic, so the
  surface area for bugs is small and stable; (2) it's the single most-tested file (every existing fault-injection
  test targets it); (3) the breaker/retry is per-provider, so a provider outage degrades rather than fails;
  (4) `complete()` returning `None` is an explicit, tested contract callers already handle. A single seam is a
  *feature* for fault injection (see below), not just a risk.

- **Testing implications.** One seam = trivial fault injection: patch `llm_calls.run_claude_p` and
  `urllib.request.urlopen` to simulate any provider state. The existing tests already do exactly this
  (`test_llm_call_openrouter_failure_falls_back_to_claude`, `test_cheap_llm_call` key-unset). New behaviors
  (retry classification, breaker TTL, embed→None) are all pure-function/monkeypatch testable with no network.
  *Risk:* the breaker introduces process-global state that can leak across tests. *Mitigation:* expose a
  `_reset_breaker()` test hook (or an autouse fixture clearing the module dict), keep TTL logic in a pure
  helper `breaker_is_open(state, provider, now)` for deterministic unit tests.

- **Migration risk to dogfood bypass.** `dogfood.py:130-146` runs in a scheduled headless routine that's
  hard to exercise interactively. *Mitigation:* its replacement (`run_claude_p(..., output_format="json",
  cost_tracker=ct)`) is behavior-identical and unit-testable by patching `subprocess.run`; verify cost
  accumulation matches the old inline block before deleting it.

## Acceptance

- All existing LLM tests pass unchanged.
- `complete/complete_json/embed` exist and are intent-named; no caller passes a raw model id.
- `tests/test_no_direct_llm_calls.py` passes AND fails when a direct `claude -p`/completions call is
  reintroduced anywhere outside the gateway (proven by a temporary injected violation in the task's verify step).
- `dogfood.py` no longer spawns `claude -p` directly.
- Model ids exist in exactly one map; inline literals removed from `schedules/*`, `juggle_project_summary.py`,
  `juggle_cmd_projects.py`.
- OpenRouter text calls update `CostTracker` (cost no longer silently zero on the primary path).
