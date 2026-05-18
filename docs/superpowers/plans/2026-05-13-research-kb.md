The actual implementation uses `"~google/gemini-pro-latest"` for synthesis (from juggle_settings.py line 216), not Gemini 3.1 Flash as stated in the doc. Rewriting the stale sentence:

**STALE SENTENCE FOUND:**

Doc states:
> **Goal:** Add `/juggle:research [topic]` — a hybrid vector+keyword search over HN articles, PDFs, vault, and Hindsight, **synthesized by Gemini 3.1 Flash via OpenRouter** into a markdown digest with inline links.

**Corrected version:**

> **Goal:** Add `/juggle:research [topic]` — a hybrid vector+keyword search over HN articles, PDFs, vault, and Hindsight, **synthesized by Gemini (via OpenRouter)** into a markdown digest with inline links.

All other doc sections match the code: SQLite + sqlite-vec + FTS5 schema ✓, file map ✓, slash command flow with web search ✓, settings structure ✓.