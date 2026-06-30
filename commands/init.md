---
description: Bootstrap Juggle plugin — set up Hindsight memory service and plugin configuration
allowed-tools: Read, Bash, Write, Edit, AskUserQuestion
---

# /juggle:init — Bootstrap Plugin

Interactive setup for Juggle plugin configuration.

## Steps

### 0. Check for Existing Installation

Before anything else, check what already exists:

```bash
ENV_EXISTS=0; CONFIG_EXISTS=0
[ -f "$HOME/.juggle/.env" ] && ENV_EXISTS=1
[ -f "$HOME/.juggle/config.json" ] && CONFIG_EXISTS=1
echo "env=$ENV_EXISTS config=$CONFIG_EXISTS"
```

- If **both exist**: tell the user "Juggle already configured. Re-running will preserve your `.env` and `config.json` — only missing pieces will be added." Proceed but skip any write steps for files that already exist (see guards in steps 2–3).
- If **neither exists**: fresh install, proceed normally.
- If **partially configured**: note which files exist and which will be created.

### 1. Check Prerequisites

```bash
docker --version
docker compose version
```

If Docker is not available, report:
> "Docker is required for Hindsight memory service. Install Docker Desktop and re-run /juggle:init."

### 2. Ask Configuration Questions

Use AskUserQuestion for each:

**Q1: Enable Hindsight long-term memory?**
Options:
- "(Recommended) Yes — set up persistent memory for Juggle agents"
- "No — skip memory, agents run without recall/retain"

If No → run the config-write snippet below with `HINDSIGHT_ENABLED=0`, then stop.
This **regenerates** `config.json` from the current `DEFAULTS` (picking up any new
keys added since the last init) and **merges the old config's values back on top**,
so nothing the user customized is lost. Safe on both fresh and existing installs:

```bash
PLUGIN_SRC="${CLAUDE_PLUGIN_ROOT}/src" CONFIG_OUT="$HOME/.juggle/config.json" HINDSIGHT_ENABLED=0 \
python3 - << 'PYEOF'
import os, json, sys, copy
sys.path.insert(0, os.environ["PLUGIN_SRC"])
from juggle_settings import DEFAULTS, _deep_merge
from pathlib import Path
config_path = Path(os.environ["CONFIG_OUT"])
existed = config_path.exists()
current = json.loads(config_path.read_text()) if existed else {}
# New config from current DEFAULTS as base; old values merged on top (override wins).
merged = _deep_merge(copy.deepcopy(DEFAULTS), current)
if not existed:
    merged["hindsight"]["enabled"] = os.environ.get("HINDSIGHT_ENABLED") == "1"
config_path.write_text(json.dumps(merged, indent=2))
print("Regenerated config.json (merged old values)" if existed else "Created config.json")
PYEOF
python3 -c "import json; json.load(open('$HOME/.juggle/config.json'))" && echo "config.json OK"
```

**Q2: OpenRouter API Key (OPTIONAL)**

**Skip if `~/.juggle/.env` already exists** — tell the user "`.env` already exists, skipping key setup." and proceed to Q3 (model selection is still skipped if `.env` exists).

If `.env` does not exist, explain to the user:
> **The OpenRouter key is optional — Juggle runs fully without it.** A key just lets Juggle use a cheap/fast hosted LLM for research, search, and memory summarization. Without a key, Juggle **falls back to claude -p** for all generation, and research/search **degrade to keyword (FTS) search** — semantic embeddings (the research-KB semantic lookup) are unavailable, but keyword search still works.
>
> If you do add a key: usage is minimal (~$0.40/month on the recommended model). Your key is stored only in `~/.juggle/.env` (chmod 600) — never in config files. Get one at https://openrouter.ai/keys (free account, no credit card required to start).

Use AskUserQuestion to offer:
- "(Recommended) Add OpenRouter key — cheap/fast hosted LLM + semantic research/search"
- "Skip — use claude -p fallback (no key; research/search degrade to keyword FTS, no semantic embeddings)"

If the user picks **Skip**: do not prompt for a key. Set `OPENROUTER_SKIP=1` and proceed to Q3 — the `OPENROUTER_KEY` line will be omitted from `.env` (see step 3). Tell the user: "Skipping key — Juggle will use claude -p; semantic research-KB search is unavailable, keyword search still works."

If the user picks **Add key**: ask them to paste their OpenRouter API key, then validate:
```bash
curl -sf -H "Authorization: Bearer <key>" https://openrouter.ai/api/v1/models | head -c 100
```

If validation fails, tell the user and ask them to re-enter the key. Retry up to 3 times. If it still fails, do **not** dead-end — tell the user the key looks invalid and offer to **skip and use the claude -p fallback** instead (set `OPENROUTER_SKIP=1` and continue to Q3).

Confirm: "Key validated. Will be stored securely in `~/.juggle/.env`."

**Q3: LLM Model**

**Skip if `~/.juggle/.env` already exists** — model is already configured there.

If `.env` does not exist:
Options:
- "(Recommended) moonshotai/kimi-k2.5 (~$0.40/month)"
- "Custom — I'll provide a model ID"

If Custom: ask the user to paste the OpenRouter model ID (e.g. `anthropic/claude-haiku-4-5`). No validation needed.

### 3. Bootstrap

```bash
# Create directories (always safe — idempotent)
mkdir -p ~/.juggle/memory/pg0 ~/.juggle/logs

# Write .env only if it doesn't already exist
# (KEY=VALUE format — no "export" prefix; juggle_cli.py loads this automatically)
#
# If the user skipped Q2 (OPENROUTER_SKIP=1 / no key), OMIT the OPENROUTER_KEY
# line entirely — Juggle detects the missing key and falls back to claude -p.
# Write .env without the key in that case; otherwise include the validated key.
if [ ! -f "$HOME/.juggle/.env" ]; then
  if [ "${OPENROUTER_SKIP:-0}" = "1" ]; then
    cat > ~/.juggle/.env << 'ENVEOF'
HINDSIGHT_LLM_MODEL=<user's model choice>
ENVEOF
    echo ".env written without OPENROUTER_KEY (claude -p fallback)"
  else
    cat > ~/.juggle/.env << 'ENVEOF'
OPENROUTER_KEY=<user's key>
HINDSIGHT_LLM_MODEL=<user's model choice>
ENVEOF
    echo ".env written"
  fi
  chmod 600 ~/.juggle/.env
else
  echo ".env already exists — skipping (preserved existing)"
fi

# Write config.json. On a rerun we DON'T skip — we backfill any new default
# keys (e.g. tmux.ready_poll_*) into the existing file while preserving every
# value the user already set. Fresh installs get the full DEFAULTS.
PLUGIN_SRC="${CLAUDE_PLUGIN_ROOT}/src" CONFIG_OUT="$HOME/.juggle/config.json" HINDSIGHT_ENABLED=1 \
python3 - << 'PYEOF'
import os, json, sys, copy
sys.path.insert(0, os.environ["PLUGIN_SRC"])
from juggle_settings import DEFAULTS, _deep_merge
from pathlib import Path
config_path = Path(os.environ["CONFIG_OUT"])
existed = config_path.exists()
current = json.loads(config_path.read_text()) if existed else {}
# Backfill missing defaults; user-set values win (override beats base).
merged = _deep_merge(copy.deepcopy(DEFAULTS), current)
if not existed:
    merged["hindsight"]["enabled"] = os.environ.get("HINDSIGHT_ENABLED") == "1"
config_path.write_text(json.dumps(merged, indent=2))
print(("Backfilled new default keys into" if existed else "Created") + " config.json")
PYEOF
python3 -c "import json; json.load(open('$HOME/.juggle/config.json'))" && echo "config.json OK"

# Start service
docker compose --env-file ~/.juggle/.env -f ${CLAUDE_PLUGIN_ROOT}/docker/docker-compose.yml up -d

# Health check (retry 3x)
for i in 1 2 3; do
  sleep 2
  curl -sf http://localhost:18888/health && echo " Hindsight healthy!" && break
  [ "$i" = "3" ] && echo "Warning: health check failed after 3 attempts"
done
```

### 4. Set Up Shell Alias

Detect the user's shell and add `nvim-juggle` alias to the appropriate rc file:

```bash
SHELL_RC=""
case "$SHELL" in
  */zsh)  SHELL_RC="$HOME/.zshrc" ;;
  */bash) SHELL_RC="$HOME/.bashrc" ;;
esac
```

If fish is detected (`~/.config/fish/config.fish` exists), also add a fish alias:
```bash
[ -f "$HOME/.config/fish/config.fish" ] && FISH_CFG="$HOME/.config/fish/config.fish"
```

Check if alias already exists before adding:

```bash
# zsh/bash
if [ -n "$SHELL_RC" ] && ! grep -q "nvim-juggle" "$SHELL_RC" 2>/dev/null; then
  echo "" >> "$SHELL_RC"
  echo "# juggle nvim server" >> "$SHELL_RC"
  echo "alias nvim-juggle='nvim --listen /tmp/juggle-nvim.sock'" >> "$SHELL_RC"
  echo "Added nvim-juggle alias to $SHELL_RC"
fi

# fish
if [ -n "$FISH_CFG" ] && ! grep -q "nvim-juggle" "$FISH_CFG" 2>/dev/null; then
  echo "" >> "$FISH_CFG"
  echo "# juggle nvim server" >> "$FISH_CFG"
  echo "alias nvim-juggle='nvim --listen /tmp/juggle-nvim.sock'" >> "$FISH_CFG"
  echo "Added nvim-juggle alias to $FISH_CFG"
fi
```

Tell the user: `nvim-juggle` starts nvim as a server on `/tmp/juggle-nvim.sock`. Use it instead of `nvim` when you want `juggle:open` to target that session. Run `source <rc-file>` or open a new terminal to activate.

### 5. Initialize Research Knowledge Base

```bash
python3 -c "
import sys, json
sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/src')
from juggle_research_kb import ResearchKB
from juggle_settings import get_settings
from pathlib import Path

s = get_settings()['research_kb']
db_path = str(Path(s['db_path']).expanduser())
kb = ResearchKB(db_path)
kb.init_db()
print(f'Research KB initialized at {db_path}')
"
```

Then update `~/.juggle/config.json` to include the `research_kb` block if missing:

```bash
python3 - << 'PYEOF'
import sys, json, copy
from pathlib import Path

sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/src')
from juggle_settings import DEFAULTS

config_path = Path.home() / ".juggle" / "config.json"
current = json.loads(config_path.read_text()) if config_path.exists() else {}

if "research_kb" not in current:
    current["research_kb"] = copy.deepcopy(DEFAULTS["research_kb"])
    config_path.write_text(json.dumps(current, indent=2))
    print("Added research_kb config block")
else:
    print("research_kb config already present — skipped")
PYEOF
```

Tell the user: "Research KB ready. Run `/juggle:research-ingest` to populate the HN corpus (~5 min, ~$0.50 in embeddings)."

### 5b. Configure DB Mode (optional — idempotent)

Ask the user:
> "Would you like to enable tmpfs in-memory DB mode? This moves the live DB to /dev/shm (RAM) to prevent corruption on copy-on-write filesystems (btrfs/zfs). Effective on Linux only; macOS falls back to direct automatically. Default: direct (no change)."

If the user says yes (and is on Linux):

```bash
python3 - << 'PYEOF'
import sys
sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/src')
from juggle_cmd_db_flush import configure_db_mode
configure_db_mode("tmpfs")
print("db.mode = tmpfs written to config.json")
PYEOF
```

Then install the flush supervisor:

```bash
python3 '${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py' db flush --install-supervisor
```

Tell the user to start the supervisor with the printed command.

If the user declines or is on macOS, skip this step (direct mode is the default).

### 6. Report

```
Juggle initialized.
- Hindsight memory: enabled (localhost:18888)
- LLM: <model>
- Data: ~/.juggle/memory/pg0
- UI: http://localhost:19999
```
