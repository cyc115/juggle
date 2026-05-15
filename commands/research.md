---
description: Search research KB — HN articles, PDFs, vault, memory, and web — via parallel background agents
allowed-tools: Bash
---

# /juggle:research — Research Knowledge Base

Delegates research to a background agent. Asks clarifying intent questions first, then dispatches multi-round parallel web research. Loops back with a report and optionally mid-research for directional guidance.

**Usage:** `/juggle:research <topic> [--no-web] [--verbose] [--deep] [--no-clarify]`

---

## Steps

### 1. Parse arguments from `$ARGUMENTS`

- `TOPIC` — everything except flags (required)
- `NO_WEB` — true if `--no-web` present
- `VERBOSE` — true if `--verbose` present
- `DEEP_FLAG` — `--deep` if `--deep` present, otherwise empty
- `NO_CLARIFY` — true if `--no-clarify` present (skip intent questions, dispatch immediately)

Derive `SLUG` from TOPIC: first 4 words, lowercase, hyphens only.

---

### 2. Clarify research intent (skip if `--no-clarify` or `--no-web`)

Load `AskUserQuestion` via ToolSearch (`select:AskUserQuestion`), then ask **1–2 focused questions** to sharpen the research direction. Do NOT ask more than 2 questions. Keep options concrete and actionable.

**Always ask Q1 — Purpose:**
- `Deep background / learning` — understand the topic thoroughly, cover fundamentals + current state
- `Decision support` — researching to make a specific decision (investment, tool choice, approach)
- `Build something` — looking for tools, libraries, implementations, how-tos
- `Explore ideas` — open-ended discovery, surface surprising angles and connections

**Ask Q2 only if the topic is broad enough to need scoping:**
Phrase it as: "Any specific angles to prioritize?" with 3–4 options derived from the topic itself (e.g. for "trading strategies": "Technical analysis", "Quantitative/systematic", "Fundamental/macro", "AI/ML-driven").

From the answers, derive:
- `INTENT` — one of: background, decision, build, explore
- `FOCUS_AREAS` — comma-separated list of specific angles to prioritize (from Q2, or empty)

Include `INTENT` and `FOCUS_AREAS` as context in the task file (step 4).

---

### 3. Create thread and get agent

```bash
CREATE_OUT=$(python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py create-thread "research-<SLUG>")
THREAD_LABEL=$(echo "$CREATE_OUT" | grep -oP '(?<=Created Topic )\w+')
echo "Thread: $THREAD_LABEL"

AGENT_INFO=$(python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent "$THREAD_LABEL" --role researcher)
AGENT_ID=$(echo "$AGENT_INFO" | awk '{print $1}')
echo "Agent: $AGENT_ID"
```

If `get-agent` exits non-zero or prints "Agent pool full", tell the user and stop.

---

### 4. Write task file and dispatch

Fill in all `<PLACEHOLDERS>` with real values. `VERBOSE_FLAG`, `NO_WEB_FLAG`, `DEEP_FLAG` are the respective flags or empty.

```bash
TASK_FILE="/tmp/juggle_research_$(date +%s%N).txt"
cat > "$TASK_FILE" << 'TASKEOF'
[JUGGLE_THREAD:<THREAD_LABEL>]
Research topic: "<TOPIC>"
Intent: <INTENT>
Focus areas: <FOCUS_AREAS>

## Research process

### Step 1 — Check web search config
source ~/.juggle/.env 2>/dev/null
python3 -c "import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/src'); from juggle_settings import get_settings; print(get_settings()['research_kb'].get('web_search_enabled', True))"

### Step 2 — Multi-round parallel web search (skip if web_search_enabled=False or --no-web)

Run all searches within each round in parallel using mcp__web-search__search-web (depth="deep").

**Round 1 — Broad exploration (run ALL queries in parallel):**
Generate 4–6 queries covering the topic from different angles. Always include:
- The topic as stated: "<TOPIC>"
- One query per focus area (if FOCUS_AREAS non-empty): e.g. "<FOCUS_AREA_1> <TOPIC>"
- One "best tools / frameworks" query
- One "current state / 2025 2026" recency query

After Round 1: scan the result titles and snippets. Identify 2–4 distinct sub-topic clusters
that look most promising given the intent. Note which areas have thin coverage.

**Round 2 — Targeted deep dives (run ALL queries in parallel):**
Generate 3–5 queries drilling into the most promising sub-topics and gaps from Round 1.
For "explore" or "decision" intent, also add:
- One contrarian / "problems with <approach>" query
- One "alternative to <mainstream answer>" query

**Round 3 — Gap fill (only if --deep or significant gaps remain):**
2–3 queries on any sub-topics still underrepresented after Round 2.

After all rounds: deduplicate all results by URL. Keep highest-quality entries per sub-topic.
Write combined results to temp file (title, url, snippet only):
WEB_FILE="/tmp/juggle_web_<SLUG>_$(date +%s).json"
echo '<JSON_ARRAY_OF_{title,url,snippet}>' > "$WEB_FILE"

### Step 3 — Optional directional loopback

After Round 1, if you found a surprising or high-value angle that the user may not have anticipated,
surface it immediately via request-action before proceeding to Round 2:
  python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py request-action <THREAD_LABEL> \
    "Research direction check: found [interesting angle]. Worth exploring? Reply to steer." \
    --type manual_step

Then continue with Round 2 regardless (don't wait — the user can redirect in the next action review).

### Step 4 — KB + vault + memory + web synthesis
source ~/.juggle/.env 2>/dev/null
REPORT=$(python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cmd_research.py "<TOPIC>" <NO_WEB_FLAG> <VERBOSE_FLAG> <DEEP_FLAG> ${WEB_FILE:+--web-results-file "$WEB_FILE"})
rm -f "$WEB_FILE"

### Step 5 — Save report to vault
VAULT_PATH=$(python3 -c "
import sys, os
sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/src')
from juggle_settings import get_settings
paths = get_settings()['domains']['initial_domain_paths']
vault = next((p[0] for p in paths if p[1] == 'vault'), None)
print(os.path.expanduser('~') + vault if vault else '')
" 2>/dev/null)
REPORT_FILE="${VAULT_PATH}/research/$(date +%Y-%m-%d)-<SLUG>.md"
{
  printf "# Research: <TOPIC>\nDate: $(date +%Y-%m-%d)\nIntent: <INTENT>\n\n"
  echo "$REPORT"
} > "$REPORT_FILE"
echo "Saved: $REPORT_FILE"

### Step 6 — Print report and notify orchestrator
echo "$REPORT"
ONE_LINE=$(echo "$REPORT" | head -5 | tr '\n' ' ' | cut -c1-200)
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py request-action <THREAD_LABEL> "Research complete: <TOPIC> — report at $REPORT_FILE" --type manual_step
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <THREAD_LABEL> "Research complete: $REPORT_FILE" --retain "$ONE_LINE"
TASKEOF

python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task "$AGENT_ID" "$TASK_FILE"
```

---

### 5. Confirm dispatch

Tell the user:
> "Researching **<TOPIC>** in background — thread [<THREAD_LABEL>] (intent: <INTENT>, focus: <FOCUS_AREAS or "broad">). Running multi-round parallel web search. Report will be saved to the vault's `research/` directory and I'll loop back when done."
