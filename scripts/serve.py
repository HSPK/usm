#!/usr/bin/env python3
"""Serve files via miniserve, locally or pulled from a remote ssh host.

  usm serve ./reports                       # local; auto-installs miniserve on first run
  usm serve ./reports -p 8000 --bind 0.0.0.0
  usm serve user@host:/srv/logs             # ssh -L + miniserve on the remote
  usm serve user@host:~/models -p 8080
  usm serve ./reports --tunnel u@bastion    # ssh -R (same push semantics as `usm share`)
  usm serve ./reports --no-upload           # uploads are on by default; opt out
  usm serve ./reports --auth alice:secret -q

miniserve is a single Rust binary. If it's missing on either end, usm
downloads the pinned version into ``~/.cache/usm/bin/miniserve`` (chmod
+x). No system packages, no sudo.
"""

from __future__ import annotations

import contextlib
import platform
import random
import re
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import click
from rich.console import Console

console = Console()

MINISERVE_VERSION = "0.35.0"
MINISERVE_RELEASE = (
    f"https://github.com/svenstaro/miniserve/releases/download/v{MINISERVE_VERSION}/"
)
USM_CACHE_DIR = Path.home() / ".cache" / "usm"
LOCAL_BIN_DIR = USM_CACHE_DIR / "bin"
LOCAL_MINISERVE = LOCAL_BIN_DIR / "miniserve"
REMOTE_BIN_DIR = "~/.cache/usm/bin"
REMOTE_MINISERVE = f"{REMOTE_BIN_DIR}/miniserve"

_SSH_KEEPALIVE: tuple[str, ...] = (
    "-o",
    "ExitOnForwardFailure=yes",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=3",
    "-o",
    "StrictHostKeyChecking=accept-new",
)
_SSH_QUICK: tuple[str, ...] = (
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
)


# ---- platform → release-asset mapping ---------------------------------------


@dataclass(frozen=True)
class Target:
    system: str
    machine: str

    @classmethod
    def local(cls) -> "Target":
        return cls(platform.system(), platform.machine())

    @property
    def asset_suffix(self) -> str:
        """Filename suffix for miniserve releases (Target → asset name part)."""
        s, m = self.system.lower(), self.machine.lower()
        if s == "linux":
            if m in ("x86_64", "amd64"):
                return "x86_64-unknown-linux-gnu"
            if m in ("aarch64", "arm64"):
                return "aarch64-unknown-linux-gnu"
            if m.startswith("armv7"):
                return "armv7-unknown-linux-gnueabihf"
            if m == "riscv64":
                return "riscv64gc-unknown-linux-gnu"
        if s == "darwin":
            if m in ("x86_64", "amd64"):
                return "x86_64-apple-darwin"
            if m in ("arm64", "aarch64"):
                return "aarch64-apple-darwin"
        if s == "windows":
            if m in ("amd64", "x86_64"):
                return "x86_64-pc-windows-msvc.exe"
            if m in ("i686", "x86"):
                return "i686-pc-windows-msvc.exe"
        raise click.ClickException(
            f"no miniserve build for {self.system}/{self.machine}; "
            f"please install it manually: https://github.com/svenstaro/miniserve"
        )

    @property
    def asset_url(self) -> str:
        return f"{MINISERVE_RELEASE}miniserve-{MINISERVE_VERSION}-{self.asset_suffix}"


# ---- local install ----------------------------------------------------------


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
    except (urllib.error.URLError, OSError) as e:
        tmp.unlink(missing_ok=True)
        raise click.ClickException(f"download failed: {url}: {e}") from e
    tmp.chmod(0o755)
    tmp.replace(dest)


def ensure_local_miniserve(*, upgrade: bool = False) -> Path:
    """Return a usable miniserve path. Prefer usm-managed, then $PATH, else install."""
    if not upgrade and LOCAL_MINISERVE.exists():
        return LOCAL_MINISERVE
    if not upgrade:
        from shutil import which

        sys_bin = which("miniserve")
        if sys_bin:
            return Path(sys_bin)
    target = Target.local()
    console.print(
        f"[dim]installing miniserve {MINISERVE_VERSION} ({target.asset_suffix}) "
        f"→ {LOCAL_MINISERVE}[/dim]"
    )
    _download(target.asset_url, LOCAL_MINISERVE)
    return LOCAL_MINISERVE


# ---- remote install ---------------------------------------------------------


def _ssh_run(
    target: str, snippet: str, *, timeout: float = 60
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["ssh", *_SSH_QUICK, target, snippet],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise click.ClickException(f"ssh {target} failed: {e}") from e


@dataclass(frozen=True)
class RemoteProbe:
    kind: str  # 'dir' or 'file'
    system: str
    machine: str

    @property
    def target(self) -> Target:
        return Target(self.system, self.machine)


def probe_remote(ssh_target: str, path: str) -> RemoteProbe:
    quoted = _quote_remote_path(path)
    snippet = (
        f"if [ -d {quoted} ]; then K=dir; "
        f"elif [ -f {quoted} ]; then K=file; "
        "else echo missing; exit 0; fi; "
        "OS=$(uname -s); ARCH=$(uname -m); "
        'echo "$K $OS $ARCH"'
    )
    r = _ssh_run(ssh_target, snippet, timeout=30)
    if r.returncode != 0:
        msg = r.stderr.strip() or f"exit {r.returncode}"
        raise click.ClickException(f"ssh {ssh_target} probe failed: {msg}")
    out = (r.stdout.strip().splitlines() or [""])[-1]
    if out == "missing":
        raise click.ClickException(f"path not found on {ssh_target}: {path}")
    parts = out.split()
    if len(parts) != 3 or parts[0] not in ("dir", "file"):
        raise click.ClickException(f"unexpected probe output: {r.stdout!r}")
    return RemoteProbe(kind=parts[0], system=parts[1], machine=parts[2])


def ensure_remote_miniserve(
    ssh_target: str, probe: RemoteProbe, *, upgrade: bool = False
) -> str:
    """Return remote path to a usable miniserve, installing it via ssh if needed."""
    check = _ssh_run(
        ssh_target,
        f"[ -x {REMOTE_MINISERVE} ] && echo managed && exit 0; "
        "command -v miniserve >/dev/null 2>&1 && command -v miniserve && exit 0; "
        "echo missing",
        timeout=15,
    )
    if check.returncode == 0 and not upgrade:
        out = check.stdout.strip().splitlines()[-1]
        if out == "managed":
            return REMOTE_MINISERVE
        if out != "missing":
            return out  # absolute path printed by `command -v`
    url = probe.target.asset_url
    console.print(
        f"[dim]installing miniserve {MINISERVE_VERSION} on {ssh_target} "
        f"({probe.target.asset_suffix}) → {REMOTE_MINISERVE}[/dim]"
    )
    qurl = shlex.quote(url)
    install_snippet = (
        f"mkdir -p {REMOTE_BIN_DIR} && "
        f"TMP={REMOTE_MINISERVE}.part && "
        f"if command -v curl >/dev/null 2>&1; then "
        f'  curl -fsSL {qurl} -o "$TMP"; '
        f"elif command -v wget >/dev/null 2>&1; then "
        f'  wget -q {qurl} -O "$TMP"; '
        f"elif command -v python3 >/dev/null 2>&1; then "
        f'  python3 -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" {qurl} "$TMP"; '
        f"else "
        f"  echo 'no curl, wget, or python3 on remote' >&2; exit 1; "
        f'fi && chmod +x "$TMP" && mv "$TMP" {REMOTE_MINISERVE} && echo ok'
    )
    r = _ssh_run(ssh_target, install_snippet, timeout=180)
    if r.returncode != 0 or "ok" not in r.stdout:
        msg = r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}"
        raise click.ClickException(f"remote install failed on {ssh_target}: {msg}")
    return REMOTE_MINISERVE


# ---- source spec parsing (scp-style) ----------------------------------------


_HOST_RE = re.compile(r"^(?:[^@\s:]+@)?[^@\s:/]+$")


def parse_remote(spec: str) -> tuple[str, str] | None:
    head, sep, path = spec.partition(":")
    if not sep or "/" in head or not _HOST_RE.match(head):
        return None
    return head, path or "~"


def _quote_remote_path(p: str) -> str:
    """shlex.quote that preserves leading ~ / ~user for shell expansion."""
    if p == "~" or re.fullmatch(r"~[^/]+", p):
        return p
    m = re.match(r"^(~[^/]*)/(.*)$", p)
    if m:
        prefix, rest = m.group(1), m.group(2)
        return f"{prefix}/{shlex.quote(rest)}" if rest else f"{prefix}/"
    return shlex.quote(p)


# ---- ports ------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def resolve_port(port: int | None) -> int:
    if port is None:
        return free_port()
    if port_in_use(port):
        raise click.ClickException(
            f"Port {port} is in use. Pick a different --port or omit to auto-pick."
        )
    return port


# ---- miniserve argv ---------------------------------------------------------


@dataclass(frozen=True)
class MiniserveOpts:
    upload: bool = True
    auth: str | None = None
    qrcode: bool = False
    hidden: bool = True
    verbose: bool = False
    enable_archive: bool = True  # tar / tar.gz / zip download buttons
    delete: bool = False  # allow deletion via the web UI

    def args(self) -> list[str]:
        out: list[str] = ["--quiet"] if not self.verbose else ["--verbose"]
        if self.upload:
            out += [
                "--upload-files",
                "--mkdir",
                "--on-duplicate-files",
                "overwrite",
            ]
        if self.delete:
            out.append("--rm-files")
        if self.auth:
            out += ["--auth", self.auth]
        if self.qrcode:
            out.append("--qrcode")
        if self.hidden:
            out.append("--hidden")
        if self.enable_archive:
            out += ["--enable-tar", "--enable-tar-gz", "--enable-zip"]
        return out


def _miniserve_argv(
    binary: str, path: str, port: int, bind: str, opts: MiniserveOpts
) -> list[str]:
    return [
        binary,
        "--port",
        str(port),
        "--interfaces",
        bind,
        *opts.args(),
        path,
    ]


# ---- ssh helpers ------------------------------------------------------------


def _spawn(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True)


def open_reverse_tunnel(spec: str, lport: int) -> tuple[subprocess.Popen, int, str]:
    if ":" in spec and "@" in spec and spec.rfind(":") > spec.rfind("@"):
        ssh_target, _, rport_s = spec.rpartition(":")
        try:
            rport = int(rport_s)
        except ValueError as e:
            raise click.BadParameter(f"Invalid remote port in {spec!r}.") from e
    else:
        ssh_target = spec
        rport = random.randint(20000, 65000)
    argv = [
        "ssh",
        "-N",
        "-T",
        *_SSH_KEEPALIVE,
        "-R",
        f"{rport}:localhost:{lport}",
        ssh_target,
    ]
    return _spawn(argv), rport, ssh_target


def open_forward_serve(
    ssh_target: str,
    remote_path: str,
    miniserve_bin: str,
    lport: int,
    bind: str,
    opts: MiniserveOpts,
) -> tuple[subprocess.Popen, int]:
    rport = random.randint(20000, 65000)
    parts = [
        _quote_remote_path(miniserve_bin),
        "--port",
        str(rport),
        "--interfaces",
        "127.0.0.1",
        *[shlex.quote(a) for a in opts.args()],
        _quote_remote_path(remote_path),
    ]
    remote_cmd = "exec " + " ".join(parts)
    forward = (
        f"{lport}:127.0.0.1:{rport}"
        if bind == "127.0.0.1"
        else f"{bind}:{lport}:127.0.0.1:{rport}"
    )
    argv = ["ssh", "-T", *_SSH_KEEPALIVE]
    if bind not in ("127.0.0.1", "localhost"):
        argv.append("-g")
    argv += ["-L", forward, ssh_target, remote_cmd]
    return _spawn(argv), rport


# ---- session ----------------------------------------------------------------


@dataclass
class Session:
    headline: str
    lines: list[str] = field(default_factory=list)
    _procs: list[subprocess.Popen] = field(default_factory=list)
    _closers: list[Callable[[], None]] = field(default_factory=list)

    def add_proc(self, p: subprocess.Popen) -> None:
        self._procs.append(p)

    def add_closer(self, c: Callable[[], None]) -> None:
        self._closers.append(c)

    def add_line(self, line: str) -> None:
        self.lines.append(line)

    def banner(self) -> str:
        return "\n".join(
            [f"[green]✓[/green] serving [bold]{self.headline}[/bold]", *self.lines]
        )

    def healthy(self) -> bool:
        return all(p.poll() is None for p in self._procs)

    def close(self) -> None:
        for c in self._closers:
            with contextlib.suppress(Exception):
                c()
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _wait_for_or_die(proc: subprocess.Popen, sess: Session, hint: str) -> None:
    time.sleep(1.5)
    if proc.poll() is not None:
        sess.close()
        raise click.ClickException(f"{hint} (exit {proc.returncode}).")


# ---- sources ----------------------------------------------------------------


class Source(Protocol):
    def open(self, port: int, bind: str, opts: MiniserveOpts) -> Session: ...


def _feature_lines(opts: MiniserveOpts, base_url: str) -> list[str]:
    lines: list[str] = []
    if opts.enable_archive:
        lines.append(
            "  [dim]folder download:[/dim] "
            f"[cyan]{base_url}?download=tar_gz[/cyan] "
            "[dim](or zip / tar)[/dim]"
        )
    bits: list[str] = []
    if opts.upload:
        bits.append("uploads")
    if opts.delete:
        bits.append("delete")
    if opts.hidden:
        bits.append("dotfiles")
    if opts.auth:
        bits.append("basic-auth")
    if bits:
        lines.append("  [dim]enabled:[/dim] " + ", ".join(bits))
    return lines


@dataclass
class LocalServe:
    path: Path
    tunnel: str | None = None
    upgrade: bool = False

    def open(self, port: int, bind: str, opts: MiniserveOpts) -> Session:
        binary = str(ensure_local_miniserve(upgrade=self.upgrade))
        path = self.path.resolve()
        argv = _miniserve_argv(binary, str(path), port, bind, opts)
        proc = _spawn(argv)
        sess = Session(headline=str(path))
        sess.add_proc(proc)
        base = f"http://{bind}:{port}/"
        sess.add_line(f"  local:  [cyan]{base}[/cyan]")
        if path.is_file():
            sess.add_line(
                "  [yellow]single-file mode:[/yellow] miniserve is serving just this file; "
                "no folder listing / no archive download. "
                "Point at the parent directory to get the full UI."
            )
        else:
            sess.lines.extend(_feature_lines(opts, base))
        _wait_for_or_die(proc, sess, "miniserve exited immediately")
        if self.tunnel:
            self._attach_push(sess, port)
        return sess

    def _attach_push(self, sess: Session, lport: int) -> None:
        try:
            proc, rport, ssh_target = open_reverse_tunnel(self.tunnel, lport)
        except OSError as e:
            sess.close()
            raise click.ClickException(f"tunnel failed: {e}") from e
        sess.add_proc(proc)
        host = ssh_target.split("@", 1)[-1]
        sess.add_line(
            f"  remote: [cyan]http://{host}:{rport}/[/cyan]  "
            f"[dim](via ssh -R; reachable on {ssh_target}'s localhost)[/dim]"
        )
        _wait_for_or_die(proc, sess, "tunnel ssh exited immediately")


@dataclass
class RemoteServe:
    ssh_target: str
    remote_path: str
    upgrade: bool = False

    def open(self, port: int, bind: str, opts: MiniserveOpts) -> Session:
        probe = probe_remote(self.ssh_target, self.remote_path)
        if probe.kind == "file":
            raise click.ClickException(
                f"miniserve serves directories; {self.remote_path!r} is a file. "
                "Point at the parent directory instead."
            )
        binary = ensure_remote_miniserve(self.ssh_target, probe, upgrade=self.upgrade)
        proc, rport = open_forward_serve(
            self.ssh_target,
            self.remote_path,
            binary,
            port,
            bind,
            opts,
        )
        sess = Session(
            headline=f"{self.ssh_target}:{self.remote_path} [dim](remote)[/dim]"
        )
        sess.add_proc(proc)
        base = f"http://{bind}:{port}/"
        sess.add_line(
            f"  local:  [cyan]{base}[/cyan]  "
            f"[dim](ssh -L {port}->{rport}; miniserve on remote)[/dim]"
        )
        sess.lines.extend(_feature_lines(opts, base))
        if opts.upload:
            sess.add_line("  [dim]note: uploads write to the remote filesystem[/dim]")
        _wait_for_or_die(proc, sess, "ssh exited immediately; remote port may be taken")
        return sess


def make_source(spec: str, tunnel: str | None, upgrade: bool) -> Source:
    remote = parse_remote(spec)
    if remote is not None:
        if tunnel:
            raise click.ClickException(
                "--tunnel can't be combined with a remote source (user@host:/path)."
            )
        ssh_target, remote_path = remote
        return RemoteServe(ssh_target, remote_path, upgrade=upgrade)
    path = Path(spec)
    if not path.exists():
        raise click.ClickException(f"path not found: {spec}")
    return LocalServe(path, tunnel=tunnel, upgrade=upgrade)


def run_until_done(sess: Session) -> None:
    console.print(sess.banner())
    console.print("[dim]ctrl-c to stop[/dim]")
    try:
        with sess:
            while sess.healthy():
                time.sleep(0.5)
            console.print("[yellow]child process exited; stopping.[/yellow]")
    except KeyboardInterrupt:
        pass
    console.print("[dim]stopped.[/dim]")


# ---- CLI --------------------------------------------------------------------


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Serve a directory via miniserve (auto-installed on first run). "
        "PATH is local or user@host:/remote/dir."
    ),
)
@click.argument("path", type=str)
@click.option(
    "-p", "--port", type=int, default=None, help="Local port (default: random free)."
)
@click.option(
    "--bind",
    default="127.0.0.1",
    show_default=True,
    help="Local bind address (use 0.0.0.0 for LAN).",
)
@click.option(
    "--upload/--no-upload",
    default=True,
    show_default=True,
    help="Allow uploads / mkdir / overwrites in the web UI.",
)
@click.option(
    "--delete",
    "delete",
    is_flag=True,
    help="Allow file/folder deletion from the web UI (destructive).",
)
@click.option(
    "-a",
    "--auth",
    default=None,
    help="Basic auth, format USER:PASS (or USER:hash). "
    "See miniserve docs for hashed form.",
)
@click.option(
    "-q",
    "--qr",
    "qrcode",
    is_flag=True,
    help="Print a QR code to the terminal for the URL.",
)
@click.option(
    "--hidden/--no-hidden",
    default=True,
    show_default=True,
    help="Show hidden (dotfile) entries in the listing.",
)
@click.option(
    "--no-archive",
    "archive",
    flag_value=False,
    default=True,
    help="Disable on-the-fly tar.gz / zip download of directories.",
)
@click.option("-v", "--verbose", is_flag=True, help="miniserve access logs.")
@click.option(
    "--tunnel",
    default=None,
    help="SSH target (user@host[:rport]); reverse tunnel like `usm share`. "
    "Push mode; mutually exclusive with a remote source.",
)
@click.option(
    "-U",
    "--upgrade",
    is_flag=True,
    help="Force re-download of the miniserve binary (local and/or remote).",
)
def cli(
    path,
    port,
    bind,
    upload,
    delete,
    auth,
    qrcode,
    hidden,
    archive,
    verbose,
    tunnel,
    upgrade,
):
    opts = MiniserveOpts(
        upload=upload,
        delete=delete,
        auth=auth,
        qrcode=qrcode,
        hidden=hidden,
        verbose=verbose,
        enable_archive=archive,
    )
    source = make_source(path, tunnel, upgrade)
    sess = source.open(resolve_port(port), bind, opts)
    run_until_done(sess)


if __name__ == "__main__":
    cli()
