I found the drift. The summarization model in the default config has changed.

**Line to fix in the doc:**

```json
"summarization_model": "google/gemini-3.1-flash",
```

Should be:

```json
"summarization_model": "~google/gemini-pro-latest",
```

(Source: `src/juggle_settings.py:216`)

Also, the final sentence is incomplete — it ends with "already i" instead of a complete thought. Since the code confirms OpenRouter routing is correct, I'll complete it naturally.

Here are the rewrites:

**In "Config block" section:**
Change `"summarization_model": "google/gemini-3.1-flash",` to `"summarization_model": "~google/gemini-pro-latest",`

**Last sentence:**
Change `All API calls (embeddings + summarization) route through `OPENROUTER_KEY` already i` to `All API calls (embeddings + summarization) route through the OpenRouter API via `OPENROUTER_KEY`.`