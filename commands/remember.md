---
description: Retain a memory in Hindsight and optionally catalog to vault (files/PDFs auto-captured)
allowed-tools: Bash, Read, Write, Edit, mcp__personal-mcp__extract_text_from_file, Skill
---

# /juggle:remember — Memory Retain + Vault Capture

Store something in Juggle's long-term memory. If the input is a file path or clearly factual/reference content, also capture it to the vault.

**Usage:**
```
/juggle:remember <text to remember>
/juggle:remember <path/to/file.pdf>
/juggle:remember <path/to/image.png>
```

If no arguments provided, ask: "What should I remember?"

---

## Step 1 — Detect input type

Check `$ARGUMENTS`:

- **File path** — argument is a path ending in `.pdf`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, or resolves to an existing file on disk → **FILE MODE**
- **Factual/reference text** — contains specific numbers, dates, names, account info, decisions, or knowledge worth persisting beyond a session → **VAULT + HINDSIGHT MODE**
- **Ephemeral preference/context** — conversational, preference, or session context with no lasting reference value → **HINDSIGHT ONLY MODE**

---

## FILE MODE

### Step 1 — Extract content
- Image (jpg/png/gif/webp): call `mcp__personal-mcp__extract_text_from_file` with the file path to OCR.
- PDF: call `mcp__personal-mcp__extract_text_from_file` with the file path to extract text.

### Step 2 — Classify and route to vault
Use the document type table to determine vault destination:

| Document type | Vault path |
|---|---|
| Bank/brokerage statement | `personal/finance/statements/YYYY/` |
| Receipt / invoice | `personal/finance/receipts/YYYY/` |
| Tax document (1099, W2, K-1, etc.) | `personal/finance/tax/YYYY/` |
| Insurance document | `personal/finance/` or `personal/events/` |
| Work meeting notes | `meetings/YYYY/Q[N]/transcripts/` |
| Health / fitness | `personal/fitness/` |
| Trip / travel | `personal/trips/YYYY/MM/<trip-name>/` |
| Reference / how-to | `knowledge/<tech|finance|health|career|misc>/` |
| Stock idea | `knowledge/finance/stock-ideas/` |
| Scanned handwritten note | `personal/handwritten/` |

Vault base: `/Users/mikechen/Documents/personal/`

### Step 3 — Write to vault
```bash
# Copy original file to res/ subdir
cp "<source_path>" "<vault_destination>/res/<filename>"
```
Write a companion `.md` note at `<vault_destination>/YYYY-MM-DD-<slug>.md`:
```markdown
---
date: YYYY-MM-DD
source: <original filename>
type: <document type>
---
# <Title>

<extracted text / key facts as structured markdown>
```

### Step 4 — Log to inbox
```bash
# Append to /Users/mikechen/Documents/personal/inbox.md
echo "- $(date '+%Y-%m-%d %H:%M') — Filed [[<relative-vault-path>|<short description>]]" >> /Users/mikechen/Documents/personal/inbox.md
```

### Step 5 — Retain to Hindsight
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py retain <current_thread_id> "<concise summary of key facts: type, amounts, dates, names, account numbers>" --context preferences
```

Confirm: `Remembered + filed: "<summary>"`

---

## VAULT + HINDSIGHT MODE

For factual text worth persisting as a reference note:

### Step 1 — Determine knowledge subdir

| Topic signals | Subdir |
|---|---|
| stock idea, ticker, buy/sell/watch | `knowledge/finance/stock-ideas/` |
| investing, tax, HSA, 401k, banking, insurance | `knowledge/finance/` |
| ML, code, Docker, infra, CLI, APIs, tools | `knowledge/tech/` |
| fitness, nutrition, health, medical | `knowledge/health/` |
| career, promotion, interviews, leadership | `knowledge/career/` |
| security, threat, CTF | `knowledge/security/` |
| travel, countries, visas | `knowledge/travel/` |
| startup idea, product idea | `knowledge/misc/` |
| anything else | `knowledge/misc/` |

### Step 2 — Find or create note
1. Search the target subdir for an existing note closely related to the content.
2. If found → append as a new section or bullet.
3. If not found → create `<subdir>/YYYY-MM-DD-<slug>.md`:
   ```markdown
   ---
   tags: [knowledge/<subdir-name>]
   created: YYYY-MM-DD
   ---
   # <Title>

   <content>
   ```

### Step 3 — Retain to Hindsight
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py retain <current_thread_id> "<ARGUMENTS>" --context preferences
```

Confirm: `Remembered + filed: "<thing>"`

---

## HINDSIGHT ONLY MODE

For conversational preferences, session context, or anything without lasting reference value:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py retain <current_thread_id> "<ARGUMENTS>" --context preferences
```

Confirm: `Remembered: "<thing>"`
