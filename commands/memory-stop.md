---
description: Stop the Juggle Hindsight memory service (data persists)
allowed-tools: Bash
---

# /juggle:memory-stop

Stop the juggle-hindsight Docker service. Data persists on volume.

```bash
docker compose --env-file ~/.juggle/.env -f ${CLAUDE_PLUGIN_ROOT}/docker/docker-compose.yml stop
echo "Hindsight stopped. Data preserved at ~/.juggle/memory/"
```
