# Linux Scheduling Support for `/juggle:schedule`

**Date:** 2026-06-05  
**Status:** Design — ready for implementation  
**Author:** researcher agent (mike:code-research)

---

## 1. Current macOS Mechanism (file:line)

The scheduling system has **two separate layers**, both macOS-only:

### Layer 1 — Registration (`/juggle:schedule` command)

`commands/schedule.md` is a Claude command that, when invoked, writes a **launchd plist** to:
```
~/Library/LaunchAgents/me.mikechen.<label>.plist
```

Then loads it:
```bash
launchctl unload ~/Library/LaunchAgents/me.mikechen.<label>.plist 2>/dev/null || true
launchctl load   ~/Library/LaunchAgents/me.mikechen.<label>.plist
launchctl list   me.mikechen.<label>
```

The plist uses either `StartInterval` (every-N-seconds) or `StartCalendarInterval` (daily at HH:MM). The `ProgramArguments` array contains the path to the script/binary to run.

### Layer 2 — Cockpit monitoring (`juggle_cockpit_model.py`)

`fetch_scheduled_tasks()` at `src/juggle_cockpit_model.py:125`:
- Glob-scans `~/Library/LaunchAgents/me.mikechen.*.plist` and `com.claude.schedule.*.plist`
- Reads each plist via `plistlib.load()` (`juggle_cockpit_model.py:134`)
- Calls `_launchctl_status()` at `juggle_cockpit_model.py:97` which runs:
  ```python
  subprocess.run(["launchctl", "list", label], ...)
  ```
  and parses `PID` / `LastExitStatus` from the output
- `_parse_schedule()` at `juggle_cockpit_model.py:72–94` parses `StartInterval` / `StartCalendarInterval` keys

### The three built-in routines

`juggle_schedule_autofix.py`, `juggle_schedule_dogfood.py`, `juggle_schedule_reflect.py` are **platform-agnostic** Python. They are exposed as CLI subcommands via `juggle_cli.py:766–848`:
```python
p_dogfood = subparsers.add_parser("schedule-dogfood", ...)
p_autofix = subparsers.add_parser("schedule-autofix", ...)
p_reflect  = subparsers.add_parser("schedule-reflect", ...)
```
These are triggered externally (by launchd, cron, or systemd) via their CLI entry points. The design spec (`docs/superpowers/specs/2026-05-18-schedule-routines-design.md`) intended them to run via **Claude Code Routines** (cloud), with launchd as fallback.

### Schedule state storage

`src/juggle_schedule_common.py:19`:
```python
STATE_FILE = Path.home() / ".juggle" / "schedule_state.json"
```
Tracks `last_success` timestamp per routine name. **Not stored in the DB.**  
Registered task identities live in the plist files themselves — there is no schedule table.

### No platform abstraction

There is **zero platform abstraction** today. macOS is hardcoded at:
- `commands/schedule.md` — plist template, `launchctl` calls
- `juggle_cockpit_model.py:97–159` — `launchctl`, `plistlib`, `~/Library/LaunchAgents`

The Python routine modules (`juggle_schedule_*.py`) and `juggle_schedule_common.py` are fully cross-platform.

---

## 2. What a Scheduled Task Does When It Fires

When launchd fires a task it exec's `ProgramArguments[0]` directly (no shell). For arbitrary tasks registered via `/juggle:schedule` this is a user script path. For the three built-in routines, the intended invocation would be:

```bash
/path/to/python3 /path/to/juggle_cli.py schedule-dogfood   # Sat 03:00
/path/to/python3 /path/to/juggle_cli.py schedule-autofix   # Sun 03:00
/path/to/python3 /path/to/juggle_cli.py schedule-reflect   # Mon 03:00
```

Each routine calls `claude -p` headlessly (no tmux required), writes reports to `reports/`, and (for autofix) opens a GitHub PR. The routines are designed to run **without any active user session** — `schedule_dogfood.py:291` explicitly checks `_check_active_session()` and defers if Juggle is in use.

**Does the mac version run tasks when the user is logged out / machine asleep?**  
LaunchAgents (user-context) fire when the user is **logged in**. They are suspended during machine sleep and do not run when logged out. This is the semantic to match on Linux.

---

## 3. Proposed Linux Implementation

### 3.1 Evaluation

| Mechanism | Fires when logged out? | Missed-run catch-up | Log access | Env vars | Verdict |
|---|---|---|---|---|---|
| **systemd user timer** | Yes (with `loginctl enable-linger`) | Yes (`Persistent=true`) | `journalctl --user` | Full `.service` env block | **Primary** |
| **cron** | Yes (system cron) | No | Syslog or redirect | Limited (no PATH/env) | **Fallback** |
| **juggle internal daemon** | Only while daemon runs | No | None | N/A | Non-starter |

### 3.2 Recommendation: systemd user timers (primary) + cron (fallback)

**Primary — systemd user timers:**
- Semantically equivalent to launchd LaunchAgents: per-user, persistent, survive logout with linger enabled
- `Persistent=true` gives missed-run catch-up (fires immediately on next login if a scheduled run was missed during sleep/shutdown)
- Logs accessible via `journalctl --user -u juggle-<label>` — surface this path to the user on install
- `loginctl enable-linger <user>` required once at install time for tasks to fire without an active login session

**Fallback — cron:**
- Universal — works on any Linux without systemd (Alpine, old Debian, WSL without systemd)
- No catch-up for missed runs; env must be explicitly set in crontab
- Harder to list/remove programmatically (requires parsing `crontab -l` output)
- Use only when systemd is unavailable

**Why not juggle-internal daemon:**  
The routines are designed to run when Juggle may not be open. A daemon that only fires while juggle runs breaks the `schedule_dogfood.py` design invariant (schedule fires at 03:00, not "whenever juggle is next launched").

---

## 4. Backend Abstraction Design

### 4.1 New module: `src/juggle_scheduler.py`

```python
import platform, subprocess, shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

@dataclass
class ScheduleSpec:
    label: str          # short kebab-case name, e.g. "trading-edge-news-ingest"
    program: str        # absolute path to script/binary
    interval_secs: int | None = None        # every-N-seconds
    calendar: dict | None = None            # {"hour": H, "minute": M} for daily
    env: dict[str, str] | None = None       # extra env vars

@dataclass
class ScheduledTaskInfo:
    label: str
    schedule: str       # human-readable, e.g. "every 15m" or "daily 03:00"
    status: str         # "running" | "ok" | "failed" | "unknown"
    pid: int | None
    log_path: str | None

class SchedulerBackend(ABC):
    @abstractmethod
    def install(self, spec: ScheduleSpec) -> None: ...
    @abstractmethod
    def uninstall(self, label: str) -> None: ...
    @abstractmethod
    def list_tasks(self) -> list[ScheduledTaskInfo]: ...
    @abstractmethod
    def get_log_path(self, label: str) -> str: ...

def get_backend() -> SchedulerBackend:
    if platform.system() == "Darwin":
        return LaunchdBackend()
    if shutil.which("systemctl") and _systemd_user_available():
        return SystemdUserBackend()
    if shutil.which("crontab"):
        return CronBackend()
    raise RuntimeError("No supported scheduler backend found")

def _systemd_user_available() -> bool:
    r = subprocess.run(["systemctl", "--user", "is-system-running"],
                       capture_output=True, text=True)
    return r.returncode in (0, 1)  # 0=running, 1=degraded — both usable
```

### 4.2 LaunchdBackend (existing macOS, refactored)

Extract the existing logic from `juggle_cockpit_model.py:97–159` and `commands/schedule.md` into `LaunchdBackend`:

```python
class LaunchdBackend(SchedulerBackend):
    AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
    LABEL_PREFIX = "me.mikechen."

    def install(self, spec: ScheduleSpec) -> None:
        # existing plist write + launchctl load logic from commands/schedule.md
        ...

    def list_tasks(self) -> list[ScheduledTaskInfo]:
        # existing fetch_scheduled_tasks() logic from juggle_cockpit_model.py:125
        ...

    def get_log_path(self, label: str) -> str:
        return str(Path.home() / "Library" / "Logs" / f"{self.LABEL_PREFIX}{label}.log")
```

### 4.3 SystemdUserBackend (new Linux primary)

```python
class SystemdUserBackend(SchedulerBackend):
    UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
    LABEL_PREFIX = "juggle-"

    def install(self, spec: ScheduleSpec) -> None:
        self.UNIT_DIR.mkdir(parents=True, exist_ok=True)
        svc_path = self.UNIT_DIR / f"{self.LABEL_PREFIX}{spec.label}.service"
        tmr_path = self.UNIT_DIR / f"{self.LABEL_PREFIX}{spec.label}.timer"

        env_block = "\n".join(f"Environment={k}={v}"
                               for k, v in (spec.env or {}).items())
        svc_path.write_text(_SERVICE_TEMPLATE.format(
            label=spec.label,
            program=spec.program,
            env_block=env_block,
        ))
        tmr_path.write_text(_TIMER_TEMPLATE.format(
            label=spec.label,
            on_calendar=_spec_to_on_calendar(spec),
        ))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now",
                        f"{self.LABEL_PREFIX}{spec.label}.timer"], check=True)

    def uninstall(self, label: str) -> None:
        full = f"{self.LABEL_PREFIX}{label}"
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{full}.timer"],
                       check=False)
        for suffix in (".service", ".timer"):
            (self.UNIT_DIR / f"{full}{suffix}").unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

    def list_tasks(self) -> list[ScheduledTaskInfo]:
        tasks = []
        for tmr in self.UNIT_DIR.glob(f"{self.LABEL_PREFIX}*.timer"):
            label = tmr.stem.removeprefix(self.LABEL_PREFIX)
            r = subprocess.run(
                ["systemctl", "--user", "show", tmr.stem,
                 "--property=ActiveState,SubState,MainPID"],
                capture_output=True, text=True)
            props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
            state = props.get("ActiveState", "unknown")
            pid = int(props["MainPID"]) if props.get("MainPID", "0") != "0" else None
            status = "running" if pid else ("ok" if state == "active" else state)
            tasks.append(ScheduledTaskInfo(
                label=label, schedule=_read_timer_schedule(tmr),
                status=status, pid=pid, log_path=self.get_log_path(label),
            ))
        return tasks

    def get_log_path(self, label: str) -> str:
        # journald — no static file; surface the command instead
        return f"journalctl --user -u {self.LABEL_PREFIX}{label}"
```

### 4.4 Unit file templates

**.service template:**
```ini
[Unit]
Description=Juggle scheduled task: {label}
After=network.target

[Service]
Type=oneshot
ExecStart={program}
{env_block}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=juggle-{label}

[Install]
WantedBy=default.target
```

**.timer template:**
```ini
[Unit]
Description=Juggle timer: {label}

[Timer]
OnCalendar={on_calendar}
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
```

**`OnCalendar` mapping:**
| Input | systemd `OnCalendar=` |
|---|---|
| `StartInterval=900` (15m) | `*:0/15` |
| `StartInterval=3600` (1h) | `hourly` |
| `StartCalendarInterval Hour=3 Minute=0` | `*-*-* 03:00:00` |
| Cron `0 3 * * 0` (Sun 03:00) | `Sun *-*-* 03:00:00` |

### 4.5 CronBackend (fallback)

```python
class CronBackend(SchedulerBackend):
    def install(self, spec: ScheduleSpec) -> None:
        cron_expr = _spec_to_cron(spec)
        env_prefix = " ".join(f"{k}={v}" for k, v in (spec.env or {}).items())
        entry = f"{cron_expr} {env_prefix} {spec.program} >> {self.get_log_path(spec.label)} 2>&1"
        # Read existing, append, write back
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        lines = existing.stdout.splitlines() if existing.returncode == 0 else []
        lines = [l for l in lines if f"juggle-{spec.label}" not in l]  # dedup
        lines.append(f"# juggle-{spec.label}")
        lines.append(entry)
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cron", delete=False) as f:
            f.write("\n".join(lines) + "\n")
            tmp = f.name
        subprocess.run(["crontab", tmp], check=True)
        os.unlink(tmp)

    def get_log_path(self, label: str) -> str:
        return str(Path.home() / ".juggle" / "logs" / f"{label}.log")
```

### 4.6 Cockpit integration

Replace `juggle_cockpit_model.py:fetch_scheduled_tasks()` (line 125) with:

```python
def fetch_scheduled_tasks() -> list[ScheduledTask]:
    from juggle_scheduler import get_backend
    try:
        backend = get_backend()
        infos = backend.list_tasks()
    except Exception:
        return []
    return [ScheduledTask(label=i.label, schedule=i.schedule,
                          status=i.status, pid=i.pid) for i in infos]
```

Remove the `plistlib`, `launchctl`, and `~/Library/LaunchAgents` references from `juggle_cockpit_model.py`.

### 4.7 Command update (`commands/schedule.md`)

Replace the macOS-only plist section with backend-aware prose, or generate the correct commands based on `platform.system()`. The command invokes `juggle_scheduler.install(spec)` rather than writing a plist directly.

---

## 5. Install-Time Concerns (Linux)

| Concern | Action |
|---|---|
| systemd availability | `get_backend()` checks `shutil.which("systemctl")` + `_systemd_user_available()` before selecting SystemdUserBackend |
| `loginctl enable-linger` | Required once so units fire without an active login session. Must prompt user: `sudo loginctl enable-linger $USER`. Cannot be done automatically without sudo. |
| WSL without systemd | WSL1 has no systemd. WSL2 with `systemd=true` in `.wslconfig` works. Fall through to CronBackend otherwise. |
| journald log path | Surface `journalctl --user -u juggle-<label> -f` to the user on install instead of a static file path. |
| Uninstall/cleanup | `uninstall(label)` disables + removes unit files + daemon-reload. Document this — launchd cleanup was also manual. |

---

## 6. Edge Cases and Critical Issues

### 6.1 "No systemd" on Linux
Containers, Alpine, Amazon Linux 1, WSL1: no systemd. `get_backend()` must fall through to `CronBackend` cleanly. Detection: `shutil.which("systemctl")` returns None, OR `systemctl --user is-system-running` exits non-zero (not just degraded).

### 6.2 WSL-specific
- WSL2 with systemd enabled: SystemdUserBackend works correctly
- WSL1 / WSL2 without systemd: fall through to cron
- Linger is not needed in WSL (always a "login" session from Windows perspective)

### 6.3 Missed runs and `Persistent=true`
`Persistent=true` in the `.timer` causes systemd to fire the timer immediately on next startup if a run was missed. This matches launchd's `StartCalendarInterval` behavior (launchd also catches up on missed calendar-based runs at next boot). **Cron has no equivalent** — missed cron runs are silently dropped.

### 6.4 Linger — the critical semantic difference
Without `loginctl enable-linger <user>`, systemd user units only run while the user has an active login session. This is *worse* than launchd (which fires any LaunchAgent while logged in). **The install must prompt for linger** or the routines will only fire while the user is actively logged in. This is the most important Linux-specific install step.

### 6.5 Environment in systemd units
systemd does not inherit the login shell environment. The `PATH`, `HOME`, `CLAUDE_PLUGIN_DATA`, and other env vars must be explicitly set in the `.service` `Environment=` block (or `EnvironmentFile=`). Current launchd plist already handles this with an explicit `EnvironmentVariables` dict — copy this pattern.

### 6.6 Uninstall/cleanup
No equivalent to `launchctl unload` + delete plist. Must run `systemctl --user disable --now <unit>` + remove files + `daemon-reload`. Document clearly. Partial uninstall (file deleted but unit still loaded) leaves a dangling unit — `daemon-reload` resolves this.

### 6.7 Cockpit "log_path" for systemd
The cockpit currently shows a log file path. With journald there is no static file. Options: (a) show the `journalctl` command string, (b) redirect stdout/stderr to a log file via `StandardOutput=append:/path/to/file` in the `.service`. Option (b) matches launchd `StandardOutPath` semantics exactly and is simpler for the cockpit.

**Recommendation:** Use `StandardOutput=append:~/.juggle/logs/juggle-<label>.log` in the service template. This gives a static file path the cockpit can display, while also flowing through journald.

---

## 7. Subtask Breakdown for Implementation Agent

### Task 1 — Extract scheduler abstraction (no behavior change)
- Create `src/juggle_scheduler.py` with `SchedulerBackend`, `ScheduleSpec`, `ScheduledTaskInfo`
- Implement `LaunchdBackend` by migrating existing logic from `juggle_cockpit_model.py:97–159`
- Update `juggle_cockpit_model.py:fetch_scheduled_tasks()` to call `LaunchdBackend().list_tasks()`
- All tests must pass; no behavior change on macOS

### Task 2 — SystemdUserBackend
- Implement `SystemdUserBackend` with `install()`, `uninstall()`, `list_tasks()`
- Unit templates: `.service` with `StandardOutput=append:~/.juggle/logs/juggle-<label>.log`, `.timer` with `Persistent=true`
- `_spec_to_on_calendar()` mapper (interval → `*:0/N`, calendar → `*-*-* HH:MM:SS`, weekday cron → `Mon *-*-* HH:MM:SS`)
- `get_backend()` platform detection

### Task 3 — CronBackend
- Implement `CronBackend` with `install()`, `uninstall()`, `list_tasks()`
- Parse `crontab -l` to list/dedup/remove entries
- Log to `~/.juggle/logs/<label>.log` via shell redirect in cron entry

### Task 4 — `commands/schedule.md` update
- Add Linux section documenting the systemd timer units created
- Document linger requirement and how to check it (`loginctl show-user $USER | grep Linger`)
- Keep macOS section unchanged

### Task 5 — Install helper
- `juggle_cli.py install-schedule-backend` subcommand: checks platform, prints what will be used, checks linger status, prompts user to run `sudo loginctl enable-linger $USER` if needed
- Run this as part of `juggle_cli.py doctor` on Linux

### Task 6 — Tests
- TDD: unit tests for `_spec_to_on_calendar()`, `CronBackend.install()` (mock crontab), `SystemdUserBackend.install()` (mock subprocess)
- `get_backend()` platform selection test (mock `platform.system()`, `shutil.which()`)
- Existing cockpit model tests must pass unchanged (LaunchdBackend wraps existing behavior)

---

## 8. Summary

| Layer | macOS today | Linux primary | Linux fallback |
|---|---|---|---|
| Register task | `launchctl load` + plist | `systemctl --user enable` + `.timer`/`.service` | `crontab -e` entry |
| Fire trigger | launchd daemon | systemd (user instance) | crond |
| List tasks | glob `~/Library/LaunchAgents/*.plist` | glob `~/.config/systemd/user/juggle-*.timer` | parse `crontab -l` |
| Task status | `launchctl list <label>` | `systemctl --user show <unit>` | log file age heuristic |
| Logs | `~/Library/Logs/<label>.log` | `~/.juggle/logs/<label>.log` (append redirect) | `~/.juggle/logs/<label>.log` |
| Run without login | Yes (LaunchAgent) | Yes (with `loginctl enable-linger`) | Yes (system cron) |
| Missed-run catch-up | Yes (StartCalendarInterval) | Yes (`Persistent=true`) | No |
| Platform abstraction seam | `juggle_cockpit_model.py:125` + `commands/schedule.md` | `juggle_scheduler.get_backend()` | same |

**Routine scripts are already cross-platform** — only the registration/monitoring layer needs work. The abstraction is thin (~300 lines) and the LaunchdBackend is a zero-behavior-change refactor of existing code.
