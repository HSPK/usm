from __future__ import annotations

import json
import os
import plistlib
import sys
from pathlib import Path

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


def test_supervisor_is_not_a_cli_command():
    assert "supervise" not in tunnel.cli.commands
