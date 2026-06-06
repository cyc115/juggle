"""
juggle_scheduler.py — Cross-platform scheduler backend abstraction.

get_backend() returns the appropriate SchedulerBackend for the current platform:
  Darwin  → LaunchdBackend
  Linux + systemd user → SystemdUserBackend
  else    → CronBackend
  none    → RuntimeError

All subprocess calls (systemctl/launchctl/crontab) are done here; callers never
touch platform-specific commands directly.
"""
from __future__ import annotations

import glob
import os
import platform
import plistlib
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ScheduleSpec:
    label: str                              # short kebab-case, e.g. "trading-edge-news-ingest"
    program: str                            # absolute path (or "python3 /path/to/script.py")
    interval_secs: int | None = None        # every-N-seconds
    calendar: dict | None = None            # {"hour": H, "minute": M} or + "weekday": "Sun"
    env: dict[str, str] | None = None       # extra env vars


@dataclass
class ScheduledTaskInfo:
    label: str
    schedule: str       # human-readable, e.g. "every 15m" or "daily 03:00"
    status: str         # "running" | "ok" | "failed" | "unknown"
    pid: int | None
    log_path: str | None


# ── ABC ───────────────────────────────────────────────────────────────────────

class SchedulerBackend(ABC):
    @abstractmethod
    def install(self, spec: ScheduleSpec) -> None: ...

    @abstractmethod
    def uninstall(self, label: str) -> None: ...

    @abstractmethod
    def list_tasks(self) -> list[ScheduledTaskInfo]: ...

    @abstractmethod
    def get_log_path(self, label: str) -> str: ...


# ── Platform detection ────────────────────────────────────────────────────────

def _systemd_user_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    r = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        capture_output=True, text=True,
    )
    return r.returncode in (0, 1)  # 0=running, 1=degraded — both usable


def get_backend() -> SchedulerBackend:
    if platform.system() == "Darwin":
        return LaunchdBackend()
    if shutil.which("systemctl") and _systemd_user_available():
        return SystemdUserBackend()
    if shutil.which("crontab"):
        return CronBackend()
    raise RuntimeError("No supported scheduler backend found (no launchd, systemd, or crontab)")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _parse_schedule_from_plist(data: dict) -> str:
    """Parse a human-readable schedule string from a launchd plist dict."""
    if "StartInterval" in data:
        secs = int(data["StartInterval"])
        if secs < 60:
            return f"every {secs}s"
        if secs < 3600:
            return f"every {secs // 60}m"
        return f"every {secs // 3600}h"
    if "StartCalendarInterval" in data:
        entry = data["StartCalendarInterval"]
        if isinstance(entry, list):
            entry = entry[0]
        h = entry.get("Hour")
        m = entry.get("Minute", 0)
        if h is not None:
            return f"daily {h:02d}:{m:02d}"
    if "WatchPaths" in data:
        return "on-change"
    return "on-demand"


def _spec_to_on_calendar(spec: ScheduleSpec) -> str:
    """Map a ScheduleSpec to a systemd OnCalendar= expression."""
    if spec.interval_secs is not None:
        secs = spec.interval_secs
        if secs >= 3600 and secs % 3600 == 0:
            hours = secs // 3600
            if hours == 1:
                return "hourly"
            return f"*:0/{hours * 60}"  # fallback for multi-hour intervals
        minutes = secs // 60
        if minutes == 60:
            return "hourly"
        return f"*:0/{minutes}"
    if spec.calendar is not None:
        cal = spec.calendar
        h = cal.get("hour", 0)
        m = cal.get("minute", 0)
        weekday = cal.get("weekday")
        time_str = f"{h:02d}:{m:02d}:00"
        if weekday:
            return f"{weekday} *-*-* {time_str}"
        return f"*-*-* {time_str}"
    return "daily"


def _spec_to_cron_expr(spec: ScheduleSpec) -> str:
    """Map a ScheduleSpec to a cron expression (minute hour dom month dow)."""
    if spec.interval_secs is not None:
        minutes = spec.interval_secs // 60
        if minutes == 0:
            return "* * * * *"
        return f"*/{minutes} * * * *"
    if spec.calendar is not None:
        cal = spec.calendar
        h = cal.get("hour", 0)
        m = cal.get("minute", 0)
        weekday = cal.get("weekday")
        dow_map = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}
        if weekday and weekday in dow_map:
            return f"{m} {h} * * {dow_map[weekday]}"
        return f"{m} {h} * * *"
    return "0 0 * * *"


# ── LaunchdBackend (macOS) ────────────────────────────────────────────────────

class LaunchdBackend(SchedulerBackend):
    LABEL_PREFIX = "me.mikechen."

    def __init__(self, agents_dir: Path | None = None) -> None:
        self._agents_dir = agents_dir or (Path.home() / "Library" / "LaunchAgents")

    def install(self, spec: ScheduleSpec) -> None:
        import plistlib as _pl
        label = f"{self.LABEL_PREFIX}{spec.label}"
        plist_path = self._agents_dir / f"{label}.plist"
        env = dict(spec.env or {})
        env.setdefault("HOME", str(Path.home()))

        if spec.interval_secs is not None:
            schedule: dict = {"StartInterval": spec.interval_secs}
        elif spec.calendar:
            cal = spec.calendar
            schedule = {"StartCalendarInterval": {"Hour": cal["hour"], "Minute": cal.get("minute", 0)}}
        else:
            schedule = {}

        plist_data: dict = {
            "Label": label,
            "ProgramArguments": spec.program.split(),
            "StandardOutPath": self.get_log_path(spec.label),
            "StandardErrorPath": self.get_log_path(spec.label),
            "RunAtLoad": False,
            "EnvironmentVariables": env,
            **schedule,
        }
        self._agents_dir.mkdir(parents=True, exist_ok=True)
        with open(plist_path, "wb") as f:
            _pl.dump(plist_data, f)

        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)

    def uninstall(self, label: str) -> None:
        full_label = f"{self.LABEL_PREFIX}{label}"
        plist_path = self._agents_dir / f"{full_label}.plist"
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink(missing_ok=True)

    def list_tasks(self) -> list[ScheduledTaskInfo]:
        tasks: list[ScheduledTaskInfo] = []
        seen: set[str] = set()
        for pattern in ("me.mikechen.*.plist", "com.claude.schedule.*.plist"):
            for path in sorted(glob.glob(str(self._agents_dir / pattern))):
                try:
                    with open(path, "rb") as f:
                        data = plistlib.load(f)
                    full_label = data.get("Label", Path(path).stem)
                    if full_label in seen:
                        continue
                    seen.add(full_label)
                    schedule = _parse_schedule_from_plist(data)
                    pid, last_exit = self._launchctl_status(full_label)
                    if pid is not None:
                        status = "running"
                    elif last_exit is None:
                        status = "unknown"
                    elif last_exit == 0:
                        status = "ok"
                    else:
                        status = "failed"
                    short = full_label.removeprefix("me.mikechen.").removeprefix(
                        "com.claude.schedule."
                    )
                    tasks.append(ScheduledTaskInfo(
                        label=short,
                        schedule=schedule,
                        status=status,
                        pid=pid,
                        log_path=self.get_log_path(short),
                    ))
                except Exception:
                    continue
        return tasks

    def get_log_path(self, label: str) -> str:
        logs_dir = Path.home() / "Library" / "Logs"
        return str(logs_dir / f"{self.LABEL_PREFIX}{label}.log")

    def _launchctl_status(self, label: str) -> tuple[int | None, int | None]:
        try:
            r = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True, timeout=2,
            )
            pid: int | None = None
            exit_status: int | None = None
            for line in r.stdout.splitlines():
                line = line.strip()
                if '"PID"' in line:
                    try:
                        pid = int(line.split("=")[1].strip().rstrip(";"))
                    except (ValueError, IndexError):
                        pass
                if '"LastExitStatus"' in line:
                    try:
                        exit_status = int(line.split("=")[1].strip().rstrip(";"))
                    except (ValueError, IndexError):
                        pass
            return pid, exit_status
        except Exception:
            return None, None


# ── SystemdUserBackend (Linux primary) ────────────────────────────────────────

_SERVICE_TEMPLATE = """\
[Unit]
Description=Juggle scheduled task: {label}
After=network.target

[Service]
Type=oneshot
ExecStart={program}
{env_block}
StandardOutput=append:{log_path}
StandardError=append:{log_path}
SyslogIdentifier=juggle-{label}

[Install]
WantedBy=default.target
"""

_TIMER_TEMPLATE = """\
[Unit]
Description=Juggle timer: {label}

[Timer]
OnCalendar={on_calendar}
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
"""


class SystemdUserBackend(SchedulerBackend):
    LABEL_PREFIX = "juggle-"
    _DEFAULT_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
    _DEFAULT_LOG_DIR = Path.home() / ".juggle" / "logs"

    def __init__(self, unit_dir: Path | None = None, log_dir: Path | None = None) -> None:
        self._unit_dir = unit_dir or self._DEFAULT_UNIT_DIR
        self._log_dir = log_dir or self._DEFAULT_LOG_DIR

    def install(self, spec: ScheduleSpec) -> None:
        self._unit_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        unit_name = f"{self.LABEL_PREFIX}{spec.label}"
        svc_path = self._unit_dir / f"{unit_name}.service"
        tmr_path = self._unit_dir / f"{unit_name}.timer"
        log_path = self.get_log_path(spec.label)

        env_lines = [f"Environment={k}={v}" for k, v in (spec.env or {}).items()]
        env_block = "\n".join(env_lines)

        svc_path.write_text(_SERVICE_TEMPLATE.format(
            label=spec.label,
            program=spec.program,
            env_block=env_block,
            log_path=log_path,
        ))
        tmr_path.write_text(_TIMER_TEMPLATE.format(
            label=spec.label,
            on_calendar=_spec_to_on_calendar(spec),
        ))

        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{unit_name}.timer"],
            check=True,
        )

    def uninstall(self, label: str) -> None:
        unit_name = f"{self.LABEL_PREFIX}{label}"
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", f"{unit_name}.timer"],
            capture_output=True,
        )
        for suffix in (".service", ".timer"):
            path = self._unit_dir / f"{unit_name}{suffix}"
            path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    def list_tasks(self) -> list[ScheduledTaskInfo]:
        tasks: list[ScheduledTaskInfo] = []
        for tmr in sorted(self._unit_dir.glob(f"{self.LABEL_PREFIX}*.timer")):
            label = tmr.stem.removeprefix(self.LABEL_PREFIX)
            unit_name = f"{self.LABEL_PREFIX}{label}"
            r = subprocess.run(
                ["systemctl", "--user", "show", f"{unit_name}.timer",
                 "--property=ActiveState,SubState,MainPID"],
                capture_output=True, text=True,
            )
            props = {}
            for line in r.stdout.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    props[k] = v
            active = props.get("ActiveState", "unknown")
            main_pid = props.get("MainPID", "0")
            pid = int(main_pid) if main_pid.isdigit() and int(main_pid) != 0 else None
            if pid:
                status = "running"
            elif active == "active":
                status = "ok"
            else:
                status = active

            schedule = self._read_timer_schedule(tmr)
            tasks.append(ScheduledTaskInfo(
                label=label,
                schedule=schedule,
                status=status,
                pid=pid,
                log_path=self.get_log_path(label),
            ))
        return tasks

    def get_log_path(self, label: str) -> str:
        return str(self._log_dir / f"juggle-{label}.log")

    def _read_timer_schedule(self, tmr_path: Path) -> str:
        try:
            text = tmr_path.read_text()
            for line in text.splitlines():
                if line.startswith("OnCalendar="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
        return "unknown"


# ── CronBackend (fallback) ────────────────────────────────────────────────────

class CronBackend(SchedulerBackend):
    _DEFAULT_LOG_DIR = Path.home() / ".juggle" / "logs"

    def __init__(self, log_dir: Path | None = None) -> None:
        self._log_dir = log_dir or self._DEFAULT_LOG_DIR

    def install(self, spec: ScheduleSpec) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        cron_expr = _spec_to_cron_expr(spec)
        log_path = self.get_log_path(spec.label)
        env_prefix = " ".join(f"{k}={v}" for k, v in (spec.env or {}).items())
        env_part = f"{env_prefix} " if env_prefix else ""
        entry = f"{cron_expr} {env_part}{spec.program} >> {log_path} 2>&1"

        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        lines = existing.stdout.splitlines() if existing.returncode == 0 else []
        # Remove any existing juggle-<label> entry (dedup)
        sentinel = f"# juggle-{spec.label}"
        clean: list[str] = []
        skip_next = False
        for line in lines:
            if line.strip() == sentinel:
                skip_next = True
                continue
            if skip_next:
                skip_next = False
                continue
            clean.append(line)
        clean.append(sentinel)
        clean.append(entry)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".cron", delete=False) as f:
            f.write("\n".join(clean) + "\n")
            tmp = f.name
        try:
            subprocess.run(["crontab", tmp], check=True)
        finally:
            os.unlink(tmp)

    def uninstall(self, label: str) -> None:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if existing.returncode != 0:
            return
        lines = existing.stdout.splitlines()
        sentinel = f"# juggle-{label}"
        clean: list[str] = []
        skip_next = False
        for line in lines:
            if line.strip() == sentinel:
                skip_next = True
                continue
            if skip_next:
                skip_next = False
                continue
            clean.append(line)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".cron", delete=False) as f:
            f.write("\n".join(clean) + "\n")
            tmp = f.name
        try:
            subprocess.run(["crontab", tmp], check=True)
        finally:
            os.unlink(tmp)

    def list_tasks(self) -> list[ScheduledTaskInfo]:
        tasks: list[ScheduledTaskInfo] = []
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if r.returncode != 0:
            return tasks
        lines = r.stdout.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("# juggle-") and i + 1 < len(lines):
                label = line.removeprefix("# juggle-").strip()
                entry_line = lines[i + 1]
                log_path = self.get_log_path(label)
                tasks.append(ScheduledTaskInfo(
                    label=label,
                    schedule=self._parse_cron_schedule(entry_line),
                    status=self._check_log_status(log_path),
                    pid=None,
                    log_path=log_path,
                ))
        return tasks

    def get_log_path(self, label: str) -> str:
        return str(self._log_dir / f"{label}.log")

    def _parse_cron_schedule(self, line: str) -> str:
        parts = line.split()
        if len(parts) >= 5:
            expr = " ".join(parts[:5])
            return f"cron: {expr}"
        return "cron"

    def _check_log_status(self, log_path: str) -> str:
        p = Path(log_path)
        if not p.exists():
            return "unknown"
        return "ok"
