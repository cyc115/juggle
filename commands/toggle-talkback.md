---
description: Toggle the talkback voice server on/off
---

Check if talkback is running:
```bash
curl -sf http://localhost:18787/health && echo "running" || echo "stopped"
```

If running, stop it and disable:
```bash
pkill -f "talkback --listen" 2>/dev/null
python3 -c "
import json; from pathlib import Path
p = Path.home() / '.juggle/config.json'
c = json.loads(p.read_text()) if p.exists() else {}
c.setdefault('talkback', {})['enabled'] = False
p.write_text(json.dumps(c, indent=2))
"
echo "Talkback stopped."
```

If stopped, enable and start:
```bash
python3 -c "
import json; from pathlib import Path
p = Path.home() / '.juggle/config.json'
c = json.loads(p.read_text()) if p.exists() else {}
c.setdefault('talkback', {})['enabled'] = True
p.write_text(json.dumps(c, indent=2))
"
nohup ${CLAUDE_PLUGIN_ROOT}/scripts/talkback --listen 18787 > /tmp/talkback.log 2>&1 & disown
echo "Talkback started."
```
