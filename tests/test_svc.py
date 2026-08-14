"""Tests for scripts/svc.py — supervising an arbitrary command.

The supervisor is exercised for real: services here run actual short-lived
processes (`sleep`, `true`, a crash loop) under a redirected state directory,
so restart policy, backoff, log capture and stop semantics are observed
rather than mocked. Boot integration is stubbed, because systemd is not
available in a test run and is already covered in test_usm_daemon.py.

Error paths get equal billing: a command that does not exist, a state file
written by hand, a supervisor that is already running, and a service that
exits too fast to ever be up.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

import svc
from svc import Service


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect every on-disk location, including for spawned children."""
    root = tmp_path / "svc"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(svc, "STATE_DIR", root)
    monkeypatch.setenv("USM_SVC_STATE_DIR", str(root))
    return root


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def no_boot_integration(monkeypatch):
    """Never touch the real systemd/launchd from a test."""
    monkeypatch.setattr(svc.SERVICE, "enabled_kind", lambda ident: None)
    monkeypatch.setattr(svc.SERVICE, "start", lambda ident: None)
    monkeypatch.setattr(svc.SERVICE, "stop", lambda ident: None)
    monkeypatch.setattr(svc.SERVICE, "disable", lambda ident: None)


def invoke(runner, args, **kw):
    return runner.invoke(svc.cli, args, **kw)


def invoke_ok(runner, *args, **kw):
    result = invoke(runner, list(args), **kw)
    assert result.exit_code == 0, result.output + str(result.exception)
    return result


def make(ident="demo", argv=None, **kw) -> Service:
    service = Service(
        id=ident,
        argv=["true"] if argv is None else list(argv),
        created_at=time.time(),
        **kw,
    )
    svc.save(service)
    return service


def wait_until(predicate, timeout=10.0, interval=0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# -- the record ------------------------------------------------------------


class TestServiceRecord:
    def test_round_trips_through_disk(self, state_dir):
        make("web", ["python", "-m", "http.server"], cwd="/tmp")
        loaded = svc.load("web")
        assert loaded is not None
        assert loaded.argv == ["python", "-m", "http.server"]
        assert loaded.cwd == "/tmp"

    def test_command_is_shell_quoted(self, state_dir):
        service = Service(id="x", argv=["echo", "a b", "c'd"])
        assert service.command == "echo 'a b' 'c'\"'\"'d'"

    def test_a_hand_written_string_argv_is_tolerated(self, state_dir):
        """People edit state files; a string command should still parse."""
        path = state_dir / "manual.json"
        path.write_text(json.dumps({"id": "manual", "argv": "echo hello world"}))
        service = svc.load("manual")
        assert service.argv == ["echo", "hello", "world"]

    def test_missing_fields_fall_back_to_defaults(self, state_dir):
        (state_dir / "bare.json").write_text(json.dumps({"id": "bare"}))
        service = svc.load("bare")
        assert service.restart == "always"
        assert service.restart_sec == svc.DEFAULT_RESTART_SEC
        assert service.env == {}

    def test_a_corrupt_state_file_is_ignored(self, state_dir):
        (state_dir / "broken.json").write_text("{not json")
        assert svc.load("broken") is None
        assert svc.load_all() == []

    def test_a_json_array_state_file_is_ignored(self, state_dir):
        (state_dir / "weird.json").write_text("[1, 2, 3]")
        assert svc.load("weird") is None

    def test_a_record_without_an_id_is_ignored(self, state_dir):
        (state_dir / "anon.json").write_text(json.dumps({"argv": ["true"]}))
        assert svc.load("anon") is None

    def test_runtime_files_are_not_mistaken_for_services(self, state_dir):
        make("real")
        svc.write_runtime(svc.load("real"), pid=123)
        assert [s.id for s in svc.load_all()] == ["real"]

    def test_load_all_is_sorted(self, state_dir):
        for name in ("charlie", "alpha", "bravo"):
            make(name)
        assert [s.id for s in svc.load_all()] == ["alpha", "bravo", "charlie"]

    def test_load_all_on_a_missing_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "STATE_DIR", tmp_path / "nope")
        assert svc.load_all() == []

    def test_working_dir_defaults_to_home(self, state_dir):
        assert Service(id="x", argv=["true"]).working_dir() == Path.home()


# -- restart policy --------------------------------------------------------


class TestRestartPolicy:
    @pytest.mark.parametrize(
        "policy,code,expected",
        [
            ("always", 0, True),
            ("always", 1, True),
            ("always", None, True),
            ("on-failure", 0, False),
            ("on-failure", 1, True),
            ("on-failure", None, True),
            ("never", 0, False),
            ("never", 1, False),
        ],
    )
    def test_decides_correctly(self, policy, code, expected):
        assert svc._should_restart(policy, code) is expected

    def test_an_unknown_policy_does_not_restart(self):
        """A hand-edited policy should fail closed, not loop forever."""
        assert svc._should_restart("nonsense", 1) is False


# -- the supervisor --------------------------------------------------------


class TestSupervisor:
    def test_runs_the_command_and_captures_output(self, state_dir):
        service = make("hi", ["sh", "-c", "echo hello-from-child"], restart="never")
        svc.supervise(service)
        assert "hello-from-child" in service.log_path().read_text()

    def test_records_the_exit_code(self, state_dir):
        service = make("bye", ["sh", "-c", "exit 7"], restart="never")
        assert svc.supervise(service) == 7
        assert svc.read_runtime(service)["last_exit"] == 7

    def test_restarts_until_the_cap(self, state_dir):
        service = make(
            "loop", ["sh", "-c", "exit 1"], restart="always", restart_sec=0.01
        )
        svc.supervise(service, max_restarts=3)
        assert svc.read_runtime(service)["restarts"] == 3

    def test_on_failure_stops_after_a_clean_exit(self, state_dir):
        service = make("clean", ["true"], restart="on-failure", restart_sec=0.01)
        svc.supervise(service, max_restarts=5)
        assert svc.read_runtime(service)["restarts"] == 0

    def test_a_missing_binary_is_reported_not_raised(self, state_dir):
        service = make("ghost", ["definitely-not-a-real-binary-xyz"])
        assert svc.supervise(service) == 127
        assert svc.read_runtime(service)["last_error"]

    def test_an_empty_argv_is_reported(self, state_dir):
        """Popen([]) raises IndexError; the daemon must not die on it."""
        service = make("empty", [], restart="never")
        assert svc.supervise(service) == 127
        assert svc.read_runtime(service)["last_error"] == "no command configured"

    def test_clears_its_pids_when_it_finishes(self, state_dir):
        service = make("tidy", ["true"], restart="never")
        svc.supervise(service)
        state = svc.read_runtime(service)
        assert state["supervisor_pid"] is None and state["pid"] is None
        assert not svc.running(service)

    def test_a_second_supervisor_is_refused(self, state_dir):
        """The lock is what stops two supervisors fighting over one service."""
        service = make("locked", ["true"], restart="never")
        lock = svc.FileLock(service.lock_path())
        assert lock.acquire()
        try:
            with pytest.raises(Exception, match="already being supervised"):
                svc.supervise(service)
        finally:
            lock.release()

    def test_stop_event_ends_a_long_running_child(self, state_dir):
        service = make("sleeper", ["sleep", "30"], restart="always")
        stop = threading.Event()
        thread = threading.Thread(
            target=svc.supervise, args=(service,), kwargs={"stop_event": stop}
        )
        thread.start()
        assert wait_until(lambda: svc.read_runtime(service).get("pid"))
        stop.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert not svc.running(service)

    def test_the_environment_is_extended_not_replaced(self, state_dir):
        service = make(
            "env",
            ["sh", "-c", "echo $USM_TEST_MARKER; echo path=${PATH:+yes}"],
            env={"USM_TEST_MARKER": "marker-value"},
            restart="never",
        )
        svc.supervise(service)
        text = service.log_path().read_text()
        assert "marker-value" in text and "path=yes" in text

    def test_runs_in_the_configured_directory(self, state_dir, tmp_path):
        workdir = tmp_path / "work"
        workdir.mkdir()
        service = make("cwd", ["pwd"], cwd=str(workdir), restart="never")
        svc.supervise(service)
        assert str(workdir) in service.log_path().read_text()

    def test_backoff_grows_for_a_crash_loop(self, state_dir, monkeypatch):
        """A process that dies instantly must not be respawned in a tight loop."""
        delays = []
        monkeypatch.setattr(
            svc,
            "sleep_until",
            lambda deadline, ev, **kw: delays.append(deadline) or False,
        )
        service = make("crash", ["sh", "-c", "exit 1"], restart_sec=1.0)
        svc.supervise(service, max_restarts=4)
        gaps = [round(d - t, 1) for d, t in zip(delays, [time.time()] * len(delays))]
        assert len(delays) >= 2
        assert gaps[1] > gaps[0]

    def test_log_is_rotated_when_it_grows(self, state_dir, monkeypatch):
        service = make("big", ["true"], restart="never")
        service.log_path().parent.mkdir(parents=True, exist_ok=True)
        service.log_path().write_text("x" * 32)
        monkeypatch.setattr(svc, "LOG_MAX_BYTES", 8)
        svc.supervise(service)
        assert service.log_path().with_name(service.log_path().name + ".1").exists()


# -- liveness --------------------------------------------------------------


class TestLiveness:
    def test_nothing_recorded_means_stopped(self, state_dir):
        assert svc.running(make("idle")) is False

    def test_a_dead_pid_means_stopped(self, state_dir):
        service = make("dead")
        svc.write_runtime(service, pid=0x7FFFFFFF, supervisor_pid=None)
        assert svc.running(service) is False

    def test_a_live_pid_counts_as_running(self, state_dir):
        service = make("live")
        svc.write_runtime(service, pid=os.getpid())
        assert svc.running(service) is True

    def test_a_live_supervisor_counts_even_without_a_child(self, state_dir):
        service = make("starting")
        svc.write_runtime(service, supervisor_pid=os.getpid(), pid=None)
        assert svc.running(service) is True

    def test_stop_reports_when_nothing_was_running(self, state_dir):
        assert svc.stop_service(make("nothing")) is False


# -- CLI -------------------------------------------------------------------


class TestAdd:
    def test_defines_and_starts(self, state_dir, runner, monkeypatch):
        spawned = []
        monkeypatch.setattr(
            svc, "spawn_supervisor", lambda s: spawned.append(s.id) or 1
        )
        monkeypatch.setattr(svc, "running", lambda s: True)
        result = invoke_ok(runner, "add", "web", "--", "sleep", "5")
        assert "Defined" in result.output
        assert spawned == ["web"]
        assert svc.load("web").argv == ["sleep", "5"]

    def test_no_start_only_defines(self, state_dir, runner, monkeypatch):
        monkeypatch.setattr(
            svc, "spawn_supervisor", lambda s: pytest.fail("must not start")
        )
        invoke_ok(runner, "add", "quiet", "--no-start", "--", "sleep", "5")
        assert svc.load("quiet") is not None

    def test_rejects_a_duplicate_without_force(self, state_dir, runner):
        make("dup")
        result = invoke(runner, ["add", "dup", "--no-start", "--", "true"])
        assert result.exit_code != 0 and "already exists" in result.output

    def test_force_replaces(self, state_dir, runner):
        make("dup", ["old"])
        invoke_ok(runner, "add", "dup", "--force", "--no-start", "--", "new")
        assert svc.load("dup").argv == ["new"]

    def test_requires_a_command(self, state_dir, runner):
        result = invoke(runner, ["add", "empty"])
        assert result.exit_code != 0

    def test_a_lone_double_dash_is_stripped(self, state_dir, runner):
        invoke_ok(runner, "add", "d", "--no-start", "--", "echo", "hi")
        assert svc.load("d").argv == ["echo", "hi"]

    def test_the_id_is_slugified(self, state_dir, runner):
        invoke_ok(runner, "add", "My Service!", "--no-start", "--", "true")
        assert svc.load("my-service") is not None

    def test_an_id_with_no_usable_characters_is_refused(self, state_dir, runner):
        result = invoke(runner, ["add", "!!!", "--no-start", "--", "true"])
        assert result.exit_code != 0 and "letter or digit" in result.output

    def test_env_pairs_are_parsed(self, state_dir, runner):
        invoke_ok(
            runner,
            "add",
            "e",
            "--no-start",
            "--env",
            "A=1",
            "--env",
            "B=x=y",
            "--",
            "true",
        )
        assert svc.load("e").env == {"A": "1", "B": "x=y"}

    def test_a_malformed_env_pair_is_refused(self, state_dir, runner):
        result = invoke(
            runner, ["add", "e", "--no-start", "--env", "NOPE", "--", "true"]
        )
        assert result.exit_code != 0 and "K=V" in result.output

    def test_warns_when_the_service_does_not_stay_up(
        self, state_dir, runner, monkeypatch
    ):
        monkeypatch.setattr(svc, "spawn_supervisor", lambda s: 1)
        monkeypatch.setattr(svc, "running", lambda s: False)
        result = invoke_ok(runner, "add", "flap", "--", "false")
        assert "did not stay up" in result.output


class TestListing:
    def test_empty_is_a_hint_not_a_table(self, state_dir, runner):
        result = invoke_ok(runner, "ls")
        assert "No services" in result.output

    def test_lists_each_service(self, state_dir, runner):
        make("alpha")
        make("bravo")
        out = invoke_ok(runner, "ls").output
        assert "alpha" in out and "bravo" in out

    def test_the_bare_group_lists_too(self, state_dir, runner):
        make("alpha")
        assert "alpha" in invoke_ok(runner).output

    def test_json_is_machine_readable(self, state_dir, runner):
        make("alpha", ["sleep", "1"])
        data = json.loads(invoke_ok(runner, "ls", "--json").output)
        assert data[0]["id"] == "alpha"
        assert data[0]["running"] is False
        assert data[0]["argv"] == ["sleep", "1"]

    @pytest.mark.parametrize("width", ["40", "60", "80", "120", "200"])
    def test_one_line_per_service_at_any_width(
        self, state_dir, runner, monkeypatch, width
    ):
        monkeypatch.setenv("COLUMNS", width)
        make("alpha", ["sleep", "1"])
        make("bravo", ["sleep", "2"])
        out = invoke_ok(runner, "ls").output
        assert out.count("alpha") == 1 and out.count("bravo") == 1


class TestStatus:
    def test_shows_the_essentials(self, state_dir, runner):
        make("web", ["sleep", "9"], description="the web thing")
        out = invoke_ok(runner, "status", "web").output
        assert "web" in out and "stopped" in out and "the web thing" in out

    def test_json_shape(self, state_dir, runner):
        make("web", ["sleep", "9"])
        data = json.loads(invoke_ok(runner, "status", "web", "--json").output)
        assert data["id"] == "web" and "runtime" in data and data["running"] is False

    def test_unknown_service_lists_the_known_ones(self, state_dir, runner):
        make("alpha")
        result = invoke(runner, ["status", "nope"])
        assert result.exit_code != 0
        assert "alpha" in result.output

    def test_unknown_service_with_none_defined(self, state_dir, runner):
        result = invoke(runner, ["status", "nope"])
        assert result.exit_code != 0 and "No service" in result.output

    def test_environment_values_are_redacted(self, state_dir, runner):
        make("secretive", env={"TOKEN": "sig=abcdefghijklmnop"})
        out = invoke_ok(runner, "status", "secretive").output
        assert "abcdefghijklmnop" not in out

    def test_reports_a_start_error(self, state_dir, runner):
        service = make("bad")
        svc.write_runtime(service, last_error="No such file")
        assert "No such file" in invoke_ok(runner, "status", "bad").output


class TestLifecycleCommands:
    def test_start_spawns_a_supervisor(self, state_dir, runner, monkeypatch):
        make("web")
        spawned = []
        monkeypatch.setattr(
            svc, "spawn_supervisor", lambda s: spawned.append(s.id) or 1
        )
        monkeypatch.setattr(svc, "running", lambda s: bool(spawned))
        assert "Started" in invoke_ok(runner, "start", "web").output
        assert spawned == ["web"]

    def test_start_is_a_no_op_when_already_up(self, state_dir, runner, monkeypatch):
        make("web")
        monkeypatch.setattr(svc, "running", lambda s: True)
        monkeypatch.setattr(
            svc, "spawn_supervisor", lambda s: pytest.fail("must not respawn")
        )
        assert "already running" in invoke_ok(runner, "start", "web").output

    def test_start_warns_when_it_fails_to_come_up(self, state_dir, runner, monkeypatch):
        make("web")
        monkeypatch.setattr(svc, "spawn_supervisor", lambda s: 1)
        monkeypatch.setattr(svc, "running", lambda s: False)
        assert "did not come up" in invoke_ok(runner, "start", "web").output

    def test_stop_reports_when_idle(self, state_dir, runner):
        make("web")
        assert "was not running" in invoke_ok(runner, "stop", "web").output

    def test_stop_reports_success(self, state_dir, runner, monkeypatch):
        make("web")
        monkeypatch.setattr(svc, "stop_service", lambda s, **kw: True)
        assert "Stopped" in invoke_ok(runner, "stop", "web").output

    def test_restart_stops_then_starts(self, state_dir, runner, monkeypatch):
        make("web")
        order = []
        monkeypatch.setattr(svc, "stop_service", lambda s, **kw: order.append("stop"))
        monkeypatch.setattr(svc, "spawn_supervisor", lambda s: order.append("start"))
        monkeypatch.setattr(svc, "running", lambda s: True)
        invoke_ok(runner, "restart", "web")
        assert order == ["stop", "start"]

    def test_restart_warns_when_it_does_not_come_back(
        self, state_dir, runner, monkeypatch
    ):
        make("web")
        monkeypatch.setattr(svc, "stop_service", lambda s, **kw: True)
        monkeypatch.setattr(svc, "spawn_supervisor", lambda s: 1)
        monkeypatch.setattr(svc, "running", lambda s: False)
        assert "did not come back" in invoke_ok(runner, "restart", "web").output

    def test_rm_forgets_everything(self, state_dir, runner):
        service = make("gone")
        service.log_path().parent.mkdir(parents=True, exist_ok=True)
        service.log_path().write_text("noise")
        svc.write_runtime(service, pid=None)
        invoke_ok(runner, "rm", "gone")
        assert svc.load("gone") is None
        assert not service.log_path().exists()
        assert not service.runtime_path().exists()

    def test_rm_can_keep_logs(self, state_dir, runner):
        service = make("gone")
        service.log_path().parent.mkdir(parents=True, exist_ok=True)
        service.log_path().write_text("keep me")
        invoke_ok(runner, "rm", "gone", "--keep-logs")
        assert service.log_path().read_text() == "keep me"

    def test_rm_of_an_unknown_service(self, state_dir, runner):
        assert invoke(runner, ["rm", "nope"]).exit_code != 0


class TestLogs:
    def test_reports_when_there_is_no_log(self, state_dir, runner):
        make("quiet")
        assert "No log yet" in invoke_ok(runner, "logs", "quiet").output

    def test_shows_the_tail(self, state_dir, runner):
        service = make("chatty")
        service.log_path().parent.mkdir(parents=True, exist_ok=True)
        service.log_path().write_text("\n".join(f"line {i}" for i in range(100)))
        out = invoke_ok(runner, "logs", "chatty", "-n", "3").output
        assert "line 99" in out and "line 50" not in out

    def test_survives_undecodable_output(self, state_dir, runner):
        service = make("binary")
        service.log_path().parent.mkdir(parents=True, exist_ok=True)
        service.log_path().write_bytes(b"\xff\xfe not utf-8\n")
        assert invoke_ok(runner, "logs", "binary").exit_code == 0


class TestBootIntegration:
    def test_enable_installs_a_unit(self, state_dir, runner, monkeypatch):
        make("web")
        calls = {}

        def fake_enable(ident, argv, *, description, log_path=None, binary=None):
            calls.update(ident=ident, argv=argv, description=description)
            return "systemd"

        monkeypatch.setattr(svc.SERVICE, "enable", fake_enable)
        out = invoke_ok(runner, "enable", "web").output
        assert "systemd" in out
        assert calls["argv"][-2:] == ["run", "web"]

    def test_enable_surfaces_a_backend_failure(self, state_dir, runner, monkeypatch):
        make("web")

        def boom(*a, **kw):
            raise RuntimeError("systemctl said no")

        monkeypatch.setattr(svc.SERVICE, "enable", boom)
        result = invoke(runner, ["enable", "web"])
        assert result.exit_code != 0 and "systemctl said no" in result.output

    def test_enable_hands_over_a_running_supervisor(
        self, state_dir, runner, monkeypatch
    ):
        """Two supervisors for one service would fight; stop ours first."""
        make("web")
        stopped = []
        monkeypatch.setattr(svc, "running", lambda s: True)
        monkeypatch.setattr(svc, "stop_service", lambda s, **kw: stopped.append(s.id))
        monkeypatch.setattr(svc.SERVICE, "enable", lambda *a, **kw: "systemd")
        invoke_ok(runner, "enable", "web")
        assert stopped == ["web"]

    def test_disable_reports_when_not_enabled(self, state_dir, runner):
        make("web")
        assert "was not enabled" in invoke_ok(runner, "disable", "web").output

    def test_disable_reports_the_backend(self, state_dir, runner, monkeypatch):
        make("web")
        monkeypatch.setattr(svc.SERVICE, "disable", lambda ident: "systemd")
        assert "systemd" in invoke_ok(runner, "disable", "web").output

    def test_start_prefers_the_service_manager_when_enabled(
        self, state_dir, runner, monkeypatch
    ):
        make("web")
        monkeypatch.setattr(svc.SERVICE, "enabled_kind", lambda ident: "systemd")
        started = []
        monkeypatch.setattr(svc.SERVICE, "start", lambda ident: started.append(ident))
        monkeypatch.setattr(
            svc, "spawn_supervisor", lambda s: pytest.fail("systemd owns this one")
        )
        monkeypatch.setattr(svc, "running", lambda s: bool(started))
        invoke_ok(runner, "start", "web")
        assert started == ["web"]


class TestHelp:
    @pytest.mark.parametrize(
        "args",
        [
            [],
            ["add"],
            ["ls"],
            ["status"],
            ["start"],
            ["stop"],
            ["restart"],
            ["logs"],
            ["enable"],
            ["disable"],
            ["rm"],
        ],
    )
    def test_help_works_everywhere(self, state_dir, runner, args):
        """-h must never be swallowed by a subcommand's own parsing."""
        result = invoke(runner, args + ["-h"])
        assert result.exit_code == 0 and "Usage:" in result.output


class TestSupervisorArgv:
    def test_uses_the_running_interpreter(self, state_dir):
        argv = svc.supervisor_argv()
        assert argv[0] == sys.executable
        assert argv[1].endswith("svc.py")
