---
description: Bootstrap Juggle plugin — set up Hindsight memory service and plugin configuration
allowed-tools: Read, Bash, Write, Edit, AskUserQuestion
---

# /juggle:init — Bootstrap Plugin

Interactive setup for Juggle plugin configuration.

## Steps

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

If No → run the config-write snippet below with `HINDSIGHT_ENABLED=0`, then stop:

```bash
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
```

**Q2: OpenRouter API Key**
Ask user to paste their OpenRouter API key.
Validate with:
```bash
curl -sf -H "Authorization: Bearer <key>" https://openrouter.ai/api/v1/models | head -c 100
```

**Q3: LLM Model**
Options:
- "(Recommended) moonshotai/kimi-k2.5 (~$0.40/month)"
- "Custom — I'll provide a model ID"

### 3. Bootstrap

```bash
# Create directories
mkdir -p ~/.juggle/memory/pg0 ~/.juggle/logs

# Write .env
cat > ~/.juggle/.env << 'ENVEOF'
OPENROUTER_KEY=<user's key>
HINDSIGHT_LLM_MODEL=<user's model choice>
ENVEOF
chmod 600 ~/.juggle/.env

# Write config.json from DEFAULTS in juggle_settings.py
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

### 5. Report

```
Juggle initialized.
- Hindsight memory: enabled (localhost:18888)
- LLM: <model>
- Data: ~/.juggle/memory/pg0
- UI: http://localhost:19999
```
