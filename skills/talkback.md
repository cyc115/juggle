---
name: talkback
description: Toggle talkback pair-programming voice mode. When active, use POST localhost:18787/speak to voice key points to the user.
triggers:
  - /juggle:toggle-talkback
  - toggle-talkback
  - talkback
---

# Talkback — Voice Pair Programming

## Toggle

Check if talkback server is running:
```bash
curl -sf http://localhost:18787/health && echo "running" || echo "stopped"
```

### To start:
```bash
${CLAUDE_PLUGIN_ROOT}/scripts/talkback --listen 18787 &
```

### To stop:
```bash
curl -sf -X POST http://localhost:18787/speak -H "Content-Type: application/json" -d '{"text": ""}'
pkill -f "talkback --listen"
```

## Auto-speak hook

When talkback is running, a Stop hook (`${CLAUDE_PLUGIN_ROOT}/scripts/talkback-stop-hook`) automatically speaks the `✅ **A:**` context card line after every response. No manual curl needed.

- Toggle **on**: start the server → hook fires automatically
- Toggle **off**: stop the server → hook silently no-ops

## When talkback is active

You are a pair programmer. Be concise but personable. Use voice as **another channel** to communicate key points — the user can already see your text response, so don't repeat everything. Voice should add value:

- Announce what you're about to do: "Alright, I'm going to refactor the auth module"
- Flag important findings: "Heads up — found a race condition in the queue handler"
- Celebrate wins: "Tests are passing, nice"
- Ask clarifying questions when stuck

To speak:
```bash
curl -sf -X POST http://localhost:18787/speak -H "Content-Type: application/json" -d '{"text": "your message here"}'
```

Keep messages under 2 sentences. Natural, conversational tone. Not a narrator — a collaborator.
