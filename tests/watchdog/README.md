# Watchdog Test Harness

Tests for the Juggle agent watchdog system. Uses mock tmux panes — no real Claude.

## Prerequisites

- `tmux` installed
- `pytest` available (`uv run pytest` or activated venv)
- No requirement on watchdog daemon running

## How to Run

### Baseline suite (gap documentation)

```bash
cd ~/github/juggle
pytest tests/watchdog/test_baseline.py -v --tb=short
```

**Expected:** `5 passed` — confirms detection gaps exist without watchdog.

### Active suite (gap closed)

```bash
cd ~/github/juggle
pytest tests/watchdog/test_watchdog_active.py -v --tb=short
```

**Expected before watchdog ships:** `1 error` (ImportError on `juggle_watchdog`)  
**Expected after watchdog ships:** `5 passed`

### Full suite

```bash
pytest tests/watchdog/ -v --tb=short
```

## Interpreting Results

| Scenario | baseline | active |
|---|---|---|
| No watchdog | 5/5 PASS | 1 ERROR (import) |
| Watchdog shipped | 5/5 PASS | 5/5 PASS |
| Watchdog partial | 5/5 PASS | N/5 PASS |

Active suite failures after watchdog ships = watchdog implementation is incomplete.

## Fixture Scripts

All scripts in `fixtures/` are standalone bash scripts. Run them manually
in a terminal to observe the mock behavior:

```bash
bash tests/watchdog/fixtures/working.sh          # ctrl-C to stop
bash tests/watchdog/fixtures/crashed.sh          # exits immediately
bash tests/watchdog/fixtures/stalled-silent.sh   # hangs — kill manually
bash tests/watchdog/fixtures/recoverable-prompt.sh <<< "2"   # sends "2" to dismiss
bash tests/watchdog/fixtures/stuck-at-prompt.sh <<< ""       # press enter to unblock
```

## Watchdog API Contract

`src/juggle_watchdog.py` must expose:

```python
def inspect_agent(agent_id: str, db: JuggleDB, tmux_session: str) -> dict:
    """
    Returns:
      {
        'state': 'working' | 'recoverable_prompt' | 'stalled_silent' | 'crashed' | 'stuck_at_prompt',
        'actions': list[str],        # tokens: 'sent_key', 'sent_enter', 'filed_action_item', etc.
        'action_item_id': int | None,
        'notification_id': int | None,
      }
    """
```

Additional requirements:
- Accept `db: JuggleDB` explicitly — never read `CLAUDE_PLUGIN_DATA` internally
- Accept `tmux_session: str` explicitly — never hardcode session name
- Strip ANSI codes: `re.sub(r'\x1b\[[0-9;]*m', '', raw_content)` before pattern matching
- Detect ╭─╮ box via `r'^╭─+╮\s*$'` (width-agnostic)
- Read `JUGGLE_WATCHDOG_STALL_SECS` env var (default: 60) for stall threshold
- For stalled: write snapshot to `~/.juggle/watchdog/snapshots/{agent_id}-{timestamp}.txt`

## Isolation Guarantees

- All DB writes use a per-test `tmp_path` database — real `juggle.db` is never touched
- Tmux panes spawn in `juggle-watchdog-test` session — real `juggle` session untouched
- Session is killed after the test run via session-scoped `ensure_tmux_session` fixture

## State Table

| State | Mock behavior | Baseline assertion | Active assertion |
|---|---|---|---|
| working | `$EPOCHSECONDS working...` every 2s | pane has output | state=working, no action item |
| recoverable_prompt | prints dialog then `read` | no action item, agent=busy | state=recoverable_prompt, sent_key, 1 notif |
| stalled_silent | `Starting analysis...` then sleep 999 | no action item, agent=busy | state=stalled_silent, action_item filed, snapshot written |
| crashed | prints lines then `exit 1` | no action item, agent=busy | state=crashed, thread=failed, action_item filed |
| stuck_at_prompt | prints ╭─╮ box then `read` | no action item, agent=busy | state=stuck_at_prompt, sent_enter, 1 notif |
