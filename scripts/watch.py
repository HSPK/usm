#!/usr/bin/env python3
"""Re-run a command whenever files change.

The watcher that keeps `usm azsync` honest, pointed at your own command
instead of at a transfer. Useful on a remote box where the edit happens on
one machine and the test run has to happen on the other.

Examples:
  usm watch -- pytest -q                  # re-run tests on every save
  usm watch src tests -- pytest -q        # only watch these paths
  usm watch --ext py,toml -- ruff check   # only these file types
  usm watch --clear --initial -- make     # clear the screen, run once now
  usm watch --settle 2 -- ./deploy.sh     # wait for edits to stop first

Changes are debounced: a burst of writes (a formatter rewriting a tree, git
checking out a branch) is one run, not a hundred. While the command is
running, further changes are remembered and collapsed into a single re-run
once it finishes.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import click
from usmo import ui

from usm_daemon import (
    DEFAULT_POLL_INTERVAL,
    ExcludeSpec,
    WatcherUnavailable,
    build_watcher,
)

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}

DEFAULT_SETTLE = 0.4
#: Anything below this and a single editor save (write, chmod, rename) can
#: still arrive as two separate runs.
MIN_SETTLE = 0.05


class ChangeFlag:
    """The smallest thing that satisfies the watcher's sink protocol.

    azsync accumulates bytes and deletions because it has to decide whether a
    transfer is worth starting. Here the only question is "did anything
    happen since the last run", so this is a counter and an event.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self.updated = threading.Event()

    def record(
        self,
        now: float,
        *,
        path: str | None = None,
        size: int = 0,
        previous_size: int | None = None,
        created: bool = False,
        deleted: bool = False,
    ) -> None:
        with self._lock:
            self._count += 1
        self.updated.set()

    def mark_degraded(self) -> None:
        self.record(time.time())

    def take(self) -> int:
        with self._lock:
            count, self._count = self._count, 0
            self.updated.clear()
            return count

    @property
    def pending(self) -> int:
        with self._lock:
            return self._count


def build_excludes(extra: tuple[str, ...], *, defaults: bool) -> ExcludeSpec:
    return ExcludeSpec.build(extra, defaults=defaults)


def extension_filter(exts: tuple[str, ...]):
    """Return a predicate for --ext, or None when everything counts."""
    wanted = {e.strip().lstrip(".").lower() for e in exts if e.strip()}
    if not wanted:
        return None

    def matches(relpath: str) -> bool:
        return Path(relpath).suffix.lstrip(".").lower() in wanted

    return matches


class Runner:
    """Runs the command, never overlapping two runs of it."""

    def __init__(self, argv: list[str], *, cwd: Path, clear: bool, quiet: bool):
        self.argv = argv
        self.cwd = cwd
        self.clear = clear
        self.quiet = quiet
        self.runs = 0
        self.last_code: int | None = None
        self.last_duration = 0.0
        self._proc: subprocess.Popen | None = None

    @property
    def command(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv)

    def kill(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:  # pragma: no cover - already gone
            try:
                proc.terminate()
            except OSError:
                pass

    def run_once(self, reason: str = "") -> int:
        if self.clear:
            click.clear()
        if not self.quiet:
            ui.step(ui.joined(self.command, reason))
        started = time.time()
        try:
            self._proc = subprocess.Popen(
                self.argv, cwd=str(self.cwd), start_new_session=True
            )
        except (OSError, ValueError, IndexError) as exc:
            ui.fail(f"cannot run {self.command}: {exc}")
            self.last_code = 127
            return 127
        try:
            code = self._proc.wait()
        except KeyboardInterrupt:  # pragma: no cover - interactive
            self.kill()
            raise
        finally:
            self._proc = None
        self.runs += 1
        self.last_code = code
        self.last_duration = time.time() - started
        if not self.quiet:
            took = ui.compact_duration(self.last_duration)
            if code == 0:
                ui.ok(ui.joined("ok", took))
            else:
                ui.warn(ui.joined(f"exit {code}", took))
        return code


def watch_loop(
    runner: Runner,
    flag: ChangeFlag,
    *,
    settle: float,
    stop_event: threading.Event,
    max_runs: int | None = None,
) -> int:
    """Wait for changes, let them settle, then run. Repeat.

    Returns the last exit code. *max_runs* bounds the loop for tests; in
    normal use only ctrl-c ends it.
    """
    while not stop_event.is_set():
        if not flag.updated.wait(timeout=0.2):
            continue
        # Let a burst finish before running: keep extending the window for as
        # long as changes keep arriving.
        while not stop_event.is_set():
            before = flag.pending
            if stop_event.wait(settle):
                break
            if flag.pending == before:
                break
        if stop_event.is_set():
            break
        count = flag.take()
        runner.run_once(reason=ui.plural(count, "change"))
        if max_runs is not None and runner.runs >= max_runs:
            break
    return runner.last_code if runner.last_code is not None else 0


class SplitAtDashDash(click.Command):
    """Split ``usm watch <paths> -- <command>`` before click parses it.

    Click removes the ``--`` separator and refuses two variadic arguments,
    so the command has to be lifted out of the argument list first. Anything
    to its left is parsed normally (options and paths); anything to its
    right is the command, verbatim, options and all.
    """

    def parse_args(self, ctx, args):
        if "--" in args:
            cut = args.index("--")
            ctx.meta["watch.command"] = args[cut + 1 :]
            args = args[:cut]
        else:
            ctx.meta["watch.command"] = []
        return super().parse_args(ctx, args)


@click.command(cls=SplitAtDashDash, context_settings=CONTEXT_SETTINGS)
@click.argument("paths", nargs=-1, type=click.Path(exists=True), required=False)
@click.option(
    "--cmd",
    "explicit_cmd",
    default=None,
    help="The command, when you'd rather not use `--`.",
)
@click.option(
    "-e",
    "--ext",
    "exts",
    default="",
    help="Only react to these extensions, comma separated (e.g. py,toml).",
)
@click.option(
    "-x",
    "--exclude",
    "excludes",
    multiple=True,
    help="Ignore paths matching this pattern (repeatable).",
)
@click.option(
    "--no-default-excludes",
    is_flag=True,
    help="Don't ignore .git, __pycache__, node_modules and friends.",
)
@click.option(
    "-s",
    "--settle",
    type=float,
    default=DEFAULT_SETTLE,
    show_default=True,
    help="Seconds of quiet before running.",
)
@click.option("-c", "--clear", is_flag=True, help="Clear the screen before each run.")
@click.option("-i", "--initial", is_flag=True, help="Run once before watching.")
@click.option("-q", "--quiet", is_flag=True, help="Only show the command's own output.")
@click.option(
    "--watch-mode",
    type=click.Choice(["auto", "inotify", "poll"]),
    default="auto",
    show_default=True,
)
@click.option(
    "--poll-interval",
    type=float,
    default=DEFAULT_POLL_INTERVAL,
    show_default=True,
    help="Seconds between scans when polling.",
)
@click.pass_context
def cli(
    ctx,
    paths,
    explicit_cmd,
    exts,
    excludes,
    no_default_excludes,
    settle,
    clear,
    initial,
    quiet,
    watch_mode,
    poll_interval,
):
    """Re-run a command whenever files change.

    PATHS default to the current directory. Everything after `--` is the
    command to run.
    """
    argv = list(ctx.meta.get("watch.command") or [])
    if explicit_cmd:
        argv = shlex.split(explicit_cmd) + argv
    if not argv:
        raise click.UsageError("No command given. Try: usm watch -- pytest -q")

    roots = [Path(p).resolve() for p in (paths or (".",))]
    settle = max(MIN_SETTLE, settle)
    spec = build_excludes(excludes, defaults=not no_default_excludes)
    ext_ok = extension_filter(tuple(exts.split(","))) if exts else None

    flag = ChangeFlag()

    runner = Runner(argv, cwd=Path.cwd(), clear=clear, quiet=quiet)
    stop_event = threading.Event()

    watchers = []
    for root in roots:
        try:
            watchers.append(
                build_watcher(
                    root,
                    spec,
                    flag,
                    mode=watch_mode,
                    poll_interval=poll_interval,
                    include=ext_ok,
                    warn=None if quiet else ui.warn,
                )
            )
        except WatcherUnavailable as exc:
            raise click.ClickException(str(exc)) from exc

    for watcher in watchers:
        watcher.start()
    backends = ", ".join(sorted({w.backend for w in watchers}))
    if not quiet:
        ui.hint(
            ui.joined(
                f"watching {ui.plural(len(roots), 'path')} ({backends})",
                f"settle {ui.compact_duration(settle)}",
                "ctrl-c to stop",
            )
        )

    code = 0
    try:
        if initial:
            code = runner.run_once(reason="initial")
        code = watch_loop(runner, flag, settle=settle, stop_event=stop_event)
    except KeyboardInterrupt:
        stop_event.set()
        runner.kill()
        if not quiet:
            click.echo()
            ui.hint(f"stopped after {ui.plural(runner.runs, 'run')}")
    finally:
        stop_event.set()
        for watcher in watchers:
            try:
                watcher.stop()
            except Exception:  # pragma: no cover - best effort teardown
                pass
    sys.exit(code)


if __name__ == "__main__":
    cli()
