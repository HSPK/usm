"""Tests for scripts/blobmount.py.

blobfuse2 is replaced by a scripted fake (via ``$USM_BLOBFUSE2_BIN``) and the
mountpoint check is stubbed, so mounting, health probing, SAS rotation and the
supervisor loop all run for real without FUSE or Azure. Error paths get equal
billing: failed mounts, expired credentials, stale endpoints, busy unmounts
and missing binaries.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

import blobmount
import usm_azure
from blobmount import (
    DENIED,
    MISSING,
    OK,
    STALE,
    UNMOUNTED,
    BlobmountError,
    Mount,
    MountState,
    MountSupervisor,
    build_mount_argv,
    make_mount_id,
    parse_target,
    probe_mount,
    render_config,
)
from usm_azure import SasError, SasManager, human_duration


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect every on-disk location, including for spawned children."""
    root = tmp_path / "blobmount"
    cache = tmp_path / "cache"
    config = tmp_path / "config"
    for path in (root, cache, config):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(blobmount, "STATE_DIR", root)
    monkeypatch.setattr(blobmount, "CACHE_ROOT", cache)
    monkeypatch.setattr(blobmount, "CONFIG_ROOT", config)
    monkeypatch.setenv("USM_BLOBMOUNT_STATE_DIR", str(root))
    monkeypatch.setenv("USM_BLOBMOUNT_CACHE_DIR", str(cache))
    monkeypatch.setenv("USM_BLOBMOUNT_CONFIG_DIR", str(config))
    return root


def make_mount(tmp_path, *, create: bool = True, **overrides) -> Mount:
    mount_dir = overrides.pop("mount_dir", None) or (tmp_path / "mnt")
    if create:
        Path(mount_dir).mkdir(parents=True, exist_ok=True)
    mount = Mount(
        id=overrides.pop("id", "data"),
        mount_dir=str(mount_dir),
        account=overrides.pop("account", "acct"),
        container=overrides.pop("container", "bucket"),
    )
    for key, value in overrides.items():
        setattr(mount, key, value)
    return mount


def sas_for(expires_in: float, *, now: float | None = None) -> str:
    import datetime

    now = now if now is not None else time.time()
    stamp = datetime.datetime.fromtimestamp(
        now + expires_in, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"sv=2021-08-06&se={stamp}&sp=racwdl&sig=SECRET123"


FAKE_BLOBFUSE = '''#!/usr/bin/env python3
"""Scripted blobfuse2. $FAKE_BLOBFUSE_PLAN holds the responses."""
import json, os, sys

plan_path = os.environ["FAKE_BLOBFUSE_PLAN"]
with open(plan_path) as fh:
    plan = json.load(fh)

calls_path = plan_path + ".calls"
calls = json.load(open(calls_path)) if os.path.exists(calls_path) else []
calls.append(sys.argv[1:])
with open(calls_path, "w") as fh:
    json.dump(calls, fh)

verb = sys.argv[1] if len(sys.argv) > 1 else ""
steps = plan.get(verb, plan.get("default", [{"exit": 0}]))
seen = len([c for c in calls if c and c[0] == verb])
step = steps[min(seen - 1, len(steps) - 1)]

if step.get("stdout"):
    print(step["stdout"])
if step.get("stderr"):
    print(step["stderr"], file=sys.stderr)
if verb == "mount" and step.get("exit", 0) == 0:
    # Record that a mount "happened" so the test can flip is_mountpoint.
    marker = os.environ.get("FAKE_MOUNT_MARKER")
    if marker:
        with open(marker, "w") as fh:
            fh.write(sys.argv[2])
if verb == "unmount" and step.get("exit", 0) == 0:
    marker = os.environ.get("FAKE_MOUNT_MARKER")
    if marker and os.path.exists(marker):
        os.unlink(marker)
sys.exit(step.get("exit", 0))
'''


@pytest.fixture
def fake_blobfuse(tmp_path, monkeypatch):
    """Install a scripted blobfuse2 and a marker-driven mountpoint check."""
    binary = tmp_path / "fake-blobfuse2"
    binary.write_text(FAKE_BLOBFUSE)
    binary.chmod(0o755)
    plan_path = tmp_path / "bf_plan.json"
    marker = tmp_path / "mounted.marker"
    monkeypatch.setenv("USM_BLOBFUSE2_BIN", str(binary))
    monkeypatch.setenv("FAKE_BLOBFUSE_PLAN", str(plan_path))
    monkeypatch.setenv("FAKE_MOUNT_MARKER", str(marker))
    # No fusermount fallback in tests: keep the fake in charge.
    monkeypatch.setattr(blobmount.shutil, "which", lambda name: None)

    class Fake:
        path = str(binary)
        marker_path = marker

        def program(self, **verbs):
            plan_path.write_text(json.dumps(verbs))

        @property
        def calls(self):
            path = Path(str(plan_path) + ".calls")
            return json.loads(path.read_text()) if path.exists() else []

        def calls_for(self, verb):
            return [c for c in self.calls if c and c[0] == verb]

        def set_mounted(self, value: bool):
            if value:
                marker.write_text("mounted")
            else:
                marker.unlink(missing_ok=True)

    fake = Fake()
    fake.program(default=[{"exit": 0}])
    monkeypatch.setattr(blobmount, "is_mountpoint", lambda _path: marker.exists())
    return fake


@pytest.fixture
def stub_sas(tmp_path):
    """A SasManager backed by a scripted provider."""

    class Provider(usm_azure.SasProvider):
        kind = "stub"

        def __init__(self, tokens):
            self.tokens = list(tokens)
            self.calls = 0

        def fetch(self):
            self.calls += 1
            value = self.tokens[min(self.calls - 1, len(self.tokens) - 1)]
            if isinstance(value, Exception):
                raise value
            return value, None

    def build(*tokens, min_remaining=1800.0):
        provider = Provider(tokens or [sas_for(7200)])
        manager = SasManager(
            provider, tmp_path / "sas.json", min_remaining=min_remaining
        )
        return manager, provider

    return build


# --- Config rendering ------------------------------------------------------


class TestRenderConfig:
    def test_shape_matches_blobfuse2(self, tmp_path):
        import yaml

        mount = make_mount(tmp_path)
        config = yaml.safe_load(render_config(mount, sas_for(3600)))
        assert config["components"] == [
            "libfuse",
            "file_cache",
            "attr_cache",
            "azstorage",
        ]
        az = config["azstorage"]
        assert az["account-name"] == "acct"
        assert az["container"] == "bucket"
        assert az["mode"] == "sas"
        assert az["endpoint"] == "https://acct.blob.core.windows.net/"
        assert az["sas"].startswith("sv=")

    def test_sas_question_mark_is_stripped(self, tmp_path):
        import yaml

        config = yaml.safe_load(render_config(make_mount(tmp_path), "?sv=1&sig=abc"))
        assert config["azstorage"]["sas"] == "sv=1&sig=abc"

    def test_aad_mode_has_no_sas(self, tmp_path):
        import yaml

        config = yaml.safe_load(render_config(make_mount(tmp_path), None))
        assert config["azstorage"]["mode"] == "azcli"
        assert "sas" not in config["azstorage"]

    def test_read_only_and_allow_other(self, tmp_path):
        import yaml

        mount = make_mount(tmp_path, read_only=True, allow_other=False)
        config = yaml.safe_load(render_config(mount, None))
        assert config["read-only"] is True
        assert config["allow-other"] is False

    def test_cache_options(self, tmp_path):
        import yaml

        mount = make_mount(tmp_path, cache_size_mb=4096, cache_dir=str(tmp_path / "c"))
        config = yaml.safe_load(render_config(mount, None))
        assert config["file_cache"]["max-size-mb"] == 4096
        assert config["file_cache"]["path"] == str(tmp_path / "c")

    def test_default_cache_path_is_per_container(self, tmp_path, state_dir):
        mount = make_mount(tmp_path)
        assert mount.cache_path().name == "acct-bucket"

    def test_config_written_owner_only(self, tmp_path, state_dir):
        mount = make_mount(tmp_path)
        path = blobmount.write_config(mount, sas_for(3600))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert "SECRET123" in path.read_text()  # the real token is on disk

    def test_config_command_redacts(self, tmp_path, state_dir):
        mount = make_mount(tmp_path)
        rendered = render_config(mount, "sv=1&sig=TOPSECRET")
        assert "TOPSECRET" in rendered
        assert "TOPSECRET" not in usm_azure.redact(rendered)


class TestMountArgv:
    def test_basic(self, tmp_path, state_dir):
        mount = make_mount(tmp_path)
        argv = build_mount_argv(mount, "/bin/blobfuse2", Path("/tmp/c.yaml"))
        assert argv[:2] == ["/bin/blobfuse2", "mount"]
        assert argv[2] == mount.mount_dir
        assert argv[argv.index("--config-file") + 1] == "/tmp/c.yaml"
        assert "--allow-other" in argv

    def test_no_allow_other(self, tmp_path, state_dir):
        mount = make_mount(tmp_path, allow_other=False)
        assert "--allow-other" not in build_mount_argv(mount, "b", Path("c"))

    def test_read_only_and_extras(self, tmp_path, state_dir):
        mount = make_mount(tmp_path, read_only=True, extra_args=["--foo", "1"])
        argv = build_mount_argv(mount, "b", Path("c"))
        assert "--read-only" in argv and argv[-2:] == ["--foo", "1"]


# --- Target parsing / ids --------------------------------------------------


class TestParseTarget:
    def test_account_and_container(self):
        assert parse_target("acct", "bucket") == ("acct", "bucket")

    def test_container_url(self):
        assert parse_target("https://acct.blob.core.windows.net/bucket", None) == (
            "acct",
            "bucket",
        )

    def test_url_with_sas_is_stripped(self):
        url = f"https://acct.blob.core.windows.net/bucket?{sas_for(60)}"
        assert parse_target(url, None) == ("acct", "bucket")

    def test_missing_container_rejected(self):
        with pytest.raises(BlobmountError):
            parse_target("acct", None)

    def test_nothing_rejected(self):
        with pytest.raises(BlobmountError):
            parse_target(None, None)


class TestMountIds:
    def test_derived_from_the_directory(self, tmp_path, state_dir):
        assert make_mount_id(tmp_path / "data", "bucket") == "data"

    def test_deduplicated(self, tmp_path, state_dir):
        blobmount.save_mount(make_mount(tmp_path, id="data"))
        assert make_mount_id(tmp_path / "data", "bucket") == "data-2"

    def test_custom_is_slugified(self, tmp_path, state_dir):
        assert make_mount_id(tmp_path, "b", "My Mount!") == "my-mount"

    def test_falls_back_to_the_container(self, tmp_path, state_dir):
        assert make_mount_id(Path("/"), "bucket") == "bucket"


# --- Health probing --------------------------------------------------------


class TestProbeMount:
    def test_missing_directory(self, tmp_path, monkeypatch):
        mount = make_mount(tmp_path, mount_dir=tmp_path / "mnt")
        Path(mount.mount_dir).rmdir()
        monkeypatch.setattr(blobmount, "is_mountpoint", lambda _p: False)
        assert probe_mount(mount)[0] == MISSING

    def test_missing_parent(self, tmp_path):
        mount = make_mount(tmp_path, create=False, mount_dir=tmp_path / "nope" / "mnt")
        assert probe_mount(mount)[0] == MISSING

    def test_not_mounted(self, tmp_path, monkeypatch):
        mount = make_mount(tmp_path)
        monkeypatch.setattr(blobmount, "is_mountpoint", lambda _p: False)
        state, detail = probe_mount(mount)
        assert state == UNMOUNTED and "mountpoint" in detail

    def test_healthy(self, tmp_path, monkeypatch):
        mount = make_mount(tmp_path)
        monkeypatch.setattr(blobmount, "is_mountpoint", lambda _p: True)
        assert probe_mount(mount) == (OK, "readable")

    def test_stale_endpoint(self, tmp_path, monkeypatch):
        """A dead FUSE mount answers ENOTCONN — mounted, but useless."""
        import errno

        mount = make_mount(tmp_path)
        monkeypatch.setattr(blobmount, "is_mountpoint", lambda _p: True)

        def boom(_path):
            raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")

        monkeypatch.setattr(blobmount.os, "listdir", boom)
        state, detail = probe_mount(mount)
        assert state == STALE and "not connected" in detail

    def test_permission_denied_looks_like_an_expired_sas(self, tmp_path, monkeypatch):
        import errno

        mount = make_mount(tmp_path)
        monkeypatch.setattr(blobmount, "is_mountpoint", lambda _p: True)

        def boom(_path):
            raise OSError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(blobmount.os, "listdir", boom)
        assert probe_mount(mount)[0] == DENIED

    def test_hung_mount_times_out(self, tmp_path, monkeypatch):
        mount = make_mount(tmp_path)
        monkeypatch.setattr(blobmount, "is_mountpoint", lambda _p: True)

        def hang(_path):
            time.sleep(5)

        monkeypatch.setattr(blobmount.os, "listdir", hang)
        started = time.time()
        state, detail = probe_mount(mount, timeout=0.4)
        assert state == DENIED and "timed out" in detail
        assert time.time() - started < 3, "probe did not honour its timeout"

    def test_is_mountpoint_survives_enotconn(self, tmp_path, monkeypatch):
        def boom(_path):
            raise OSError(107, "Transport endpoint is not connected")

        monkeypatch.setattr(blobmount.os.path, "ismount", boom)
        assert blobmount.is_mountpoint(tmp_path) is True


# --- Mount / unmount -------------------------------------------------------


class TestDoMount:
    def test_writes_config_and_calls_blobfuse(self, tmp_path, state_dir, fake_blobfuse):
        mount = make_mount(tmp_path)
        config, _ = blobmount.do_mount(mount, sas_for(3600))
        assert config.exists()
        call = fake_blobfuse.calls_for("mount")[0]
        assert call[1] == mount.mount_dir
        assert "--allow-other" in call
        assert mount.cache_path().exists()

    def test_mount_failure_is_reported(self, tmp_path, state_dir, fake_blobfuse):
        fake_blobfuse.program(
            mount=[{"exit": 1, "stderr": "failed to authenticate sig=SECRET123"}]
        )
        mount = make_mount(tmp_path)
        with pytest.raises(BlobmountError) as exc:
            blobmount.do_mount(mount, sas_for(3600))
        assert "SECRET123" not in str(exc.value), "SAS leaked into the error"
        assert "sig=***" in str(exc.value)

    def test_unwritable_parent_is_rejected(self, tmp_path, state_dir, fake_blobfuse):
        parent = tmp_path / "locked"
        parent.mkdir()
        parent.chmod(0o500)
        mount = make_mount(tmp_path, create=False, mount_dir=parent / "mnt")
        try:
            with pytest.raises(BlobmountError, match="not writable"):
                blobmount.do_mount(mount, None)
        finally:
            parent.chmod(0o700)

    def test_missing_binary(self, tmp_path, state_dir, monkeypatch):
        monkeypatch.delenv("USM_BLOBFUSE2_BIN", raising=False)
        monkeypatch.setattr(blobmount.shutil, "which", lambda _n: None)
        with pytest.raises(BlobmountError, match="blobmount install"):
            blobmount.ensure_blobfuse2()

    def test_env_override_wins(self, tmp_path, monkeypatch):
        binary = tmp_path / "bf"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setenv("USM_BLOBFUSE2_BIN", str(binary))
        assert blobmount.find_blobfuse2() == str(binary)

    def test_non_executable_override_ignored(self, tmp_path, monkeypatch):
        binary = tmp_path / "bf"
        binary.write_text("")
        binary.chmod(0o644)
        monkeypatch.setenv("USM_BLOBFUSE2_BIN", str(binary))
        monkeypatch.setattr(blobmount.shutil, "which", lambda _n: None)
        assert blobmount.find_blobfuse2() is None


class TestDoUnmount:
    def test_success(self, tmp_path, state_dir, fake_blobfuse):
        fake_blobfuse.set_mounted(True)
        mount = make_mount(tmp_path)
        assert blobmount.do_unmount(mount) is True
        assert fake_blobfuse.calls_for("unmount")

    def test_failure_when_still_mounted(self, tmp_path, state_dir, fake_blobfuse):
        fake_blobfuse.program(unmount=[{"exit": 1, "stderr": "device is busy"}])
        fake_blobfuse.set_mounted(True)
        assert blobmount.do_unmount(make_mount(tmp_path)) is False

    def test_reports_success_if_already_gone(self, tmp_path, state_dir, fake_blobfuse):
        fake_blobfuse.program(unmount=[{"exit": 1}])
        fake_blobfuse.set_mounted(False)
        assert blobmount.do_unmount(make_mount(tmp_path)) is True


# --- Supervisor ------------------------------------------------------------


class _Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, secs):
        self.now += secs


def build_supervisor(tmp_path, fake_blobfuse, stub_sas, *tokens, **mount_kw):
    mount = make_mount(tmp_path, **mount_kw)
    blobmount.save_mount(mount)
    clock = _Clock()
    manager, provider = stub_sas(*(tokens or (sas_for(7200, now=clock.now),)))
    supervisor = MountSupervisor(mount, sas=manager, clock=clock, log=lambda _m: None)
    return supervisor, clock, provider


class TestSupervisorDecisions:
    def test_healthy_with_fresh_sas_does_nothing(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, clock, provider = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        sup.sas.ensure(clock.now)
        sup.state.health = OK
        needed, _ = sup.needs_refresh(clock.now)
        assert needed is False

    def test_expiring_sas_triggers_a_rotation(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, clock, _ = build_supervisor(
            tmp_path, fake_blobfuse, stub_sas, sas_for(600, now=1_000_000.0)
        )
        sup.sas.ensure(clock.now)
        sup.state.health = OK
        needed, reason = sup.needs_refresh(clock.now)
        assert needed is True and "expires" in reason

    @pytest.mark.parametrize("health", [STALE, DENIED, UNMOUNTED, MISSING])
    def test_unhealthy_triggers_a_remount(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas, health
    ):
        sup, clock, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        sup.sas.ensure(clock.now)
        sup.state.health = health
        needed, reason = sup.needs_refresh(clock.now)
        assert needed is True and health in reason

    def test_manual_request_wins(self, tmp_path, state_dir, fake_blobfuse, stub_sas):
        sup, clock, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        sup.state.health = OK
        sup.sas.ensure(clock.now)
        sup.request_refresh()
        assert sup.needs_refresh(clock.now) == (True, "manual")

    def test_no_cached_sas_forces_one(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, clock, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        sup.state.health = OK
        assert sup.needs_refresh(clock.now) == (True, "no cached SAS")

    def test_aad_never_rotates(self, tmp_path, state_dir, fake_blobfuse):
        mount = make_mount(tmp_path, auth="aad")
        blobmount.save_mount(mount)
        sup = MountSupervisor(mount, clock=_Clock(), log=lambda _m: None)
        sup.state.health = OK
        assert sup.needs_refresh(time.time()) == (False, "")

    def test_deadline_is_the_earliest_of_probe_and_expiry(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, clock, _ = build_supervisor(
            tmp_path,
            fake_blobfuse,
            stub_sas,
            sas_for(3600, now=1_000_000.0),
            probe_interval=60.0,
            sas_min_remaining=1800.0,
        )
        sup.sas.ensure(clock.now)
        # probe in 60s beats the SAS deadline (3600-1800 = 1800s away).
        assert sup.next_deadline(clock.now) == pytest.approx(clock.now + 60, abs=2)

    def test_deadline_never_goes_backwards(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, clock, _ = build_supervisor(
            tmp_path, fake_blobfuse, stub_sas, sas_for(10, now=1_000_000.0)
        )
        sup.sas.ensure(clock.now)
        assert sup.next_deadline(clock.now) >= clock.now + 1


class TestSupervisorRemount:
    def test_successful_rotation(self, tmp_path, state_dir, fake_blobfuse, stub_sas):
        sup, clock, provider = build_supervisor(
            tmp_path,
            fake_blobfuse,
            stub_sas,
            sas_for(600, now=1_000_000.0),
            sas_for(7200, now=1_000_000.0),
        )
        fake_blobfuse.set_mounted(True)
        assert sup.remount("test") is True
        assert provider.calls >= 1
        assert fake_blobfuse.calls_for("unmount"), "should unmount before remounting"
        assert fake_blobfuse.calls_for("mount")
        assert sup.state.state == "mounted"
        assert sup.state.refreshes == 1 and sup.state.remounts == 1
        assert sup.state.health == OK

    def test_config_carries_the_new_token(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        import yaml

        fresh = sas_for(7200, now=1_000_000.0)
        sup, _, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas, fresh)
        sup.remount("test")
        config = yaml.safe_load(sup.mount.config_path().read_text())
        assert config["azstorage"]["sas"] == fresh

    def test_no_unmount_when_not_mounted(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, _, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        fake_blobfuse.set_mounted(False)
        sup.remount("initial")
        assert fake_blobfuse.calls_for("unmount") == []

    def test_sas_failure_aborts_before_touching_the_mount(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, _, _ = build_supervisor(
            tmp_path, fake_blobfuse, stub_sas, SasError("no credentials")
        )
        fake_blobfuse.set_mounted(True)
        assert sup.remount("test") is False
        assert sup.state.state == "failed"
        assert "no credentials" in sup.state.last_error
        assert fake_blobfuse.calls == [], "must not unmount when it can't remount"

    def test_mount_failure_is_recorded(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        fake_blobfuse.program(
            unmount=[{"exit": 0}], mount=[{"exit": 1, "stderr": "boom"}]
        )
        sup, _, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        assert sup.remount("test") is False
        assert sup.state.state == "failed"
        assert sup.state.failures == 1
        assert "boom" in sup.state.last_error

    def test_unmount_failure_still_attempts_the_mount(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        fake_blobfuse.program(unmount=[{"exit": 1}], mount=[{"exit": 0}])
        fake_blobfuse.set_mounted(True)
        logs = []
        sup, _, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        sup._log = logs.append
        assert sup.remount("test") is True
        assert any("unmount reported failure" in m for m in logs)

    def test_forced_flag_is_cleared_after_success(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, clock, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        sup.request_refresh()
        sup.remount("manual")
        sup.state.health = OK
        assert sup.needs_refresh(clock.now)[0] is False

    def test_state_is_persisted(self, tmp_path, state_dir, fake_blobfuse, stub_sas):
        sup, _, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        sup.remount("test")
        persisted = blobmount.load_state(sup.mount.id)
        assert persisted.state == "mounted" and persisted.refreshes == 1
        assert persisted.sas_expires_at is not None


class TestSupervisorTick:
    def test_tick_is_a_noop_when_healthy(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, clock, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        sup.sas.ensure(clock.now)
        fake_blobfuse.set_mounted(True)
        assert sup.tick() is None
        assert fake_blobfuse.calls_for("mount") == []
        assert sup.state.state == "mounted"

    def test_tick_remounts_a_stale_mount(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas, monkeypatch
    ):
        import errno

        sup, clock, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        sup.sas.ensure(clock.now)
        fake_blobfuse.set_mounted(True)
        calls = {"n": 0}

        def flaky(_path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")

        monkeypatch.setattr(blobmount.os, "listdir", flaky)
        reason = sup.tick()
        assert reason and STALE in reason
        assert fake_blobfuse.calls_for("mount")

    def test_tick_rotates_an_expiring_sas(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, clock, provider = build_supervisor(
            tmp_path,
            fake_blobfuse,
            stub_sas,
            sas_for(300, now=1_000_000.0),
            sas_for(7200, now=1_000_000.0),
        )
        sup.sas.ensure(clock.now)
        fake_blobfuse.set_mounted(True)
        reason = sup.tick()
        assert reason and "expires" in reason
        assert provider.calls == 2

    def test_tick_records_the_next_deadline(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, clock, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        sup.sas.ensure(clock.now)
        fake_blobfuse.set_mounted(True)
        sup.tick()
        assert sup.state.next_refresh_at > clock.now


class TestSupervisorRun:
    def test_mounts_then_stops_cleanly(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, _, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        fake_blobfuse.set_mounted(False)
        sup.request_stop()  # exit after the initial mount
        assert sup.run() == 0
        assert fake_blobfuse.calls_for("mount")
        assert blobmount.load_state(sup.mount.id).state == "stopped"

    def test_initial_mount_failure_exits_nonzero(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        fake_blobfuse.program(mount=[{"exit": 1, "stderr": "nope"}])
        sup, _, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        fake_blobfuse.set_mounted(False)
        assert sup.run() == 1
        assert blobmount.load_state(sup.mount.id).state == "failed"

    def test_already_healthy_skips_the_mount(
        self, tmp_path, state_dir, fake_blobfuse, stub_sas
    ):
        sup, _, _ = build_supervisor(tmp_path, fake_blobfuse, stub_sas)
        fake_blobfuse.set_mounted(True)
        sup.request_stop()
        assert sup.run() == 0
        assert fake_blobfuse.calls_for("mount") == []


# --- Store -----------------------------------------------------------------


class TestStore:
    def test_roundtrip(self, tmp_path, state_dir):
        mount = make_mount(tmp_path, id="m1", sas_spec="X", read_only=True)
        blobmount.save_mount(mount)
        assert blobmount.load_mount("m1") == mount

    def test_unknown(self, state_dir):
        with pytest.raises(KeyError):
            blobmount.load_mount("ghost")

    def test_list_skips_state_files(self, tmp_path, state_dir):
        blobmount.save_mount(make_mount(tmp_path, id="m1"))
        blobmount.save_state("m1", MountState(state="mounted"))
        assert [m.id for m in blobmount.list_mounts()] == ["m1"]

    def test_list_skips_corrupt(self, tmp_path, state_dir):
        blobmount.save_mount(make_mount(tmp_path, id="ok"))
        (state_dir / "bad.json").write_text("{nope")
        assert [m.id for m in blobmount.list_mounts()] == ["ok"]

    def test_state_defaults(self, state_dir):
        assert blobmount.load_state("ghost").state == "stopped"

    def test_delete_removes_everything(self, tmp_path, state_dir):
        blobmount.save_mount(make_mount(tmp_path, id="gone"))
        blobmount.save_state("gone", MountState())
        blobmount.sas_path("gone").write_text("{}")
        blobmount.delete_mount("gone")
        assert not list(state_dir.glob("gone*"))

    def test_sas_manager_uses_the_mount_id(self, tmp_path, state_dir):
        mount = make_mount(tmp_path, id="m1", auth="aad")
        manager = blobmount.mount_sas_manager(mount)
        assert manager.cache_path == blobmount.sas_path("m1")


# --- CLI -------------------------------------------------------------------


@pytest.fixture
def runner():
    import click.testing

    return click.testing.CliRunner()


def invoke(runner, args, **kw):
    return runner.invoke(blobmount.cli, args, **kw)


@pytest.fixture
def env_sas(monkeypatch):
    monkeypatch.setenv("BM_SAS", sas_for(7200))
    return ["--sas-env", "BM_SAS"]


class TestCliMount:
    def test_mount_defines_and_mounts(
        self, tmp_path, state_dir, fake_blobfuse, runner, env_sas
    ):
        target = tmp_path / "mnt"
        result = invoke(
            runner,
            ["mount", str(target), "acct", "bucket", "--no-supervise", *env_sas],
        )
        assert result.exit_code == 0, result.output
        mount = blobmount.load_mount("mnt")
        assert mount.account == "acct" and mount.container == "bucket"
        assert mount.auth == "env"
        assert fake_blobfuse.calls_for("mount")

    def test_mount_accepts_a_url(
        self, tmp_path, state_dir, fake_blobfuse, runner, env_sas
    ):
        result = invoke(
            runner,
            [
                "mount",
                str(tmp_path / "mnt"),
                "https://acct.blob.core.windows.net/bucket",
                "--no-supervise",
                *env_sas,
            ],
        )
        assert result.exit_code == 0, result.output
        assert blobmount.load_mount("mnt").container == "bucket"

    def test_refuses_an_existing_mountpoint(
        self, tmp_path, state_dir, fake_blobfuse, runner, env_sas
    ):
        fake_blobfuse.set_mounted(True)
        result = invoke(
            runner,
            [
                "mount",
                str(tmp_path / "mnt"),
                "acct",
                "bucket",
                "--no-supervise",
                *env_sas,
            ],
        )
        assert result.exit_code != 0
        assert "already a mountpoint" in result.output

    def test_refuses_a_duplicate_definition(
        self, tmp_path, state_dir, fake_blobfuse, runner, env_sas
    ):
        target = tmp_path / "mnt"
        args = ["mount", str(target), "acct", "bucket", "--no-supervise", *env_sas]
        assert invoke(runner, args).exit_code == 0
        second = invoke(runner, args)
        assert second.exit_code != 0 and "already managed" in second.output

    def test_conflicting_sas_flags(self, tmp_path, state_dir, fake_blobfuse, runner):
        result = invoke(
            runner,
            [
                "mount",
                str(tmp_path / "mnt"),
                "acct",
                "bucket",
                "--sas-command",
                "a",
                "--sas-url",
                "https://b",
                "--no-supervise",
            ],
        )
        assert result.exit_code != 0 and "pick one SAS source" in result.output

    def test_auth_conflict(self, tmp_path, state_dir, fake_blobfuse, runner):
        result = invoke(
            runner,
            [
                "mount",
                str(tmp_path / "mnt"),
                "acct",
                "bucket",
                "--auth",
                "env",
                "--sas-command",
                "a",
                "--no-supervise",
            ],
        )
        assert result.exit_code != 0 and "conflicts" in result.output

    def test_missing_spec_for_external_auth(
        self, tmp_path, state_dir, fake_blobfuse, runner
    ):
        result = invoke(
            runner,
            [
                "mount",
                str(tmp_path / "mnt"),
                "acct",
                "bucket",
                "--auth",
                "http",
                "--no-supervise",
            ],
        )
        assert result.exit_code != 0 and "--sas-url" in result.output

    def test_options_reach_the_definition(
        self, tmp_path, state_dir, fake_blobfuse, runner, env_sas
    ):
        result = invoke(
            runner,
            [
                "mount",
                str(tmp_path / "mnt"),
                "acct",
                "bucket",
                "--no-supervise",
                "--read-only",
                "--no-allow-other",
                "--cache-size-mb",
                "512",
                "--refresh-interval",
                "900",
                "--probe-interval",
                "30",
                *env_sas,
            ],
        )
        assert result.exit_code == 0, result.output
        mount = blobmount.load_mount("mnt")
        assert mount.read_only is True and mount.allow_other is False
        assert mount.cache_size_mb == 512
        assert mount.refresh_interval == 900 and mount.probe_interval == 30

    def test_sas_failure_surfaces(self, tmp_path, state_dir, fake_blobfuse, runner):
        result = invoke(
            runner,
            [
                "mount",
                str(tmp_path / "mnt"),
                "acct",
                "bucket",
                "--no-supervise",
                "--sas-command",
                "exit 7",
            ],
        )
        assert result.exit_code != 0
        assert "exited 7" in result.output


class TestCliInspection:
    def _define(self, tmp_path, state_dir, **kw):
        mount = make_mount(tmp_path, **kw)
        blobmount.save_mount(mount)
        blobmount.save_state(mount.id, MountState(state="mounted"))
        return mount

    def test_ls_empty(self, state_dir, runner):
        result = invoke(runner, ["ls"])
        assert result.exit_code == 0 and "No mounts" in result.output

    def test_ls_lists(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, state_dir, id="data")
        result = invoke(runner, ["ls"])
        assert result.exit_code == 0 and "data" in result.output

    def test_ls_shows_health(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, state_dir, id="data")
        fake_blobfuse.set_mounted(True)
        assert "ok" in invoke(runner, ["ls"]).output

    def test_ls_includes_external_mounts(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        monkeypatch.setattr(
            blobmount,
            "blobfuse_mounts",
            lambda: {
                "/mnt/other": {
                    "url": "https://a.blob.core.windows.net/c/",
                    "account_name": "a",
                    "container_name": "c",
                    "config_file": "/tmp/x.yaml",
                    "pid": 1,
                }
            },
        )
        result = invoke(runner, ["ls", "--all"])
        assert "/mnt/other" in result.output and "external" in result.output

    def test_status(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, state_dir, id="data")
        result = invoke(runner, ["status", "data"])
        assert result.exit_code == 0
        assert "acct/bucket" in result.output and "health" in result.output

    def test_status_unknown_lists_known(self, tmp_path, state_dir, runner):
        self._define(tmp_path, state_dir, id="data")
        result = invoke(runner, ["status", "nope"])
        assert result.exit_code != 0 and "data" in result.output

    def test_status_shows_last_error_redacted(
        self, tmp_path, state_dir, fake_blobfuse, runner
    ):
        self._define(tmp_path, state_dir, id="data")
        blobmount.save_state("data", MountState(last_error="failed with sig=SECRET123"))
        result = invoke(runner, ["status", "data"])
        assert "SECRET123" not in result.output and "sig=***" in result.output

    def test_check_ok(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, state_dir, id="data")
        fake_blobfuse.set_mounted(True)
        result = invoke(runner, ["check", "data"])
        assert result.exit_code == 0 and "ok" in result.output

    def test_check_bad_exits_nonzero(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, state_dir, id="data")
        fake_blobfuse.set_mounted(False)
        result = invoke(runner, ["check", "data"])
        assert result.exit_code == 1
        assert "unmounted" in result.output

    def test_check_without_mounts(self, state_dir, runner):
        result = invoke(runner, ["check"])
        assert result.exit_code != 0 and "no mounts" in result.output

    def test_config_is_redacted(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, state_dir, id="data")
        result = invoke(runner, ["config", "data"])
        assert result.exit_code == 0
        assert "sig=***" in result.output
        assert "account-name: acct" in result.output

    def test_logs_missing(self, tmp_path, state_dir, runner):
        self._define(tmp_path, state_dir, id="data")
        result = invoke(runner, ["logs", "data"])
        assert result.exit_code != 0 and "no log" in result.output

    def test_logs_redacts(self, tmp_path, state_dir, runner):
        self._define(tmp_path, state_dir, id="data")
        blobmount.log_path("data").write_text("mounted with sig=SECRET123\n")
        result = invoke(runner, ["logs", "data"])
        assert "SECRET123" not in result.output and "sig=***" in result.output


class TestCliLifecycle:
    def _define(self, tmp_path, state_dir, **kw):
        mount = make_mount(tmp_path, **kw)
        blobmount.save_mount(mount)
        blobmount.save_state(mount.id, MountState())
        return mount

    def test_umount_when_not_mounted(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, state_dir, id="data")
        fake_blobfuse.set_mounted(False)
        result = invoke(runner, ["umount", "data"])
        assert result.exit_code == 0 and "not mounted" in result.output

    def test_umount_success(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, state_dir, id="data")
        fake_blobfuse.set_mounted(True)
        result = invoke(runner, ["umount", "data"])
        assert result.exit_code == 0 and "Unmounted" in result.output

    def test_umount_busy_reports_clearly(
        self, tmp_path, state_dir, fake_blobfuse, runner
    ):
        self._define(tmp_path, state_dir, id="data")
        fake_blobfuse.program(unmount=[{"exit": 1, "stderr": "device is busy"}])
        fake_blobfuse.set_mounted(True)
        result = invoke(runner, ["umount", "data"])
        assert result.exit_code != 0
        assert "something is using it" in result.output

    def test_umount_lazy_flag_is_passed(
        self, tmp_path, state_dir, fake_blobfuse, runner
    ):
        self._define(tmp_path, state_dir, id="data")
        fake_blobfuse.set_mounted(True)
        invoke(runner, ["umount", "data", "--lazy"])
        assert fake_blobfuse.calls_for("unmount")

    def test_rm_unmounts_and_forgets(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, state_dir, id="data")
        fake_blobfuse.set_mounted(True)
        result = invoke(runner, ["rm", "data", "-y"])
        assert result.exit_code == 0
        assert blobmount.list_mounts() == []
        assert fake_blobfuse.calls_for("unmount")

    def test_rm_keep_mounted(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, state_dir, id="data")
        fake_blobfuse.set_mounted(True)
        invoke(runner, ["rm", "data", "-y", "--keep-mounted"])
        assert fake_blobfuse.calls_for("unmount") == []

    def test_stop_when_not_running(self, tmp_path, state_dir, runner):
        self._define(tmp_path, state_dir, id="data")
        result = invoke(runner, ["stop", "data"])
        assert result.exit_code == 0 and "was not supervised" in result.output

    def test_disable_when_not_enabled(self, tmp_path, state_dir, runner):
        self._define(tmp_path, state_dir, id="data")
        result = invoke(runner, ["disable", "data"])
        assert result.exit_code == 0 and "not enabled" in result.output

    def test_refresh_without_supervisor_remounts_here(
        self, tmp_path, state_dir, fake_blobfuse, runner, env_sas, monkeypatch
    ):
        monkeypatch.setenv("BM_SAS", sas_for(7200))
        self._define(tmp_path, state_dir, id="data", auth="env", sas_spec="BM_SAS")
        fake_blobfuse.set_mounted(True)
        result = invoke(runner, ["refresh", "data"])
        assert result.exit_code == 0, result.output
        assert "fresh SAS" in result.output
        assert fake_blobfuse.calls_for("mount")

    def test_refresh_failure_reports(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, state_dir, id="data", auth="exec", sas_spec="exit 3")
        result = invoke(runner, ["refresh", "data"])
        assert result.exit_code != 0
        assert "exited 3" in result.output


class TestServiceIntegration:
    def test_unit_names_are_namespaced(self):
        assert blobmount.SERVICE.unit_name("data") == "usm-blobmount-data.service"
        assert blobmount.SERVICE.label("data").endswith(".blobmount.data")

    def test_unit_runs_usm_blobmount_up(self, tmp_path, state_dir):
        unit = blobmount.SERVICE.render_unit(
            "usm blobmount data",
            "/usr/local/bin/usm blobmount up data",
            "/usr/local/bin/usm",
        )
        assert "ExecStart=/usr/local/bin/usm blobmount up data" in unit
        assert "Restart=always" in unit

    def test_plist_runs_usm_blobmount_up(self, tmp_path, state_dir):
        import plistlib

        payload = plistlib.loads(
            blobmount.SERVICE.render_plist(
                "data",
                ["/usr/local/bin/usm", "blobmount", "up", "data"],
                "/usr/local/bin/usm",
                log_path=blobmount.log_path("data"),
            )
        )
        assert payload["ProgramArguments"][1:] == ["blobmount", "up", "data"]
        assert payload["RunAtLoad"] is True

    def test_azsync_and_blobmount_units_do_not_collide(self):
        import azsync

        assert blobmount.SERVICE.unit_prefix != azsync.SERVICE.unit_prefix
        assert blobmount.SERVICE.label("x") != azsync.SERVICE.label("x")


# --- Live supervisor process ----------------------------------------------


def wait_until(predicate, timeout=25.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestLiveSupervisor:
    """Spawn the real supervisor process against the fake blobfuse2."""

    def _mount(self, tmp_path, monkeypatch, **kw):
        monkeypatch.setenv("BM_SAS", sas_for(7200))
        mount = make_mount(
            tmp_path,
            id="live",
            auth="env",
            sas_spec="BM_SAS",
            probe_interval=0.5,
            refresh_interval=3600.0,
            **kw,
        )
        blobmount.save_mount(mount)
        return mount

    def test_mounts_on_start_and_stops_cleanly(
        self, tmp_path, state_dir, fake_blobfuse, monkeypatch
    ):
        mount = self._mount(tmp_path, monkeypatch)
        fake_blobfuse.set_mounted(False)
        pid = blobmount.spawn_supervisor(mount)
        try:
            assert wait_until(
                lambda: blobmount.load_state("live").state == "mounted"
            ), "supervisor never mounted"
            assert fake_blobfuse.calls_for("mount")
            assert blobmount.supervisor_running("live") is True
        finally:
            blobmount.stop_supervisor("live")
        assert wait_until(lambda: not blobmount.pid_alive(pid), timeout=15)
        assert blobmount.load_state("live").state == "stopped"
        assert blobmount.supervisor_running("live") is False

    def test_second_supervisor_refuses(
        self, tmp_path, state_dir, fake_blobfuse, monkeypatch
    ):
        mount = self._mount(tmp_path, monkeypatch)
        fake_blobfuse.set_mounted(False)
        pid = blobmount.spawn_supervisor(mount)
        try:
            assert wait_until(lambda: blobmount.supervisor_running("live"))
            assert blobmount.run_supervisor("live") == 1
        finally:
            blobmount.stop_supervisor("live")
        wait_until(lambda: not blobmount.pid_alive(pid), timeout=15)

    def test_manual_refresh_signal_reaches_it(
        self, tmp_path, state_dir, fake_blobfuse, monkeypatch
    ):
        mount = self._mount(tmp_path, monkeypatch)
        fake_blobfuse.set_mounted(False)
        pid = blobmount.spawn_supervisor(mount)
        try:
            assert wait_until(lambda: blobmount.load_state("live").state == "mounted")
            before = blobmount.load_state("live").refreshes
            assert blobmount.poke_supervisor("live") is True
            assert wait_until(
                lambda: blobmount.load_state("live").refreshes > before
            ), "SIGUSR1 did not trigger a rotation"
        finally:
            blobmount.stop_supervisor("live")
        wait_until(lambda: not blobmount.pid_alive(pid), timeout=15)

    def test_recovers_a_mount_that_goes_stale(
        self, tmp_path, state_dir, fake_blobfuse, monkeypatch
    ):
        mount = self._mount(tmp_path, monkeypatch)
        fake_blobfuse.set_mounted(True)
        pid = blobmount.spawn_supervisor(mount)
        try:
            assert wait_until(lambda: blobmount.load_state("live").state == "mounted")
            # Simulate the mount disappearing under us.
            fake_blobfuse.set_mounted(False)
            assert wait_until(
                lambda: len(fake_blobfuse.calls_for("mount")) >= 1, timeout=15
            ), "supervisor did not remount after the mount vanished"
        finally:
            blobmount.stop_supervisor("live")
        wait_until(lambda: not blobmount.pid_alive(pid), timeout=15)


# --- Install path ----------------------------------------------------------


class TestInstall:
    def test_rejects_non_linux(self, monkeypatch):
        monkeypatch.setattr(blobmount.platform, "system", lambda: "Darwin")
        with pytest.raises(BlobmountError, match="Debian/Ubuntu"):
            blobmount.install_blobfuse2()

    def test_rejects_missing_dpkg(self, monkeypatch):
        monkeypatch.setattr(blobmount.platform, "system", lambda: "Linux")
        monkeypatch.setattr(blobmount.shutil, "which", lambda _n: None)
        with pytest.raises(BlobmountError, match="non-Debian"):
            blobmount.install_blobfuse2()

    def test_reports_a_download_failure(self, monkeypatch, tmp_path):
        import urllib.error
        import urllib.request

        monkeypatch.setattr(blobmount.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            blobmount.shutil, "which", lambda n: "/usr/bin/dpkg" if n else None
        )
        monkeypatch.setattr(blobmount.os, "geteuid", lambda: 0)
        monkeypatch.setattr(blobmount, "USM_CACHE_DIR", tmp_path)

        def boom(*a, **kw):
            raise urllib.error.URLError("no network")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        with pytest.raises(BlobmountError, match="download failed"):
            blobmount.install_blobfuse2()


class TestFormatting:
    @pytest.mark.parametrize(
        "value,expected", [(None, "-"), (45, "45s"), (3600, "1h00m")]
    )
    def test_human_duration(self, value, expected):
        assert human_duration(value) == expected

    def test_health_labels_cover_every_state(self):
        for state in (OK, STALE, DENIED, UNMOUNTED, MISSING):
            assert blobmount._health_label(state) != "[dim]-[/dim]"
        assert blobmount._health_label(None) == "[dim]-[/dim]"

    def test_sas_label_states(self, tmp_path, state_dir):
        mount = make_mount(tmp_path, id="m", auth="aad")
        blobmount.save_mount(mount)
        assert "entra" in blobmount._sas_label(mount)

        mount.auth = "az"
        blobmount.save_state("m", MountState(sas_expires_at=time.time() - 10))
        assert "expired" in blobmount._sas_label(mount)
        blobmount.save_state("m", MountState(sas_expires_at=time.time() + 100_000))
        assert "green" in blobmount._sas_label(mount)


# --- Error paths and boundaries -------------------------------------------


class TestStoreResilience:
    def test_corrupt_definition_is_skipped_not_fatal(self, tmp_path, state_dir):
        blobmount.save_mount(make_mount(tmp_path, id="good"))
        (state_dir / "broken.json").write_text('{"id": "broken", "mount_dir": 5,')
        assert [m.id for m in blobmount.list_mounts()] == ["good"]

    def test_definition_with_wrong_types_is_skipped(self, tmp_path, state_dir):
        (state_dir / "weird.json").write_text(json.dumps({"id": "weird"}))
        assert blobmount.list_mounts() == []

    def test_unknown_fields_are_ignored(self, tmp_path, state_dir):
        raw = json.loads(json.dumps(blobmount.asdict(make_mount(tmp_path, id="m"))))
        raw["from_the_future"] = True
        (state_dir / "m.json").write_text(json.dumps(raw))
        assert blobmount.load_mount("m").id == "m"

    def test_corrupt_state_falls_back_to_defaults(self, tmp_path, state_dir):
        (state_dir / "m.state.json").write_text("{oops")
        assert blobmount.load_state("m").state == "stopped"

    def test_state_with_wrong_types_falls_back(self, state_dir):
        (state_dir / "m.state.json").write_text(json.dumps(["not", "a", "dict"]))
        assert blobmount.load_state("m").state == "stopped"

    def test_delete_is_idempotent(self, state_dir):
        blobmount.delete_mount("never-existed")

    def test_make_id_when_the_directory_has_no_name(self, tmp_path, state_dir):
        assert blobmount.make_mount_id(Path("/"), "") == "mount"


class TestProbeEdgeCases:
    def test_timeout_helper_is_inert_off_the_main_thread(self, tmp_path):
        import threading as th

        result = {}

        def worker():
            with blobmount._time_limit(0.1):
                time.sleep(0.3)
            result["done"] = True

        thread = th.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        assert result.get("done") is True, "timeout must not fire off-thread"

    def test_timeout_helper_is_inert_for_zero(self):
        with blobmount._time_limit(0):
            pass

    def test_timeout_helper_restores_the_previous_handler(self):
        import signal as sg

        previous = sg.getsignal(sg.SIGALRM)
        with blobmount._time_limit(5):
            pass
        assert sg.getsignal(sg.SIGALRM) is previous

    def test_probe_reports_generic_io_errors(self, tmp_path, monkeypatch):
        mount = make_mount(tmp_path)
        monkeypatch.setattr(blobmount, "is_mountpoint", lambda _p: True)

        def boom(_path):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(blobmount.os, "listdir", boom)
        state, detail = probe_mount(mount)
        assert state == DENIED and "Input/output" in detail

    def test_probe_detail_shortens_home_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(blobmount.Path, "home", classmethod(lambda cls: tmp_path))
        mount = make_mount(tmp_path, create=False, mount_dir=tmp_path / "gone" / "m")
        state, detail = probe_mount(mount)
        assert state == MISSING
        assert str(tmp_path) not in detail and "~" in detail


class TestUnmountFallbacks:
    def test_falls_back_to_fusermount3(self, tmp_path, state_dir, monkeypatch):
        calls = []

        monkeypatch.setattr(blobmount, "find_blobfuse2", lambda: None)
        monkeypatch.setattr(
            blobmount.shutil,
            "which",
            lambda name: "/bin/fusermount3" if name == "fusermount3" else None,
        )

        def fake_run(argv, **kw):
            calls.append(argv)

            class R:
                returncode = 0
                stdout = stderr = ""

            return R()

        monkeypatch.setattr(blobmount, "run", fake_run)
        assert blobmount.do_unmount(make_mount(tmp_path)) is True
        assert calls[0][0] == "fusermount3" and "-u" in calls[0]

    def test_falls_back_to_fusermount(self, tmp_path, state_dir, monkeypatch):
        calls = []
        monkeypatch.setattr(blobmount, "find_blobfuse2", lambda: None)
        monkeypatch.setattr(
            blobmount.shutil,
            "which",
            lambda name: "/bin/fusermount" if name == "fusermount" else None,
        )

        def fake_run(argv, **kw):
            calls.append(argv)

            class R:
                returncode = 0
                stdout = stderr = ""

            return R()

        monkeypatch.setattr(blobmount, "run", fake_run)
        blobmount.do_unmount(make_mount(tmp_path), lazy=True)
        assert calls[0][0] == "fusermount" and "-z" in calls[0]

    def test_no_tool_available_reports_by_mountpoint(
        self, tmp_path, state_dir, monkeypatch
    ):
        monkeypatch.setattr(blobmount, "find_blobfuse2", lambda: None)
        monkeypatch.setattr(blobmount.shutil, "which", lambda _n: None)
        monkeypatch.setattr(blobmount, "is_mountpoint", lambda _p: False)
        assert blobmount.do_unmount(make_mount(tmp_path)) is True


class TestInstallSteps:
    def _linux(self, monkeypatch, tmp_path, *, root=True):
        monkeypatch.setattr(blobmount.platform, "system", lambda: "Linux")
        monkeypatch.setattr(blobmount.shutil, "which", lambda n: f"/usr/bin/{n}")
        monkeypatch.setattr(blobmount.os, "geteuid", lambda: 0 if root else 1000)
        monkeypatch.setattr(blobmount, "USM_CACHE_DIR", tmp_path)

        import urllib.request

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"deb-bytes"

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: Resp())

    def test_runs_the_expected_steps_as_root(self, monkeypatch, tmp_path):
        self._linux(monkeypatch, tmp_path)
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)

            class R:
                returncode = 0
                stdout = stderr = ""

            return R()

        monkeypatch.setattr(blobmount, "run", fake_run)
        monkeypatch.setattr(blobmount, "_allow_other", lambda: None)
        monkeypatch.setattr(blobmount, "find_blobfuse2", lambda: "/usr/bin/blobfuse2")
        assert blobmount.install_blobfuse2() == "/usr/bin/blobfuse2"
        flat = [" ".join(c) for c in calls]
        assert any("apt-get update" in f for f in flat)
        assert any("fuse3" in f for f in flat)
        assert any("dpkg -i" in f for f in flat)
        assert not any(c[0] == "sudo" for c in calls), "root should not use sudo"

    def test_uses_sudo_when_not_root(self, monkeypatch, tmp_path):
        self._linux(monkeypatch, tmp_path, root=False)
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)

            class R:
                returncode = 0
                stdout = stderr = ""

            return R()

        monkeypatch.setattr(blobmount, "run", fake_run)
        monkeypatch.setattr(blobmount, "_allow_other", lambda: None)
        monkeypatch.setattr(blobmount, "find_blobfuse2", lambda: "/usr/bin/blobfuse2")
        blobmount.install_blobfuse2()
        assert all(c[0] == "sudo" for c in calls)

    def test_required_step_failure_aborts(self, monkeypatch, tmp_path):
        self._linux(monkeypatch, tmp_path)

        def fake_run(argv, **kw):
            class R:
                returncode = 0 if "update" in argv else 1
                stdout = ""
                stderr = "broken packages"

            return R()

        monkeypatch.setattr(blobmount, "run", fake_run)
        with pytest.raises(BlobmountError, match="broken packages"):
            blobmount.install_blobfuse2()

    def test_optional_step_failure_is_tolerated(self, monkeypatch, tmp_path):
        self._linux(monkeypatch, tmp_path)

        def fake_run(argv, **kw):
            class R:
                # `apt-get update` and the trailing `-f` install may fail.
                returncode = 1 if "update" in argv else 0
                stdout = stderr = ""

            return R()

        monkeypatch.setattr(blobmount, "run", fake_run)
        monkeypatch.setattr(blobmount, "_allow_other", lambda: None)
        monkeypatch.setattr(blobmount, "find_blobfuse2", lambda: "/usr/bin/blobfuse2")
        assert blobmount.install_blobfuse2()

    def test_reports_when_the_binary_is_still_absent(self, monkeypatch, tmp_path):
        self._linux(monkeypatch, tmp_path)

        def fake_run(argv, **kw):
            class R:
                returncode = 0
                stdout = stderr = ""

            return R()

        monkeypatch.setattr(blobmount, "run", fake_run)
        monkeypatch.setattr(blobmount, "_allow_other", lambda: None)
        monkeypatch.setattr(blobmount, "find_blobfuse2", lambda: None)
        with pytest.raises(BlobmountError, match="still not on PATH"):
            blobmount.install_blobfuse2()

    def test_needs_sudo_when_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(blobmount.platform, "system", lambda: "Linux")
        monkeypatch.setattr(blobmount.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(
            blobmount.shutil,
            "which",
            lambda n: "/usr/bin/dpkg" if n == "dpkg" else None,
        )
        with pytest.raises(BlobmountError, match="need root"):
            blobmount.install_blobfuse2()

    def test_allow_other_is_a_noop_without_fuse_conf(self, monkeypatch):
        monkeypatch.setattr(
            blobmount.Path,
            "read_text",
            lambda self, *a, **k: (_ for _ in ()).throw(OSError()),
        )
        blobmount._allow_other()

    def test_allow_other_skips_when_already_present(self, monkeypatch, tmp_path):
        conf = tmp_path / "fuse.conf"
        conf.write_text("user_allow_other\n")
        calls = []
        monkeypatch.setattr(blobmount, "run", lambda a, **k: calls.append(a))

        real_path = blobmount.Path

        class FakePath(real_path):
            pass

        monkeypatch.setattr(
            blobmount, "Path", lambda p: conf if p == "/etc/fuse.conf" else real_path(p)
        )
        blobmount._allow_other()
        assert calls == [], "should not rewrite a conf that already allows it"


class TestSupervisorErrorHandling:
    def test_default_logger_writes_a_timestamped_line(
        self, tmp_path, state_dir, fake_blobfuse, capsys
    ):
        mount = make_mount(tmp_path)
        blobmount.save_mount(mount)
        supervisor = MountSupervisor(mount)
        supervisor.log("hello sig=SECRET123")
        out = capsys.readouterr().out
        assert "hello" in out and "SECRET123" not in out

    def test_stop_supervisor_when_nothing_runs(self, tmp_path, state_dir):
        blobmount.save_mount(make_mount(tmp_path, id="m"))
        assert blobmount.stop_supervisor("m") is False

    def test_stop_supervisor_handles_a_dead_pid(self, tmp_path, state_dir, monkeypatch):
        blobmount.save_mount(make_mount(tmp_path, id="m"))
        blobmount.save_state("m", MountState(supervisor_pid=4242))
        monkeypatch.setattr(blobmount, "pid_alive", lambda _p: True)

        def vanished(_pid, _sig):
            raise ProcessLookupError()

        monkeypatch.setattr(blobmount.os, "kill", vanished)
        assert blobmount.stop_supervisor("m") is False

    def test_stop_supervisor_delegates_to_the_service(
        self, tmp_path, state_dir, monkeypatch
    ):
        import subprocess

        blobmount.save_mount(make_mount(tmp_path, id="m"))
        monkeypatch.setattr(blobmount.SERVICE, "enabled_kind", lambda _i: "systemd")
        monkeypatch.setattr(
            blobmount.SERVICE,
            "stop",
            lambda _i: subprocess.CompletedProcess([], 0, "", ""),
        )
        assert blobmount.stop_supervisor("m") is True

    def test_supervisor_running_uses_the_service_when_enabled(
        self, tmp_path, state_dir, monkeypatch
    ):
        blobmount.save_mount(make_mount(tmp_path, id="m"))
        monkeypatch.setattr(blobmount.SERVICE, "enabled_kind", lambda _i: "systemd")
        monkeypatch.setattr(blobmount.SERVICE, "is_active", lambda _i: True)
        assert blobmount.supervisor_running("m") is True

    def test_poke_when_nothing_runs(self, tmp_path, state_dir):
        blobmount.save_mount(make_mount(tmp_path, id="m"))
        assert blobmount.poke_supervisor("m") is False

    def test_poke_handles_a_kill_failure(self, tmp_path, state_dir, monkeypatch):
        blobmount.save_mount(make_mount(tmp_path, id="m"))
        blobmount.save_state("m", MountState(supervisor_pid=4242))
        monkeypatch.setattr(blobmount, "pid_alive", lambda _p: True)

        def boom(_pid, _sig):
            raise PermissionError()

        monkeypatch.setattr(blobmount.os, "kill", boom)
        assert blobmount.poke_supervisor("m") is False

    def test_run_supervisor_reports_an_unknown_mount(self, state_dir):
        with pytest.raises(KeyError):
            blobmount.run_supervisor("ghost")


class TestCliErrorPaths:
    def _define(self, tmp_path, **kw):
        mount = make_mount(tmp_path, **kw)
        blobmount.save_mount(mount)
        blobmount.save_state(mount.id, MountState())
        return mount

    def test_start_refuses_when_already_supervised(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        self._define(tmp_path, id="m")
        monkeypatch.setattr(blobmount, "supervisor_running", lambda _i: True)
        result = invoke(runner, ["start", "m"])
        assert result.exit_code != 0 and "already supervised" in result.output

    def test_start_delegates_to_a_configured_service(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        import subprocess

        self._define(tmp_path, id="m")
        monkeypatch.setattr(blobmount.SERVICE, "enabled_kind", lambda _i: "systemd")
        monkeypatch.setattr(
            blobmount.SERVICE,
            "start",
            lambda _i: subprocess.CompletedProcess([], 0, "", ""),
        )
        result = invoke(runner, ["start", "m"])
        assert result.exit_code == 0 and "service" in result.output

    def test_start_reports_a_service_failure(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        import subprocess

        self._define(tmp_path, id="m")
        monkeypatch.setattr(blobmount.SERVICE, "enabled_kind", lambda _i: "systemd")
        monkeypatch.setattr(
            blobmount.SERVICE,
            "start",
            lambda _i: subprocess.CompletedProcess([], 1, "", "unit not found"),
        )
        result = invoke(runner, ["start", "m"])
        assert result.exit_code != 0 and "unit not found" in result.output

    def test_refresh_is_blocked_by_the_lock(
        self, tmp_path, state_dir, fake_blobfuse, runner
    ):
        self._define(tmp_path, id="m", auth="aad")
        lock = blobmount.FileLock(blobmount.lock_path("m"))
        assert lock.acquire()
        try:
            result = invoke(runner, ["refresh", "m"])
            assert result.exit_code != 0 and "busy" in result.output
        finally:
            lock.release()

    def test_refresh_signals_a_live_supervisor(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        self._define(tmp_path, id="m")
        monkeypatch.setattr(blobmount, "supervisor_running", lambda _i: True)
        monkeypatch.setattr(blobmount, "poke_supervisor", lambda _i: True)
        result = invoke(runner, ["refresh", "m"])
        assert result.exit_code == 0 and "refresh now" in result.output

    def test_refresh_falls_back_when_the_signal_is_ignored(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        self._define(tmp_path, id="m", auth="aad")
        monkeypatch.setattr(blobmount, "supervisor_running", lambda _i: True)
        monkeypatch.setattr(blobmount, "poke_supervisor", lambda _i: False)
        result = invoke(runner, ["refresh", "m"])
        assert "did not acknowledge" in result.output

    def test_enable_reports_a_service_failure(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        self._define(tmp_path, id="m")

        def boom(*a, **kw):
            raise RuntimeError("systemctl is unavailable")

        monkeypatch.setattr(blobmount.SERVICE, "enable", boom)
        result = invoke(runner, ["enable", "m"])
        assert result.exit_code != 0 and "unavailable" in result.output

    def test_enable_and_disable_roundtrip(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        self._define(tmp_path, id="m")
        monkeypatch.setattr(blobmount.SERVICE, "enable", lambda *a, **kw: "systemd")
        assert invoke(runner, ["enable", "m"]).exit_code == 0
        monkeypatch.setattr(blobmount.SERVICE, "enabled_kind", lambda _i: "systemd")
        disabled = {}
        monkeypatch.setattr(
            blobmount.SERVICE, "disable", lambda i: disabled.setdefault("id", i)
        )
        result = invoke(runner, ["disable", "m"])
        assert result.exit_code == 0 and disabled["id"] == "m"

    def test_enable_on_launchd_reports_login(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        self._define(tmp_path, id="m")
        monkeypatch.setattr(blobmount.SERVICE, "enable", lambda *a, **kw: "launchd")
        result = invoke(runner, ["enable", "m"])
        assert "login" in result.output

    def test_rm_declines_without_confirmation(
        self, tmp_path, state_dir, fake_blobfuse, runner
    ):
        self._define(tmp_path, id="m")
        result = invoke(runner, ["rm", "m"], input="n\n")
        assert result.exit_code == 0
        assert [x.id for x in blobmount.list_mounts()] == ["m"]

    def test_rm_disables_boot_integration(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        self._define(tmp_path, id="m")
        monkeypatch.setattr(blobmount.SERVICE, "enabled_kind", lambda _i: "systemd")
        seen = {}
        monkeypatch.setattr(
            blobmount.SERVICE, "disable", lambda i: seen.setdefault("id", i)
        )
        invoke(runner, ["rm", "m", "-y"])
        assert seen["id"] == "m"

    def test_mount_force_replaces_an_existing_definition(
        self, tmp_path, state_dir, fake_blobfuse, runner, env_sas
    ):
        target = tmp_path / "mnt"
        args = ["mount", str(target), "acct", "bucket", "--no-supervise", *env_sas]
        assert invoke(runner, args).exit_code == 0
        fake_blobfuse.set_mounted(True)
        result = invoke(runner, args + ["--force"])
        assert result.exit_code == 0, result.output
        assert fake_blobfuse.calls_for("unmount"), "force should remount"

    def test_check_all_mounts(self, tmp_path, state_dir, fake_blobfuse, runner):
        self._define(tmp_path, id="a")
        self._define(tmp_path, id="b", mount_dir=tmp_path / "mnt2")
        fake_blobfuse.set_mounted(False)
        result = invoke(runner, ["check"])
        assert result.exit_code == 1
        assert "a" in result.output and "b" in result.output

    def test_logs_follow_reads_appended_lines(
        self, tmp_path, state_dir, runner, monkeypatch
    ):
        self._define(tmp_path, id="m")
        blobmount.log_path("m").write_text("first\n")
        sleeps = {"n": 0}

        def fake_sleep(_secs):
            sleeps["n"] += 1
            if sleeps["n"] > 2:
                raise KeyboardInterrupt

        monkeypatch.setattr(blobmount.time, "sleep", fake_sleep)
        result = invoke(runner, ["logs", "m", "-f"])
        assert result.exit_code == 0 and "first" in result.output

    def test_install_command_reports_the_path(self, runner, monkeypatch):
        monkeypatch.setattr(blobmount, "install_blobfuse2", lambda: "/usr/bin/bf2")

        class R:
            returncode = 0
            stdout = "blobfuse2 2.3.2"
            stderr = ""

        monkeypatch.setattr(blobmount, "run", lambda *a, **kw: R())
        result = invoke(runner, ["install"])
        assert result.exit_code == 0 and "/usr/bin/bf2" in result.output

    def test_up_rejects_an_unknown_mount(self, state_dir, runner):
        result = invoke(runner, ["up", "ghost"])
        assert result.exit_code != 0

    def test_main_dispatches_to_the_cli(self, monkeypatch, state_dir):
        monkeypatch.delenv(blobmount.SUPERVISE_ENV, raising=False)
        called = {}
        monkeypatch.setattr(blobmount, "cli", lambda: called.setdefault("cli", True))
        blobmount.main()
        assert called["cli"] is True


class TestOutputQuality:
    """The listing must stay inside the terminal and never leak a token."""

    def _wide_mount(self, tmp_path, ident):
        return make_mount(
            tmp_path,
            id=ident,
            mount_dir=tmp_path / ("deeply/nested/" + "x" * 40) / ident,
            account="averyveryverylongstorageaccountname",
            container="an-extremely-long-container-name-for-testing",
        )

    @pytest.mark.parametrize("width", [70, 80, 100, 140, 200])
    def test_ls_fits_the_terminal(
        self, tmp_path, state_dir, fake_blobfuse, runner, width, monkeypatch
    ):
        # Rich sizes itself from COLUMNS when stdout is not a tty.
        monkeypatch.setenv("COLUMNS", str(width))
        for ident in ("alpha", "beta"):
            blobmount.save_mount(self._wide_mount(tmp_path, ident))
        result = invoke(runner, ["ls"])
        assert result.exit_code == 0, result.output
        for line in result.output.splitlines():
            assert len(line) <= width, f"line exceeds {width}: {line!r}"

    def test_ls_keeps_one_line_per_mount(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        monkeypatch.setenv("COLUMNS", "80")
        blobmount.save_mount(self._wide_mount(tmp_path, "alpha"))
        result = invoke(runner, ["ls"])
        body = [ln for ln in result.output.splitlines() if "alpha" in ln]
        assert len(body) == 1

    def test_ls_shows_ids_in_full(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        monkeypatch.setenv("COLUMNS", "100")
        blobmount.save_mount(self._wide_mount(tmp_path, "production-archive"))
        assert "production-archive" in invoke(runner, ["ls"]).output

    def test_ls_never_prints_a_token(self, tmp_path, state_dir, fake_blobfuse, runner):
        mount = make_mount(tmp_path, id="m", auth="inline", sas_spec=sas_for(3600))
        blobmount.save_mount(mount)
        blobmount.save_state("m", MountState(sas_expires_at=time.time() + 3600))
        result = invoke(runner, ["ls"])
        assert "SECRET123" not in result.output

    def test_ls_mentions_unmanaged_mounts(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        blobmount.save_mount(make_mount(tmp_path, id="m"))
        monkeypatch.setattr(
            blobmount,
            "blobfuse_mounts",
            lambda: {
                "/mnt/other": {
                    "url": "https://a.blob.core.windows.net/c/",
                    "account_name": "a",
                    "container_name": "c",
                }
            },
        )
        result = invoke(runner, ["ls"])
        assert "--all" in result.output

    def test_empty_listing_hints_at_all(self, state_dir, runner):
        assert "--all" in invoke(runner, ["ls"]).output

    def test_status_shortens_home_paths(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        monkeypatch.setattr(blobmount.Path, "home", classmethod(lambda cls: tmp_path))
        blobmount.save_mount(make_mount(tmp_path, id="m"))
        result = invoke(runner, ["status", "m"])
        assert result.exit_code == 0
        assert str(tmp_path) not in result.output

    @pytest.mark.parametrize("health", [OK, STALE, DENIED, UNMOUNTED, MISSING])
    def test_status_renders_every_health_state(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch, health
    ):
        blobmount.save_mount(make_mount(tmp_path, id="m"))
        monkeypatch.setattr(
            blobmount, "probe_mount", lambda *a, **kw: (health, "detail")
        )
        result = invoke(runner, ["status", "m"])
        assert result.exit_code == 0 and health in result.output

    def test_status_of_an_aad_mount_says_nothing_to_rotate(
        self, tmp_path, state_dir, fake_blobfuse, runner
    ):
        blobmount.save_mount(make_mount(tmp_path, id="m", auth="aad"))
        assert "nothing to rotate" in invoke(runner, ["status", "m"]).output


class TestSupervisorLoopAndStart:
    def test_loop_exits_promptly_on_stop(self, tmp_path, state_dir, fake_blobfuse):
        mount = make_mount(tmp_path, id="loop", auth="aad", probe_interval=0.3)
        blobmount.save_mount(mount)
        fake_blobfuse.set_mounted(True)
        supervisor = MountSupervisor(mount, log=lambda _m: None)

        import threading as th

        th.Timer(1.0, supervisor.request_stop).start()
        started = time.time()
        assert supervisor.run() == 0
        assert time.time() - started < 20
        assert blobmount.load_state("loop").state == "stopped"

    def test_loop_rotates_when_the_credential_ages(
        self, tmp_path, state_dir, fake_blobfuse, monkeypatch
    ):
        """The whole point: an aging SAS triggers a remount unprompted."""
        monkeypatch.setenv("BM_SAS", sas_for(60))  # already below the floor
        mount = make_mount(
            tmp_path,
            id="loop2",
            auth="env",
            sas_spec="BM_SAS",
            probe_interval=0.3,
            sas_min_remaining=1800,
        )
        blobmount.save_mount(mount)
        fake_blobfuse.set_mounted(True)
        supervisor = MountSupervisor(mount, log=lambda _m: None)

        import threading as th

        th.Timer(2.0, supervisor.request_stop).start()
        supervisor.run()
        assert blobmount.load_state("loop2").refreshes >= 1
        assert fake_blobfuse.calls_for("mount")

    def test_forced_refresh_wakes_the_loop(self, tmp_path, state_dir, fake_blobfuse):
        mount = make_mount(tmp_path, id="loop3", auth="aad", probe_interval=30)
        blobmount.save_mount(mount)
        fake_blobfuse.set_mounted(True)
        supervisor = MountSupervisor(mount, log=lambda _m: None)

        import threading as th

        th.Timer(0.5, supervisor.request_refresh).start()
        th.Timer(3.0, supervisor.request_stop).start()
        supervisor.run()
        assert blobmount.load_state("loop3").remounts >= 1

    def test_start_spawns_and_reports(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        monkeypatch.setenv("BM_SAS", sas_for(7200))
        mount = make_mount(
            tmp_path, id="m", auth="env", sas_spec="BM_SAS", probe_interval=0.5
        )
        blobmount.save_mount(mount)
        blobmount.save_state("m", MountState())
        fake_blobfuse.set_mounted(False)
        try:
            result = invoke(runner, ["start", "m"])
            assert result.exit_code == 0, result.output
            assert "m" in result.output
        finally:
            blobmount.stop_supervisor("m")

    def test_start_reports_a_supervisor_that_dies_immediately(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        mount = make_mount(tmp_path, id="m", auth="exec", sas_spec="exit 9")
        blobmount.save_mount(mount)
        blobmount.save_state("m", MountState())
        fake_blobfuse.set_mounted(False)
        monkeypatch.setattr(blobmount, "STARTUP_GRACE_SECS", 3.0)
        result = invoke(runner, ["start", "m"])
        assert result.exit_code != 0
        assert "exited during startup" in result.output

    def test_mount_command_starts_a_supervisor(
        self, tmp_path, state_dir, fake_blobfuse, runner, monkeypatch
    ):
        monkeypatch.setenv("BM_SAS", sas_for(7200))
        target = tmp_path / "mnt"
        fake_blobfuse.set_mounted(False)
        try:
            result = invoke(
                runner,
                [
                    "mount",
                    str(target),
                    "acct",
                    "bucket",
                    "--sas-env",
                    "BM_SAS",
                    "--probe-interval",
                    "0.5",
                ],
            )
            assert result.exit_code == 0, result.output
            assert blobmount.load_mount("mnt").auth == "env"
        finally:
            blobmount.stop_supervisor("mnt")


class TestMountDefaults:
    def test_auth_defaults_to_az(self, tmp_path, state_dir, fake_blobfuse, runner):
        result = invoke(
            runner,
            [
                "mount",
                str(tmp_path / "mnt"),
                "acct",
                "bucket",
                "--no-supervise",
                "--auth",
                "aad",
            ],
        )
        assert result.exit_code == 0, result.output
        assert blobmount.load_mount("mnt").auth == "aad"

    def test_bare_defaults_pick_az(self, tmp_path, state_dir):
        mount = blobmount._mount_from_options(
            tmp_path / "mnt",
            "acct",
            "bucket",
            {
                "auth": None,
                "sas_ttl_hours": 168,
                "sas_min_remaining": 1800.0,
                "refresh_interval": 21600.0,
                "probe_interval": 60.0,
                "read_only": False,
                "no_allow_other": False,
                "log_level": "log_warning",
            },
        )
        assert mount.auth == "az" and mount.sas_spec is None
