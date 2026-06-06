"""
tests/test_juggle_scheduler.py — TDD for juggle_scheduler.py cross-platform backends.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ── get_backend() platform detection ─────────────────────────────────────────

def test_get_backend_darwin_returns_launchd():
    from juggle_scheduler import get_backend, LaunchdBackend
    with patch("platform.system", return_value="Darwin"):
        backend = get_backend()
    assert isinstance(backend, LaunchdBackend)


def test_get_backend_linux_with_systemd_returns_systemd():
    from juggle_scheduler import get_backend, SystemdUserBackend
    with (
        patch("platform.system", return_value="Linux"),
        patch("shutil.which", return_value="/usr/bin/systemctl"),
        patch("juggle_scheduler._systemd_user_available", return_value=True),
    ):
        backend = get_backend()
    assert isinstance(backend, SystemdUserBackend)


def test_get_backend_linux_no_systemd_returns_cron():
    from juggle_scheduler import get_backend, CronBackend
    with (
        patch("platform.system", return_value="Linux"),
        patch("shutil.which", side_effect=lambda x: "/usr/bin/crontab" if x == "crontab" else None),
        patch("juggle_scheduler._systemd_user_available", return_value=False),
    ):
        backend = get_backend()
    assert isinstance(backend, CronBackend)


def test_get_backend_no_scheduler_raises():
    from juggle_scheduler import get_backend
    with (
        patch("platform.system", return_value="Linux"),
        patch("shutil.which", return_value=None),
        patch("juggle_scheduler._systemd_user_available", return_value=False),
    ):
        with pytest.raises(RuntimeError, match="No supported scheduler"):
            get_backend()


# ── _spec_to_on_calendar() conversion ────────────────────────────────────────

def test_spec_to_on_calendar_interval_15m():
    from juggle_scheduler import _spec_to_on_calendar, ScheduleSpec
    spec = ScheduleSpec(label="test", program="/bin/test", interval_secs=900)
    assert _spec_to_on_calendar(spec) == "*:0/15"


def test_spec_to_on_calendar_interval_1h():
    from juggle_scheduler import _spec_to_on_calendar, ScheduleSpec
    spec = ScheduleSpec(label="test", program="/bin/test", interval_secs=3600)
    assert _spec_to_on_calendar(spec) == "hourly"


def test_spec_to_on_calendar_interval_30m():
    from juggle_scheduler import _spec_to_on_calendar, ScheduleSpec
    spec = ScheduleSpec(label="test", program="/bin/test", interval_secs=1800)
    assert _spec_to_on_calendar(spec) == "*:0/30"


def test_spec_to_on_calendar_daily():
    from juggle_scheduler import _spec_to_on_calendar, ScheduleSpec
    spec = ScheduleSpec(label="test", program="/bin/test", calendar={"hour": 3, "minute": 0})
    assert _spec_to_on_calendar(spec) == "*-*-* 03:00:00"


def test_spec_to_on_calendar_daily_with_minute():
    from juggle_scheduler import _spec_to_on_calendar, ScheduleSpec
    spec = ScheduleSpec(label="test", program="/bin/test", calendar={"hour": 9, "minute": 30})
    assert _spec_to_on_calendar(spec) == "*-*-* 09:30:00"


def test_spec_to_on_calendar_weekday():
    from juggle_scheduler import _spec_to_on_calendar, ScheduleSpec
    spec = ScheduleSpec(label="test", program="/bin/test",
                        calendar={"weekday": "Sun", "hour": 3, "minute": 0})
    assert _spec_to_on_calendar(spec) == "Sun *-*-* 03:00:00"


# ── SystemdUserBackend.install() — unit file content ─────────────────────────

def test_systemd_install_creates_service_file(tmp_path):
    from juggle_scheduler import SystemdUserBackend, ScheduleSpec
    backend = SystemdUserBackend(unit_dir=tmp_path)
    spec = ScheduleSpec(label="mytest", program="/usr/bin/python3 /path/to/script.py",
                        interval_secs=900)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        backend.install(spec)

    svc = (tmp_path / "juggle-mytest.service").read_text()
    assert "ExecStart=/usr/bin/python3 /path/to/script.py" in svc
    assert "Type=oneshot" in svc
    assert "juggle-mytest" in svc


def test_systemd_install_creates_timer_file(tmp_path):
    from juggle_scheduler import SystemdUserBackend, ScheduleSpec
    backend = SystemdUserBackend(unit_dir=tmp_path)
    spec = ScheduleSpec(label="mytest", program="/usr/bin/script",
                        interval_secs=900)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        backend.install(spec)

    tmr = (tmp_path / "juggle-mytest.timer").read_text()
    assert "OnCalendar=*:0/15" in tmr
    assert "Persistent=true" in tmr


def test_systemd_install_timer_daily(tmp_path):
    from juggle_scheduler import SystemdUserBackend, ScheduleSpec
    backend = SystemdUserBackend(unit_dir=tmp_path)
    spec = ScheduleSpec(label="reflect", program="/usr/bin/juggle",
                        calendar={"hour": 3, "minute": 0})
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        backend.install(spec)

    tmr = (tmp_path / "juggle-reflect.timer").read_text()
    assert "OnCalendar=*-*-* 03:00:00" in tmr


def test_systemd_install_calls_systemctl(tmp_path):
    from juggle_scheduler import SystemdUserBackend, ScheduleSpec
    backend = SystemdUserBackend(unit_dir=tmp_path)
    spec = ScheduleSpec(label="mytest", program="/bin/test", interval_secs=900)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        backend.install(spec)

    calls = [str(c) for c in mock_run.call_args_list]
    assert any("daemon-reload" in c for c in calls)
    assert any("enable" in c for c in calls)


def test_systemd_install_env_block(tmp_path):
    from juggle_scheduler import SystemdUserBackend, ScheduleSpec
    backend = SystemdUserBackend(unit_dir=tmp_path)
    spec = ScheduleSpec(label="mytest", program="/bin/test", interval_secs=60,
                        env={"FOO": "bar", "HOME": "/home/user"})
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        backend.install(spec)

    svc = (tmp_path / "juggle-mytest.service").read_text()
    assert "Environment=FOO=bar" in svc
    assert "Environment=HOME=/home/user" in svc


def test_systemd_log_path_in_service(tmp_path):
    from juggle_scheduler import SystemdUserBackend, ScheduleSpec
    backend = SystemdUserBackend(unit_dir=tmp_path)
    spec = ScheduleSpec(label="mytest", program="/bin/test", interval_secs=60)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        backend.install(spec)

    svc = (tmp_path / "juggle-mytest.service").read_text()
    # Log must be a static file path (not journalctl command) so cockpit can display it
    assert "StandardOutput=append:" in svc
    assert "juggle-mytest.log" in svc


# ── SystemdUserBackend.uninstall() ────────────────────────────────────────────

def test_systemd_uninstall_removes_files(tmp_path):
    from juggle_scheduler import SystemdUserBackend
    svc = tmp_path / "juggle-mytest.service"
    tmr = tmp_path / "juggle-mytest.timer"
    svc.write_text("[Unit]\n")
    tmr.write_text("[Unit]\n")
    backend = SystemdUserBackend(unit_dir=tmp_path)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        backend.uninstall("mytest")
    assert not svc.exists()
    assert not tmr.exists()


def test_systemd_uninstall_calls_systemctl(tmp_path):
    from juggle_scheduler import SystemdUserBackend
    backend = SystemdUserBackend(unit_dir=tmp_path)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        backend.uninstall("mytest")
    calls = [str(c) for c in mock_run.call_args_list]
    assert any("disable" in c for c in calls)
    assert any("daemon-reload" in c for c in calls)


# ── SystemdUserBackend.list_tasks() ──────────────────────────────────────────

def test_systemd_list_tasks_returns_installed(tmp_path):
    from juggle_scheduler import SystemdUserBackend
    (tmp_path / "juggle-test1.timer").write_text(
        "[Timer]\nOnCalendar=*:0/15\nPersistent=true\n"
    )
    backend = SystemdUserBackend(unit_dir=tmp_path)

    show_output = "ActiveState=active\nSubState=waiting\nMainPID=0\n"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=show_output)
        tasks = backend.list_tasks()

    assert len(tasks) == 1
    assert tasks[0].label == "test1"


def test_systemd_list_tasks_empty_dir(tmp_path):
    from juggle_scheduler import SystemdUserBackend
    backend = SystemdUserBackend(unit_dir=tmp_path)
    with patch("subprocess.run"):
        tasks = backend.list_tasks()
    assert tasks == []


# ── CronBackend.install() ─────────────────────────────────────────────────────

def test_cron_install_interval(tmp_path):
    from juggle_scheduler import CronBackend, ScheduleSpec
    backend = CronBackend(log_dir=tmp_path / "logs")
    spec = ScheduleSpec(label="mytest", program="/usr/bin/script", interval_secs=900)

    existing = MagicMock(returncode=0, stdout="# existing entry\n")
    written_cron = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "crontab" and cmd[1] == "-l":
            return existing
        if cmd[0] == "crontab" and len(cmd) == 2:
            written_cron.append(Path(cmd[1]).read_text())
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.install(spec)

    assert len(written_cron) == 1
    cron_text = written_cron[0]
    assert "juggle-mytest" in cron_text
    assert "*/15" in cron_text  # every 15 minutes
    assert "/usr/bin/script" in cron_text


def test_cron_install_daily(tmp_path):
    from juggle_scheduler import CronBackend, ScheduleSpec
    backend = CronBackend(log_dir=tmp_path / "logs")
    spec = ScheduleSpec(label="reflect", program="/usr/bin/juggle",
                        calendar={"hour": 3, "minute": 0})

    written_cron = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "crontab" and cmd[1] == "-l":
            return MagicMock(returncode=0, stdout="")
        if cmd[0] == "crontab" and len(cmd) == 2:
            written_cron.append(Path(cmd[1]).read_text())
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.install(spec)

    cron_text = written_cron[0]
    assert "0 3 * * *" in cron_text
    assert "/usr/bin/juggle" in cron_text


def test_cron_install_deduplicates(tmp_path):
    from juggle_scheduler import CronBackend, ScheduleSpec
    backend = CronBackend(log_dir=tmp_path / "logs")
    spec = ScheduleSpec(label="mytest", program="/usr/bin/script", interval_secs=900)

    existing_cron = "# juggle-mytest\n*/15 * * * * /usr/bin/script\n"
    written_cron = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "crontab" and cmd[1] == "-l":
            return MagicMock(returncode=0, stdout=existing_cron)
        if cmd[0] == "crontab" and len(cmd) == 2:
            written_cron.append(Path(cmd[1]).read_text())
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.install(spec)

    cron_text = written_cron[0]
    # Should appear exactly once (deduped)
    assert cron_text.count("juggle-mytest") == 1


def test_cron_uninstall_removes_entry(tmp_path):
    from juggle_scheduler import CronBackend
    backend = CronBackend(log_dir=tmp_path / "logs")

    existing_cron = "# juggle-mytest\n*/15 * * * * /usr/bin/script >> /tmp/mytest.log 2>&1\n"
    written_cron = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "crontab" and cmd[1] == "-l":
            return MagicMock(returncode=0, stdout=existing_cron)
        if cmd[0] == "crontab" and len(cmd) == 2:
            written_cron.append(Path(cmd[1]).read_text())
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        backend.uninstall("mytest")

    assert len(written_cron) == 1
    assert "juggle-mytest" not in written_cron[0]


# ── LaunchdBackend — cockpit model preserved behavior ─────────────────────────

def test_launchd_backend_list_tasks_empty(tmp_path):
    from juggle_scheduler import LaunchdBackend
    backend = LaunchdBackend(agents_dir=tmp_path)
    tasks = backend.list_tasks()
    assert tasks == []


def test_launchd_backend_get_log_path():
    from juggle_scheduler import LaunchdBackend
    backend = LaunchdBackend()
    path = backend.get_log_path("mytest")
    assert "mytest" in path
    assert path.endswith(".log")


def test_launchd_list_tasks_parses_plist(tmp_path):
    import plistlib
    from juggle_scheduler import LaunchdBackend
    backend = LaunchdBackend(agents_dir=tmp_path)

    plist_data = {
        "Label": "me.mikechen.mytest",
        "ProgramArguments": ["/usr/bin/python3"],
        "StartInterval": 900,
    }
    plist_path = tmp_path / "me.mikechen.mytest.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump(plist_data, f)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout='"PID" = 0;\n"LastExitStatus" = 0;\n'
        )
        tasks = backend.list_tasks()

    assert len(tasks) == 1
    assert tasks[0].label == "mytest"
    assert tasks[0].schedule == "every 15m"


# ── cockpit model integration ─────────────────────────────────────────────────

def test_fetch_scheduled_tasks_uses_backend(tmp_path):
    """fetch_scheduled_tasks() delegates to get_backend().list_tasks()."""
    from juggle_scheduler import ScheduledTaskInfo
    mock_info = ScheduledTaskInfo(
        label="test", schedule="every 15m", status="ok", pid=None, log_path="/tmp/test.log"
    )
    mock_backend = MagicMock()
    mock_backend.list_tasks.return_value = [mock_info]
    with patch("juggle_cockpit_model.get_backend", mock_backend, create=True):
        import importlib
        import juggle_cockpit_model
        with patch("juggle_scheduler.get_backend", return_value=mock_backend):
            tasks = juggle_cockpit_model.fetch_scheduled_tasks()

    assert len(tasks) == 1
    assert tasks[0].label == "test"
    assert tasks[0].schedule == "every 15m"
