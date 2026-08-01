#!/usr/bin/env python3
"""ssh wrapper that survives drops, suspends, and mouse-mode garbage.

Two independent problems, both fixed here:

1. A dead link (flaky wifi, laptop suspend, VPN flap) kills ``ssh``
   outright. ``usm ssh`` supervises the child, reconnects with exponential
   backoff, and only gives up on genuinely unrecoverable errors.
2. When ``ssh`` dies hard, the remote TUI (tmux/vim/htop) never gets to
   switch off mouse reporting, the alternate screen, or bracketed paste on
   *your* terminal -- so every click starts printing ``[<35;80;20M``. Each
   attempt is therefore followed by a termios restore plus a mode-reset burst.

Examples:
  usm ssh user@host                          # reconnect forever, repair the tty
  usm ssh --retries 5 --retry-delay 3 host
  usm ssh --tmux gpu-node                    # reattach a persistent remote tmux
  usm ssh -p 2222 -L 8080:localhost:80 host  # unknown flags go straight to ssh
  usm ssh host -- nvidia-smi                 # one-shot remote command
  usm ssh --print-cmd --tmux host            # show the resolved ssh command
  usm ssh --fix-terminal                     # un-wedge a tty some other ssh broke

Wrapper options are long-form only and must come before the ssh arguments;
anything the wrapper does not recognise is forwarded to ssh verbatim.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import random
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import click
from rich.console import Console

try:  # POSIX only; Windows consoles need no termios restore.
    import termios
except ImportError:  # pragma: no cover - Windows
    termios = None  # type: ignore[assignment]

# ssh(1) exits 255 for its own errors, including a dropped connection.
SSH_ERROR_CODE = 255

DEFAULT_RETRY_DELAY = 2.0
DEFAULT_MAX_DELAY = 60.0
DEFAULT_MIN_UPTIME = 5.0
DEFAULT_FAIL_FAST = 3
DEFAULT_SLEEP_THRESHOLD = 20.0
DEFAULT_SESSION = "usm"
BACKOFF_FACTOR = 1.8
BACKOFF_JITTER = 0.2
MAX_BACKOFF_EXPONENT = 64  # keeps base * factor**n away from float overflow
WATCH_INTERVAL = 1.0
POLL_INTERVAL = 0.2
TERMINATE_GRACE = 3.0
STDERR_JOIN_TIMEOUT = 1.0
STDERR_TAIL_BYTES = 4096

# select() only understands sockets on Windows, so pipes are read blockingly.
_CAN_POLL_PIPES = os.name != "nt"

# Shim written by `usm install <script> <alias>`; see usmo.core.aliases.
ALIAS_SHIM_MARKER = "usm-managed alias shim"

console = Console(stderr=True)


# Terminal repair ----------------------------------------------------------

# Private modes a remote full-screen app may have switched on for *our*
# terminal and, when ssh is killed mid-session, never switches back off.
_RESET_MODES = (
    9,  # X10 mouse reporting
    1000,  # VT200 click tracking
    1001,  # highlight tracking
    1002,  # button-event tracking
    1003,  # any-event tracking
    1004,  # focus in/out reporting
    1005,  # UTF-8 mouse coordinates
    1006,  # SGR mouse coordinates
    1015,  # urxvt mouse coordinates
    1016,  # SGR-pixel mouse coordinates
    2004,  # bracketed paste
    1049,  # alternate screen buffer
    1,  # DECCKM application cursor keys
)
# Deliberately no RIS (ESC c): a hard reset would wipe the scrollback.
SANITIZE_SEQUENCE = "".join(f"\x1b[?{mode}l" for mode in _RESET_MODES) + (
    "\x1b>"  # DECPNM: numeric keypad
    "\x1b[?7h"  # DECAWM: autowrap back on
    "\x1b[?25h"  # DECTCEM: cursor visible again
    "\x1b[4l"  # IRM: replace instead of insert
    "\x1b[r"  # DECSTBM: full-height scrolling region
    "\x1b(B"  # G0 back to ASCII (undo line-drawing charset)
    "\x1b[m"  # SGR reset
)


class TerminalGuard:
    """Snapshot the local tty and put it back the way we found it.

    ``restore()`` is idempotent and safe to call at exit or after every ssh
    attempt. ``reset()`` is the stronger, snapshot-free variant used by
    ``--fix-terminal``.
    """

    def __init__(self, *, sanitize: bool = True) -> None:
        self.sanitize = sanitize
        self._dirty = False
        self._fd: int | None = None
        self._saved = None
        if termios is None:
            return
        with contextlib.suppress(Exception):
            if sys.stdin.isatty():
                self._fd = sys.stdin.fileno()
                self._saved = termios.tcgetattr(self._fd)

    def arm(self) -> None:
        """Mark the terminal as about to be handed to a child process."""
        self._dirty = True

    def restore_if_needed(self) -> None:
        """Restore only if nobody has done it since the last :meth:`arm`."""
        if self._dirty:
            self.restore()

    def restore(self) -> None:
        """Undo raw mode and every private mode a remote app may have set."""
        self._dirty = False
        if self._fd is not None and self._saved is not None:
            with contextlib.suppress(Exception):
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        self._emit()

    def reset(self) -> None:
        """Force a sane terminal without consulting the startup snapshot.

        For ``--fix-terminal`` the tty is already wedged, so the snapshot
        taken moments ago *is* the broken state; only ``stty sane`` style
        settings will do.
        """
        self._dirty = False
        fd, opened = self._control_fd()
        if fd is None:
            self._emit()
            return
        try:
            if termios is not None:
                with contextlib.suppress(Exception):
                    sane_termios(fd)
            if self.sanitize:
                with contextlib.suppress(Exception):
                    os.write(fd, SANITIZE_SEQUENCE.encode())
        finally:
            if opened:
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _control_fd(self) -> tuple[int | None, bool]:
        """``(fd, we_opened_it)`` for the controlling terminal, if any."""
        if self._fd is not None:
            return self._fd, False
        try:
            return os.open("/dev/tty", os.O_RDWR), True
        except OSError:
            return None, False

    def _emit(self) -> None:
        if not self.sanitize:
            return
        stream = _tty_stream()
        if stream is None:
            return
        with contextlib.suppress(Exception):
            stream.write(SANITIZE_SEQUENCE)
            stream.flush()


def _tty_stream():
    """The stream whose terminal may need repairing, if any."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            if stream is not None and stream.isatty():
                return stream
    return None


# ``stty sane`` in the settings a broken full-screen app tends to leave behind.
SANE_INPUT_ON = ("BRKINT", "ICRNL", "IXON", "IMAXBEL")
SANE_INPUT_OFF = ("IGNBRK", "INLCR", "IGNCR", "IXOFF")
SANE_OUTPUT_ON = ("OPOST", "ONLCR")
SANE_LOCAL_ON = (
    "ISIG",
    "ICANON",
    "ECHO",
    "ECHOE",
    "ECHOK",
    "ECHOCTL",
    "ECHOKE",
    "IEXTEN",
)
SANE_CONTROL_CHARS = {
    "VINTR": 3,
    "VQUIT": 28,
    "VERASE": 127,
    "VKILL": 21,
    "VEOF": 4,
    "VSTART": 17,
    "VSTOP": 19,
    "VSUSP": 26,
}


def _termios_bits(names) -> int:
    return sum(getattr(termios, name, 0) for name in names)


def sane_termios(fd: int) -> None:
    """Put *fd* back into a usable line discipline, like ``stty sane``."""
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)
    iflag = (iflag | _termios_bits(SANE_INPUT_ON)) & ~_termios_bits(SANE_INPUT_OFF)
    oflag |= _termios_bits(SANE_OUTPUT_ON)
    lflag |= _termios_bits(SANE_LOCAL_ON)
    cc = list(cc)
    for name, value in SANE_CONTROL_CHARS.items():
        index = getattr(termios, name, None)
        if index is not None and index < len(cc):
            cc[index] = value if isinstance(cc[index], int) else bytes([value])
    for name, value in (("VMIN", 1), ("VTIME", 0)):
        index = getattr(termios, name, None)
        if index is not None and index < len(cc):
            cc[index] = value
    termios.tcsetattr(
        fd, termios.TCSADRAIN, [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
    )


# Locating the real ssh ----------------------------------------------------


def _is_usm_shim(path: Path) -> bool:
    try:
        head = path.read_bytes()[:512]
    except OSError:
        return False
    return ALIAS_SHIM_MARKER in head.decode("utf-8", "replace")


def resolve_ssh(explicit: str | None = None) -> str:
    """Return the path of the real ssh binary.

    ``usm install ssh ssh`` puts a shim named ``ssh`` on ``PATH``; calling
    plain ``ssh`` from here would then fork-bomb straight back into us, so
    usm-owned shims are skipped.
    """
    if explicit:
        found = shutil.which(explicit) or (
            explicit if os.access(explicit, os.X_OK) else None
        )
        if not found:
            raise click.ClickException(f"ssh binary not found: {explicit}")
        if _is_usm_shim(Path(found)):
            raise click.ClickException(
                f"{found} is a usm alias shim, not ssh; running it would recurse."
            )
        return found
    name = "ssh.exe" if os.name == "nt" else "ssh"
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(os.path.expanduser(entry)) / name
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        if _is_usm_shim(candidate):
            continue
        return str(candidate)
    raise click.ClickException(
        "ssh not found on PATH. Install OpenSSH, or pass --ssh-bin PATH."
    )


# Command construction -----------------------------------------------------

# ssh(1) short flags that consume a value, either glued (-p2222) or as the
# next token (-p 2222). Everything else is a boolean flag and may cluster.
SSH_VALUE_FLAGS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")

# Liveness only: these make a half-dead link surface in ~45s instead of
# hanging forever. Injected after the user's own arguments, since ssh keeps
# the *first* value it sees for an option.
KEEPALIVE_OPTIONS = (
    ("ServerAliveInterval", "15"),
    ("ServerAliveCountMax", "3"),
    ("TCPKeepAlive", "yes"),
    ("ConnectTimeout", "10"),
)
# Only meaningful with -L/-R/-D: fail the session instead of silently
# handing back a connection whose forwards are missing.
FORWARD_FLAGS = "LRD"
FORWARD_OPTION = ("ExitOnForwardFailure", "yes")
# What `ssh -G` reports for each of the above when the user has *not* set it.
# Anything else is a deliberate choice in ~/.ssh/config that we leave alone.
SSH_OPTION_DEFAULTS = {
    "serveraliveinterval": "0",
    "serveralivecountmax": "3",
    "tcpkeepalive": "yes",
    "connecttimeout": "none",
    "exitonforwardfailure": "no",
}
SSH_CONFIG_TIMEOUT = 3.0

MUX_COMMANDS = {
    # -A attaches when the session exists, -D kicks the stale client that
    # the dropped connection left behind.
    "tmux": lambda name: ["tmux", "new-session", "-A", "-D", "-s", name],
    "screen": lambda name: ["screen", "-D", "-R", "-S", name],
}


def _wants_value(token: str) -> bool:
    """True if this option token still needs the following token as value."""
    for index, char in enumerate(token[1:], start=1):
        if char in SSH_VALUE_FLAGS:
            return index == len(token) - 1
    return False


def split_ssh_args(args) -> tuple[list[str], str | None, list[str]]:
    """Split raw ssh arguments into ``(options, destination, command)``.

    Mirrors ssh's own getopt handling -- clustered flags (``-tt``), glued
    values (``-p2222``) and the ``--`` terminator -- so injected options can
    be placed where ssh still reads them as options: before the destination.
    """
    options: list[str] = []
    rest = list(args)
    destination: str | None = None
    while rest:
        token = rest.pop(0)
        if token == "--":
            destination = rest.pop(0) if rest else None
            break
        if len(token) > 1 and token.startswith("-"):
            options.append(token)
            if _wants_value(token) and rest:
                options.append(rest.pop(0))
            continue
        destination = token
        break
    if rest and rest[0] == "--":
        rest.pop(0)
    return options, destination, rest


def has_flag(options: list[str], letter: str) -> bool:
    """True if *letter* appears as a boolean flag among *options*."""
    index = 0
    while index < len(options):
        token = options[index]
        index += 1
        if len(token) < 2 or not token.startswith("-"):
            continue
        for char in token[1:]:
            if char == letter:
                return True
            if char in SSH_VALUE_FLAGS:
                break  # the remainder of the token is that flag's value
        if _wants_value(token):
            index += 1  # skip the value token
    return False


@dataclass(frozen=True)
class Plan:
    """A resolved ssh invocation plus what the supervisor needs to know."""

    argv: list[str]
    destination: str
    command: list[str]
    mux: str | None

    @property
    def one_shot(self) -> bool:
        """True when we run a remote command rather than a login shell."""
        return bool(self.command) and self.mux is None


def build_plan(
    ssh_bin: str,
    ssh_args,
    *,
    keepalive: bool = True,
    mux: str | None = None,
    session: str = DEFAULT_SESSION,
) -> Plan:
    options, destination, command = split_ssh_args(ssh_args)
    if destination is None:
        raise click.UsageError(
            "No ssh destination given. Example:\n  usm ssh user@host"
        )
    if mux:
        if command:
            raise click.UsageError(f"--{mux} cannot be combined with a remote command.")
        command = MUX_COMMANDS[mux](shlex.quote(session))
        if not has_flag(options, "t"):
            options.append("-t")  # force a tty for the multiplexer
    injected: list[str] = []
    if keepalive:
        pairs = list(KEEPALIVE_OPTIONS)
        if any(has_flag(options, flag) for flag in FORWARD_FLAGS):
            pairs.append(FORWARD_OPTION)
        configured = effective_config(ssh_bin, options, destination)
        for key, value in pairs:
            if _is_user_configured(configured, key):
                continue  # respect a deliberate ~/.ssh/config choice
            injected += ["-o", f"{key}={value}"]
    argv = [ssh_bin, *options, *injected, destination, *command]
    return Plan(argv=argv, destination=destination, command=command, mux=mux)


def _is_user_configured(configured: dict[str, str], key: str) -> bool:
    """True if ssh already resolves *key* to something other than its default."""
    lowered = key.lower()
    current = configured.get(lowered)
    return current is not None and current != SSH_OPTION_DEFAULTS[lowered]


def effective_config(
    ssh_bin: str, options: list[str], destination: str
) -> dict[str, str]:
    """ssh's own view of the config for this destination, via ``ssh -G``.

    ``-G`` evaluates Host/Match blocks and exits without connecting, which is
    the only reliable way to tell "the user set ConnectTimeout 60 for this
    bastion" from "nobody set it". Returns ``{}`` when ssh cannot answer, in
    which case the caller just injects its defaults.
    """
    try:
        proc = subprocess.run(
            [ssh_bin, "-G", *options, destination],
            capture_output=True,
            text=True,
            timeout=SSH_CONFIG_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    config: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        config.setdefault(key.lower(), value.strip())
    return config


# Retry policy -------------------------------------------------------------


class Verdict(Enum):
    RETRY = "retry"
    DONE = "done"
    FATAL = "fatal"


# Errors no amount of retrying will fix. Matched against ssh's own stderr.
# Name resolution is deliberately absent: DNS dies transiently whenever wifi
# or a VPN does, and a genuine typo is caught by the fail-fast budget.
FATAL_STDERR = (
    "permission denied",
    "too many authentication failures",
    "no supported authentication methods",
    "host key verification failed",
    "remote host identification has changed",
    "bad configuration option",
    "unknown option",
    "unknown cipher type",
)

# Unambiguous: ssh is telling us it never reached the point of a session.
PRE_SESSION_STDERR = (
    "ssh: connect to ",
    "could not resolve hostname",
    "name or service not known",
    "no address associated with hostname",
    "temporary failure in name resolution",
    "kex_exchange_identification",
    "banner exchange",
)
# Bare strerror() strings. ssh prints these while dialling *and* mid-session
# ("Read from remote host box: Connection reset by peer"), so on their own
# they cannot tell the two apart -- only trust them for an attempt that died
# too quickly to have been a real session.
AMBIGUOUS_FAILURE_STDERR = (
    "connection refused",
    "connection reset by peer",
    "connection timed out",
    "operation timed out",
    "no route to host",
    "network is unreachable",
    "host is down",
)
# `~.` disconnects locally: ssh never gets an exit status, so it exits 255
# like a dropped link. The remote-initiated variants all say "closed by
# remote host", which is a genuine drop worth reconnecting from.
LOCAL_DISCONNECT_RE = re.compile(r"connection to \S+ closed\.", re.IGNORECASE)


@dataclass
class Attempt:
    """The outcome of one ssh invocation."""

    code: int
    duration: float
    stderr_tail: str = ""
    woke: bool = False  # we killed it after detecting a host resume
    wake_gap: float = 0.0


@dataclass(frozen=True)
class Policy:
    retries: int = -1  # -1 = unlimited, 0 = never reconnect
    retry_delay: float = DEFAULT_RETRY_DELAY
    max_delay: float = DEFAULT_MAX_DELAY
    min_uptime: float = DEFAULT_MIN_UPTIME
    fail_fast: int = DEFAULT_FAIL_FAST
    sleep_detect: bool = True
    sleep_threshold: float = DEFAULT_SLEEP_THRESHOLD
    read_stderr: bool = True
    one_shot: bool = False
    retry_command: bool = False


def is_fatal_stderr(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in FATAL_STDERR)


def never_connected(attempt: Attempt, policy: Policy) -> bool:
    """True if ssh demonstrably never got a session up this attempt."""
    if not policy.read_stderr:
        return False
    lowered = attempt.stderr_tail.lower()
    if any(pattern in lowered for pattern in PRE_SESSION_STDERR):
        return True
    return attempt.duration < policy.min_uptime and any(
        pattern in lowered for pattern in AMBIGUOUS_FAILURE_STDERR
    )


def is_local_disconnect(text: str) -> bool:
    """True if the user tore the session down with ssh's ``~.`` escape."""
    return bool(LOCAL_DISCONNECT_RE.search(text))


def session_established(attempt: Attempt, policy: Policy) -> bool:
    """True if ssh actually got a session up during this attempt.

    Elapsed time alone cannot answer this: an unreachable host burns the
    whole ConnectTimeout before failing, while a real session can be cut a
    second after login. So an unambiguous connect-level error on stderr
    overrides the duration -- but a bare strerror() string does not, since
    those are printed for mid-session failures too.
    """
    if attempt.woke:
        return True
    if never_connected(attempt, policy):
        return False
    return attempt.duration >= policy.min_uptime


def classify(attempt: Attempt, policy: Policy) -> Verdict:
    """Decide what to do after an ssh invocation ended.

    Ordering matters: exit-code checks come first so that a remote command's
    own ``Permission denied`` output is never mistaken for an ssh failure.
    """
    if attempt.woke:
        return Verdict.RETRY
    if attempt.code == 0:
        return Verdict.DONE  # clean logout - never fight the user
    if attempt.code < 0:
        return Verdict.DONE  # signalled, e.g. Ctrl-C at a password prompt
    if attempt.code != SSH_ERROR_CODE:
        return Verdict.DONE  # the remote command's own exit status
    if policy.read_stderr and is_fatal_stderr(attempt.stderr_tail):
        return Verdict.FATAL
    if policy.read_stderr and is_local_disconnect(attempt.stderr_tail):
        return Verdict.DONE  # the user typed `~.`; don't drag them back in
    if policy.one_shot and not policy.retry_command:
        # 255 is ambiguous: ssh's own failure, or the remote command's status.
        # A command may already have run and had side effects, so reconnect
        # only when ssh demonstrably never got the session up. Elapsed time
        # cannot tell us that -- a connect can hang for ConnectTimeout.
        if never_connected(attempt, policy):
            return Verdict.RETRY
        return Verdict.DONE
    return Verdict.RETRY


def backoff_delay(
    failures: int,
    *,
    base: float,
    cap: float,
    factor: float = BACKOFF_FACTOR,
    jitter: float = BACKOFF_JITTER,
    rng=random.uniform,
) -> float:
    """Exponential backoff with jitter for the *failures*-th consecutive miss."""
    exponent = min(max(0, failures - 1), MAX_BACKOFF_EXPONENT)
    delay = min(cap, base * factor**exponent)
    if jitter:
        delay *= 1.0 + rng(-jitter, jitter)
    return max(0.0, min(cap, delay))


# Suspend detection --------------------------------------------------------


def _make_suspend_clock():
    """A clock that keeps counting while the host is asleep."""
    clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_id is not None:
        with contextlib.suppress(OSError):
            time.clock_gettime(clock_id)
            return lambda: time.clock_gettime(clock_id)
    return time.time  # wall clock: also advances across a suspend


suspend_clock = _make_suspend_clock()


def suspend_gap(mono_delta: float, wall_delta: float) -> float:
    """Seconds the host spent suspended between two clock samples."""
    return wall_delta - mono_delta


class SuspendWatch:
    """Fire *on_wake* once the host comes back from a suspend.

    ``time.monotonic()`` freezes while the machine sleeps while the boot/wall
    clock keeps counting, so a growing gap between the two means we just woke
    up -- and the TCP session almost certainly did not.
    """

    def __init__(
        self,
        on_wake,
        *,
        threshold: float = DEFAULT_SLEEP_THRESHOLD,
        interval: float = WATCH_INTERVAL,
    ) -> None:
        self._on_wake = on_wake
        self._threshold = threshold
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> SuspendWatch:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 2)

    def _run(self) -> None:
        mono, wall = time.monotonic(), suspend_clock()
        while not self._stop.wait(self._interval):
            now_mono, now_wall = time.monotonic(), suspend_clock()
            gap = suspend_gap(now_mono - mono, now_wall - wall)
            mono, wall = now_mono, now_wall
            if gap >= self._threshold:
                self._on_wake(gap)
                return


# stderr mirroring ---------------------------------------------------------


def _write_stderr(chunk: bytes) -> None:
    buffer = getattr(sys.stderr, "buffer", None)
    with contextlib.suppress(Exception):
        if buffer is not None:
            buffer.write(chunk)
            buffer.flush()
        else:  # pragma: no cover - exotic stderr replacements
            sys.stderr.write(chunk.decode("utf-8", "replace"))
            sys.stderr.flush()


class StderrTail:
    """Forward ssh's stderr verbatim while keeping the last few KiB.

    Only ssh's diagnostics travel here; password and passphrase prompts go
    straight to /dev/tty, so interactive logins are unaffected.

    The reader polls rather than blocking in ``read()``: a descendant such as
    a ``ProxyCommand`` can inherit the write end and outlive ssh itself, and
    an unstoppable reader would leak a thread and a pipe per reconnect.
    """

    def __init__(self, limit: int = STDERR_TAIL_BYTES) -> None:
        self._buffer: deque[int] = deque(maxlen=limit)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def pump(self, stream) -> None:
        try:
            if _CAN_POLL_PIPES:
                self._pump_polled(stream)
            else:  # pragma: no cover - Windows
                self._pump_blocking(stream)
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    def _pump_polled(self, stream) -> None:
        fd = stream.fileno()
        while not self._stop.is_set():
            readable, _, _ = select.select([fd], [], [], POLL_INTERVAL)
            if not readable:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                return
            self._absorb(chunk)

    def _pump_blocking(self, stream) -> None:  # pragma: no cover - Windows
        while chunk := stream.read1(4096):
            self._absorb(chunk)

    def _absorb(self, chunk: bytes) -> None:
        _write_stderr(chunk)
        self._buffer.extend(chunk)

    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8", "replace")


# Supervisor ---------------------------------------------------------------


def exit_status(code: int) -> int:
    """Translate a subprocess return code into a shell-style exit status."""
    return code if code >= 0 else 128 - code


def format_duration(seconds: float) -> str:
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


@dataclass
class Supervisor:
    """Run ssh in a loop, repairing the terminal between attempts."""

    plan: Plan
    policy: Policy
    guard: TerminalGuard
    quiet: bool = False

    _proc: subprocess.Popen | None = field(default=None, init=False)
    _stopping: bool = field(default=False, init=False)
    _stop_signal: int = field(default=0, init=False)
    _signals: int = field(default=0, init=False)
    _wake_gap: float | None = field(default=None, init=False)

    def run(self) -> int:
        with self._handle_signals():
            return self._loop()

    # -- main loop --

    def _loop(self) -> int:
        attempts = 0
        failures = 0
        misses = 0
        connected = 0.0
        ever_connected = False
        status = 0
        while not self._stopping:
            attempts += 1
            attempt = self._spawn()
            self.guard.restore()
            connected += attempt.duration
            status = exit_status(attempt.code)
            verdict = classify(attempt, self.policy)
            established = session_established(attempt, self.policy)

            if self._stopping:
                break
            if verdict is Verdict.DONE:
                self._report_end(attempts, connected)
                return status
            if verdict is Verdict.FATAL:
                self._note("[red]not retrying[/]: ssh reported an unrecoverable error")
                return status

            if established:
                ever_connected = True
                failures = 1
                misses = 0
            else:
                failures += 1
                misses += 1
            # Fail-fast only guards the "we have never reached this host"
            # case -- a typo, a wrong port, a firewall. Once a session has
            # worked, the user asked for a resilient link, so keep trying
            # until --retries runs out however long the host stays away.
            if not ever_connected and misses >= self.policy.fail_fast:
                self._note(
                    f"[red]giving up[/]: never connected to "
                    f"[bold]{self.plan.destination}[/] in {misses} attempts "
                    f"[dim](raise --fail-fast to keep trying)[/]"
                )
                return status
            if self.policy.retries >= 0 and attempts > self.policy.retries:
                if self.policy.retries:
                    self._note(f"[red]giving up[/]: {self.policy.retries} retries used")
                return status

            delay = backoff_delay(
                failures, base=self.policy.retry_delay, cap=self.policy.max_delay
            )
            self._report_retry(attempt, attempts, delay, established=established)
            if self._sleep(delay):
                break
        return self._stop_status(status)

    def _sleep(self, delay: float) -> bool:
        """Sleep up to *delay* seconds; True if we were asked to stop.

        Deliberately poll-based: ``Event.set()`` is not safe to call from a
        signal handler, and ``time.sleep()`` is resumed after EINTR (PEP 475).
        """
        deadline = time.monotonic() + delay
        while not self._stopping:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
        return True

    def _stop_status(self, status: int) -> int:
        """Report a signalled shutdown the way a shell would."""
        return 128 + self._stop_signal if self._stop_signal else status

    # -- one attempt --

    def _spawn(self) -> Attempt:
        self._wake_gap = None
        tail = StderrTail()
        kwargs = {"stderr": subprocess.PIPE} if self.policy.read_stderr else {}
        started = time.monotonic()
        self.guard.arm()
        try:
            proc = subprocess.Popen(self.plan.argv, **kwargs)
        except OSError as exc:
            raise click.ClickException(
                f"cannot run {self.plan.argv[0]}: {exc}"
            ) from exc
        self._proc = proc
        if self._stopping:
            self._stop_child(grace=TERMINATE_GRACE)  # signal beat the fork
        mirror: threading.Thread | None = None
        if proc.stderr is not None:
            mirror = threading.Thread(
                target=tail.pump, args=(proc.stderr,), daemon=True
            )
            mirror.start()
        try:
            if self.policy.sleep_detect:
                with SuspendWatch(self._on_wake, threshold=self.policy.sleep_threshold):
                    code = proc.wait()
            else:
                code = proc.wait()
        finally:
            self._proc = None
            tail.stop()
            if mirror is not None:
                mirror.join(timeout=STDERR_JOIN_TIMEOUT)
        return Attempt(
            code=code,
            duration=time.monotonic() - started,
            stderr_tail=tail.text(),
            woke=self._wake_gap is not None,
            wake_gap=self._wake_gap or 0.0,
        )

    # -- child control --

    def _on_wake(self, gap: float) -> None:
        # No reporting here: ssh still holds the tty in raw mode, so anything
        # printed now comes out staircased. _report_retry says it afterwards.
        self._wake_gap = gap
        self._stop_child(grace=TERMINATE_GRACE)

    def _stop_child(self, *, grace: float = TERMINATE_GRACE) -> None:
        """Terminate the child, escalating to SIGKILL after *grace* seconds.

        Never called from a signal handler -- it blocks. ``Popen.poll()`` only
        takes a non-blocking internal lock, so racing the main thread's
        ``wait()`` is safe and cannot hit a recycled pid.
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        with contextlib.suppress(OSError):
            proc.terminate()
        deadline = time.monotonic() + grace
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill()

    @contextlib.contextmanager
    def _handle_signals(self):
        previous = {}
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            with contextlib.suppress(ValueError, OSError):
                previous[signum] = signal.signal(signum, self._on_signal)
        try:
            yield
        finally:
            for signum, handler in previous.items():
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(signum, handler)

    def _on_signal(self, signum, _frame) -> None:
        """Stop retrying and pass the signal on to ssh.

        This runs on the main thread between bytecodes, so it must not take
        any lock the interrupted code may already hold: plain attribute writes
        only, plus Popen's signal helpers, which use a non-blocking lock.
        Forwarding explicitly also covers signals aimed at our pid alone,
        where the child would otherwise never hear about them.
        """
        self._stopping = True
        self._stop_signal = signum
        self._signals += 1
        proc = self._proc
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if self._signals > 1:
                proc.kill()
            else:
                proc.send_signal(signum)

    # -- reporting --

    def _note(self, message: str) -> None:
        if not self.quiet:
            console.print(f"[dim]usm ssh[/] {message}")

    def _report_retry(
        self, attempt: Attempt, retry: int, delay: float, *, established: bool
    ) -> None:
        limit = "\u221e" if self.policy.retries < 0 else str(self.policy.retries)
        if attempt.woke:
            reason = f"host resumed after {format_duration(attempt.wake_gap)} asleep"
        elif established:
            reason = (
                f"connection lost after {format_duration(attempt.duration)} "
                f"(exit {exit_status(attempt.code)})"
            )
        else:
            reason = f"could not connect (exit {exit_status(attempt.code)})"
        hint = " [dim](Ctrl-C during the wait to stop)[/]" if retry == 1 else ""
        self._note(
            f"[yellow]{reason}[/] — reconnecting to "
            f"[bold]{self.plan.destination}[/] in {delay:.1f}s "
            f"[dim](retry {retry}/{limit})[/]{hint}"
        )

    def _report_end(self, attempts: int, connected: float) -> None:
        if attempts > 1:
            self._note(
                f"session closed — {attempts} connections, "
                f"{format_duration(connected)} connected"
            )


# CLI ----------------------------------------------------------------------


@click.command(
    context_settings={
        # Deliberately no "-h": click's short-option parser walks a glued
        # token character by character, so a registered short flag would be
        # matched *inside* ssh arguments like -L8080:localhost:80 or
        # -oBatchMode=yes. Registering none at all keeps every -x token
        # falling through to ssh untouched.
        "help_option_names": ["--help"],
        "ignore_unknown_options": True,
        "allow_interspersed_args": False,
    },
    help=(
        "ssh with auto-reconnect and terminal-state repair.\n\n"
        "Wrapper options are long-form only and must come before the ssh "
        "arguments; everything else is forwarded to ssh untouched."
    ),
)
@click.option(
    "--retries",
    type=int,
    default=-1,
    show_default=True,
    help="Reconnect attempts after a drop; -1 for unlimited, 0 to disable.",
)
@click.option(
    "--retry-delay",
    type=float,
    default=DEFAULT_RETRY_DELAY,
    show_default=True,
    help="First backoff delay in seconds; grows exponentially.",
)
@click.option(
    "--max-delay",
    type=float,
    default=DEFAULT_MAX_DELAY,
    show_default=True,
    help="Upper bound for the backoff delay.",
)
@click.option(
    "--min-uptime",
    type=float,
    default=DEFAULT_MIN_UPTIME,
    show_default=True,
    help="Sessions shorter than this did not really get established.",
)
@click.option(
    "--fail-fast",
    type=int,
    default=DEFAULT_FAIL_FAST,
    show_default=True,
    help="Give up after this many attempts if we never once connected.",
)
@click.option(
    "--tmux", is_flag=True, help="Run inside a persistent remote tmux session."
)
@click.option("--screen", is_flag=True, help="Same, using GNU screen instead of tmux.")
@click.option(
    "--session",
    default=DEFAULT_SESSION,
    show_default=True,
    help="Session name used by --tmux/--screen.",
)
@click.option(
    "--sleep-detect/--no-sleep-detect",
    default=True,
    show_default=True,
    help="Reconnect immediately after the host resumes from suspend.",
)
@click.option(
    "--sleep-threshold",
    type=float,
    default=DEFAULT_SLEEP_THRESHOLD,
    show_default=True,
    help="Clock gap, in seconds, that counts as a suspend.",
)
@click.option(
    "--sanitize/--no-sanitize",
    default=True,
    show_default=True,
    help="Reset mouse reporting, alt-screen and paste modes after each attempt.",
)
@click.option(
    "--classify/--no-classify",
    "read_stderr",
    default=True,
    show_default=True,
    help="Read ssh's stderr so auth and host-key errors stop the retry loop.",
)
@click.option(
    "--keepalive/--no-keepalive",
    default=True,
    show_default=True,
    help="Inject ServerAlive/ConnectTimeout options (explicit -o still wins).",
)
@click.option(
    "--retry-command/--no-retry-command",
    default=False,
    show_default=True,
    help="Also reconnect for one-shot remote commands (may rerun them).",
)
@click.option("--ssh-bin", help="Use this ssh binary instead of the one on PATH.")
@click.option("--print-cmd", is_flag=True, help="Print the resolved ssh command, exit.")
@click.option(
    "--fix-terminal",
    is_flag=True,
    help="Repair the local terminal (mouse/alt-screen/raw mode) and exit.",
)
@click.option("--quiet", is_flag=True, help="Suppress reconnect notices.")
@click.argument("ssh_args", nargs=-1, type=click.UNPROCESSED)
def cli(
    retries,
    retry_delay,
    max_delay,
    min_uptime,
    fail_fast,
    tmux,
    screen,
    session,
    sleep_detect,
    sleep_threshold,
    sanitize,
    read_stderr,
    keepalive,
    retry_command,
    ssh_bin,
    print_cmd,
    fix_terminal,
    quiet,
    ssh_args,
):
    guard = TerminalGuard(sanitize=sanitize)
    if fix_terminal:
        guard.reset()
        return
    if tmux and screen:
        raise click.UsageError("--tmux and --screen are mutually exclusive.")
    mux = "tmux" if tmux else "screen" if screen else None

    plan = build_plan(
        resolve_ssh(ssh_bin),
        ssh_args,
        keepalive=keepalive,
        mux=mux,
        session=session,
    )
    if print_cmd:
        click.echo(shlex.join(plan.argv))
        return

    policy = Policy(
        retries=retries,
        retry_delay=max(0.0, retry_delay),
        max_delay=max(0.0, max_delay),
        min_uptime=max(0.0, min_uptime),
        fail_fast=max(1, fail_fast),
        sleep_detect=sleep_detect,
        sleep_threshold=max(1.0, sleep_threshold),
        read_stderr=read_stderr,
        one_shot=plan.one_shot,
        retry_command=retry_command,
    )
    atexit.register(guard.restore_if_needed)
    sys.exit(Supervisor(plan, policy, guard, quiet=quiet).run())


if __name__ == "__main__":
    cli()
