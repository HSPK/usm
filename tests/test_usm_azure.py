"""Tests for scripts/usm_azure.py — the layer azsync and blobmount share.

The SAS providers and ExcludeSpec are exercised in depth by
``tests/test_azsync.py`` (that is where they are actually driven). This file
covers the rest: service management, mount discovery, blob URL handling and
the small IO primitives — plus the invariants that must hold for *both*
commands, so a change made for one cannot silently break the other.
"""

from __future__ import annotations

import json
import os
import plistlib
import stat
import threading
import time

import pytest

import usm_azure
from usm_azure import (
    ExcludeSpec,
    FileLock,
    SasError,
    SasManager,
    ServiceManager,
    atomic_write,
    build_provider,
    container_url,
    human_bytes,
    human_duration,
    join_sas,
    normalize_sas,
    parse_blob_url,
    parse_iso8601,
    parse_sas_expiry,
    pid_alive,
    read_json,
    redact,
    resolve_blob_path,
    slugify,
    sleep_until,
    split_sas,
)


def sas_for(expires_in: float) -> str:
    import datetime

    stamp = datetime.datetime.fromtimestamp(
        time.time() + expires_in, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"sv=2021-08-06&se={stamp}&sp=racwdl&sig=SECRET"


# --- Small primitives ------------------------------------------------------


class TestPrimitives:
    def test_atomic_write_replaces_in_one_step(self, tmp_path):
        target = tmp_path / "f.json"
        target.write_text("old")
        atomic_write(target, "new")
        assert target.read_text() == "new"
        assert list(tmp_path.glob("*.tmp*")) == []

    def test_atomic_write_creates_parents(self, tmp_path):
        target = tmp_path / "a" / "b" / "f"
        atomic_write(target, "x")
        assert target.read_text() == "x"

    def test_atomic_write_honours_mode(self, tmp_path):
        target = tmp_path / "secret"
        atomic_write(target, "x", mode=0o600)
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600

    def test_read_json_roundtrip_and_fallbacks(self, tmp_path):
        target = tmp_path / "f.json"
        assert read_json(target) is None
        assert read_json(target, default={}) == {}
        target.write_text("{not json")
        assert read_json(target, default="fallback") == "fallback"
        target.write_text(json.dumps({"a": 1}))
        assert read_json(target) == {"a": 1}

    def test_slugify(self):
        assert slugify("My Mount!") == "my-mount"
        assert slugify("") == ""
        assert slugify("---") == ""

    @pytest.mark.parametrize(
        "value,expected", [(512, "512B"), (2048, "2.0KiB"), (1024**3, "1.0GiB")]
    )
    def test_human_bytes(self, value, expected):
        assert human_bytes(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [(None, "-"), (5, "5s"), (65, "1m05s"), (3600, "1h00m"), (90000, "1d01h")],
    )
    def test_human_duration(self, value, expected):
        assert human_duration(value) == expected

    def test_redaction_covers_the_shapes_we_log(self):
        assert redact("sig=abc") == "sig=***"
        assert redact("?sv=1&sig=abc&sp=r") == "?sv=1&sig=***&sp=r"
        assert redact('sas: "sv=1&sig=abc"') == 'sas: "sv=1&sig=***"'
        assert redact("https://x/y?sig=abc") == "https://x/y?sig=***"
        assert redact("") == ""
        assert redact(None) == ""

    def test_redaction_leaves_everything_else(self):
        assert redact("account-name: acct") == "account-name: acct"


class TestPidLiveness:
    def test_self_and_missing(self):
        assert pid_alive(os.getpid()) is True
        assert pid_alive(None) is False
        assert pid_alive(0) is False
        assert pid_alive(999_999_999) is False

    def test_zombies_are_not_alive(self):
        """os.kill(pid, 0) succeeds for unreaped children; we must not."""
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not usm_azure._is_zombie(proc.pid):
                time.sleep(0.05)
            assert usm_azure._is_zombie(proc.pid) is True
            assert pid_alive(proc.pid) is False
        finally:
            proc.wait()

    def test_zombie_probe_is_safe_for_unknown_pids(self):
        assert usm_azure._is_zombie(999_999_999) is False


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


# --- Blob URLs -------------------------------------------------------------


class TestBlobUrls:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://a.blob.core.windows.net/c", True),
            ("https://a.dfs.core.windows.net/c", True),
            ("https://a.file.core.windows.net/s", True),
            ("http://a.blob.core.windows.net/c", True),
            ("https://example.com/c", False),
            ("/local/path", False),
            ("", False),
        ],
    )
    def test_is_https_blob(self, url, expected):
        assert usm_azure.is_https_blob(url) is expected

    def test_parse_account_container(self):
        assert parse_blob_url("https://acct.blob.core.windows.net/bucket/a/b") == (
            "acct",
            "bucket",
        )

    def test_parse_rejects_container_less_urls(self):
        with pytest.raises(ValueError):
            parse_blob_url("https://acct.blob.core.windows.net/")

    def test_container_url(self):
        assert container_url("acct", "bucket") == (
            "https://acct.blob.core.windows.net/bucket"
        )

    def test_split_and_join_roundtrip(self):
        url = "https://a.blob.core.windows.net/c/d?sv=1&sig=abc"
        base, token = split_sas(url)
        assert base == "https://a.blob.core.windows.net/c/d"
        assert join_sas(base, token) == url

    def test_split_ignores_non_sas_queries(self):
        url = "https://a.blob.core.windows.net/c?snapshot=1"
        assert split_sas(url) == (url, None)

    def test_join_handles_existing_query_and_leading_question(self):
        assert join_sas("https://x/y?a=1", "?sig=z") == "https://x/y?a=1&sig=z"
        assert join_sas("https://x/y", "sig=z") == "https://x/y?sig=z"
        assert join_sas("https://x/y", None) == "https://x/y"

    def test_has_sas(self):
        assert usm_azure.has_sas("https://x/y?sig=a") is True
        assert usm_azure.has_sas("https://x/y?sv=1") is False


class TestResolveBlobPath:
    MOUNTS = {
        "/mnt/blob": {
            "url": "https://acct.blob.core.windows.net/bucket/",
            "account_name": "acct",
            "container_name": "bucket",
        }
    }

    def test_url_passthrough(self):
        url = "https://acct.blob.core.windows.net/bucket/x"
        assert resolve_blob_path(url, {}) == url

    def test_path_inside_a_mount(self, tmp_path, monkeypatch):
        mount = tmp_path / "blob"
        (mount / "deep").mkdir(parents=True)
        mounts = {str(mount): {"url": "https://acct.blob.core.windows.net/bucket/"}}
        assert resolve_blob_path(str(mount / "deep"), mounts) == (
            "https://acct.blob.core.windows.net/bucket/deep"
        )

    def test_mount_root_itself(self, tmp_path):
        mount = tmp_path / "blob"
        mount.mkdir()
        mounts = {str(mount): {"url": "https://acct.blob.core.windows.net/bucket/"}}
        assert resolve_blob_path(str(mount), mounts) == (
            "https://acct.blob.core.windows.net/bucket/"
        )

    def test_path_outside_any_mount(self, tmp_path):
        assert resolve_blob_path(str(tmp_path), {}) is None

    def test_sibling_prefix_is_not_a_match(self, tmp_path):
        mount = tmp_path / "blob"
        mount.mkdir()
        (tmp_path / "blob-other").mkdir()
        mounts = {str(mount): {"url": "https://acct.blob.core.windows.net/bucket/"}}
        assert resolve_blob_path(str(tmp_path / "blob-other"), mounts) is None

    def test_special_characters_are_quoted(self, tmp_path):
        mount = tmp_path / "blob"
        (mount / "a b").mkdir(parents=True)
        mounts = {str(mount): {"url": "https://acct.blob.core.windows.net/bucket/"}}
        assert resolve_blob_path(str(mount / "a b"), mounts).endswith("/a%20b")


class TestBlobfuseMounts:
    def _fake_psutil(self, monkeypatch, procs):
        import types

        class Proc:
            def __init__(self, info):
                self.info = info

        fake = types.SimpleNamespace(
            process_iter=lambda _attrs: [Proc(p) for p in procs],
            NoSuchProcess=Exception,
            AccessDenied=Exception,
        )
        import sys

        monkeypatch.setitem(sys.modules, "psutil", fake)

    def test_reads_the_config_of_each_process(self, tmp_path, monkeypatch):
        config = tmp_path / "c.yaml"
        config.write_text("azstorage:\n  account-name: acct\n  container: bucket\n")
        self._fake_psutil(
            monkeypatch,
            [
                {
                    "cmdline": [
                        "/usr/bin/blobfuse2",
                        "mount",
                        "/mnt/data",
                        "--config-file",
                        str(config),
                    ],
                    "pid": 42,
                }
            ],
        )
        mounts = usm_azure.blobfuse_mounts()
        assert mounts["/mnt/data"]["account_name"] == "acct"
        assert mounts["/mnt/data"]["container_name"] == "bucket"
        assert mounts["/mnt/data"]["url"].endswith("/bucket/")
        assert mounts["/mnt/data"]["pid"] == 42

    def test_equals_form_of_the_config_flag(self, tmp_path, monkeypatch):
        config = tmp_path / "c.yaml"
        config.write_text("azstorage:\n  account-name: a\n  container: b\n")
        self._fake_psutil(
            monkeypatch,
            [
                {
                    "cmdline": [
                        "blobfuse2",
                        "mount",
                        "/mnt/x",
                        f"--config-file={config}",
                    ],
                    "pid": 1,
                }
            ],
        )
        assert "/mnt/x" in usm_azure.blobfuse_mounts()

    def test_ignores_non_mount_and_non_blobfuse_processes(self, monkeypatch):
        self._fake_psutil(
            monkeypatch,
            [
                {"cmdline": ["blobfuse2", "unmount", "/mnt/x"], "pid": 1},
                {"cmdline": ["/usr/bin/python", "mount", "/mnt/y"], "pid": 2},
                {"cmdline": [], "pid": 3},
                {"cmdline": ["blobfuse2"], "pid": 4},
            ],
        )
        assert usm_azure.blobfuse_mounts() == {}

    def test_skips_unreadable_or_incomplete_configs(self, tmp_path, monkeypatch):
        missing = tmp_path / "gone.yaml"
        partial = tmp_path / "partial.yaml"
        partial.write_text("azstorage:\n  account-name: a\n")
        self._fake_psutil(
            monkeypatch,
            [
                {
                    "cmdline": ["blobfuse2", "mount", "/mnt/a", "-c", str(missing)],
                    "pid": 1,
                },
                {
                    "cmdline": ["blobfuse2", "mount", "/mnt/b", "-c", str(partial)],
                    "pid": 2,
                },
                {"cmdline": ["blobfuse2", "mount", "/mnt/c"], "pid": 3},
            ],
        )
        assert usm_azure.blobfuse_mounts() == {}

    def test_no_psutil_means_no_mounts(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kw):
            if name == "psutil":
                raise ImportError("nope")
            return real_import(name, *args, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert usm_azure.blobfuse_mounts() == {}


# --- Service management ----------------------------------------------------


@pytest.fixture
def service_dirs(tmp_path, monkeypatch):
    systemd = tmp_path / "systemd"
    launchd = tmp_path / "launchd"
    systemd.mkdir()
    launchd.mkdir()
    monkeypatch.setattr(usm_azure, "SYSTEMD_USER_DIR", systemd)
    monkeypatch.setattr(usm_azure, "LAUNCHD_USER_DIR", launchd)
    return systemd, launchd


@pytest.fixture
def fake_service_calls(monkeypatch):
    calls = {"systemctl": [], "launchctl": []}
    import subprocess

    def make(name, returncode=0, stdout=""):
        def fn(*args):
            calls[name].append(list(args))
            return subprocess.CompletedProcess([name, *args], returncode, stdout, "")

        return fn

    monkeypatch.setattr(usm_azure, "systemctl", make("systemctl"))
    monkeypatch.setattr(usm_azure, "launchctl", make("launchctl"))
    return calls


@pytest.fixture
def manager():
    return ServiceManager("usm-test-", "com.example.test.")


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
        value = usm_azure.service_path_value("/opt/pipx/bin/usm")
        parts = value.split(":")
        assert "/opt/pipx/bin" in parts
        assert str(usm_azure.LOCAL_BIN_DIR) in parts
        assert len(parts) == len(set(parts)), "PATH has duplicates"


class TestServiceActions:
    def test_enable_writes_the_unit_and_starts_it(
        self, manager, service_dirs, fake_service_calls, monkeypatch
    ):
        monkeypatch.setattr(usm_azure, "default_service_kind", lambda: "systemd")
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
        import subprocess

        def failing(*args):
            code = 1 if args and args[0] == "enable" else 0
            return subprocess.CompletedProcess(list(args), code, "", "unit is masked")

        monkeypatch.setattr(usm_azure, "systemctl", failing)
        monkeypatch.setattr(usm_azure, "default_service_kind", lambda: "systemd")
        with pytest.raises(RuntimeError, match="masked"):
            manager.enable("job", ["usm"], description="d")
        assert not manager.unit_path("job").exists(), "left a broken unit behind"

    def test_enable_launchd(
        self, manager, service_dirs, fake_service_calls, monkeypatch
    ):
        monkeypatch.setattr(usm_azure, "default_service_kind", lambda: "launchd")
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
        import subprocess

        assert manager.is_active("job") is False
        manager.unit_path("job").write_text("[Unit]")
        monkeypatch.setattr(
            usm_azure,
            "systemctl",
            lambda *a: subprocess.CompletedProcess(list(a), 0, "", ""),
        )
        assert manager.is_active("job") is True
        monkeypatch.setattr(
            usm_azure,
            "systemctl",
            lambda *a: subprocess.CompletedProcess(list(a), 3, "", ""),
        )
        assert manager.is_active("job") is False

    def test_is_active_launchd_reads_the_state_line(
        self, manager, service_dirs, monkeypatch
    ):
        import subprocess

        manager.plist_path("job").write_bytes(b"<plist/>")
        monkeypatch.setattr(
            usm_azure,
            "launchctl",
            lambda *a: subprocess.CompletedProcess(list(a), 0, "state = running", ""),
        )
        assert manager.is_active("job") is True
        monkeypatch.setattr(
            usm_azure,
            "launchctl",
            lambda *a: subprocess.CompletedProcess(list(a), 0, "state = waiting", ""),
        )
        assert manager.is_active("job") is False

    def test_default_kind_follows_the_platform(self, monkeypatch):
        monkeypatch.setattr(usm_azure.platform, "system", lambda: "Darwin")
        assert usm_azure.default_service_kind() == "launchd"
        monkeypatch.setattr(usm_azure.platform, "system", lambda: "Linux")
        assert usm_azure.default_service_kind() == "systemd"


# --- Cross-command invariants ---------------------------------------------


class TestSharedContract:
    """Guarantees both commands rely on; breaking one must fail here."""

    def test_expiry_always_comes_from_the_token(self):
        """A provider that over-promises must not extend the real deadline."""

        class Liar(usm_azure.SasProvider):
            kind = "liar"

            def fetch(self):
                return json.dumps(
                    {"sas": sas_for(600), "expires_at": time.time() + 999_999}
                ), None

        now = time.time()
        token = Liar().resolve(now)
        assert token.remaining(now) == pytest.approx(600, abs=5)

    def test_already_expired_tokens_are_rejected(self):
        class Stale(usm_azure.SasProvider):
            kind = "stale"

            def fetch(self):
                return sas_for(-60), None

        with pytest.raises(SasError, match="expired"):
            Stale().resolve(time.time())

    def test_manager_refreshes_below_the_floor(self, tmp_path):
        class Rotating(usm_azure.SasProvider):
            kind = "rot"

            def __init__(self):
                self.calls = 0

            def fetch(self):
                self.calls += 1
                return (sas_for(300) if self.calls == 1 else sas_for(7200)), None

        provider = Rotating()
        manager = SasManager(provider, tmp_path / "c", min_remaining=1800)
        now = time.time()
        manager.ensure(now)
        manager.ensure(now)
        assert provider.calls == 2

    def test_manager_caches_a_healthy_token(self, tmp_path):
        class Once(usm_azure.SasProvider):
            kind = "once"

            def __init__(self):
                self.calls = 0

            def fetch(self):
                self.calls += 1
                return sas_for(7200), None

        provider = Once()
        manager = SasManager(provider, tmp_path / "c", min_remaining=1800)
        manager.ensure(time.time())
        manager.ensure(time.time())
        assert provider.calls == 1

    def test_cache_is_owner_only(self, tmp_path):
        class P(usm_azure.SasProvider):
            kind = "p"

            def fetch(self):
                return sas_for(7200), None

        manager = SasManager(P(), tmp_path / "c")
        manager.ensure(time.time())
        assert stat.S_IMODE(os.stat(tmp_path / "c").st_mode) == 0o600

    def test_aad_manager_needs_no_token(self, tmp_path):
        manager = SasManager(usm_azure.AadProvider(), tmp_path / "c")
        assert manager.enabled is False
        assert manager.ensure(time.time()).token == ""
        assert manager.current() is None
        assert not (tmp_path / "c").exists()

    def test_inline_tokens_are_never_rotated(self, tmp_path):
        manager = SasManager(
            usm_azure.InlineProvider(sas_for(60)), tmp_path / "c", min_remaining=1800
        )
        now = time.time()
        first = manager.ensure(now)
        second = manager.ensure(now)
        assert first.token == second.token

    def test_needed_lifetime_scales_with_use(self, tmp_path):
        manager = SasManager(
            usm_azure.AadProvider(), tmp_path / "c", min_remaining=1800
        )
        assert manager.needed_lifetime(None) == 1800
        assert manager.needed_lifetime(60) == 1800
        assert manager.needed_lifetime(1200) == 3600

    def test_invalidate_forces_a_new_fetch(self, tmp_path):
        class P(usm_azure.SasProvider):
            kind = "p"

            def __init__(self):
                self.calls = 0

            def fetch(self):
                self.calls += 1
                return sas_for(7200), None

        provider = P()
        manager = SasManager(provider, tmp_path / "c")
        manager.ensure(time.time())
        manager.invalidate()
        manager.ensure(time.time())
        assert provider.calls == 2

    @pytest.mark.parametrize("auth", ["env", "file", "exec", "http"])
    def test_external_kinds_demand_their_flag(self, auth):
        with pytest.raises(SasError, match="--sas-"):
            build_provider(auth)

    def test_az_needs_an_account(self):
        with pytest.raises(SasError, match="account"):
            build_provider("az")

    def test_unknown_kind(self):
        with pytest.raises(SasError, match="unknown auth"):
            build_provider("telepathy")

    def test_every_declared_kind_is_constructible(self, tmp_path):
        for auth in usm_azure.AUTH_KINDS:
            kwargs = {
                "spec": "spec",
                "url": f"https://a.blob.core.windows.net/c?{sas_for(60)}",
                "account": "a",
                "container": "c",
            }
            assert isinstance(build_provider(auth, **kwargs), usm_azure.SasProvider)

    def test_parse_iso8601_shapes(self):
        assert parse_iso8601("2030-01-02T03:04:05Z") is not None
        assert parse_iso8601("2030-01-02T03:04Z") is not None
        assert parse_iso8601("2030-01-02T03:04:05+00:00") is not None
        assert parse_iso8601("nonsense") is None
        assert parse_iso8601("") is None

    def test_sas_expiry_extraction(self):
        assert parse_sas_expiry(sas_for(3600)) == pytest.approx(
            time.time() + 3600, abs=5
        )
        assert parse_sas_expiry("sv=1&sig=x") is None
        assert parse_sas_expiry("") is None

    def test_normalize_accepts_every_provider_shape(self):
        bare = sas_for(60)
        assert normalize_sas(bare)[0] == bare
        assert normalize_sas(f"?{bare}")[0] == bare
        assert normalize_sas(f"https://a.blob.core.windows.net/c?{bare}")[0] == bare
        assert normalize_sas(json.dumps({"sas": bare}))[0] == bare
        assert normalize_sas(json.dumps({"token": bare}))[0] == bare

    def test_excludes_agree_between_the_watcher_and_azcopy(self):
        """The two renderings must hide the same paths."""
        import re

        spec = ExcludeSpec.build()
        flags = spec.to_azcopy_flags()
        regexes = [
            re.compile(r) for r in flags[flags.index("--exclude-regex") + 1].split(";")
        ]
        patterns = flags[flags.index("--exclude-pattern") + 1].split(";")
        for rel in ("deep/.git/objects/ab", ".git/HEAD", "x/node_modules/a.js"):
            assert spec.matches(rel)
            assert any(r.match(rel) for r in regexes), rel
        for rel in ("a/b.pyc", "scratch.tmp"):
            assert spec.matches(rel)
            assert any(
                __import__("fnmatch").fnmatch(rel.rsplit("/", 1)[-1], p)
                for p in patterns
            ), rel

    def test_exclude_flags_are_pairs(self):
        flags = ExcludeSpec.build().to_azcopy_flags()
        assert len(flags) % 2 == 0
        assert all(flags[i].startswith("--exclude-") for i in range(0, len(flags), 2))


# --- Output formatting -----------------------------------------------------


class TestPathAndTargetShortening:
    def test_home_becomes_tilde(self, monkeypatch, tmp_path):
        monkeypatch.setattr(usm_azure.Path, "home", classmethod(lambda cls: tmp_path))
        assert usm_azure.shorten_path(tmp_path) == "~"
        assert usm_azure.shorten_path(tmp_path / "a" / "b") == "~/a/b"

    def test_non_home_paths_are_untouched(self, monkeypatch, tmp_path):
        monkeypatch.setattr(usm_azure.Path, "home", classmethod(lambda cls: tmp_path))
        assert usm_azure.shorten_path("/mnt/data") == "/mnt/data"

    def test_sibling_of_home_is_not_shortened(self, monkeypatch, tmp_path):
        """`/home/bob2` must not be mangled by a `/home/bob` prefix."""
        monkeypatch.setattr(usm_azure.Path, "home", classmethod(lambda cls: tmp_path))
        sibling = str(tmp_path) + "-other/x"
        assert usm_azure.shorten_path(sibling) == sibling

    def test_accepts_path_objects(self, monkeypatch, tmp_path):
        monkeypatch.setattr(usm_azure.Path, "home", classmethod(lambda cls: tmp_path))
        assert usm_azure.shorten_path(tmp_path / "z") == "~/z"

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://acct.blob.core.windows.net/c/a/b", "acct/c/a/b"),
            ("https://acct.blob.core.windows.net/c", "acct/c"),
            ("https://acct.blob.core.windows.net/", "acct"),
            ("https://acct.dfs.core.windows.net/c/d", "acct/c/d"),
        ],
    )
    def test_short_blob_target(self, url, expected):
        assert usm_azure.short_blob_target(url) == expected

    def test_short_blob_target_drops_the_sas(self):
        url = f"https://acct.blob.core.windows.net/c/d?{sas_for(60)}"
        out = usm_azure.short_blob_target(url)
        assert out == "acct/c/d" and "sig" not in out

    def test_short_blob_target_passes_through_non_urls(self):
        assert usm_azure.short_blob_target("not a url") == "not a url"
        assert usm_azure.short_blob_target("") == ""


class TestElide:
    def test_short_text_is_untouched(self):
        assert usm_azure.elide("abc", 10) == "abc"
        assert usm_azure.elide("abc", 3) == "abc"

    def test_tail_is_kept_by_default(self):
        assert usm_azure.elide("abcdefghij", 5) == "…ghij"
        assert usm_azure.elide("abcdefghij", 5).endswith("j")

    def test_head_mode(self):
        out = usm_azure.elide("abcdefghij", 5, keep="head")
        assert out.startswith("a") and out.endswith("…")

    def test_degenerate_widths(self):
        assert usm_azure.elide("abcdef", 1) == "…"
        assert usm_azure.elide("abcdef", 0) == "abcdef"

    def test_result_never_exceeds_the_width(self):
        for width in range(1, 12):
            assert len(usm_azure.elide("a" * 40, width)) <= width


class TestCompactDuration:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, "-"),
            (0, "0s"),
            (59, "59s"),
            (60, "1m"),
            (3599, "59m"),
            (3600, "1h"),
            (86399, "23h"),
            (86400, "1d"),
            (86400 * 400, "400d"),
            (-5, "0s"),
        ],
    )
    def test_values(self, value, expected):
        assert usm_azure.compact_duration(value) == expected

    def test_stays_narrow_enough_for_a_column(self):
        for secs in (0, 59, 60, 3600, 86400, 86400 * 999):
            assert len(usm_azure.compact_duration(secs)) <= 4


def render(renderable, width: int = 80) -> str:
    from rich.console import Console

    console = Console(width=width, file=__import__("io").StringIO(), no_color=True)
    console.print(renderable)
    return console.file.getvalue()


class TestTables:
    def test_rows_stay_on_one_line(self):
        table = usm_azure.new_table("A", "B")
        table.add_row("x" * 200, "y" * 200)
        body = [ln for ln in render(table, 60).splitlines() if ln.strip()]
        # header + rule + one row
        assert len(body) == 3

    def test_never_exceeds_the_console_width(self):
        table = usm_azure.new_table("A", "B", "C")
        table.add_row("x" * 100, "y" * 100, "z" * 100)
        for width in (60, 80, 120):
            for line in render(table, width).splitlines():
                assert len(line) <= width, f"line overflows at width {width}"

    def test_column_options_are_forwarded(self):
        table = usm_azure.new_table(("A", {"justify": "right"}), "B")
        assert table.columns[0].justify == "right"
        assert table.columns[0].no_wrap is True
        assert table.columns[1].no_wrap is True

    def test_kv_table_sections_render_as_blank_rows(self):
        out = render(usm_azure.kv_table([("a", "1"), usm_azure.SECTION, ("b", "2")]))
        lines = out.splitlines()
        assert any("a" in ln for ln in lines) and any("b" in ln for ln in lines)
        assert any(not ln.strip() for ln in lines), "no blank separator emitted"

    def test_kv_table_wraps_instead_of_truncating(self):
        out = render(usm_azure.kv_table([("key", "v" * 120)]), 60)
        assert "…" not in out, "detail values must not be silently cut"
        assert out.count("v") == 120

    def test_kv_table_handles_none(self):
        assert "key" in render(usm_azure.kv_table([("key", None)]))


# --- Error and boundary paths ---------------------------------------------


class TestErrorPaths:
    def test_human_bytes_reaches_terabytes(self):
        assert usm_azure.human_bytes(5 * 1024**4) == "5.0TiB"
        assert usm_azure.human_bytes(1024**6).endswith("TiB")

    def test_pid_alive_treats_permission_denied_as_alive(self, monkeypatch):
        def denied(_pid, _sig):
            raise PermissionError("not yours")

        monkeypatch.setattr(usm_azure.os, "kill", denied)
        assert pid_alive(1) is True

    def test_pid_alive_swallows_other_oserrors(self, monkeypatch):
        def boom(_pid, _sig):
            raise OSError("weird")

        monkeypatch.setattr(usm_azure.os, "kill", boom)
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
        assert usm_azure._is_zombie(1234) is False

    def test_zombie_probe_handles_comm_with_parens(self, monkeypatch, tmp_path):
        import builtins

        real_open = builtins.open
        (tmp_path / "stat").write_text("42 (weird (name) here) Z 1 2 3")

        def fake_open(path, *a, **kw):
            if str(path).startswith("/proc/"):
                return real_open(tmp_path / "stat", *a, **kw)
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", fake_open)
        assert usm_azure._is_zombie(42) is True

    def test_exec_provider_reports_a_missing_command(self):
        provider = usm_azure.ExecProvider("definitely-not-a-command-xyz")
        with pytest.raises(SasError, match="exited"):
            provider.resolve(time.time())

    def test_exec_provider_timeout(self):
        provider = usm_azure.ExecProvider("sleep 5", timeout=0.2)
        with pytest.raises(SasError, match="failed"):
            provider.resolve(time.time())

    def test_exec_provider_with_no_output_at_all(self):
        with pytest.raises(SasError, match="nothing"):
            usm_azure.ExecProvider("true").resolve(time.time())

    def test_http_provider_network_failure(self, monkeypatch):
        import urllib.error
        import urllib.request

        def boom(*a, **kw):
            raise urllib.error.URLError("unreachable")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        with pytest.raises(SasError, match="failed"):
            usm_azure.HttpProvider("https://x").resolve(time.time())

    def test_az_provider_without_the_cli(self, monkeypatch):
        def missing(*a, **kw):
            raise FileNotFoundError("az")

        monkeypatch.setattr(usm_azure.subprocess, "run", missing)
        with pytest.raises(SasError, match="not on PATH"):
            usm_azure.AzCliProvider("a", "c").resolve(time.time())

    def test_az_provider_nonzero_exit_is_redacted(self, monkeypatch):
        import subprocess

        def failing(*a, **kw):
            return subprocess.CompletedProcess(
                a[0], 1, "", "login failed for sig=SECRET"
            )

        monkeypatch.setattr(usm_azure.subprocess, "run", failing)
        with pytest.raises(SasError) as exc:
            usm_azure.AzCliProvider("a", "c").resolve(time.time())
        assert "SECRET" not in str(exc.value)

    def test_az_provider_subprocess_error(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("fork failed")

        monkeypatch.setattr(usm_azure.subprocess, "run", boom)
        with pytest.raises(SasError, match="generate-sas failed"):
            usm_azure.AzCliProvider("a", "c").resolve(time.time())

    def test_az_provider_builds_the_expected_command(self, monkeypatch):
        import subprocess

        captured = {}

        def capture(argv, **kw):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, sas_for(3600), "")

        monkeypatch.setattr(usm_azure.subprocess, "run", capture)
        usm_azure.AzCliProvider("acct", "bucket", ttl_hours=4).resolve(time.time())
        argv = captured["argv"]
        assert argv[:4] == ["az", "storage", "container", "generate-sas"]
        assert argv[argv.index("--account-name") + 1] == "acct"
        assert argv[argv.index("--name") + 1] == "bucket"
        assert "--as-user" in argv and "--auth-mode" in argv

    def test_manager_ignores_a_corrupt_cache(self, tmp_path):
        class P(usm_azure.SasProvider):
            kind = "p"

            def __init__(self):
                self.calls = 0

            def fetch(self):
                self.calls += 1
                return sas_for(7200), None

        cache = tmp_path / "c"
        cache.write_text("{not json")
        provider = P()
        manager = SasManager(provider, cache)
        assert manager.ensure(time.time()).token
        assert provider.calls == 1

    def test_manager_ignores_a_cache_without_a_token(self, tmp_path):
        cache = tmp_path / "c"
        cache.write_text(json.dumps({"expires_at": 1}))
        manager = SasManager(usm_azure.InlineProvider(sas_for(60)), cache)
        assert manager.ensure(time.time()).token

    def test_service_start_stop_on_launchd(self, monkeypatch, tmp_path):
        import subprocess

        systemd, launchd = tmp_path / "s", tmp_path / "l"
        systemd.mkdir()
        launchd.mkdir()
        monkeypatch.setattr(usm_azure, "SYSTEMD_USER_DIR", systemd)
        monkeypatch.setattr(usm_azure, "LAUNCHD_USER_DIR", launchd)
        calls = []

        def fake(*args):
            calls.append(list(args))
            return subprocess.CompletedProcess(list(args), 0, "", "")

        monkeypatch.setattr(usm_azure, "launchctl", fake)
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

        monkeypatch.setattr(usm_azure, "run", fake_run)
        usm_azure.systemctl("is-active", "x")
        assert seen["argv"][:2] == ["systemctl", "--user"]
        usm_azure.launchctl("print", "y")
        assert seen["argv"][0] == "launchctl"

    def test_exclude_directory_glob_becomes_a_regex(self):
        flags = ExcludeSpec.build(["build-*/"], defaults=False).to_azcopy_flags()
        assert "--exclude-regex" in flags
        assert "--exclude-path" not in flags

    def test_exclude_leading_dot_slash_is_normalised(self):
        spec = ExcludeSpec.build(["*.log"], defaults=False)
        assert spec.matches("./a.log") is True
        assert spec.matches("././a.log") is True

    def test_exclude_empty_pattern_list(self):
        spec = ExcludeSpec.build([], defaults=False)
        assert spec.patterns == ()
        assert spec.matches("anything") is False
        assert spec.to_azcopy_flags() == []

    def test_exclude_blank_patterns_are_dropped(self):
        spec = ExcludeSpec.build(["  ", "", "*.log"], defaults=False)
        assert spec.patterns == ("*.log",)

    def test_exclude_duplicates_are_collapsed(self):
        spec = ExcludeSpec.build(["*.log", "*.log"], defaults=False)
        assert spec.patterns == ("*.log",)

    def test_parse_iso8601_naive_timestamp_is_utc(self):
        assert parse_iso8601("2030-01-02T03:04:05") == parse_iso8601(
            "2030-01-02T03:04:05Z"
        )

    def test_blobfuse_mounts_survives_a_vanishing_process(self, monkeypatch):
        import sys
        import types

        class Boom:
            @property
            def info(self):
                raise RuntimeError("process went away")

        fake = types.SimpleNamespace(
            process_iter=lambda _a: [Boom()],
            NoSuchProcess=RuntimeError,
            AccessDenied=RuntimeError,
        )
        monkeypatch.setitem(sys.modules, "psutil", fake)
        assert usm_azure.blobfuse_mounts() == {}

    def test_blobfuse_mounts_skips_invalid_yaml(self, tmp_path, monkeypatch):
        import sys
        import types

        bad = tmp_path / "bad.yaml"
        bad.write_text("azstorage: [unclosed\n")

        class Proc:
            info = {
                "cmdline": ["blobfuse2", "mount", "/mnt/x", "-c", str(bad)],
                "pid": 1,
            }

        fake = types.SimpleNamespace(
            process_iter=lambda _a: [Proc()],
            NoSuchProcess=Exception,
            AccessDenied=Exception,
        )
        monkeypatch.setitem(sys.modules, "psutil", fake)
        assert usm_azure.blobfuse_mounts() == {}

    def test_cache_is_reused_from_disk_by_a_new_manager(self, tmp_path):
        """A restarted process must pick the cached token back up."""

        class P(usm_azure.SasProvider):
            kind = "p"

            def __init__(self):
                self.calls = 0

            def fetch(self):
                self.calls += 1
                return sas_for(7200), None

        cache = tmp_path / "c"
        first = SasManager(P(), cache)
        token = first.ensure(time.time())

        second_provider = P()
        second = SasManager(second_provider, cache)
        assert second.current().token == token.token
        assert second.ensure(time.time()).token == token.token
        assert second_provider.calls == 0, "should not re-mint a healthy token"

    def test_exclude_matches_a_nested_path_pattern(self):
        spec = ExcludeSpec.build(["build/cache"], defaults=False)
        assert spec.matches("build/cache/deep/x.o") is True
        assert spec.matches("other/build/cache") is False

    def test_exclude_matches_a_bare_name_anywhere(self):
        spec = ExcludeSpec.build(["target"], defaults=False)
        assert spec.matches("a/target/b.class") is True
        assert spec.matches("target") is True
