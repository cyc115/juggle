---
description: Start the Juggle Hindsight memory service
allowed-tools: Bash
---

# /juggle:memory-start

Start the juggle-hindsight Docker service.

```bash
docker compose --env-file ~/.juggle/.env -f ${CLAUDE_PLUGIN_ROOT}/docker/docker-compose.yml up -d
```

Verify:
```bash
curl -sf http://localhost:18888/health && echo "Hindsight running." || echo "Warning: Hindsight not healthy."
```
