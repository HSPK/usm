"""Tests for scripts/watch.py — re-running a command on change.

The debounce loop is driven directly rather than through a real filesystem
watcher: the watcher backends are already covered in test_usm_daemon.py, and
what matters here is that a burst of writes collapses into one run, that
changes during a run are not lost, and that the command's exit code survives.

Error paths get equal billing: no command, a command that does not exist, a
watcher backend that is unavailable, and ctrl-c mid-run.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

import watch
from watch import ChangeFlag, Runner


@pytest.fixture
def runner():
    return CliRunner()


def invoke(runner, args, **kw):
    return runner.invoke(watch.cli, args, **kw)


class FakeRunner:
    """Records runs instead of spawning anything."""

    def __init__(self, code=0):
        self.runs = 0
        self.reasons = []
        self.last_code = None
        self._code = code

    def run_once(self, reason=""):
        self.runs += 1
        self.reasons.append(reason)
        self.last_code = self._code
        return self._code

    def kill(self):
        pass


# -- the sink --------------------------------------------------------------


class TestChangeFlag:
    def test_starts_empty(self):
        flag = ChangeFlag()
        assert flag.pending == 0
        assert not flag.updated.is_set()

    def test_records_and_signals(self):
        flag = ChangeFlag()
        flag.record(time.time())
        assert flag.pending == 1
        assert flag.updated.is_set()

    def test_take_drains_and_clears(self):
        flag = ChangeFlag()
        for _ in range(4):
            flag.record(time.time())
        assert flag.take() == 4
        assert flag.pending == 0
        assert not flag.updated.is_set()

    def test_a_degraded_watcher_counts_as_a_change(self):
        """Losing events must still trigger a run, not silence."""
        flag = ChangeFlag()
        flag.mark_degraded()
        assert flag.pending == 1

    def test_is_thread_safe(self):
        flag = ChangeFlag()

        def hammer():
            for _ in range(200):
                flag.record(time.time())

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert flag.take() == 800

    def test_satisfies_the_watcher_sink_protocol(self):
        """The daemon's watchers only ever call these two methods."""
        flag = ChangeFlag()
        flag.record(time.time(), size=10, deleted=False)
        flag.record(time.time(), size=0, deleted=True)
        flag.mark_degraded()
        assert flag.pending == 3


# -- extension filtering ---------------------------------------------------


class TestExtensionFilter:
    def test_no_extensions_means_no_filter(self):
        assert watch.extension_filter(()) is None
        assert watch.extension_filter(("", " ")) is None

    @pytest.mark.parametrize(
        "spec,path,expected",
        [
            (("py",), "a/b.py", True),
            (("py",), "a/b.txt", False),
            ((".py",), "a/b.py", True),
            (("PY",), "a/b.py", True),
            (("py",), "a/b.PY", True),
            (("py", "toml"), "pyproject.toml", True),
            (("py",), "noextension", False),
            (("py",), "a.py.bak", False),
        ],
    )
    def test_matching(self, spec, path, expected):
        assert watch.extension_filter(spec)(path) is expected

    def test_blank_entries_are_ignored(self):
        predicate = watch.extension_filter(("py", "", " ", "toml"))
        assert predicate("x.py") and predicate("x.toml")


# -- excludes --------------------------------------------------------------


class TestExcludes:
    def test_defaults_cover_the_usual_noise(self):
        spec = watch.build_excludes((), defaults=True)
        assert spec.matches(".git/config")
        assert spec.matches("src/__pycache__/x.pyc")
        assert not spec.matches("src/a.py")

    def test_defaults_can_be_dropped(self):
        spec = watch.build_excludes((), defaults=False)
        assert not spec.matches(".git/config")

    def test_extra_patterns_are_added(self):
        spec = watch.build_excludes(("*.log",), defaults=True)
        assert spec.matches("run.log")
        assert spec.matches(".git/config")


# -- the run loop ----------------------------------------------------------


class TestWatchLoop:
    def _spin(self, flag, fake, *, settle=0.01, max_runs=1, timeout=5):
        stop = threading.Event()
        thread = threading.Thread(
            target=watch.watch_loop,
            args=(fake, flag),
            kwargs={"settle": settle, "stop_event": stop, "max_runs": max_runs},
        )
        thread.start()
        thread.join(timeout=timeout)
        stop.set()
        thread.join(timeout=2)
        return thread

    def test_a_change_triggers_one_run(self):
        flag, fake = ChangeFlag(), FakeRunner()
        flag.record(time.time())
        self._spin(flag, fake)
        assert fake.runs == 1

    def test_a_burst_collapses_into_one_run(self):
        """A formatter rewriting a tree is one run, not a hundred."""
        flag, fake = ChangeFlag(), FakeRunner()
        for _ in range(50):
            flag.record(time.time())
        self._spin(flag, fake)
        assert fake.runs == 1
        assert "50 changes" in fake.reasons[0]

    def test_the_reason_is_singular_for_one_change(self):
        flag, fake = ChangeFlag(), FakeRunner()
        flag.record(time.time())
        self._spin(flag, fake)
        assert "1 change" in fake.reasons[0]

    def test_no_change_means_no_run(self):
        flag, fake = ChangeFlag(), FakeRunner()
        stop = threading.Event()
        thread = threading.Thread(
            target=watch.watch_loop,
            args=(fake, flag),
            kwargs={"settle": 0.01, "stop_event": stop, "max_runs": 1},
        )
        thread.start()
        time.sleep(0.5)
        stop.set()
        thread.join(timeout=3)
        assert fake.runs == 0

    def test_stop_event_ends_the_loop(self):
        flag, fake = ChangeFlag(), FakeRunner()
        stop = threading.Event()
        thread = threading.Thread(
            target=watch.watch_loop,
            args=(fake, flag),
            kwargs={"settle": 0.01, "stop_event": stop},
        )
        thread.start()
        stop.set()
        thread.join(timeout=3)
        assert not thread.is_alive()

    def test_changes_during_the_settle_window_extend_it(self):
        """Still-arriving writes should delay the run, not start it."""
        flag, fake = ChangeFlag(), FakeRunner()
        stop = threading.Event()
        thread = threading.Thread(
            target=watch.watch_loop,
            args=(fake, flag),
            kwargs={"settle": 0.15, "stop_event": stop, "max_runs": 1},
        )
        flag.record(time.time())
        thread.start()
        for _ in range(4):
            time.sleep(0.1)
            flag.record(time.time())
        assert fake.runs == 0, "ran while writes were still arriving"
        thread.join(timeout=5)
        stop.set()
        assert fake.runs == 1

    def test_returns_the_last_exit_code(self):
        flag, fake = ChangeFlag(), FakeRunner(code=3)
        flag.record(time.time())
        stop = threading.Event()
        result = {}
        thread = threading.Thread(
            target=lambda: result.update(
                code=watch.watch_loop(
                    fake, flag, settle=0.01, stop_event=stop, max_runs=1
                )
            )
        )
        thread.start()
        thread.join(timeout=5)
        assert result["code"] == 3

    def test_returns_zero_when_it_never_ran(self):
        flag, fake = ChangeFlag(), FakeRunner()
        stop = threading.Event()
        stop.set()
        assert watch.watch_loop(fake, flag, settle=0.01, stop_event=stop) == 0

    def test_runs_again_for_a_later_change(self):
        flag, fake = ChangeFlag(), FakeRunner()
        flag.record(time.time())
        self._spin(flag, fake, max_runs=1)
        flag.record(time.time())
        self._spin(flag, fake, max_runs=2)
        assert fake.runs == 2


# -- the command runner ----------------------------------------------------


class TestRunner:
    def test_runs_and_reports_success(self, tmp_path, capsys):
        runner = Runner(["true"], cwd=tmp_path, clear=False, quiet=False)
        assert runner.run_once() == 0
        assert runner.runs == 1

    def test_reports_a_failing_exit_code(self, tmp_path):
        runner = Runner(["sh", "-c", "exit 4"], cwd=tmp_path, clear=False, quiet=True)
        assert runner.run_once() == 4
        assert runner.last_code == 4

    def test_a_missing_binary_is_reported_not_raised(self, tmp_path):
        runner = Runner(
            ["definitely-not-real-xyz"], cwd=tmp_path, clear=False, quiet=True
        )
        assert runner.run_once() == 127

    def test_an_empty_argv_is_reported(self, tmp_path):
        runner = Runner([], cwd=tmp_path, clear=False, quiet=True)
        assert runner.run_once() == 127

    def test_runs_in_the_given_directory(self, tmp_path):
        marker = tmp_path / "here"
        runner = Runner(
            ["sh", "-c", "touch here"], cwd=tmp_path, clear=False, quiet=True
        )
        runner.run_once()
        assert marker.exists()

    def test_command_is_shell_quoted_for_display(self, tmp_path):
        runner = Runner(["echo", "a b"], cwd=tmp_path, clear=False, quiet=True)
        assert runner.command == "echo 'a b'"

    def test_quiet_prints_nothing_of_its_own(self, tmp_path, capsys):
        Runner(["true"], cwd=tmp_path, clear=False, quiet=True).run_once()
        assert capsys.readouterr().out == ""

    def test_kill_is_safe_when_nothing_is_running(self, tmp_path):
        Runner(["true"], cwd=tmp_path, clear=False, quiet=True).kill()

    def test_records_the_duration(self, tmp_path):
        runner = Runner(["true"], cwd=tmp_path, clear=False, quiet=True)
        runner.run_once()
        assert runner.last_duration >= 0


# -- CLI -------------------------------------------------------------------


class TestCommandLine:
    def test_requires_a_command(self, runner, tmp_path):
        result = invoke(runner, [str(tmp_path)])
        assert result.exit_code != 0
        assert "No command given" in result.output

    def test_the_command_after_the_separator_is_taken_verbatim(
        self, runner, monkeypatch, tmp_path
    ):
        """Options belonging to the command must not be parsed by us."""
        seen = {}
        monkeypatch.setattr(watch, "build_watcher", lambda *a, **kw: _NullWatcher())
        monkeypatch.setattr(
            watch, "watch_loop", lambda r, f, **kw: seen.update(argv=r.argv) or 0
        )
        result = invoke(runner, [str(tmp_path), "--", "pytest", "-q", "--tb=short"])
        assert result.exit_code == 0
        assert seen["argv"] == ["pytest", "-q", "--tb=short"]

    def test_our_options_before_the_separator_still_work(
        self, runner, monkeypatch, tmp_path
    ):
        seen = {}
        monkeypatch.setattr(watch, "build_watcher", lambda *a, **kw: _NullWatcher())
        monkeypatch.setattr(
            watch,
            "watch_loop",
            lambda r, f, **kw: seen.update(settle=kw["settle"]) or 0,
        )
        invoke(runner, [str(tmp_path), "--settle", "1.5", "--", "true"])
        assert seen["settle"] == 1.5

    def test_cmd_option_is_an_alternative_to_the_separator(
        self, runner, monkeypatch, tmp_path
    ):
        seen = {}
        monkeypatch.setattr(watch, "build_watcher", lambda *a, **kw: _NullWatcher())
        monkeypatch.setattr(
            watch, "watch_loop", lambda r, f, **kw: seen.update(argv=r.argv) or 0
        )
        invoke(runner, [str(tmp_path), "--cmd", "pytest -q"])
        assert seen["argv"] == ["pytest", "-q"]

    def test_a_nonexistent_path_is_refused(self, runner, tmp_path):
        result = invoke(runner, [str(tmp_path / "nope"), "--", "true"])
        assert result.exit_code != 0

    def test_the_exit_code_comes_from_the_command(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr(watch, "build_watcher", lambda *a, **kw: _NullWatcher())
        monkeypatch.setattr(watch, "watch_loop", lambda r, f, **kw: 7)
        assert invoke(runner, [str(tmp_path), "--", "true"]).exit_code == 7

    def test_initial_runs_before_watching(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr(watch, "build_watcher", lambda *a, **kw: _NullWatcher())
        order = []
        monkeypatch.setattr(
            watch, "watch_loop", lambda r, f, **kw: order.append("loop") or 0
        )
        monkeypatch.setattr(
            watch.Runner, "run_once", lambda self, reason="": order.append(reason) or 0
        )
        invoke(runner, [str(tmp_path), "--initial", "--", "true"])
        assert order == ["initial", "loop"]

    def test_an_unavailable_backend_is_a_clean_error(
        self, runner, monkeypatch, tmp_path
    ):
        def boom(*a, **kw):
            raise watch.WatcherUnavailable("inotify mode needs the 'watchdog' package.")

        monkeypatch.setattr(watch, "build_watcher", boom)
        result = invoke(
            runner, [str(tmp_path), "--watch-mode", "inotify", "--", "true"]
        )
        assert result.exit_code != 0 and "watchdog" in result.output

    def test_watchers_are_stopped_on_the_way_out(self, runner, monkeypatch, tmp_path):
        stopped = []
        watcher = _NullWatcher(on_stop=lambda: stopped.append(True))
        monkeypatch.setattr(watch, "build_watcher", lambda *a, **kw: watcher)
        monkeypatch.setattr(watch, "watch_loop", lambda r, f, **kw: 0)
        invoke(runner, [str(tmp_path), "--", "true"])
        assert stopped == [True]

    def test_every_path_gets_a_watcher(self, runner, monkeypatch, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        roots = []

        def record(root, *a, **kw):
            roots.append(Path(root).name)
            return _NullWatcher()

        monkeypatch.setattr(watch, "build_watcher", record)
        monkeypatch.setattr(watch, "watch_loop", lambda r, f, **kw: 0)
        invoke(runner, [str(tmp_path / "a"), str(tmp_path / "b"), "--", "true"])
        assert sorted(roots) == ["a", "b"]

    def test_the_extension_filter_reaches_the_watcher(
        self, runner, monkeypatch, tmp_path
    ):
        seen = {}

        def record(root, excludes, sink, **kw):
            seen.update(include=kw.get("include"))
            return _NullWatcher()

        monkeypatch.setattr(watch, "build_watcher", record)
        monkeypatch.setattr(watch, "watch_loop", lambda r, f, **kw: 0)
        invoke(runner, [str(tmp_path), "--ext", "py", "--", "true"])
        assert seen["include"]("a.py") is True
        assert seen["include"]("a.txt") is False

    def test_a_tiny_settle_is_clamped(self, runner, monkeypatch, tmp_path):
        """Below the floor a single save arrives as two runs."""
        seen = {}
        monkeypatch.setattr(watch, "build_watcher", lambda *a, **kw: _NullWatcher())
        monkeypatch.setattr(
            watch,
            "watch_loop",
            lambda r, f, **kw: seen.update(settle=kw["settle"]) or 0,
        )
        invoke(runner, [str(tmp_path), "--settle", "0", "--", "true"])
        assert seen["settle"] == watch.MIN_SETTLE

    def test_ctrl_c_is_not_a_traceback(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr(watch, "build_watcher", lambda *a, **kw: _NullWatcher())

        def interrupt(*a, **kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(watch, "watch_loop", interrupt)
        result = invoke(runner, [str(tmp_path), "--", "true"])
        assert result.exit_code == 0
        assert "stopped after" in result.output

    def test_help_works(self, runner):
        result = invoke(runner, ["-h"])
        assert result.exit_code == 0 and "Usage:" in result.output

    @pytest.mark.parametrize("width", ["40", "60", "80", "120", "200"])
    def test_the_banner_survives_any_width(self, runner, monkeypatch, tmp_path, width):
        monkeypatch.setenv("COLUMNS", width)
        monkeypatch.setattr(watch, "build_watcher", lambda *a, **kw: _NullWatcher())
        monkeypatch.setattr(watch, "watch_loop", lambda r, f, **kw: 0)
        result = invoke(runner, [str(tmp_path), "--", "true"])
        assert result.exit_code == 0 and "watching" in result.output

    def test_quiet_suppresses_the_banner(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr(watch, "build_watcher", lambda *a, **kw: _NullWatcher())
        monkeypatch.setattr(watch, "watch_loop", lambda r, f, **kw: 0)
        result = invoke(runner, [str(tmp_path), "--quiet", "--", "true"])
        assert "watching" not in result.output


class _NullWatcher:
    backend = "test"

    def __init__(self, on_stop=None):
        self.on_stop = on_stop

    def start(self):
        pass

    def stop(self):
        if self.on_stop:
            self.on_stop()


# -- integration -----------------------------------------------------------


class TestAgainstARealFilesystem:
    """One end-to-end pass, so the wiring is proven and not just mocked."""

    def test_a_real_write_triggers_a_real_run(self, tmp_path, monkeypatch):
        from usm_daemon import ExcludeSpec, PollingWatcher

        target = tmp_path / "a.txt"
        target.write_text("one")
        flag = ChangeFlag()
        watcher = PollingWatcher(tmp_path, ExcludeSpec.build(), flag, interval=0.05)
        watcher.start()
        try:
            time.sleep(0.1)
            target.write_text("two")
            deadline = time.time() + 5
            while time.time() < deadline and flag.pending == 0:
                time.sleep(0.05)
            assert flag.pending > 0
        finally:
            watcher.stop()

    def test_an_excluded_file_does_not_trigger(self, tmp_path):
        from usm_daemon import ExcludeSpec, PollingWatcher

        (tmp_path / "__pycache__").mkdir()
        flag = ChangeFlag()
        watcher = PollingWatcher(tmp_path, ExcludeSpec.build(), flag, interval=0.05)
        watcher.start()
        try:
            time.sleep(0.1)
            (tmp_path / "__pycache__" / "x.pyc").write_text("noise")
            time.sleep(0.4)
            assert flag.pending == 0
        finally:
            watcher.stop()

    def test_the_extension_filter_applies_to_real_files(self, tmp_path):
        from usm_daemon import ExcludeSpec, PollingWatcher

        flag = ChangeFlag()
        watcher = PollingWatcher(
            tmp_path,
            ExcludeSpec.build(),
            flag,
            interval=0.05,
            include=watch.extension_filter(("py",)),
        )
        watcher.start()
        try:
            time.sleep(0.1)
            (tmp_path / "ignored.txt").write_text("no")
            time.sleep(0.3)
            assert flag.pending == 0
            (tmp_path / "counted.py").write_text("yes")
            deadline = time.time() + 5
            while time.time() < deadline and flag.pending == 0:
                time.sleep(0.05)
            assert flag.pending > 0
        finally:
            watcher.stop()
