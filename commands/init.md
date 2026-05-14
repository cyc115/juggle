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
**Skip if `~/.juggle/config.json` already exists** — tell the user their existing config was preserved:

```bash
if [ ! -f "$HOME/.juggle/config.json" ]; then
  PLUGIN_SRC="${CLAUDE_PLUGIN_ROOT}/src" CONFIG_OUT="$HOME/.juggle/config.json" HINDSIGHT_ENABLED=0 \
  python3 - << 'PYEOF'
import os, json, sys, copy
sys.path.insert(0, os.environ["PLUGIN_SRC"])
from juggle_settings import DEFAULTS
cfg = copy.deepcopy(DEFAULTS)
cfg["hindsight"]["enabled"] = os.environ.get("HINDSIGHT_ENABLED") == "1"
with open(os.environ["CONFIG_OUT"], "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF
  python3 -c "import json; json.load(open('$HOME/.juggle/config.json'))" && echo "config.json OK"
else
  echo "config.json already exists — skipping (preserved existing)"
fi
```

**Q2: OpenRouter API Key**

**Skip if `~/.juggle/.env` already exists** — tell the user "`.env` already exists, skipping key setup." and proceed to Q3 (model selection is still skipped if `.env` exists).

If `.env` does not exist, explain to the user:
> Hindsight uses OpenRouter to run a small LLM for memory summarization. Usage is minimal (~$0.40/month on the recommended model). Your key will be stored only in `~/.juggle/.env` (chmod 600) — never in config files.
>
> Get a key at https://openrouter.ai/keys (free account, no credit card required to start).

Ask the user to paste their OpenRouter API key. Then validate:
```bash
curl -sf -H "Authorization: Bearer <key>" https://openrouter.ai/api/v1/models | head -c 100
```

If validation fails, tell the user and ask them to re-enter the key. Retry up to 3 times before giving up with a clear error.

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
if [ ! -f "$HOME/.juggle/.env" ]; then
  cat > ~/.juggle/.env << 'ENVEOF'
OPENROUTER_KEY=<user's key>
HINDSIGHT_LLM_MODEL=<user's model choice>
ENVEOF
  chmod 600 ~/.juggle/.env
  echo ".env written"
else
  echo ".env already exists — skipping (preserved existing)"
fi

# Write config.json only if it doesn't already exist
if [ ! -f "$HOME/.juggle/config.json" ]; then
  PLUGIN_SRC="${CLAUDE_PLUGIN_ROOT}/src" CONFIG_OUT="$HOME/.juggle/config.json" HINDSIGHT_ENABLED=1 \
  python3 - << 'PYEOF'
import os, json, sys, copy
sys.path.insert(0, os.environ["PLUGIN_SRC"])
from juggle_settings import DEFAULTS
cfg = copy.deepcopy(DEFAULTS)
cfg["hindsight"]["enabled"] = os.environ.get("HINDSIGHT_ENABLED") == "1"
with open(os.environ["CONFIG_OUT"], "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF
  python3 -c "import json; json.load(open('$HOME/.juggle/config.json'))" && echo "config.json OK"
else
  echo "config.json already exists — skipping (preserved existing)"
fi

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

### 6. Report

```
Juggle initialized.
- Hindsight memory: enabled (localhost:18888)
- LLM: <model>
- Data: ~/.juggle/memory/pg0
- UI: http://localhost:19999
```
