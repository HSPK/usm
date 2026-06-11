#!/usr/bin/env python3
"""Turn a box into an HTTP/SOCKS (+ Shadowsocks) proxy, or a Clash client that
routes rule-matched traffic through one — powered by mihomo (Clash.Meta).

Two roles, one binary (auto-installed to ``~/.cache/usm/bin/mihomo``):

Server — make *this* machine a proxy other hosts dial into:
  usm proxy server                              # mixed HTTP+SOCKS on :7890
  usm proxy server --auth alice:s3cret          # require username/password
  usm proxy server --ss                         # also expose encrypted ss://
  usm proxy server --ss --no-mixed --host 1.2.3.4   # ss:// only, advertise IP
  usm proxy url 0                               # print URLs to feed a client

Client — send rule-matched traffic out through a remote proxy:
  usm proxy client http://alice:s3cret@1.2.3.4:7890
  usm proxy client ss://...                     # paste an `usm proxy url` line
  usm proxy client http://1.2.3.4:7890 --rule 'DOMAIN-SUFFIX,github.com,PROXY' \\
                  --final direct                # only matched traffic proxied
  # then point apps at http://127.0.0.1:7890

Manage (like `usm tunnel`):
  usm proxy ls
  usm proxy stop 0            usm proxy start 0          usm proxy restart 0
  usm proxy enable 0          # systemd --user autostart
  usm proxy logs 0 -n 100     usm proxy show 0           usm proxy rm 0
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

import click
import yaml
from rich.console import Console
from rich.table import Table

MIHOMO_VERSION = "1.19.27"
MIHOMO_RELEASE = (
    f"https://github.com/MetaCubeX/mihomo/releases/download/v{MIHOMO_VERSION}/"
)

USM_CACHE_DIR = Path.home() / ".cache" / "usm"
LOCAL_BIN_DIR = USM_CACHE_DIR / "bin"
STATE_DIR = USM_CACHE_DIR / "proxy"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_PREFIX = "usm-proxy-"

DEFAULT_MIXED_PORT = 7890
DEFAULT_SS_PORT = 8388
DEFAULT_CIPHER = "aes-256-gcm"
SS_CIPHERS = (
    "aes-128-gcm",
    "aes-256-gcm",
    "chacha20-ietf-poly1305",
    "xchacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
)
# Always kept DIRECT in rule mode so local/LAN traffic never loops the proxy.
PRIVATE_CIDRS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "169.254.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)

try:
    _SIGKILL = signal.SIGKILL
except AttributeError:
    _SIGKILL = signal.SIGTERM

console = Console()


# Data model ---------------------------------------------------------------


@dataclass
class Instance:
    id: str
    role: str  # "server" | "client"
    listen: str
    port: int  # mixed HTTP+SOCKS inbound port (0 = disabled, server only)
    # server inbound
    auth: Optional[str] = None  # "user:pass" applied to http/socks
    ss_port: Optional[int] = None
    ss_cipher: Optional[str] = None
    ss_password: Optional[str] = None
    public_host: Optional[str] = None  # advertised host for `url`
    # client outbound
    upstream: Optional[str] = None
    final: str = "proxy"  # MATCH target: "proxy" | "direct"
    rules: list[str] = field(default_factory=list)
    controller: Optional[str] = None  # external-controller addr
    secret: Optional[str] = None
    # runtime
    pid: Optional[int] = None
    started_at: Optional[float] = None

    def dir(self) -> Path:
        return STATE_DIR / self.id

    def state_path(self) -> Path:
        return self.dir() / "state.json"

    def config_path(self) -> Path:
        return self.dir() / "config.yaml"

    def log_path(self) -> Path:
        return self.dir() / "mihomo.log"

    def save(self) -> None:
        self.dir().mkdir(parents=True, exist_ok=True)
        self.state_path().write_text(json.dumps(asdict(self), indent=2))

    def alive(self) -> bool:
        if _is_enabled(self.id):
            return _systemd_is_active(self.id)
        if not self.pid:
            return False
        try:
            os.kill(self.pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    def route(self) -> str:
        if self.role == "server":
            parts = []
            if self.port:
                parts.append(f"http+socks :{self.port}")
            if self.ss_port:
                parts.append(f"ss :{self.ss_port}")
            tail = " (auth)" if self.auth else ""
            return f"{self.listen} → " + ", ".join(parts) + tail
        final = "PROXY" if self.final == "proxy" else "DIRECT"
        return f"{self.listen}:{self.port} → {_redact(self.upstream)} [MATCH→{final}]"


# Binary install (mihomo) ---------------------------------------------------


def _mihomo_filename() -> str:
    return "mihomo.exe" if platform.system().lower() == "windows" else "mihomo"


def _local_mihomo() -> Path:
    return LOCAL_BIN_DIR / _mihomo_filename()


def _asset_name() -> str:
    s, m = platform.system().lower(), platform.machine().lower()
    v = MIHOMO_VERSION
    if s == "linux":
        if m in ("x86_64", "amd64"):
            return f"mihomo-linux-amd64-compatible-v{v}.gz"
        if m in ("aarch64", "arm64"):
            return f"mihomo-linux-arm64-v{v}.gz"
        if m.startswith("armv7") or m == "armv8l":
            return f"mihomo-linux-armv7-v{v}.gz"
        if m in ("i386", "i686", "x86"):
            return f"mihomo-linux-386-v{v}.gz"
    elif s == "darwin":
        if m in ("x86_64", "amd64"):
            return f"mihomo-darwin-amd64-compatible-v{v}.gz"
        if m in ("arm64", "aarch64"):
            return f"mihomo-darwin-arm64-v{v}.gz"
    elif s == "windows":
        if m in ("amd64", "x86_64"):
            return f"mihomo-windows-amd64-compatible-v{v}.zip"
        if m in ("arm64", "aarch64"):
            return f"mihomo-windows-arm64-v{v}.zip"
    raise click.ClickException(
        f"no mihomo build mapped for {platform.system()}/{platform.machine()}; "
        "install it manually and set $USM_MIHOMO_BIN. "
        "See https://github.com/MetaCubeX/mihomo/releases"
    )


def _download_extract(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            payload = r.read()
    except (urllib.error.URLError, OSError) as e:
        raise click.ClickException(f"download failed: {url}: {e}") from e
    try:
        if url.endswith(".gz"):
            tmp.write_bytes(gzip.decompress(payload))
        elif url.endswith(".zip"):
            import io

            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                name = next(
                    (n for n in zf.namelist() if n.lower().endswith("mihomo.exe")),
                    None,
                ) or next((n for n in zf.namelist() if "mihomo" in n.lower()), None)
                if not name:
                    raise click.ClickException("no mihomo binary inside the zip asset")
                tmp.write_bytes(zf.read(name))
        else:
            tmp.write_bytes(payload)
    except (OSError, zipfile.BadZipFile, EOFError) as e:
        tmp.unlink(missing_ok=True)
        raise click.ClickException(f"failed to extract {url}: {e}") from e
    tmp.chmod(0o755)
    tmp.replace(dest)


def ensure_mihomo(*, upgrade: bool = False) -> Path:
    """Resolve a usable mihomo binary.

    Order: ``$USM_MIHOMO_BIN`` escape hatch → managed pinned binary →
    download the pinned release into ``~/.cache/usm/bin``.
    """
    override = os.environ.get("USM_MIHOMO_BIN")
    if override and os.access(override, os.X_OK):
        return Path(override)
    local = _local_mihomo()
    if not upgrade and local.exists():
        return local
    asset = _asset_name()
    console.print(f"[dim]installing mihomo {MIHOMO_VERSION} ({asset}) → {local}[/dim]")
    _download_extract(MIHOMO_RELEASE + asset, local)
    return local


# Upstream URL <-> proxy dict ----------------------------------------------


def _b64decode(s: str) -> str:
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4)).decode()


def _parse_ss(url: str) -> tuple[str, str, str, int, str]:
    """Parse an ``ss://`` URL (SIP002 or legacy) → (cipher, pwd, host, port, tag)."""
    body = url[len("ss://") :]
    tag = ""
    if "#" in body:
        body, tag = body.split("#", 1)
    if "@" in body:  # SIP002: base64(method:pass)@host:port
        userinfo, hostport = body.rsplit("@", 1)
        try:
            method, password = _b64decode(userinfo).split(":", 1)
        except Exception:
            method, password = userinfo.split(":", 1)
    else:  # legacy: base64(method:pass@host:port)
        decoded = _b64decode(body)
        method, rest = decoded.split(":", 1)
        password, hostport = rest.rsplit("@", 1)
    if ":" not in hostport:
        raise click.BadParameter(f"ss:// URL missing host:port: {url!r}")
    host, port = hostport.rsplit(":", 1)
    try:
        return method, password, host, int(port), urllib.parse.unquote(tag)
    except ValueError as e:
        raise click.BadParameter(f"ss:// URL has a non-numeric port: {url!r}") from e


def _emit_ss(cipher: str, password: str, host: str, port: int, tag: str = "") -> str:
    ui = base64.urlsafe_b64encode(f"{cipher}:{password}".encode()).decode().rstrip("=")
    url = f"ss://{ui}@{host}:{port}"
    if tag:
        url += "#" + urllib.parse.quote(tag)
    return url


def _proxy_from_upstream(url: str) -> dict:
    """Build a mihomo proxy dict (named ``gw``) from an upstream proxy URL."""
    if url.startswith("ss://"):
        cipher, password, host, port, _ = _parse_ss(url)
        return {
            "name": "gw",
            "type": "ss",
            "server": host,
            "port": port,
            "cipher": cipher,
            "password": password,
            "udp": True,
        }
    p = urllib.parse.urlsplit(url)
    if not p.hostname or not p.port:
        raise click.BadParameter(
            f"upstream must include host and port: {url!r} "
            "(e.g. http://user:pass@host:7890)"
        )
    if p.scheme in ("http", "https"):
        proxy = {"name": "gw", "type": "http", "server": p.hostname, "port": p.port}
        if p.scheme == "https":
            proxy["tls"] = True
        if p.username:
            proxy["username"] = urllib.parse.unquote(p.username)
        if p.password:
            proxy["password"] = urllib.parse.unquote(p.password)
        return proxy
    if p.scheme in ("socks5", "socks", "socks5h"):
        proxy = {
            "name": "gw",
            "type": "socks5",
            "server": p.hostname,
            "port": p.port,
            "udp": True,
        }
        if p.username:
            proxy["username"] = urllib.parse.unquote(p.username)
        if p.password:
            proxy["password"] = urllib.parse.unquote(p.password)
        return proxy
    raise click.BadParameter(
        f"unsupported upstream scheme in {url!r}; use http(s)://, socks5://, or ss://"
    )


def _redact(url: Optional[str]) -> str:
    if not url:
        return "?"
    if url.startswith("ss://"):
        return "ss://…"
    return re.sub(r"//[^@/]+@", "//…@", url)


# Config generation ---------------------------------------------------------


def _server_config(inst: Instance) -> dict:
    listeners: list[dict] = []
    if inst.port:
        listeners.append(
            {
                "name": "mixed-in",
                "type": "mixed",
                "port": inst.port,
                "listen": inst.listen,
            }
        )
    if inst.ss_port:
        listeners.append(
            {
                "name": "ss-in",
                "type": "shadowsocks",
                "port": inst.ss_port,
                "listen": inst.listen,
                "cipher": inst.ss_cipher,
                "password": inst.ss_password,
                "udp": True,
            }
        )
    cfg: dict = {
        "log-level": "info",
        "ipv6": True,
        "mode": "direct",
        "listeners": listeners,
    }
    if inst.auth:
        cfg["authentication"] = [inst.auth]
    return cfg


def _build_rules(inst: Instance) -> list[str]:
    rules = [f"IP-CIDR,{c},DIRECT,no-resolve" for c in PRIVATE_CIDRS]
    rules += list(inst.rules)
    rules.append("MATCH,PROXY" if inst.final == "proxy" else "MATCH,DIRECT")
    return rules


def _client_config(inst: Instance) -> dict:
    proxy = _proxy_from_upstream(inst.upstream or "")
    cfg: dict = {
        "log-level": "info",
        "ipv6": True,
        "mode": "rule",
        "listeners": [
            {
                "name": "mixed-in",
                "type": "mixed",
                "port": inst.port,
                "listen": inst.listen,
            }
        ],
        "proxies": [proxy],
        "proxy-groups": [
            {"name": "PROXY", "type": "select", "proxies": [proxy["name"], "DIRECT"]}
        ],
        "rules": _build_rules(inst),
    }
    if inst.controller:
        cfg["external-controller"] = inst.controller
        if inst.secret:
            cfg["secret"] = inst.secret
    return cfg


def _write_config(inst: Instance) -> None:
    inst.dir().mkdir(parents=True, exist_ok=True)
    cfg = _server_config(inst) if inst.role == "server" else _client_config(inst)
    inst.config_path().write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    )


def _server_urls(inst: Instance) -> list[str]:
    host = inst.public_host or _guess_host()
    out: list[str] = []
    if inst.port:
        cred = ""
        if inst.auth:
            user, _, pw = inst.auth.partition(":")
            cred = f"{urllib.parse.quote(user)}:{urllib.parse.quote(pw)}@"
        out.append(f"http://{cred}{host}:{inst.port}")
        out.append(f"socks5://{cred}{host}:{inst.port}")
    if inst.ss_port:
        out.append(
            _emit_ss(
                inst.ss_cipher, inst.ss_password, host, inst.ss_port, f"usm-{inst.id}"
            )
        )
    return out


def _guess_host() -> str:
    """Best-effort primary egress IP (no packets sent); user should pass --host."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "<server-ip>"


# State helpers -------------------------------------------------------------


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower() or "proxy"


def _make_id(custom: Optional[str]) -> str:
    if custom:
        return _slug(custom)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    used = set()
    for p in STATE_DIR.iterdir():
        if p.is_dir():
            try:
                used.add(int(p.name))
            except ValueError:
                continue
    n = 0
    while n in used:
        n += 1
    return str(n)


def _instance_from_raw(raw: dict) -> Instance:
    allowed = {f.name for f in fields(Instance)}
    return Instance(**{k: v for k, v in raw.items() if k in allowed})


def _load_all() -> list[Instance]:
    if not STATE_DIR.exists():
        return []
    out: list[Instance] = []
    for d in sorted(STATE_DIR.iterdir()):
        sp = d / "state.json"
        if not sp.exists():
            continue
        try:
            out.append(_instance_from_raw(json.loads(sp.read_text())))
        except (json.JSONDecodeError, TypeError, OSError):
            continue
    return out


def _load(iid: str) -> Instance:
    sp = STATE_DIR / iid / "state.json"
    if not sp.exists():
        raise click.ClickException(f"No proxy with id '{iid}'.")
    return _instance_from_raw(json.loads(sp.read_text()))


def _delete(inst: Instance) -> None:
    shutil.rmtree(inst.dir(), ignore_errors=True)


def _tail(path: Path, n: int) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _fmt_uptime(secs: float) -> str:
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# systemd user-unit helpers -------------------------------------------------


def _unit_name(iid: str) -> str:
    return f"{UNIT_PREFIX}{iid}.service"


def _unit_path(iid: str) -> Path:
    return SYSTEMD_USER_DIR / _unit_name(iid)


def _is_enabled(iid: str) -> bool:
    return _unit_path(iid).exists()


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], text=True, capture_output=True, check=check
    )


def _require_systemd() -> None:
    if os.name != "posix" or not shutil.which("systemctl"):
        raise click.ClickException(
            "Autostart needs systemd (user instance). Not available on this system."
        )
    p = _systemctl("--version")
    if p.returncode != 0:
        raise click.ClickException(
            f"systemctl --user not usable: {p.stderr.strip() or p.stdout.strip()}"
        )


def _systemd_is_active(iid: str) -> bool:
    return _systemctl("is-active", _unit_name(iid)).stdout.strip() == "active"


def _systemd_main_pid(iid: str) -> Optional[int]:
    p = _systemctl("show", "-p", "MainPID", "--value", _unit_name(iid))
    try:
        pid = int(p.stdout.strip())
    except ValueError:
        return None
    return pid or None


def _current_user() -> str:
    try:
        return os.getlogin()
    except OSError:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name


def _linger_enabled() -> bool:
    if not shutil.which("loginctl"):
        return False
    try:
        out = subprocess.check_output(
            ["loginctl", "show-user", _current_user(), "-p", "Linger", "--value"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return False
    return out.lower() == "yes"


def _render_unit(inst: Instance, usm_bin: str) -> str:
    uv_bin = shutil.which("uv")
    extra_paths = [os.path.dirname(usm_bin)]
    if uv_bin:
        extra_paths.append(os.path.dirname(uv_bin))
    path_value = ":".join(
        dict.fromkeys(
            extra_paths
            + [
                f"{Path.home()}/.local/bin",
                f"{Path.home()}/.cargo/bin",
                "/usr/local/sbin",
                "/usr/local/bin",
                "/usr/sbin",
                "/usr/bin",
                "/sbin",
                "/bin",
            ]
        )
    )
    return (
        "[Unit]\n"
        f"Description=usm proxy {inst.id} ({inst.role}): {inst.route()}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f'Environment="PATH={path_value}"\n'
        f"ExecStart={usm_bin} proxy up {inst.id}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# Launch / stop -------------------------------------------------------------


def _test_config(inst: Instance) -> None:
    """Run ``mihomo -t`` so bad rules/ciphers fail loudly before we launch."""
    mihomo = ensure_mihomo()
    p = subprocess.run(
        [str(mihomo), "-t", "-d", str(inst.dir()), "-f", str(inst.config_path())],
        text=True,
        capture_output=True,
    )
    if p.returncode != 0:
        lines = [
            ln
            for ln in (p.stdout + p.stderr).splitlines()
            if "error" in ln.lower() or "fatal" in ln.lower()
        ]
        detail = lines[-1] if lines else (p.stderr.strip() or p.stdout.strip())
        raise click.ClickException(f"mihomo rejected the config: {detail}")


def _build_argv(inst: Instance) -> list[str]:
    mihomo = ensure_mihomo()
    return [str(mihomo), "-d", str(inst.dir()), "-f", str(inst.config_path())]


def _start(inst: Instance, *, new: bool = False) -> None:
    _write_config(inst)
    _test_config(inst)
    argv = _build_argv(inst)
    log = open(inst.log_path(), "ab", buffering=0)
    log.write(f"\n--- start {time.strftime('%Y-%m-%dT%H:%M:%S')} ---\n".encode())
    log.write(("$ " + " ".join(argv) + "\n").encode())

    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except FileNotFoundError as exc:
        raise click.ClickException(f"{argv[0]} not found.") from exc

    inst.pid = proc.pid
    inst.started_at = time.time()
    inst.save()

    time.sleep(1.5)
    if proc.poll() is not None:
        tail = _tail(inst.log_path(), 12)
        if new:
            _delete(inst)
        else:
            inst.pid = None
            inst.started_at = None
            inst.save()
        console.print(
            f"[red]✗[/red] mihomo exited immediately (code {proc.returncode}). Log:"
        )
        for line in tail:
            console.print(f"  [dim]{line}[/dim]")
        raise click.ClickException("Proxy failed to start.")

    console.print(f"[green]✓[/green] Started [bold]{inst.id}[/bold] (pid {inst.pid})")
    console.print(f"  {inst.route()}")
    if inst.role == "server":
        console.print("  [dim]connect URLs:[/dim]")
        for u in _server_urls(inst):
            console.print(f"    {u}")
        console.print("  [dim]copy one into:[/dim] usm proxy client <url>")
    else:
        console.print(f"  [dim]point apps at[/dim] http://{inst.listen}:{inst.port}")


def _kill_pid(inst: Instance) -> bool:
    if not inst.pid:
        return False
    try:
        os.kill(inst.pid, 0)
    except OSError:
        return False
    try:
        os.kill(inst.pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(inst.pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    try:
        os.kill(inst.pid, _SIGKILL)
    except OSError:
        pass
    return True


def _ensure_free_id(iid: str) -> None:
    if (STATE_DIR / iid / "state.json").exists():
        raise click.ClickException(
            f"A proxy with id '{iid}' already exists. "
            f"Use 'usm proxy start {iid}', 'usm proxy rm {iid}', or pass --name."
        )


# CLI -----------------------------------------------------------------------


@click.group(
    help=__doc__.splitlines()[0],
    context_settings={"help_option_names": ["-h", "--help"]},
)
def cli() -> None:
    pass


@cli.command("server", short_help="Make this box an HTTP/SOCKS (+ ss) proxy.")
@click.option(
    "-p",
    "--port",
    type=int,
    default=DEFAULT_MIXED_PORT,
    show_default=True,
    help="Mixed HTTP+SOCKS inbound port.",
)
@click.option(
    "--mixed/--no-mixed",
    default=True,
    show_default=True,
    help="Expose the mixed HTTP+SOCKS inbound.",
)
@click.option(
    "--listen", default="0.0.0.0", show_default=True, help="Bind address for inbounds."
)
@click.option(
    "--auth",
    metavar="USER:PASS",
    help="Require username/password on the HTTP/SOCKS inbound.",
)
@click.option(
    "--ss", "ss_on", is_flag=True, help="Also expose an encrypted ss:// inbound."
)
@click.option(
    "--ss-port",
    type=int,
    default=DEFAULT_SS_PORT,
    show_default=True,
    help="Shadowsocks inbound port (implies --ss).",
)
@click.option(
    "--cipher",
    type=click.Choice(SS_CIPHERS),
    default=DEFAULT_CIPHER,
    show_default=True,
    help="Shadowsocks cipher.",
)
@click.option("--password", help="Shadowsocks password (default: random).")
@click.option(
    "--host",
    "public_host",
    help="Host/IP advertised in `usm proxy url` (default: auto-detected).",
)
@click.option("--name", help="Custom id (default: next free integer).")
def cmd_server(
    port, mixed, listen, auth, ss_on, ss_port, cipher, password, public_host, name
):
    ss_on = ss_on or bool(password) or ss_port != DEFAULT_SS_PORT
    if not mixed and not ss_on:
        raise click.BadParameter("nothing to serve: enable --mixed and/or --ss.")
    if auth and ":" not in auth:
        raise click.BadParameter("--auth must be USER:PASS.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    iid = _make_id(name)
    _ensure_free_id(iid)
    inst = Instance(
        id=iid,
        role="server",
        listen=listen,
        port=port if mixed else 0,
        auth=auth,
        ss_port=ss_port if ss_on else None,
        ss_cipher=cipher if ss_on else None,
        ss_password=(password or secrets.token_urlsafe(16)) if ss_on else None,
        public_host=public_host,
    )
    _start(inst, new=True)


@cli.command("client", short_help="Route rule-matched traffic through a remote proxy.")
@click.argument("upstream")
@click.option(
    "-p",
    "--port",
    type=int,
    default=DEFAULT_MIXED_PORT,
    show_default=True,
    help="Local mixed HTTP+SOCKS inbound for your apps.",
)
@click.option(
    "--listen",
    default="127.0.0.1",
    show_default=True,
    help="Bind address for the local inbound.",
)
@click.option(
    "--rule",
    "rules",
    multiple=True,
    metavar="RULE",
    help="Extra mihomo rule, e.g. 'DOMAIN-SUFFIX,github.com,PROXY' (repeatable).",
)
@click.option(
    "--rules-file",
    type=click.Path(exists=True, dir_okay=False),
    help="File of rules (one per line; '#' comments ignored).",
)
@click.option(
    "--final",
    type=click.Choice(["proxy", "direct"]),
    default="proxy",
    show_default=True,
    help="Where unmatched traffic goes (the MATCH rule).",
)
@click.option(
    "--controller",
    is_flag=False,
    flag_value="127.0.0.1:9090",
    default=None,
    help="Enable the RESTful controller (default 127.0.0.1:9090).",
)
@click.option("--secret", help="Secret for the external controller.")
@click.option("--name", help="Custom id (default: next free integer).")
def cmd_client(
    upstream, port, listen, rules, rules_file, final, controller, secret, name
):
    extra = list(rules)
    if rules_file:
        for line in Path(rules_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                extra.append(line)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    iid = _make_id(name)
    _ensure_free_id(iid)
    inst = Instance(
        id=iid,
        role="client",
        listen=listen,
        port=port,
        upstream=upstream,
        final=final,
        rules=extra,
        controller=controller,
        secret=secret,
    )
    _proxy_from_upstream(upstream)  # validate early with a clear error
    _start(inst, new=True)


@cli.command("ls", short_help="List proxies.")
@click.option("--prune", is_flag=True, help="Delete stopped, non-enabled definitions.")
def cmd_ls(prune):
    insts = _load_all()
    if prune:
        gone = [i for i in insts if not i.alive() and not _is_enabled(i.id)]
        for i in gone:
            _delete(i)
        if gone:
            console.print(f"[dim]Pruned {len(gone)} stopped proxy(ies).[/dim]")
        insts = [i for i in insts if i not in gone]
    if not insts:
        console.print("[dim]No proxies recorded.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("Role")
    table.add_column("Route")
    table.add_column("PID", justify="right")
    table.add_column("Up", justify="right")
    table.add_column("Status")
    table.add_column("Boot")
    for i in insts:
        enabled = _is_enabled(i.id)
        if enabled:
            pid = _systemd_main_pid(i.id)
            alive = bool(pid) and _systemd_is_active(i.id)
        else:
            pid = i.pid if (i.pid and i.alive()) else None
            alive = bool(pid)
        up = _fmt_uptime(time.time() - i.started_at) if alive and i.started_at else "-"
        status = "[green]running[/green]" if alive else "[dim]stopped[/dim]"
        boot = "[cyan]enabled[/cyan]" if enabled else "[dim]-[/dim]"
        table.add_row(i.id, i.role, i.route(), str(pid or "-"), up, status, boot)
    console.print(table)


@cli.command("url", short_help="Print connect URLs for a server proxy.")
@click.argument("iid")
def cmd_url(iid):
    inst = _load(iid)
    if inst.role != "server":
        raise click.ClickException(f"{iid} is a client; nothing to hand out.")
    for u in _server_urls(inst):
        click.echo(u)


@cli.command("stop", short_help="Stop a proxy (keeps the definition).")
@click.argument("target")
def cmd_stop(target):
    insts = _load_all() if target == "all" else [_load(target)]
    if not insts:
        console.print("[dim]Nothing to stop.[/dim]")
        return
    for inst in insts:
        if _is_enabled(inst.id):
            p = _systemctl("stop", _unit_name(inst.id))
            ok = p.returncode == 0
            console.print(
                f"[green]✓[/green] {inst.id}: stopped (systemd)"
                if ok
                else f"[red]✗[/red] {inst.id}: {p.stderr.strip() or 'stop failed'}"
            )
            continue
        was = _kill_pid(inst)
        inst.pid = None
        inst.started_at = None
        inst.save()
        console.print(
            f"[green]✓[/green] {inst.id}: {'stopped' if was else 'already stopped'}"
        )


@cli.command("start", short_help="Start a stopped proxy by id.")
@click.argument("iid")
def cmd_start(iid):
    inst = _load(iid)
    if _is_enabled(iid):
        p = _systemctl("start", _unit_name(iid))
        if p.returncode != 0:
            raise click.ClickException(p.stderr.strip() or "systemctl start failed.")
        console.print(f"[green]✓[/green] Started {iid} via systemd.")
        return
    if inst.alive():
        raise click.ClickException(f"{iid} is already running (pid {inst.pid}).")
    _start(inst)


@cli.command("restart", short_help="Restart a proxy by id.")
@click.argument("iid")
def cmd_restart(iid):
    inst = _load(iid)
    if _is_enabled(iid):
        p = _systemctl("restart", _unit_name(iid))
        if p.returncode != 0:
            raise click.ClickException(p.stderr.strip() or "systemctl restart failed.")
        console.print(f"[green]✓[/green] Restarted {iid} via systemd.")
        return
    _kill_pid(inst)
    inst.pid = None
    inst.started_at = None
    _start(inst)


@cli.command("rm", short_help="Delete a proxy definition.")
@click.argument("target")
def cmd_rm(target):
    insts = _load_all() if target == "all" else [_load(target)]
    if not insts:
        console.print("[dim]Nothing to remove.[/dim]")
        return
    for inst in insts:
        if _is_enabled(inst.id):
            _systemctl("disable", "--now", _unit_name(inst.id))
            _unit_path(inst.id).unlink(missing_ok=True)
            _systemctl("daemon-reload")
        _kill_pid(inst)
        _delete(inst)
        console.print(f"[green]✓[/green] removed {inst.id}")


@cli.command("enable", short_help="Install a systemd user unit so it autostarts.")
@click.argument("iid")
def cmd_enable(iid):
    inst = _load(iid)
    _require_systemd()
    usm_bin = shutil.which("usm")
    if not usm_bin:
        raise click.ClickException(
            "'usm' not found on PATH; install it (e.g. `uv tool install usmo`) first."
        )
    _write_config(inst)
    _test_config(inst)
    _kill_pid(inst)
    inst.pid = None
    inst.started_at = time.time()
    inst.save()
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    _unit_path(iid).write_text(_render_unit(inst, usm_bin))
    _systemctl("daemon-reload", check=True)
    p = _systemctl("enable", "--now", _unit_name(iid))
    if p.returncode != 0:
        raise click.ClickException(p.stderr.strip() or "systemctl enable --now failed.")
    console.print(f"[green]✓[/green] Enabled & started [bold]{iid}[/bold].")
    if not _linger_enabled():
        console.print(
            "  [yellow]note:[/yellow] to start at boot without logging in, run "
            f"[bold]sudo loginctl enable-linger {_current_user()}[/bold]"
        )


@cli.command("disable", short_help="Remove the systemd user unit (keeps definition).")
@click.argument("iid")
def cmd_disable(iid):
    _load(iid)
    if not _is_enabled(iid):
        console.print(f"[dim]{iid} is not enabled.[/dim]")
        return
    _require_systemd()
    _systemctl("disable", "--now", _unit_name(iid))
    _unit_path(iid).unlink(missing_ok=True)
    _systemctl("daemon-reload")
    console.print(f"[green]✓[/green] Disabled {iid}.")


@cli.command("up", hidden=True, short_help="(internal) exec mihomo in foreground.")
@click.argument("iid")
def cmd_up(iid):
    inst = _load(iid)
    _write_config(inst)
    argv = _build_argv(inst)
    inst.pid = os.getpid()
    inst.started_at = time.time()
    inst.save()
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError as exc:
        raise click.ClickException(f"{argv[0]} not found.") from exc


@cli.command("show", short_help="Show a proxy definition (secrets redacted).")
@click.argument("iid")
def cmd_show(iid):
    inst = _load(iid)
    data = asdict(inst)
    if data.get("upstream"):
        data["upstream"] = _redact(data["upstream"])
    if data.get("ss_password"):
        data["ss_password"] = "***"
    if data.get("auth"):
        user = data["auth"].split(":", 1)[0]
        data["auth"] = f"{user}:***"
    if data.get("secret"):
        data["secret"] = "***"
    data["alive"] = inst.alive()
    data["enabled"] = _is_enabled(iid)
    data["config_path"] = str(inst.config_path())
    console.print_json(json.dumps(data))


@cli.command("logs", short_help="Print the tail of a proxy log.")
@click.argument("iid")
@click.option("-n", "--lines", type=int, default=50, show_default=True)
def cmd_logs(iid, lines):
    inst = _load(iid)
    if _is_enabled(iid) and not inst.log_path().exists():
        console.print(
            f"[dim]Running under systemd; use[/dim] journalctl --user -u {_unit_name(iid)}"
        )
        return
    if not inst.log_path().exists():
        console.print(f"[dim]No logs for {iid}.[/dim]")
        return
    for line in _tail(inst.log_path(), lines):
        click.echo(line)


@cli.command("install", short_help="Pre-download the mihomo binary.")
@click.option("--upgrade", is_flag=True, help="Re-download even if already present.")
def cmd_install(upgrade):
    path = ensure_mihomo(upgrade=upgrade)
    console.print(f"[green]✓[/green] mihomo {MIHOMO_VERSION} at {path}")


def main() -> None:
    try:
        cli(standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.Abort:
        sys.exit(130)


if __name__ == "__main__":
    main()
