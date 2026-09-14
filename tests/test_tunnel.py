from __future__ import annotations

import json
import os
import plistlib
import sys
from pathlib import Path

from click.testing import CliRunner

import tunnel


def _sample_tunnel() -> tunnel.Tunnel:
    return tunnel.Tunnel(
        id="db",
        kind="local",
        bind_addr="127.0.0.1",
        listen_port=15432,
        target_host="db.internal",
        target_port=5432,
        ssh_target="user@bastion",
    )


def test_render_unit_restarts_on_any_ssh_exit(monkeypatch):
    monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)

    unit = tunnel._render_unit(_sample_tunnel(), "/usr/local/bin/usm")

    assert "Restart=always" in unit
    assert "Restart=on-failure" not in unit


def test_render_launchd_plist_keeps_tunnel_alive(monkeypatch):
    monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)

    plist = plistlib.loads(
        tunnel._render_plist(_sample_tunnel(), "/opt/homebrew/bin/usm")
    )

    assert plist["Label"] == "com.github.hspk.usm.tunnel.db"
    assert plist["ProgramArguments"] == ["/opt/homebrew/bin/usm", "tunnel", "up", "db"]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] == 5


def test_enabled_kind_is_platform_specific(tmp_path, monkeypatch):
    launchd_dir = tmp_path / "LaunchAgents"
    systemd_dir = tmp_path / "systemd"
    launchd_dir.mkdir()
    systemd_dir.mkdir()
    monkeypatch.setattr(tunnel, "LAUNCHD_USER_DIR", launchd_dir)
    monkeypatch.setattr(tunnel, "SYSTEMD_USER_DIR", systemd_dir)
    tunnel._launchd_path("db").write_text("")
    tunnel._unit_path("db").write_text("")

    monkeypatch.setattr(tunnel.sys, "platform", "darwin")
    assert tunnel._enabled_kind("db") == "launchd"

    monkeypatch.setattr(tunnel.sys, "platform", "linux")
    assert tunnel._enabled_kind("db") == "systemd"


def test_tunnel_alive_uses_supervisor_pid(monkeypatch):
    t = _sample_tunnel()
    t.pid = 111
    t.supervisor_pid = 222
    monkeypatch.setattr(tunnel, "_is_enabled", lambda tid: False)
    monkeypatch.setattr(tunnel, "_pid_alive", lambda pid: pid == 222)

    assert t.alive()


def test_windows_pid_probe_is_read_only(monkeypatch):
    calls = []

    monkeypatch.setattr(tunnel, "_is_windows", lambda: True)
    monkeypatch.setattr(
        tunnel, "_windows_pid_alive", lambda pid: calls.append(pid) or True
    )
    monkeypatch.setattr(
        tunnel.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not signal")),
    )

    assert tunnel._pid_alive(222) is True
    assert calls == [222]


def test_windows_pid_probe_checks_exit_code_and_closes_handle(monkeypatch):
    class Kernel32:
        def __init__(self):
            self.exit_code = tunnel.WINDOWS_STILL_ACTIVE
            self.closed = []

        def OpenProcess(self, access, inherit, pid):
            assert access == tunnel.WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION
            assert inherit is False
            return pid

        def GetExitCodeProcess(self, handle, exit_code):
            exit_code._obj.value = self.exit_code
            return True

        def CloseHandle(self, handle):
            self.closed.append(handle)
            return True

    kernel32 = Kernel32()
    monkeypatch.setattr(tunnel, "_win32_kernel32", lambda: kernel32)

    assert tunnel._windows_pid_alive(222) is True
    assert kernel32.closed == [222]

    kernel32.exit_code = 0
    assert tunnel._windows_pid_alive(333) is False
    assert kernel32.closed == [222, 333]


def test_ls_reports_running_on_windows(tmp_path, monkeypatch):
    state_dir = tmp_path / "tunnels"
    monkeypatch.setattr(tunnel, "STATE_DIR", state_dir)
    monkeypatch.setattr(tunnel, "LOG_DIR", state_dir / "logs")
    monkeypatch.setattr(tunnel, "_is_windows", lambda: True)
    monkeypatch.setattr(tunnel, "_is_enabled", lambda _tid: False)
    monkeypatch.setattr(tunnel, "_enabled_kind", lambda _tid: None)
    monkeypatch.setattr(tunnel, "_windows_pid_alive", lambda pid: pid == 222)

    t = _sample_tunnel()
    t.supervisor_pid = 222
    t.started_at = 1
    t.save()

    result = CliRunner().invoke(tunnel.cli, ["ls"])

    assert result.exit_code == 0, result.output
    assert "running" in result.output
    assert "222" in result.output


def test_start_launches_supervisor(tmp_path, monkeypatch):
    state_dir = tmp_path / "tunnels"
    monkeypatch.setattr(tunnel, "STATE_DIR", state_dir)
    monkeypatch.setattr(tunnel, "LOG_DIR", state_dir / "logs")
    monkeypatch.setattr(tunnel.time, "sleep", lambda seconds: None)

    calls = []

    class FakePopen:
        pid = 4321
        returncode = None

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

        def poll(self):
            return None

    monkeypatch.setattr(tunnel.subprocess, "Popen", FakePopen)

    t = _sample_tunnel()
    tunnel._start(t, new=True)

    assert calls[0][0] == [
        sys.executable,
        str(Path(tunnel.__file__).resolve()),
    ]
    assert calls[0][1]["env"][tunnel.SUPERVISE_ENV] == "db"
    if os.name == "posix":
        assert calls[0][1]["start_new_session"] is True

    state = json.loads((state_dir / "db.json").read_text())
    assert state["pid"] is None
    assert state["supervisor_pid"] == 4321


def test_start_hides_windows_supervisor_console(tmp_path, monkeypatch):
    state_dir = tmp_path / "tunnels"
    monkeypatch.setattr(tunnel, "STATE_DIR", state_dir)
    monkeypatch.setattr(tunnel, "LOG_DIR", state_dir / "logs")
    monkeypatch.setattr(tunnel, "_is_windows", lambda: True)
    monkeypatch.setattr(tunnel.time, "sleep", lambda _seconds: None)
    calls = []

    class FakePopen:
        pid = 4321
        returncode = None

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

        def poll(self):
            return None

    monkeypatch.setattr(tunnel.subprocess, "Popen", FakePopen)

    tunnel._start(_sample_tunnel(), new=True)

    assert calls[0][1]["creationflags"] == tunnel.WINDOWS_CREATE_NO_WINDOW


def test_supervisor_hides_windows_ssh_console(tmp_path, monkeypatch):
    state_dir = tmp_path / "tunnels"
    monkeypatch.setattr(tunnel, "STATE_DIR", state_dir)
    monkeypatch.setattr(tunnel, "LOG_DIR", state_dir / "logs")
    monkeypatch.setattr(tunnel, "_is_windows", lambda: True)
    calls = []

    class FakePopen:
        pid = 9876

        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

        def wait(self):
            return 1

        def poll(self):
            return None

    monkeypatch.setattr(tunnel.subprocess, "Popen", FakePopen)
    t = _sample_tunnel()
    t.save()

    assert tunnel._supervise(t.id) == 1
    assert calls[0][1]["creationflags"] == tunnel.WINDOWS_CREATE_NO_WINDOW


def test_windows_stop_terminates_supervisor_and_ssh(monkeypatch):
    t = _sample_tunnel()
    t.pid = 111
    t.supervisor_pid = 222
    alive = {111, 222}
    terminated = []

    monkeypatch.setattr(tunnel, "_is_windows", lambda: True)
    monkeypatch.setattr(tunnel, "_windows_pid_alive", lambda pid: pid in alive)

    def terminate(pid):
        terminated.append(pid)
        alive.remove(pid)
        return True

    monkeypatch.setattr(tunnel, "_windows_terminate_pid", terminate)

    assert tunnel._kill_pid(t) is True
    assert terminated == [222, 111]
    assert alive == set()


def test_supervisor_is_not_a_cli_command():
    assert "supervise" not in tunnel.cli.commands
