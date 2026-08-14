"""Tests for scripts/usm_daemon.py — the plumbing every usm daemon shares.

These moved here wholesale from ``test_usm_daemon.py`` when the generic half
of that module was extracted: liveness, locking, interruptible waits, ignore
patterns and boot integration were never Azure's, and ``usm svc``/``watch``
now depend on them too. The behaviour is unchanged; only the module that
owns it is.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import threading
import time

import pytest

import usm_daemon
from usm_daemon import (
    ExcludeSpec,
    FileLock,
    ServiceManager,
    pid_alive,
    sleep_until,
)


@pytest.fixture
def service_dirs(tmp_path, monkeypatch):
    systemd = tmp_path / "systemd"
    launchd = tmp_path / "launchd"
    systemd.mkdir()
    launchd.mkdir()
    monkeypatch.setattr(usm_daemon, "SYSTEMD_USER_DIR", systemd)
    monkeypatch.setattr(usm_daemon, "LAUNCHD_USER_DIR", launchd)
    return systemd, launchd


@pytest.fixture
def fake_service_calls(monkeypatch):
    calls = {"systemctl": [], "launchctl": []}

    def make(name, returncode=0, stdout=""):
        def fn(*args):
            calls[name].append(list(args))
            return subprocess.CompletedProcess([name, *args], returncode, stdout, "")

        return fn

    monkeypatch.setattr(usm_daemon, "systemctl", make("systemctl"))
    monkeypatch.setattr(usm_daemon, "launchctl", make("launchctl"))
    return calls


@pytest.fixture
def manager():
    return ServiceManager("usm-test-", "com.example.test.")


class TestPidLiveness:
    def test_self_and_missing(self):
        assert pid_alive(os.getpid()) is True
        assert pid_alive(None) is False
        assert pid_alive(0) is False
        assert pid_alive(999_999_999) is False

    def test_zombies_are_not_alive(self):
        """os.kill(pid, 0) succeeds for unreaped children; we must not."""
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not usm_daemon._is_zombie(proc.pid):
                time.sleep(0.05)
            assert usm_daemon._is_zombie(proc.pid) is True
            assert pid_alive(proc.pid) is False
        finally:
            proc.wait()

    def test_zombie_probe_is_safe_for_unknown_pids(self):
        assert usm_daemon._is_zombie(999_999_999) is False


class TestFileLock:
    def test_exclusive(self, tmp_path):
        first, second = FileLock(tmp_path / "l"), FileLock(tmp_path / "l")
        assert first.acquire() is True
        assert second.acquire() is False
        first.release()
        assert second.acquire() is True
        second.release()

    def test_context_manager_releases(self, tmp_path):
        with FileLock(tmp_path / "l"):
            pass
        assert FileLock(tmp_path / "l").acquire() is True

    def test_release_without_acquire_is_safe(self, tmp_path):
        FileLock(tmp_path / "l").release()


class TestSleepUntil:
    def test_returns_when_the_deadline_passes(self):
        stop = threading.Event()
        started = time.time()
        assert sleep_until(started + 0.2, stop, max_slice=0.05) is False
        assert time.time() - started >= 0.15

    def test_wakes_early_on_stop(self):
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        started = time.time()
        assert sleep_until(started + 30, stop, max_slice=0.05) is True
        assert time.time() - started < 5

    def test_already_stopped_returns_immediately(self):
        stop = threading.Event()
        stop.set()
        assert sleep_until(time.time() + 30, stop) is True


class TestServiceNaming:
    def test_unit_and_label(self, manager, service_dirs):
        assert manager.unit_name("job") == "usm-test-job.service"
        assert manager.label("job") == "com.example.test.job"
        assert manager.unit_path("job").name == "usm-test-job.service"
        assert manager.plist_path("job").name == "com.example.test.job.plist"

    def test_enabled_kind_detects_each_backend(self, manager, service_dirs):
        systemd, launchd = service_dirs
        assert manager.enabled_kind("job") is None
        manager.unit_path("job").write_text("[Unit]")
        assert manager.enabled_kind("job") == "systemd"
        manager.unit_path("job").unlink()
        manager.plist_path("job").write_bytes(b"<plist/>")
        assert manager.enabled_kind("job") == "launchd"

    def test_two_managers_do_not_collide(self, service_dirs):
        a = ServiceManager("usm-a-", "com.example.a.")
        b = ServiceManager("usm-b-", "com.example.b.")
        assert a.unit_name("x") != b.unit_name("x")
        assert a.label("x") != b.label("x")


class TestServiceRendering:
    def test_unit_contents(self, manager, service_dirs):
        unit = manager.render_unit(
            "usm test job", "/usr/local/bin/usm test up job", "/usr/local/bin/usm"
        )
        assert "Description=usm test job" in unit
        assert "ExecStart=/usr/local/bin/usm test up job" in unit
        assert "Restart=always" in unit
        assert "WantedBy=default.target" in unit
        assert "After=network-online.target" in unit
        assert "/usr/local/bin" in unit  # PATH carries the usm location

    def test_unit_restart_delay_is_configurable(self, manager, service_dirs):
        assert "RestartSec=30" in manager.render_unit("d", "e", "b", restart_sec=30)

    def test_plist_contents(self, manager, service_dirs, tmp_path):
        payload = plistlib.loads(
            manager.render_plist(
                "job",
                ["/usr/local/bin/usm", "test", "up", "job"],
                "/usr/local/bin/usm",
                log_path=tmp_path / "job.log",
            )
        )
        assert payload["Label"] == "com.example.test.job"
        assert payload["ProgramArguments"][-1] == "job"
        assert payload["RunAtLoad"] is True and payload["KeepAlive"] is True
        assert payload["StandardOutPath"] == str(tmp_path / "job.log")

    def test_plist_without_a_log(self, manager, service_dirs):
        payload = plistlib.loads(manager.render_plist("job", ["a"], "b"))
        assert "StandardOutPath" not in payload

    def test_path_value_includes_managed_bin_and_dedupes(self):
        value = usm_daemon.service_path_value("/opt/pipx/bin/usm")
        parts = value.split(":")
        assert "/opt/pipx/bin" in parts
        assert str(usm_daemon.LOCAL_BIN_DIR) in parts
        assert len(parts) == len(set(parts)), "PATH has duplicates"


class TestServiceActions:
    def test_enable_writes_the_unit_and_starts_it(
        self, manager, service_dirs, fake_service_calls, monkeypatch
    ):
        monkeypatch.setattr(usm_daemon, "default_service_kind", lambda: "systemd")
        kind = manager.enable(
            "job", ["/usr/bin/usm", "test", "up", "job"], description="d"
        )
        assert kind == "systemd"
        assert manager.unit_path("job").exists()
        assert ["daemon-reload"] in fake_service_calls["systemctl"]
        assert ["enable", "--now", "usm-test-job.service"] in (
            fake_service_calls["systemctl"]
        )

    def test_enable_cleans_up_when_systemctl_fails(
        self, manager, service_dirs, monkeypatch
    ):
        def failing(*args):
            code = 1 if args and args[0] == "enable" else 0
            return subprocess.CompletedProcess(list(args), code, "", "unit is masked")

        monkeypatch.setattr(usm_daemon, "systemctl", failing)
        monkeypatch.setattr(usm_daemon, "default_service_kind", lambda: "systemd")
        with pytest.raises(RuntimeError, match="masked"):
            manager.enable("job", ["usm"], description="d")
        assert not manager.unit_path("job").exists(), "left a broken unit behind"

    def test_enable_launchd(
        self, manager, service_dirs, fake_service_calls, monkeypatch
    ):
        monkeypatch.setattr(usm_daemon, "default_service_kind", lambda: "launchd")
        kind = manager.enable(
            "job", ["/usr/bin/usm", "test", "up", "job"], description="d"
        )
        assert kind == "launchd"
        assert manager.plist_path("job").exists()
        assert any(c[0] == "bootstrap" for c in fake_service_calls["launchctl"])
        assert any(c[0] == "kickstart" for c in fake_service_calls["launchctl"])

    def test_disable_systemd(self, manager, service_dirs, fake_service_calls):
        manager.unit_path("job").write_text("[Unit]")
        assert manager.disable("job") == "systemd"
        assert not manager.unit_path("job").exists()
        assert ["disable", "--now", "usm-test-job.service"] in (
            fake_service_calls["systemctl"]
        )

    def test_disable_launchd(self, manager, service_dirs, fake_service_calls):
        manager.plist_path("job").write_bytes(b"<plist/>")
        assert manager.disable("job") == "launchd"
        assert not manager.plist_path("job").exists()
        assert any(c[0] == "bootout" for c in fake_service_calls["launchctl"])

    def test_disable_when_not_enabled(self, manager, service_dirs, fake_service_calls):
        assert manager.disable("job") is None
        assert fake_service_calls["systemctl"] == []

    def test_start_stop_dispatch(self, manager, service_dirs, fake_service_calls):
        assert manager.start("job") is None
        assert manager.stop("job") is None
        manager.unit_path("job").write_text("[Unit]")
        assert manager.start("job").returncode == 0
        assert manager.stop("job").returncode == 0
        assert ["start", "usm-test-job.service"] in fake_service_calls["systemctl"]
        assert ["stop", "usm-test-job.service"] in fake_service_calls["systemctl"]

    def test_is_active(self, manager, service_dirs, monkeypatch):
        assert manager.is_active("job") is False
        manager.unit_path("job").write_text("[Unit]")
        monkeypatch.setattr(
            usm_daemon,
            "systemctl",
            lambda *a: subprocess.CompletedProcess(list(a), 0, "", ""),
        )
        assert manager.is_active("job") is True
        monkeypatch.setattr(
            usm_daemon,
            "systemctl",
            lambda *a: subprocess.CompletedProcess(list(a), 3, "", ""),
        )
        assert manager.is_active("job") is False

    def test_is_active_launchd_reads_the_state_line(
        self, manager, service_dirs, monkeypatch
    ):
        manager.plist_path("job").write_bytes(b"<plist/>")
        monkeypatch.setattr(
            usm_daemon,
            "launchctl",
            lambda *a: subprocess.CompletedProcess(list(a), 0, "state = running", ""),
        )
        assert manager.is_active("job") is True
        monkeypatch.setattr(
            usm_daemon,
            "launchctl",
            lambda *a: subprocess.CompletedProcess(list(a), 0, "state = waiting", ""),
        )
        assert manager.is_active("job") is False

    def test_default_kind_follows_the_platform(self, monkeypatch):
        monkeypatch.setattr(usm_daemon.platform, "system", lambda: "Darwin")
        assert usm_daemon.default_service_kind() == "launchd"
        monkeypatch.setattr(usm_daemon.platform, "system", lambda: "Linux")
        assert usm_daemon.default_service_kind() == "systemd"


class TestErrorPaths:
    """Failure and edge paths for the shared plumbing."""

    def test_human_bytes_reaches_terabytes(self):
        assert usm_daemon.human_bytes(5 * 1024**4) == "5.0TiB"
        assert usm_daemon.human_bytes(1024**6).endswith("TiB")

    def test_pid_alive_treats_permission_denied_as_alive(self, monkeypatch):
        def denied(_pid, _sig):
            raise PermissionError("not yours")

        monkeypatch.setattr(usm_daemon.os, "kill", denied)
        assert pid_alive(1) is True

    def test_pid_alive_swallows_other_oserrors(self, monkeypatch):
        def boom(_pid, _sig):
            raise OSError("weird")

        monkeypatch.setattr(usm_daemon.os, "kill", boom)
        assert pid_alive(1) is False

    def test_zombie_probe_ignores_malformed_stat(self, monkeypatch, tmp_path):
        import builtins

        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if str(path).startswith("/proc/"):
                return real_open(tmp_path / "stat", *a, **kw)
            return real_open(path, *a, **kw)

        (tmp_path / "stat").write_text("no parens here")
        monkeypatch.setattr(builtins, "open", fake_open)
        assert usm_daemon._is_zombie(1234) is False

    def test_zombie_probe_handles_comm_with_parens(self, monkeypatch, tmp_path):
        import builtins

        real_open = builtins.open
        (tmp_path / "stat").write_text("42 (weird (name) here) Z 1 2 3")

        def fake_open(path, *a, **kw):
            if str(path).startswith("/proc/"):
                return real_open(tmp_path / "stat", *a, **kw)
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", fake_open)
        assert usm_daemon._is_zombie(42) is True

    def test_service_start_stop_on_launchd(self, monkeypatch, tmp_path):
        systemd, launchd = tmp_path / "s", tmp_path / "l"
        systemd.mkdir()
        launchd.mkdir()
        monkeypatch.setattr(usm_daemon, "SYSTEMD_USER_DIR", systemd)
        monkeypatch.setattr(usm_daemon, "LAUNCHD_USER_DIR", launchd)
        calls = []

        def fake(*args):
            calls.append(list(args))
            return subprocess.CompletedProcess(list(args), 0, "", "")

        monkeypatch.setattr(usm_daemon, "launchctl", fake)
        manager = ServiceManager("usm-t-", "com.t.")
        manager.plist_path("j").write_bytes(b"<plist/>")
        assert manager.start("j").returncode == 0
        assert manager.stop("j").returncode == 0
        assert calls[0][0] == "kickstart" and calls[1][:2] == ["kill", "SIGTERM"]

    def test_systemctl_and_launchctl_shell_out(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv

            class R:
                returncode = 0
                stdout = ""
                stderr = ""

            return R()

        monkeypatch.setattr(usm_daemon, "run", fake_run)
        usm_daemon.systemctl("is-active", "x")
        assert seen["argv"][:2] == ["systemctl", "--user"]
        usm_daemon.launchctl("print", "y")
        assert seen["argv"][0] == "launchctl"

    def test_exclude_leading_dot_slash_is_normalised(self):
        spec = ExcludeSpec.build(["*.log"], defaults=False)
        assert spec.matches("./a.log") is True
        assert spec.matches("././a.log") is True

    def test_exclude_blank_patterns_are_dropped(self):
        spec = ExcludeSpec.build(["  ", "", "*.log"], defaults=False)
        assert spec.patterns == ("*.log",)

    def test_exclude_duplicates_are_collapsed(self):
        spec = ExcludeSpec.build(["*.log", "*.log"], defaults=False)
        assert spec.patterns == ("*.log",)

    def test_exclude_matches_a_nested_path_pattern(self):
        spec = ExcludeSpec.build(["build/cache"], defaults=False)
        assert spec.matches("build/cache/deep/x.o") is True
        assert spec.matches("other/build/cache") is False

    def test_exclude_matches_a_bare_name_anywhere(self):
        spec = ExcludeSpec.build(["target"], defaults=False)
        assert spec.matches("a/target/b.class") is True
        assert spec.matches("target") is True
