#!/usr/bin/env python3
"""Shared daemon plumbing for usm commands that outlive their invocation.

Everything here is generic: no Azure, no ssh, no assumption about what is
being supervised. It exists because the same problems kept being solved
separately in every long-running usm command --

* **Liveness that isn't fooled by zombies.** ``os.kill(pid, 0)`` succeeds
  for an exited-but-unreaped child, which made ``stop`` wait out its whole
  timeout on a process that had already died.
* **State you can trust.** Atomic writes so a reader never sees half a file,
  tolerant JSON reads, and an advisory lock so a manual command cannot race
  the daemon it is inspecting.
* **Boot integration.** One systemd-user / launchd implementation, so every
  command gets identical ``enable``/``disable``/``start``/``stop`` semantics
  instead of its own dialect.
* **Interruptible waits.** Sleeping until a deadline while still noticing a
  stop request promptly.
* **Ignore patterns.** rsync/gitignore-flavoured matching, shared so a
  watcher and a transfer tool cannot disagree about what is ignored.

Presentation is deliberately *not* here: tables, glyphs, durations and
redaction come from :mod:`usmo.ui`, the design system every usm command
shares. The pieces scripts need most are re-exported below so a script
needs one import rather than two.

Declared as a ``modules`` entry in ``_config.json``, so it is fetched into
the same directory as its dependents and resolves via ``sys.path[0]``.
"""

from __future__ import annotations

import fnmatch
import json
import os
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from usmo.ui import (
    SECTION,
    Column,
    compact_duration,
    elide,
    human_bytes,
    human_duration,
    redact,
    shorten_path,
)
from usmo.ui import detail as kv_table
from usmo.ui import table as new_table

__all__ = [
    # presentation, re-exported so scripts need one import
    "SECTION",
    "Column",
    "compact_duration",
    "elide",
    "human_bytes",
    "human_duration",
    "kv_table",
    "new_table",
    "redact",
    "shorten_path",
    # paths
    "USM_CACHE_DIR",
    "LOCAL_BIN_DIR",
    "SYSTEMD_USER_DIR",
    "LAUNCHD_USER_DIR",
    # state and process plumbing
    "slugify",
    "atomic_write",
    "read_json",
    "pid_alive",
    "FileLock",
    "run",
    "sleep_until",
    # ignore patterns
    "DEFAULT_EXCLUDES",
    "ExcludeSpec",
    # services
    "default_service_kind",
    "usm_bin",
    "service_path_value",
    "systemctl",
    "launchctl",
    "ServiceManager",
    # watching
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_MAX_INDEX_FILES",
    "ChangeSink",
    "Watcher",
    "InotifyWatcher",
    "PollingWatcher",
    "WatcherUnavailable",
    "build_watcher",
]


USM_CACHE_DIR = Path.home() / ".cache" / "usm"
LOCAL_BIN_DIR = USM_CACHE_DIR / "bin"

SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
LAUNCHD_USER_DIR = Path.home() / "Library" / "LaunchAgents"


# ==========================================================================
# State on disk
# ==========================================================================


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value or "").strip("-").lower()


def atomic_write(path: Path, data: str, *, mode: int = 0o644) -> None:
    """Write *data* so readers never observe a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(data)
    os.chmod(tmp, mode)
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


# ==========================================================================
# Process plumbing
# ==========================================================================


def _is_zombie(pid: int) -> bool:
    """True for an exited-but-unreaped child (Linux; best effort elsewhere)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
    except OSError:
        return False
    # The comm field is parenthesised and may itself contain spaces or ')',
    # so the state character is the first field after the *last* ')'.
    end = data.rfind(")")
    if end == -1:
        return False
    rest = data[end + 1 :].split()
    return bool(rest) and rest[0] == "Z"


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # signal 0 also succeeds for a zombie, which is not actually running.
    return not _is_zombie(pid)


class FileLock:
    """Advisory lock so a manual command can't race a running daemon."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self, *, blocking: bool = False) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            import fcntl

            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(self._fh, flags)
            return True
        except ImportError:  # pragma: no cover - non-POSIX
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import fcntl

            fcntl.flock(self._fh, fcntl.LOCK_UN)
        except (ImportError, OSError):  # pragma: no cover
            pass
        self._fh.close()
        self._fh = None

    def __enter__(self):
        self.acquire(blocking=True)
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run with the defaults every caller here wants."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(argv, **kwargs)


def sleep_until(deadline: float, stop_event, *, max_slice: float = 5.0) -> bool:
    """Wait until *deadline*, waking early if *stop_event* is set.

    Sliced so a stop request is noticed promptly even for long waits.
    Returns True when it was interrupted.
    """
    while not stop_event.is_set():
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        if stop_event.wait(min(max_slice, remaining)):
            return True
    return True


# ==========================================================================
# Ignore patterns
# ==========================================================================


DEFAULT_EXCLUDES = (
    ".git/",
    ".venv/",
    "venv/",
    "node_modules/",
    "__pycache__/",
    "*.pyc",
    ".DS_Store",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    "*.tmp",
    "*.part",
    "*.swp",
    ".~*",
)


def _has_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def _glob_segment_to_regex(stem: str) -> str:
    """Regex matching a directory whose name globs *stem*, at any depth."""
    body = fnmatch.translate(stem)
    body = body.replace(r"(?s:", "").replace(r")\Z", "")
    return rf"(^|.*/){body}/.*"


@dataclass(frozen=True)
class ExcludeSpec:
    """One list of patterns, two renderings.

    A watcher and azcopy must agree on what is ignored — if they drift, the
    watcher fires on files azcopy then refuses to transfer and the daemon
    spins. So patterns live here once and are rendered for both consumers.

    Pattern forms follow rsync/gitignore intuition:
      ``name``      match any path segment or file name
      ``name/``     match a directory and everything under it
      ``*.ext``     glob on the file name
      ``a/b``       match that relative path prefix
      ``a/*.log``   glob against the whole relative path
    """

    patterns: tuple[str, ...] = DEFAULT_EXCLUDES

    @classmethod
    def build(
        cls, extra: Iterable[str] = (), *, defaults: bool = True
    ) -> "ExcludeSpec":
        pats: list[str] = list(DEFAULT_EXCLUDES) if defaults else []
        for pat in extra:
            pat = (pat or "").strip()
            if pat and pat not in pats:
                pats.append(pat)
        return cls(tuple(pats))

    def matches(self, relpath: str) -> bool:
        """True when *relpath* (POSIX, relative to the root) is excluded."""
        rel = (relpath or "").replace(os.sep, "/")
        while rel.startswith("./"):
            rel = rel[2:]
        rel = rel.strip("/")
        if not rel or rel == ".":
            return False
        segments = rel.split("/")
        name = segments[-1]
        for pat in self.patterns:
            if pat.endswith("/"):
                stem = pat.rstrip("/")
                # A directory pattern hides the directory and its whole subtree.
                if any(fnmatch.fnmatch(seg, stem) for seg in segments[:-1]):
                    return True
                if fnmatch.fnmatch(name, stem):
                    return True
                continue
            if "/" in pat:
                if fnmatch.fnmatch(rel, pat) or rel.startswith(pat.rstrip("*")):
                    return True
                continue
            if fnmatch.fnmatch(name, pat):
                return True
            if any(fnmatch.fnmatch(seg, pat) for seg in segments[:-1]):
                return True
        return False


# ==========================================================================
# Services (systemd user units / launchd agents)
# ==========================================================================


def default_service_kind() -> str:
    return "launchd" if platform.system().lower() == "darwin" else "systemd"


def usm_bin() -> str:
    import shutil

    return shutil.which("usm") or "usm"


def service_path_value(binary: str) -> str:
    parts = [str(Path(binary).parent), str(LOCAL_BIN_DIR)]
    for extra in ("/usr/local/bin", "/usr/bin", "/bin"):
        parts.append(extra)
    return ":".join(dict.fromkeys(p for p in parts if p))


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return run(["systemctl", "--user", *args])


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return run(["launchctl", *args])


class ServiceManager:
    """One boot-integration story for every usm daemon.

    A command supplies its unit/label prefixes and the argv that should run;
    everything else (enable, disable, is-active, start, stop) is identical.
    """

    def __init__(self, unit_prefix: str, label_prefix: str) -> None:
        self.unit_prefix = unit_prefix
        self.label_prefix = label_prefix

    # -- naming ------------------------------------------------------------

    def unit_name(self, ident: str) -> str:
        return f"{self.unit_prefix}{ident}.service"

    def unit_path(self, ident: str) -> Path:
        return SYSTEMD_USER_DIR / self.unit_name(ident)

    def label(self, ident: str) -> str:
        return f"{self.label_prefix}{ident}"

    def plist_path(self, ident: str) -> Path:
        return LAUNCHD_USER_DIR / f"{self.label(ident)}.plist"

    def _domain_target(self, ident: str) -> str:
        return f"gui/{os.getuid()}/{self.label(ident)}"

    # -- state -------------------------------------------------------------

    def enabled_kind(self, ident: str) -> str | None:
        if self.unit_path(ident).exists():
            return "systemd"
        if self.plist_path(ident).exists():
            return "launchd"
        return None

    def is_active(self, ident: str) -> bool:
        kind = self.enabled_kind(ident)
        if kind == "systemd":
            return (
                systemctl("is-active", "--quiet", self.unit_name(ident)).returncode == 0
            )
        if kind == "launchd":
            proc = launchctl("print", self._domain_target(ident))
            return proc.returncode == 0 and "state = running" in proc.stdout
        return False

    # -- rendering ---------------------------------------------------------

    def render_unit(
        self, description: str, exec_start: str, binary: str, *, restart_sec: int = 10
    ) -> str:
        return (
            "[Unit]\n"
            f"Description={description}\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f'Environment="PATH={service_path_value(binary)}"\n'
            f"ExecStart={exec_start}\n"
            "Restart=always\n"
            f"RestartSec={restart_sec}\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def render_plist(
        self,
        ident: str,
        argv: list[str],
        binary: str,
        *,
        log_path: Path | None = None,
    ) -> bytes:
        import plistlib

        payload = {
            "Label": self.label(ident),
            "ProgramArguments": argv,
            "RunAtLoad": True,
            "KeepAlive": True,
            "EnvironmentVariables": {"PATH": service_path_value(binary)},
        }
        if log_path is not None:
            payload["StandardOutPath"] = str(log_path)
            payload["StandardErrorPath"] = str(log_path)
        return plistlib.dumps(payload)

    # -- actions -----------------------------------------------------------

    def enable(
        self,
        ident: str,
        argv: list[str],
        *,
        description: str,
        binary: str | None = None,
        log_path: Path | None = None,
    ) -> str:
        """Install and start the unit. Returns the backend used."""
        binary = binary or usm_bin()
        if default_service_kind() == "systemd":
            SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
            exec_start = " ".join(argv)
            self.unit_path(ident).write_text(
                self.render_unit(description, exec_start, binary)
            )
            systemctl("daemon-reload")
            proc = systemctl("enable", "--now", self.unit_name(ident))
            if proc.returncode != 0:
                self.unit_path(ident).unlink(missing_ok=True)
                raise RuntimeError(proc.stderr.strip() or "systemctl enable failed")
            return "systemd"
        LAUNCHD_USER_DIR.mkdir(parents=True, exist_ok=True)
        self.plist_path(ident).write_bytes(
            self.render_plist(ident, argv, binary, log_path=log_path)
        )
        launchctl("bootstrap", f"gui/{os.getuid()}", str(self.plist_path(ident)))
        launchctl("kickstart", self._domain_target(ident))
        return "launchd"

    def disable(self, ident: str) -> str | None:
        kind = self.enabled_kind(ident)
        if kind == "systemd":
            systemctl("disable", "--now", self.unit_name(ident))
            self.unit_path(ident).unlink(missing_ok=True)
            systemctl("daemon-reload")
        elif kind == "launchd":
            launchctl("bootout", self._domain_target(ident))
            self.plist_path(ident).unlink(missing_ok=True)
        return kind

    def start(self, ident: str) -> subprocess.CompletedProcess | None:
        kind = self.enabled_kind(ident)
        if kind == "systemd":
            return systemctl("start", self.unit_name(ident))
        if kind == "launchd":
            return launchctl("kickstart", self._domain_target(ident))
        return None

    def stop(self, ident: str) -> subprocess.CompletedProcess | None:
        kind = self.enabled_kind(ident)
        if kind == "systemd":
            return systemctl("stop", self.unit_name(ident))
        if kind == "launchd":
            return launchctl("kill", "SIGTERM", self._domain_target(ident))
        return None


# ==========================================================================
# Filesystem watching
#
# Backends differ only in how they notice a change; both report into a sink
# that only needs ``record(now, size=, deleted=)`` and ``mark_degraded()``.
# Keeping the sink duck-typed is what lets azsync accumulate bytes for its
# transfer decisions while `usm watch` just needs to know that *something*
# happened.
# ==========================================================================

DEFAULT_POLL_INTERVAL = 15.0
DEFAULT_MAX_INDEX_FILES = 200_000


class ChangeSink(Protocol):
    """What a watcher needs from whatever is counting changes.

    Deliberately tiny: azsync accumulates bytes and deletions to decide when
    a transfer is worth starting, while `usm watch` only needs to know that
    something happened. Keeping this a protocol is what lets both share the
    backends below.
    """

    def record(self, now: float, *, size: int = 0, deleted: bool = False) -> None: ...

    def mark_degraded(self) -> None: ...


class WatcherUnavailable(RuntimeError):
    """The requested backend cannot run here."""


class Watcher:
    """Feed a ChangeAccumulator. Backends differ only in how they notice."""

    backend = "none"

    def __init__(
        self,
        root: Path,
        excludes: ExcludeSpec,
        acc: ChangeSink,
        *,
        include=None,
    ):
        self.root = root
        self.excludes = excludes
        self.acc = acc
        #: Optional predicate on the relative path: when set, only files it
        #: accepts count as changes. Directories are always descended into,
        #: or an extension filter would hide the tree below it.
        self.include = include

    def start(self) -> None:  # pragma: no cover - overridden
        pass

    def stop(self) -> None:  # pragma: no cover - overridden
        pass

    def _relpath(self, path: str) -> str | None:
        try:
            rel = os.path.relpath(path, self.root)
        except ValueError:
            return None
        if rel.startswith(".."):
            return None
        return rel.replace(os.sep, "/")

    def _admit(self, path: str) -> str | None:
        """Return the relative path if it should count as a change."""
        rel = self._relpath(path)
        if rel is None or rel == ".":
            return None
        if self.excludes.matches(rel):
            return None
        if self.include is not None and not self.include(rel):
            return None
        return rel


class InotifyWatcher(Watcher):
    """watchdog-backed: inotify on Linux, FSEvents on macOS."""

    backend = "inotify"

    def __init__(
        self, root: Path, excludes: ExcludeSpec, acc: ChangeSink, *, include=None
    ):
        super().__init__(root, excludes, acc, include=include)
        self._observer = None

    @staticmethod
    def available() -> bool:
        try:
            import watchdog.observers  # noqa: F401
        except Exception:
            return False
        return True

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:
                if getattr(event, "is_directory", False):
                    return
                kind = event.event_type
                if kind not in ("created", "modified", "deleted", "moved", "closed"):
                    return
                # A move shows up as one event with both ends; count the
                # destination (that's the file azcopy will upload).
                path = getattr(event, "dest_path", None) or event.src_path
                if watcher._admit(path) is None:
                    return
                size = 0
                deleted = kind == "deleted"
                if not deleted:
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
                watcher.acc.record(time.time(), size=size, deleted=deleted)

        self._observer = Observer()
        self._observer.schedule(_Handler(), str(self.root), recursive=True)
        self._observer.start()

    def stop(self) -> None:
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except RuntimeError:  # pragma: no cover - observer already dead
            pass
        self._observer = None


class PollingWatcher(Watcher):
    """Stdlib fallback for network mounts, blobfuse, and inotify-less hosts.

    Keeps a ``{relpath: (mtime, size)}`` index and diffs it. Above
    ``max_index_files`` the index is dropped and the tree is reduced to an
    aggregate signature — still enough to *trigger* a sync, which is all a
    watcher owes us.
    """

    backend = "poll"

    def __init__(
        self,
        root: Path,
        excludes: ExcludeSpec,
        acc: ChangeSink,
        *,
        interval: float = DEFAULT_POLL_INTERVAL,
        max_index_files: int = DEFAULT_MAX_INDEX_FILES,
        include=None,
    ) -> None:
        super().__init__(root, excludes, acc, include=include)
        self.interval = interval
        self.max_index_files = max_index_files
        self._index: dict[str, tuple[float, int]] | None = {}
        self._signature: tuple[int, int, float] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def scan(self) -> tuple[dict[str, tuple[float, int]], tuple[int, int, float]]:
        index: dict[str, tuple[float, int]] = {}
        count = 0
        total = 0
        newest = 0.0
        stack = [self.root]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                rel = self._relpath(entry.path)
                if rel is None or self.excludes.matches(rel):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    if self.include is not None and not self.include(rel):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                count += 1
                total += stat.st_size
                newest = max(newest, stat.st_mtime)
                if len(index) < self.max_index_files:
                    index[rel] = (stat.st_mtime, stat.st_size)
        return index, (count, total, newest)

    def poll_once(self) -> None:
        index, signature = self.scan()
        indexed = len(index) < self.max_index_files
        previous, prev_sig = self._index, self._signature
        self._index = index if indexed else None
        self._signature = signature

        if prev_sig is None:
            return  # first scan establishes the baseline
        if not indexed or previous is None:
            if signature != prev_sig:
                # No per-file detail available; report one aggregate change.
                delta = abs(signature[1] - prev_sig[1])
                self.acc.record(time.time(), size=delta)
                self.acc.mark_degraded()
            return

        now = time.time()
        changed = 0
        for rel, meta in index.items():
            old = previous.get(rel)
            if old is None or old != meta:
                self.acc.record(now, size=meta[1])
                changed += 1
        for rel in previous:
            if rel not in index:
                self.acc.record(now, deleted=True)
                changed += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - never kill the watcher
                self.acc.mark_degraded()
            self._stop.wait(self.interval)

    def start(self) -> None:
        self.poll_once()  # baseline, synchronously
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="usm-watch-poll", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


class _StartedWatcher(Watcher):
    """Wrap a watcher that `build_watcher` already had to start to test it."""

    def __init__(self, inner: Watcher) -> None:
        self.inner = inner
        self.backend = inner.backend
        self.root = inner.root
        self.excludes = inner.excludes
        self.acc = inner.acc
        self.include = inner.include

    def start(self) -> None:
        pass  # already running

    def stop(self) -> None:
        self.inner.stop()


def build_watcher(
    root: Path,
    excludes: ExcludeSpec,
    sink,
    *,
    mode: str = "auto",
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    max_index_files: int = DEFAULT_MAX_INDEX_FILES,
    include=None,
    warn=None,
) -> Watcher:
    """Pick a backend, honouring *mode* and degrading when inotify refuses.

    ``auto`` prefers inotify but has to *start* it to find out whether it
    works -- inotify watch limits and network filesystems only fail at that
    point -- so a successfully started watcher is returned pre-started.
    """
    mode = (mode or "auto").lower()

    def polling() -> Watcher:
        return PollingWatcher(
            root,
            excludes,
            sink,
            interval=poll_interval,
            max_index_files=max_index_files,
            include=include,
        )

    if mode == "poll":
        return polling()
    if mode == "inotify":
        if not InotifyWatcher.available():
            raise WatcherUnavailable("inotify mode needs the 'watchdog' package.")
        return InotifyWatcher(root, excludes, sink, include=include)

    if InotifyWatcher.available():
        watcher = InotifyWatcher(root, excludes, sink, include=include)
        try:
            watcher.start()
            return _StartedWatcher(watcher)
        except Exception as exc:  # watch limits, network fs, permissions
            if warn is not None:
                warn(f"inotify unavailable ({exc}); falling back to polling.")
            try:
                watcher.stop()
            except Exception:
                pass
    elif warn is not None:
        warn("watchdog not installed; using the polling watcher.")
    return polling()
