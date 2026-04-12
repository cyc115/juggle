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

If No → write `~/.juggle/config.json` with `{"hindsight": {"enabled": false}}` and stop.

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

# Write config.json
cat > ~/.juggle/config.json << 'CFGEOF'
{
  "hindsight": {
    "enabled": true,
    "api_url": "http://localhost:18888",
    "api_key": "juggle",
    "bank": "juggle",
    "llm_model": "<user's model choice>"
  }
}
CFGEOF

# Start service
docker compose --env-file ~/.juggle/.env -f ${CLAUDE_PLUGIN_ROOT}/docker/docker-compose.yml up -d

# Health check (retry 3x)
for i in 1 2 3; do
  sleep 2
  curl -sf http://localhost:18888/health && echo " Hindsight healthy!" && break
  [ "$i" = "3" ] && echo "Warning: health check failed after 3 attempts"
done
```

### 4. Report

```
Juggle initialized.
- Hindsight memory: enabled (localhost:18888)
- LLM: <model>
- Data: ~/.juggle/memory/pg0
- UI: http://localhost:19999
```
