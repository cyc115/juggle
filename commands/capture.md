---
description: Capture task, note, file, or knowledge to vault inbox — with OCR, project routing, and Hindsight memory
allowed-tools: Read, Edit, Write, Bash, mcp__personal-mcp__extract_text_from_file, Skill
---

# /juggle:capture — Vault Capture (Task · File · Knowledge)

**Syntax:**
```
/juggle:capture [--file <path>]        # store a file in vault + remember to Hindsight
/juggle:capture [--knowledge] <text>   # write reference note to knowledge/ + remember
/juggle:capture [--project <name>] description [due date]   # task/note (default)
```

**Mode detection (priority order):**
1. `--file <path>` present → FILE mode
2. `--knowledge` flag, or input is clearly factual/reference (not action-oriented) → KNOWLEDGE mode
3. Anything else → TASK mode (default)

---

## Dynamic Vault Resolution

At the start of each mode, resolve VAULT_PATH, VAULT_NAME, and INBOX via:

```bash
VAULT_PATH=$(uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py vault-path)
VAULT_NAME=$(uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py vault-name)
INBOX="${VAULT_PATH}/inbox.md"
```

All paths below use `${VAULT_PATH}`, `${VAULT_NAME}`, and `${INBOX}` — never hardcoded values.

---

## FILE MODE

**Trigger:** `--file <path>` is present.

**Step 1 — Extract content:**
- If the file is an image or PDF, call `mcp__personal-mcp__extract_text_from_file` to OCR it.
- If the tool is unavailable, skip OCR and proceed with the file path only.
- Otherwise, read the file directly.

**Step 2 — Classify document type → vault destination:**

| Document type | Vault destination |
|---|---|
| Bank/brokerage statement | `${VAULT_PATH}/personal/finance/statements/YYYY/` |
| Receipt | `${VAULT_PATH}/personal/finance/receipts/YYYY/` |
| Tax document | `${VAULT_PATH}/personal/finance/tax/YYYY/` |
| Insurance | `${VAULT_PATH}/personal/finance/` or `personal/events/` |
| Work meeting transcript | `${VAULT_PATH}/meetings/YYYY/Q[N]/transcripts/` |
| Work project doc | `${VAULT_PATH}/work/projects/` |
| Health/fitness | `${VAULT_PATH}/personal/fitness/` |
| Trip/travel | `${VAULT_PATH}/personal/trips/YYYY/MM/<trip-name>/` |
| Reference/how-to | `${VAULT_PATH}/knowledge/tech\|finance\|health\|career\|misc/` |
| Scanned handwritten | `${VAULT_PATH}/personal/handwritten/` |

Attachments go in a `res/` subdirectory within the destination.

**Step 3 — Write to vault (MANDATORY: copy original file):**

1. Create the destination `res/` subdirectory:
   `mkdir -p <destination>/res/`
2. **Copy the original file** into `res/`:
   `cp "<source_path>" "<destination>/res/<clean-filename>"`
   Use a clean filename: `YYYY-MM-DD-<slug>.<ext>` (no URL-encoded chars, no spaces).
3. Write a companion `.md` file at `<destination>/<clean-filename>.md` with:
   - Frontmatter: `date`, `source`, `type`
   - Extracted facts/summary
   - **A wikilink to the copied file**: `[[<vault-relative-path-to-res-file>|Source PDF]]`
   The wikilink must use a vault-relative path (e.g. `areas/finance/tax/2024/res/filename.pdf`), not an absolute filesystem path.

**Step 4 — Log to inbox:**
Append to `${INBOX}`:
```
- {ts} — Filed [[<relative-vault-path>|<short description>]]
```

**Step 5 — Remember:**
Invoke `juggle:remember` with a concise summary of the key facts extracted from the file.

---

## KNOWLEDGE MODE

**Trigger:** `--knowledge` flag, or input is clearly factual/reference (not action-oriented).

**Step 1 — Get timestamp** (`date +%Y-%m-%d`).

**Step 2 — Route by topic:**

| Topic keywords | Destination |
|---|---|
| Stock idea, ticker, buy/sell/watch | `${VAULT_PATH}/knowledge/finance/stock-ideas/` |
| Investing, tax, HSA, 401k, banking, insurance | `${VAULT_PATH}/knowledge/finance/` |
| ML, code, Docker, infra, CLI, APIs, tools | `${VAULT_PATH}/knowledge/tech/` |
| Fitness, nutrition, health, medical | `${VAULT_PATH}/knowledge/health/` |
| Career, promotion, interviews, leadership | `${VAULT_PATH}/knowledge/career/` |
| Security, threat, CISSP, CTF | `${VAULT_PATH}/knowledge/security/` |
| Travel, countries, visas | `${VAULT_PATH}/knowledge/travel/` |
| Anything else | `${VAULT_PATH}/knowledge/misc/` |

**Step 3 — Find or create note:**
- **Stock ideas:** Each ticker gets its own file `YYYY-MM-DD-<TICKER>.md` with template:
  ```markdown
  # <TICKER>
  - **Direction:** BUY / SELL / WATCH
  - **Thesis:** ...
  - **Catalysts:** ...
  - **Risks:** ...
  ```
- **All others:** Find an existing related note and append, or create `YYYY-MM-DD-<slug>.md`.

**Step 4 — Remember:**
Invoke `juggle:remember` with the key facts.

---

## TASK MODE (default)

**Trigger:** Anything that is not FILE or KNOWLEDGE mode.

**Step 1 — Parse input:**
- Extract `--project` flag (if present), description text, and optional due date.
- **Classify** as one of:
  - **Action Item** — imperative verb present: `call`, `buy`, `file`, `fix`, `research`, `schedule`, `submit`, `review`, `cancel`, `transfer`, `update`, `send`, `draft`, `build`, `write`; or a clear future obligation.
  - **Note** — status log, observation, numeric logging (weight, spending, etc.).
  - Ambiguous → treat as Action Item.

**Step 2 — Get timestamp** (`date +%Y-%m-%dT%H:%M`).

**Step 3 — Route by keyword:**

| Keywords | Project |
|---|---|
| lifeos, telegram-bot, daemon, hindsight, EC2-agent, claude-agent | LifeOS → `${VAULT_PATH}/projects/lifeos/TODO.md` |
| juggle, orchestrator, juggle-agent, juggle-thread | Juggle project TODO |
| real-estate, property, apartment, lease, mortgage | Real-estate project TODO |
| AI-engineering, pipeline, embedding, model-training | AI engineering project TODO |
| automation, script, launchd, cron, scheduled-task | Automation project TODO |

**D2 rule:** Only route to a project TODO for sustained/ongoing work. One-off tasks or notes without a clear project context → `${INBOX}`.

**Step 4 — For Notes only:** Search the target file semantically to find a parent task to nest under.

**Step 5 — Format and write:**

| Type | Format |
|---|---|
| Action Item | `- [ ] {description} 📅 {due_date} <!-- captured {ts} -->` |
| Note nested under parent | `    - {ts} — {description}` (4-space indent) |
| Note orphan (no parent found) | `- {ts} — {description}` |

Append to the appropriate file (`${INBOX}` or the project `TODO.md`).

After writing, open the file in Obsidian:
```bash
open "obsidian://open?vault=${VAULT_NAME}&file=<relative-path>"
```
