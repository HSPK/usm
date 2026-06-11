#!/usr/bin/env python3
"""A ClashX-style command-line manager for the mihomo (Clash.Meta) core.

Subscriptions, profile switching, rule/global/direct mode, node selection,
latency tests, TUN, system-proxy, LAN sharing, live logs/traffic, and
connection inspection — all from the terminal. The mihomo binary is
auto-installed to ``~/.cache/usm/bin/mihomo`` (no sudo, no system packages).

Quick start
-----------
  usm clash sub add https://example.com/sub --name work   # add a subscription
  usm clash up                                             # start the core
  usm clash status                                         # what's running
  usm clash proxies                                        # groups + nodes
  usm clash test PROXY                                     # latency-test a group
  usm clash select PROXY my-node                           # pick a node
  usm clash mode global                                    # rule | global | direct
  usm clash system-proxy on                                # set OS proxy
  usm clash logs -f                                        # stream live logs
  usm clash down                                           # stop

Routing modes
-------------
  rule    follow the profile's rules (default)
  global  send everything through the selected node
  direct  bypass all proxies

TUN (transparent, system-wide) needs CAP_NET_ADMIN; ``usm clash tun on``
prints the one-time ``setcap`` command when it's missing.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import zipfile
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import click
import requests
import yaml
from rich.console import Console
from rich.table import Table

MIHOMO_VERSION = "1.19.27"
MIHOMO_RELEASE = (
    f"https://github.com/MetaCubeX/mihomo/releases/download/v{MIHOMO_VERSION}/"
)

USM_CACHE_DIR = Path.home() / ".cache" / "usm"
LOCAL_BIN_DIR = USM_CACHE_DIR / "bin"
ROOT = USM_CACHE_DIR / "clash"
PROFILES_DIR = ROOT / "profiles"
STATE_PATH = ROOT / "state.json"
RUNTIME_CONFIG = ROOT / "runtime.yaml"
LOG_PATH = ROOT / "mihomo.log"
PROXY_ENV_PATH = ROOT / "proxy.env"

SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_NAME = "usm-clash.service"

DEFAULT_PORT = 7890
DEFAULT_CONTROLLER = "127.0.0.1:9090"
DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"
MODES = ("rule", "global", "direct")
TUN_STACKS = ("gvisor", "system", "mixed")
CLASH_UA = f"clash.meta/usm mihomo/{MIHOMO_VERSION}"
DASHBOARD_BASE = "https://d.metacubex.one"

try:
    _SIGKILL = signal.SIGKILL
except AttributeError:
    _SIGKILL = signal.SIGTERM

console = Console()


# mihomo binary install -----------------------------------------------------


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
        payload = requests.get(url, timeout=120).content
    except requests.RequestException as e:
        raise click.ClickException(f"download failed: {url}: {e}") from e
    try:
        if url.endswith(".gz"):
            tmp.write_bytes(gzip.decompress(payload))
        elif url.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                name = next(
                    (n for n in zf.namelist() if n.lower().endswith("mihomo.exe")), None
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


# Manager state -------------------------------------------------------------


@dataclass
class State:
    active: Optional[str] = None
    port: int = DEFAULT_PORT
    mode: str = "rule"
    allow_lan: bool = False
    tun: bool = False
    tun_stack: str = "gvisor"
    system_proxy: bool = False
    controller: str = DEFAULT_CONTROLLER
    secret: str = ""
    log_level: str = "info"
    pid: Optional[int] = None
    started_at: Optional[float] = None
    sysproxy_backup: Optional[dict] = None


def load_state() -> State:
    if not STATE_PATH.exists():
        return State()
    try:
        raw = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return State()
    allowed = {f.name for f in fields(State)}
    return State(**{k: v for k, v in raw.items() if k in allowed})


def save_state(state: State) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(asdict(state), indent=2))


def _is_running(state: State) -> bool:
    if _is_enabled():
        return _systemd_is_active()
    if not state.pid:
        return False
    try:
        os.kill(state.pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


# Profiles & subscriptions --------------------------------------------------


def _profile_yaml(name: str) -> Path:
    return PROFILES_DIR / f"{name}.yaml"


def _profile_meta(name: str) -> Path:
    return PROFILES_DIR / f"{name}.json"


def _list_profiles() -> list[dict]:
    if not PROFILES_DIR.exists():
        return []
    out = []
    for meta in sorted(PROFILES_DIR.glob("*.json")):
        try:
            out.append(json.loads(meta.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def _load_profile_meta(name: str) -> dict:
    meta = _profile_meta(name)
    if not meta.exists():
        raise click.ClickException(
            f"No profile '{name}'. Add one with 'usm clash sub add <url>'."
        )
    return json.loads(meta.read_text())


def _parse_userinfo(header: str) -> dict:
    info: dict = {}
    for part in header.split(";"):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            try:
                info[k] = int(v)
            except ValueError:
                info[k] = v
    return info


def _fetch_subscription(url: str) -> tuple[str, dict]:
    """Fetch a remote Clash config; return (yaml_text, userinfo)."""
    try:
        r = requests.get(url, headers={"User-Agent": CLASH_UA}, timeout=30)
    except requests.RequestException as e:
        raise click.ClickException(f"failed to fetch subscription: {e}") from e
    if r.status_code != 200:
        raise click.ClickException(f"subscription returned HTTP {r.status_code}")
    text = r.text
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        doc = None
    if not (isinstance(doc, dict) and "proxies" in doc):
        raise click.ClickException(
            "subscription did not return a Clash config (no 'proxies:' found). "
            "Use the provider's Clash/Clash.Meta subscription link "
            "(raw base64 node lists are not supported)."
        )
    userinfo = {}
    header = r.headers.get("Subscription-Userinfo") or r.headers.get(
        "subscription-userinfo"
    )
    if header:
        userinfo = _parse_userinfo(header)
    return text, userinfo


def _validate_clash_text(text: str) -> None:
    doc = yaml.safe_load(text)
    if not (isinstance(doc, dict) and "proxies" in doc):
        raise click.ClickException("not a Clash config (no 'proxies:' key).")


def _save_profile(
    name: str, url: str, kind: str, text: str, userinfo: dict, interval: int
) -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    _profile_yaml(name).write_text(text)
    existing = {}
    if _profile_meta(name).exists():
        try:
            existing = json.loads(_profile_meta(name).read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}
    now = time.time()
    meta = {
        "name": name,
        "url": url,
        "type": kind,
        "added_at": existing.get("added_at", now),
        "updated_at": now,
        "interval_hours": interval,
        "userinfo": userinfo or existing.get("userinfo") or {},
    }
    _profile_meta(name).write_text(json.dumps(meta, indent=2))


def _refresh_profile(meta: dict) -> bool:
    if meta.get("type") != "remote":
        return False
    text, userinfo = _fetch_subscription(meta["url"])
    _save_profile(
        meta["name"],
        meta["url"],
        "remote",
        text,
        userinfo,
        meta.get("interval_hours", 0),
    )
    return True


# Config composition --------------------------------------------------------


def _default_dns() -> dict:
    return {
        "enable": True,
        "ipv6": True,
        "enhanced-mode": "fake-ip",
        "fake-ip-range": "198.18.0.1/16",
        "nameserver": ["https://dns.google/dns-query", "tls://8.8.8.8"],
        "fallback": ["https://1.1.1.1/dns-query", "tls://1.0.0.1"],
    }


def _compose_config(state: State) -> dict:
    if not state.active:
        raise click.ClickException(
            "No active profile. Add one with 'usm clash sub add <url>' "
            "then 'usm clash use <name>'."
        )
    path = _profile_yaml(state.active)
    if not path.exists():
        raise click.ClickException(f"profile file missing for '{state.active}'.")
    base = yaml.safe_load(path.read_text())
    if not isinstance(base, dict):
        raise click.ClickException(
            f"profile '{state.active}' is not a valid Clash config."
        )

    for key in ("port", "socks-port", "redir-port", "tproxy-port", "mixed-port"):
        base.pop(key, None)
    base["mixed-port"] = state.port
    base["allow-lan"] = state.allow_lan
    base["bind-address"] = "*" if state.allow_lan else "127.0.0.1"
    base["mode"] = state.mode
    base["log-level"] = state.log_level
    base.setdefault("ipv6", True)
    base["external-controller"] = state.controller
    base["secret"] = state.secret
    base["profile"] = {**(base.get("profile") or {}), "store-selected": True}

    if state.tun:
        base["tun"] = {
            "enable": True,
            "stack": state.tun_stack,
            "auto-route": True,
            "auto-detect-interface": True,
            "dns-hijack": ["any:53"],
        }
        if not isinstance(base.get("dns"), dict) or not base["dns"].get("enable"):
            base["dns"] = _default_dns()
    else:
        base["tun"] = {"enable": False}
    return base


def _write_runtime(state: State) -> dict:
    ROOT.mkdir(parents=True, exist_ok=True)
    cfg = _compose_config(state)
    RUNTIME_CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    return cfg


def _test_config() -> None:
    mihomo = ensure_mihomo()
    p = subprocess.run(
        [str(mihomo), "-t", "-d", str(ROOT), "-f", str(RUNTIME_CONFIG)],
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


# Process management --------------------------------------------------------


def _kill_pid(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, _SIGKILL)
    except OSError:
        pass
    return True


def _tail(path: Path, n: int) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _start_process(state: State) -> None:
    _write_runtime(state)
    _test_config()
    mihomo = ensure_mihomo()
    argv = [str(mihomo), "-d", str(ROOT), "-f", str(RUNTIME_CONFIG)]
    log = open(LOG_PATH, "ab", buffering=0)
    log.write(f"\n--- start {time.strftime('%Y-%m-%dT%H:%M:%S')} ---\n".encode())
    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    proc = subprocess.Popen(argv, **popen_kwargs)
    state.pid = proc.pid
    state.started_at = time.time()
    save_state(state)
    time.sleep(1.5)
    if proc.poll() is not None:
        state.pid = None
        state.started_at = None
        save_state(state)
        console.print(
            f"[red]✗[/red] mihomo exited immediately (code {proc.returncode}). Log:"
        )
        for line in _tail(LOG_PATH, 15):
            console.print(f"  [dim]{line}[/dim]")
        raise click.ClickException("clash failed to start.")


def _fmt_uptime(secs: float) -> str:
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}EiB"


def _as_int(v: object) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _fmt_epoch(v: object) -> str:
    try:
        return datetime.fromtimestamp(int(v)).strftime("%Y-%m-%d")  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


# systemd user-unit helpers -------------------------------------------------


def _unit_path() -> Path:
    return SYSTEMD_USER_DIR / UNIT_NAME


def _is_enabled() -> bool:
    return _unit_path().exists()


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], text=True, capture_output=True, check=check
    )


def _require_systemd() -> None:
    if os.name != "posix" or not shutil.which("systemctl"):
        raise click.ClickException(
            "Autostart needs systemd (user instance). Not available on this system."
        )
    if _systemctl("--version").returncode != 0:
        raise click.ClickException("systemctl --user is not usable here.")


def _systemd_is_active() -> bool:
    return _systemctl("is-active", UNIT_NAME).stdout.strip() == "active"


def _systemd_main_pid() -> Optional[int]:
    p = _systemctl("show", "-p", "MainPID", "--value", UNIT_NAME)
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


def _render_unit(usm_bin: str) -> str:
    uv_bin = shutil.which("uv")
    extra = [os.path.dirname(usm_bin)] + ([os.path.dirname(uv_bin)] if uv_bin else [])
    path_value = ":".join(
        dict.fromkeys(
            extra
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
        "Description=usm clash (mihomo) manager\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f'Environment="PATH={path_value}"\n'
        f"ExecStart={usm_bin} clash run\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# RESTful API client --------------------------------------------------------


class ClashAPI:
    def __init__(self, state: State) -> None:
        host, _, port = state.controller.rpartition(":")
        if host in ("", "0.0.0.0", "::", "*"):
            host = "127.0.0.1"
        self.base = f"http://{host}:{port}"
        self.headers = (
            {"Authorization": f"Bearer {state.secret}"} if state.secret else {}
        )

    def _req(self, method: str, path: str, *, timeout: float = 10, **kw):
        try:
            return requests.request(
                method, self.base + path, headers=self.headers, timeout=timeout, **kw
            )
        except requests.RequestException as e:
            raise click.ClickException(
                f"clash controller unreachable at {self.base} ({e}). "
                "Is it running? Try 'usm clash up'."
            ) from e

    def configs(self) -> dict:
        return self._req("GET", "/configs").json()

    def set_mode(self, mode: str) -> None:
        r = self._req("PATCH", "/configs", json={"mode": mode})
        if r.status_code >= 400:
            raise click.ClickException(f"failed to set mode: HTTP {r.status_code}")

    def reload(self) -> None:
        r = self._req(
            "PUT",
            "/configs",
            params={"force": "true"},
            json={"path": str(RUNTIME_CONFIG)},
        )
        if r.status_code >= 400:
            raise click.ClickException(f"config reload failed: HTTP {r.status_code}")

    def proxies(self) -> dict:
        return self._req("GET", "/proxies").json().get("proxies", {})

    def select(self, group: str, name: str) -> None:
        r = self._req(
            "PUT", f"/proxies/{urllib.parse.quote(group)}", json={"name": name}
        )
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("message", "")
            except ValueError:
                pass
            raise click.ClickException(
                f"failed to select '{name}' in '{group}': {detail or f'HTTP {r.status_code}'}"
            )

    def delay(self, name: str, url: str, timeout_ms: int) -> Optional[int]:
        r = self._req(
            "GET",
            f"/proxies/{urllib.parse.quote(name)}/delay",
            params={"url": url, "timeout": timeout_ms},
            timeout=timeout_ms / 1000 + 5,
        )
        if r.status_code == 200:
            return r.json().get("delay")
        return None

    def group_delay(self, group: str, url: str, timeout_ms: int) -> dict:
        r = self._req(
            "GET",
            f"/group/{urllib.parse.quote(group)}/delay",
            params={"url": url, "timeout": timeout_ms},
            timeout=timeout_ms / 1000 + 10,
        )
        if r.status_code == 200:
            return r.json()
        return {}

    def connections(self) -> dict:
        return self._req("GET", "/connections").json()

    def close_connections(self) -> None:
        self._req("DELETE", "/connections")

    def stream(self, path: str, params: Optional[dict] = None) -> Iterator[dict]:
        r = self._req("GET", path, params=params or {}, stream=True, timeout=None)
        for line in r.iter_lines():
            if not line:
                continue
            try:
                yield json.loads(line.decode())
            except (ValueError, UnicodeDecodeError):
                continue


def _api(state: Optional[State] = None) -> ClashAPI:
    return ClashAPI(state or load_state())


def _require_running(state: State) -> None:
    if not _is_running(state):
        raise click.ClickException(
            "clash is not running. Start it with 'usm clash up'."
        )


# System proxy --------------------------------------------------------------


def _gsettings(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gsettings", *args], text=True, capture_output=True)


def _write_proxy_env(host: str, port: int, enable: bool) -> None:
    if enable:
        url = f"http://{host}:{port}"
        body = (
            f"export http_proxy={url}\nexport https_proxy={url}\n"
            f"export HTTP_PROXY={url}\nexport HTTPS_PROXY={url}\n"
            f"export all_proxy=socks5://{host}:{port}\n"
            "export no_proxy=localhost,127.0.0.1,::1\n"
        )
    else:
        body = (
            "unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy no_proxy\n"
        )
    ROOT.mkdir(parents=True, exist_ok=True)
    PROXY_ENV_PATH.write_text(body)


def _set_system_proxy(state: State, enable: bool) -> list[str]:
    host = "127.0.0.1"
    port = state.port
    notes: list[str] = []
    system = platform.system().lower()

    if system == "darwin" and shutil.which("networksetup"):
        services = subprocess.run(
            ["networksetup", "-listallnetworkservices"],
            text=True,
            capture_output=True,
        ).stdout.splitlines()[1:]
        for svc in services:
            svc = svc.lstrip("* ").strip()
            if not svc:
                continue
            if enable:
                subprocess.run(["networksetup", "-setwebproxy", svc, host, str(port)])
                subprocess.run(
                    ["networksetup", "-setsecurewebproxy", svc, host, str(port)]
                )
                subprocess.run(
                    ["networksetup", "-setsocksfirewallproxy", svc, host, str(port)]
                )
            else:
                for kind in (
                    "-setwebproxystate",
                    "-setsecurewebproxystate",
                    "-setsocksfirewallproxystate",
                ):
                    subprocess.run(["networksetup", kind, svc, "off"])
        notes.append(f"macOS network services proxy {'set' if enable else 'cleared'}.")
    elif shutil.which("gsettings"):
        if enable:
            prev = _gsettings("get", "org.gnome.system.proxy", "mode").stdout.strip()
            if state.sysproxy_backup is None:
                state.sysproxy_backup = {"gnome_mode": prev}
            _gsettings("set", "org.gnome.system.proxy", "mode", "manual")
            for scheme in ("http", "https"):
                _gsettings("set", f"org.gnome.system.proxy.{scheme}", "host", host)
                _gsettings("set", f"org.gnome.system.proxy.{scheme}", "port", str(port))
            _gsettings("set", "org.gnome.system.proxy.socks", "host", host)
            _gsettings("set", "org.gnome.system.proxy.socks", "port", str(port))
        else:
            prev = (state.sysproxy_backup or {}).get("gnome_mode", "'none'").strip("'")
            _gsettings("set", "org.gnome.system.proxy", "mode", prev or "none")
            state.sysproxy_backup = None
        notes.append(f"GNOME proxy {'set to manual' if enable else 'restored'}.")
    elif system == "windows":
        import winreg  # type: ignore

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enable else 0)
        if enable:
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"{host}:{port}")
        winreg.CloseKey(key)
        notes.append(f"Windows proxy {'enabled' if enable else 'disabled'}.")

    _write_proxy_env(host, port, enable)
    if enable:
        notes.append(
            f"shell exports written to {PROXY_ENV_PATH} (source it for CLI apps)."
        )
    return notes


# TUN privilege -------------------------------------------------------------


def _mihomo_has_net_admin(mihomo: Path) -> bool:
    if os.geteuid() == 0 if hasattr(os, "geteuid") else False:
        return True
    if not shutil.which("getcap"):
        return False
    out = subprocess.run(["getcap", str(mihomo)], text=True, capture_output=True).stdout
    return "cap_net_admin" in out.lower()


def _tun_privilege_hint(mihomo: Path) -> None:
    console.print(
        "[yellow]TUN needs CAP_NET_ADMIN.[/yellow] Grant it once (no per-run sudo):"
    )
    console.print(
        f"  [bold]sudo setcap cap_net_admin,cap_net_bind_service+ep {mihomo}[/bold]"
    )
    console.print("  then re-run [bold]usm clash tun on[/bold].")


# CLI -----------------------------------------------------------------------


@click.group(
    help=__doc__.splitlines()[0],
    context_settings={"help_option_names": ["-h", "--help"]},
)
def cli() -> None:
    pass


# ---- subscriptions ----


@cli.group("sub", short_help="Manage subscriptions / profiles.")
def sub() -> None:
    pass


@sub.command("add", short_help="Add a subscription URL or local config file.")
@click.argument("source")
@click.option("--name", help="Profile name (default: derived from the URL/file).")
@click.option(
    "--interval",
    type=int,
    default=12,
    show_default=True,
    help="Auto-refresh interval in hours (0 disables).",
)
@click.option(
    "--use/--no-use",
    default=True,
    show_default=True,
    help="Make this the active profile.",
)
def cmd_sub_add(source, name, interval, use):
    is_remote = source.startswith(("http://", "https://"))
    if not name:
        if is_remote:
            name = urllib.parse.urlparse(source).hostname or "profile"
            name = name.split(":")[0].replace(".", "-")
        else:
            name = Path(source).stem
    if is_remote:
        text, userinfo = _fetch_subscription(source)
        _save_profile(name, source, "remote", text, userinfo, interval)
    else:
        p = Path(source).expanduser()
        if not p.exists():
            raise click.ClickException(f"file not found: {source}")
        text = p.read_text()
        _validate_clash_text(text)
        _save_profile(name, str(p), "local", text, {}, 0)
    console.print(f"[green]✓[/green] saved profile [bold]{name}[/bold].")
    if use:
        state = load_state()
        state.active = name
        save_state(state)
        console.print(f"  active profile → [bold]{name}[/bold]")
        if _is_running(state):
            console.print("  [dim]run 'usm clash restart' to apply.[/dim]")


@sub.command("ls", short_help="List profiles.")
def cmd_sub_ls():
    profiles = _list_profiles()
    if not profiles:
        console.print("[dim]No profiles. Add one with 'usm clash sub add <url>'.[/dim]")
        return
    state = load_state()
    table = Table(show_header=True, header_style="bold")
    table.add_column("")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Updated")
    table.add_column("Traffic (used / total)")
    table.add_column("Expires")
    for meta in profiles:
        active = "[green]●[/green]" if meta["name"] == state.active else ""
        updated = datetime.fromtimestamp(meta.get("updated_at", 0)).strftime(
            "%Y-%m-%d %H:%M"
        )
        ui = meta.get("userinfo") or {}
        traffic = "-"
        total = _as_int(ui.get("total"))
        if total:
            used = _as_int(ui.get("upload")) + _as_int(ui.get("download"))
            traffic = f"{_fmt_bytes(used)} / {_fmt_bytes(total)}"
        expire = _fmt_epoch(ui["expire"]) if ui.get("expire") else "-"
        table.add_row(
            active, meta["name"], meta.get("type", "?"), updated, traffic, expire
        )
    console.print(table)


@sub.command("update", short_help="Refresh remote profiles (NAME or all).")
@click.argument("name", required=False)
def cmd_sub_update(name):
    targets = _list_profiles()
    if name:
        targets = [m for m in targets if m["name"] == name]
        if not targets:
            raise click.ClickException(f"No profile '{name}'.")
    updated = 0
    for meta in targets:
        if meta.get("type") != "remote":
            console.print(f"  [dim]–[/dim] {meta['name']} (local, skipped)")
            continue
        try:
            _refresh_profile(meta)
            console.print(f"  [green]✓[/green] {meta['name']}")
            updated += 1
        except click.ClickException as e:
            console.print(f"  [red]✗[/red] {meta['name']}: {e.message}")
    if updated:
        state = load_state()
        if _is_running(state) and state.active in {m["name"] for m in targets}:
            console.print("[dim]run 'usm clash restart' to apply the new config.[/dim]")


@sub.command("rm", short_help="Delete a profile.")
@click.argument("name")
def cmd_sub_rm(name):
    if not _profile_meta(name).exists():
        raise click.ClickException(f"No profile '{name}'.")
    _profile_yaml(name).unlink(missing_ok=True)
    _profile_meta(name).unlink(missing_ok=True)
    state = load_state()
    if state.active == name:
        state.active = None
        save_state(state)
    console.print(f"[green]✓[/green] removed profile {name}")


@cli.command("use", short_help="Set the active profile.")
@click.argument("name")
def cmd_use(name):
    if not _profile_meta(name).exists():
        raise click.ClickException(f"No profile '{name}'.")
    state = load_state()
    state.active = name
    save_state(state)
    console.print(f"[green]✓[/green] active profile → [bold]{name}[/bold]")
    if _is_running(state):
        _start_or_reload(state)
        console.print("  [dim]applied to the running core.[/dim]")


# ---- lifecycle ----


def _maybe_autorefresh(state: State) -> None:
    if not state.active:
        return
    try:
        meta = _load_profile_meta(state.active)
    except click.ClickException:
        return
    interval = meta.get("interval_hours") or 0
    if meta.get("type") != "remote" or interval <= 0:
        return
    if time.time() - meta.get("updated_at", 0) < interval * 3600:
        return
    try:
        _refresh_profile(meta)
        console.print(f"[dim]refreshed subscription '{state.active}'.[/dim]")
    except click.ClickException:
        pass


def _start_or_reload(state: State) -> None:
    """Apply config to a running core (hot reload) or note it's not running."""
    if _is_running(state):
        _write_runtime(state)
        _test_config()
        _api(state).reload()


@cli.command("up", short_help="Start the clash core with the active profile.")
@click.argument("name", required=False)
@click.option("--tun/--no-tun", default=None, help="Enable/disable TUN for this run.")
@click.option("--lan/--no-lan", default=None, help="Allow LAN connections.")
@click.option(
    "--system-proxy/--no-system-proxy",
    default=None,
    help="Set the OS proxy after start.",
)
@click.option("-p", "--port", type=int, help="Mixed HTTP+SOCKS port.")
def cmd_up(name, tun, lan, system_proxy, port):
    state = load_state()
    if name:
        if not _profile_meta(name).exists():
            raise click.ClickException(f"No profile '{name}'.")
        state.active = name
    if not state.secret:
        import secrets

        state.secret = secrets.token_urlsafe(16)
    if port:
        state.port = port
    if lan is not None:
        state.allow_lan = lan
    if system_proxy is not None:
        state.system_proxy = system_proxy
    if tun is not None:
        if tun and not _mihomo_has_net_admin(ensure_mihomo()):
            _tun_privilege_hint(ensure_mihomo())
            raise click.ClickException("missing CAP_NET_ADMIN for TUN.")
        state.tun = tun
    save_state(state)

    if _is_enabled():
        p = _systemctl("start", UNIT_NAME)
        if p.returncode != 0:
            raise click.ClickException(p.stderr.strip() or "systemctl start failed.")
        console.print("[green]✓[/green] started via systemd.")
        if state.system_proxy:
            for note in _set_system_proxy(state, True):
                console.print(f"  [dim]{note}[/dim]")
            save_state(state)
        return
    if _is_running(state):
        raise click.ClickException(f"clash is already running (pid {state.pid}).")

    _maybe_autorefresh(state)
    _start_process(state)
    console.print(
        f"[green]✓[/green] clash up (pid {state.pid}) — profile [bold]{state.active}[/bold]"
    )
    console.print(
        f"  mixed proxy: http+socks://{'0.0.0.0' if state.allow_lan else '127.0.0.1'}:{state.port}"
        f"  |  mode: {state.mode}  |  tun: {'on' if state.tun else 'off'}"
    )
    if state.system_proxy:
        for note in _set_system_proxy(state, True):
            console.print(f"  [dim]{note}[/dim]")
        save_state(state)
    console.print(f"  [dim]point apps at[/dim] http://127.0.0.1:{state.port}")


@cli.command("down", short_help="Stop the clash core.")
def cmd_down():
    state = load_state()
    if state.system_proxy:
        for note in _set_system_proxy(state, False):
            console.print(f"  [dim]{note}[/dim]")
    if _is_enabled():
        p = _systemctl("stop", UNIT_NAME)
        console.print(
            "[green]✓[/green] stopped (systemd)."
            if p.returncode == 0
            else f"[red]✗[/red] {p.stderr.strip() or 'stop failed'}"
        )
    else:
        was = _kill_pid(state.pid)
        console.print(f"[green]✓[/green] {'stopped' if was else 'already stopped'}.")
    state.pid = None
    state.started_at = None
    save_state(state)


@cli.command("restart", short_help="Restart the clash core.")
def cmd_restart():
    state = load_state()
    if _is_enabled():
        p = _systemctl("restart", UNIT_NAME)
        if p.returncode != 0:
            raise click.ClickException(p.stderr.strip() or "systemctl restart failed.")
        console.print("[green]✓[/green] restarted via systemd.")
        if state.system_proxy:
            _set_system_proxy(state, True)
            save_state(state)
        return
    _kill_pid(state.pid)
    state.pid = None
    _maybe_autorefresh(state)
    _start_process(state)
    console.print(f"[green]✓[/green] restarted (pid {state.pid}).")
    if state.system_proxy:
        _set_system_proxy(state, True)
        save_state(state)


@cli.command("status", short_help="Show what's running.")
def cmd_status():
    state = load_state()
    enabled = _is_enabled()
    if enabled:
        pid = _systemd_main_pid()
        running = bool(pid) and _systemd_is_active()
    else:
        pid = state.pid if _is_running(state) else None
        running = bool(pid)
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold")
    table.add_column()
    dot = "[green]● running[/green]" if running else "[dim]○ stopped[/dim]"
    table.add_row("status", f"{dot}" + (f"  (pid {pid})" if pid else ""))
    table.add_row("profile", state.active or "[dim]none[/dim]")
    table.add_row("mode", state.mode)
    bind = "0.0.0.0" if state.allow_lan else "127.0.0.1"
    table.add_row("mixed port", f"{bind}:{state.port}")
    table.add_row("controller", state.controller)
    table.add_row("tun", "[green]on[/green]" if state.tun else "off")
    table.add_row("allow-lan", "[green]on[/green]" if state.allow_lan else "off")
    table.add_row("system-proxy", "[green]on[/green]" if state.system_proxy else "off")
    table.add_row("autostart", "[cyan]enabled[/cyan]" if enabled else "off")
    if running and state.started_at:
        table.add_row("uptime", _fmt_uptime(time.time() - state.started_at))
    console.print(table)
    if running:
        try:
            conns = _api(state).connections()
            console.print(
                f"[dim]traffic: ↓ {_fmt_bytes(conns.get('downloadTotal', 0))}  "
                f"↑ {_fmt_bytes(conns.get('uploadTotal', 0))}  "
                f"| active conns: {len(conns.get('connections') or [])}[/dim]"
            )
        except click.ClickException:
            pass


# ---- runtime control ----


@cli.command("mode", short_help="Get or set routing mode (rule|global|direct).")
@click.argument("mode", required=False, type=click.Choice(MODES))
def cmd_mode(mode):
    state = load_state()
    if not mode:
        console.print(f"mode: [bold]{state.mode}[/bold]")
        return
    state.mode = mode
    save_state(state)
    if _is_running(state):
        _api(state).set_mode(mode)
    console.print(f"[green]✓[/green] mode → [bold]{mode}[/bold]")


@cli.command("proxies", short_help="List proxy groups, members, and selection.")
@click.argument("group", required=False)
def cmd_proxies(group):
    state = load_state()
    _require_running(state)
    proxies = _api(state).proxies()
    groups = {
        k: v
        for k, v in proxies.items()
        if v.get("type") in ("Selector", "URLTest", "Fallback", "LoadBalance")
    }
    if group:
        if group not in groups:
            raise click.ClickException(
                f"No proxy group '{group}'. Groups: {', '.join(groups)}"
            )
        groups = {group: groups[group]}
    if not groups:
        console.print("[dim]No selectable groups in this profile.[/dim]")
        return
    for gname, g in groups.items():
        console.print(f"[bold cyan]{gname}[/bold cyan] [dim]({g.get('type')})[/dim]")
        for member in g.get("all", []):
            mark = "[green]→[/green]" if member == g.get("now") else " "
            node = proxies.get(member, {})
            delay = ""
            hist = node.get("history") or []
            if hist and hist[-1].get("delay"):
                delay = f"  [dim]{hist[-1]['delay']}ms[/dim]"
            console.print(f"  {mark} {member}{delay}")


@cli.command("select", short_help="Select a node in a group.")
@click.argument("group")
@click.argument("node")
def cmd_select(group, node):
    state = load_state()
    _require_running(state)
    _api(state).select(group, node)
    console.print(f"[green]✓[/green] {group} → [bold]{node}[/bold]")


@cli.command("test", short_help="Latency-test a group or a single node.")
@click.argument("target", required=False)
@click.option("--url", default=DEFAULT_TEST_URL, show_default=True, help="Test URL.")
@click.option(
    "--timeout", type=int, default=5000, show_default=True, help="Timeout (ms)."
)
def cmd_test(target, url, timeout):
    state = load_state()
    _require_running(state)
    api = _api(state)
    proxies = api.proxies()
    groups = {
        k
        for k, v in proxies.items()
        if v.get("type") in ("Selector", "URLTest", "Fallback", "LoadBalance")
    }
    if target and target in groups:
        console.print(f"[dim]testing group {target} …[/dim]")
        result = api.group_delay(target, url, timeout)
        if not result:
            console.print("[yellow]no node responded.[/yellow]")
            return
        for node, delay in sorted(result.items(), key=lambda kv: kv[1]):
            console.print(f"  {node}: [green]{delay}ms[/green]")
        return
    if target:
        delay = api.delay(target, url, timeout)
        console.print(
            f"  {target}: [green]{delay}ms[/green]"
            if delay
            else f"  {target}: [red]timeout[/red]"
        )
        return
    nodes = [
        k
        for k, v in proxies.items()
        if v.get("type")
        not in ("Selector", "URLTest", "Fallback", "LoadBalance", "Direct", "Reject")
    ]
    console.print(f"[dim]testing {len(nodes)} node(s) …[/dim]")
    for node in nodes:
        delay = api.delay(node, url, timeout)
        console.print(
            f"  {node}: [green]{delay}ms[/green]"
            if delay
            else f"  {node}: [red]timeout[/red]"
        )


# ---- system integration ----


@cli.command("tun", short_help="Toggle TUN (transparent system-wide proxy).")
@click.argument("action", type=click.Choice(["on", "off", "status"]))
def cmd_tun(action):
    state = load_state()
    if action == "status":
        console.print(f"tun: [bold]{'on' if state.tun else 'off'}[/bold]")
        return
    if action == "on":
        mihomo = ensure_mihomo()
        if not _mihomo_has_net_admin(mihomo):
            _tun_privilege_hint(mihomo)
            raise click.ClickException("missing CAP_NET_ADMIN for TUN.")
    state.tun = action == "on"
    save_state(state)
    if _is_running(state):
        _start_or_reload(state)
    console.print(f"[green]✓[/green] tun → [bold]{action}[/bold]")
    if action == "on":
        console.print("  [dim]all system traffic is now captured by the core.[/dim]")


@cli.command("system-proxy", short_help="Set/clear the OS HTTP/SOCKS proxy.")
@click.argument("action", type=click.Choice(["on", "off", "status"]))
def cmd_system_proxy(action):
    state = load_state()
    if action == "status":
        console.print(
            f"system-proxy: [bold]{'on' if state.system_proxy else 'off'}[/bold]"
        )
        return
    enable = action == "on"
    notes = _set_system_proxy(state, enable)
    state.system_proxy = enable
    save_state(state)
    console.print(f"[green]✓[/green] system-proxy → [bold]{action}[/bold]")
    for note in notes:
        console.print(f"  [dim]{note}[/dim]")
    if enable:
        console.print(f"  [dim]for the current shell:[/dim] source {PROXY_ENV_PATH}")


@cli.command("lan", short_help="Toggle LAN access (allow-lan).")
@click.argument("action", type=click.Choice(["on", "off", "status"]))
def cmd_lan(action):
    state = load_state()
    if action == "status":
        console.print(f"allow-lan: [bold]{'on' if state.allow_lan else 'off'}[/bold]")
        return
    state.allow_lan = action == "on"
    save_state(state)
    if _is_running(state):
        _start_or_reload(state)
    console.print(f"[green]✓[/green] allow-lan → [bold]{action}[/bold]")
    if state.allow_lan:
        console.print(
            f"  [dim]other devices can use[/dim] http://<this-host-ip>:{state.port}"
        )


# ---- observability ----


@cli.command("logs", short_help="Show recent logs, or stream live with -f.")
@click.option("-f", "--follow", is_flag=True, help="Stream live logs via the API.")
@click.option("-n", "--lines", type=int, default=50, show_default=True)
@click.option(
    "--level",
    default="info",
    show_default=True,
    type=click.Choice(["debug", "info", "warning", "error", "silent"]),
)
def cmd_logs(follow, lines, level):
    state = load_state()
    if follow:
        _require_running(state)
        try:
            for entry in _api(state).stream("/logs", {"level": level}):
                lvl = entry.get("type", "info")
                color = {"warning": "yellow", "error": "red", "debug": "dim"}.get(
                    lvl, "white"
                )
                console.print(f"[{color}]{lvl:>7}[/{color}] {entry.get('payload', '')}")
        except KeyboardInterrupt:
            pass
        return
    if not LOG_PATH.exists():
        console.print("[dim]No logs yet.[/dim]")
        return
    for line in _tail(LOG_PATH, lines):
        click.echo(line)


@cli.command("conns", short_help="Show active connections.")
@click.option("--close", is_flag=True, help="Close all active connections.")
def cmd_conns(close):
    state = load_state()
    _require_running(state)
    api = _api(state)
    if close:
        api.close_connections()
        console.print("[green]✓[/green] closed all connections.")
        return
    data = api.connections()
    conns = data.get("connections") or []
    console.print(
        f"[dim]↓ {_fmt_bytes(data.get('downloadTotal', 0))}  "
        f"↑ {_fmt_bytes(data.get('uploadTotal', 0))}  |  "
        f"{len(conns)} active[/dim]"
    )
    if not conns:
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Host")
    table.add_column("Chain")
    table.add_column("Rule")
    table.add_column("↑", justify="right")
    table.add_column("↓", justify="right")
    for c in conns[:40]:
        meta = c.get("metadata", {})
        host = meta.get("host") or meta.get("destinationIP", "")
        dport = meta.get("destinationPort", "")
        chain = " → ".join(c.get("chains", [])[::-1])
        table.add_row(
            f"{host}:{dport}",
            chain,
            c.get("rule", ""),
            _fmt_bytes(c.get("upload", 0)),
            _fmt_bytes(c.get("download", 0)),
        )
    console.print(table)


@cli.command("dashboard", short_help="Print a web dashboard URL for the running core.")
def cmd_dashboard():
    state = load_state()
    host, _, port = state.controller.rpartition(":")
    if host in ("", "0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    q = urllib.parse.urlencode(
        {"hostname": host, "port": port, "secret": state.secret, "http": "true"}
    )
    console.print("Open the metacubexd dashboard (the core has CORS enabled):")
    console.print(f"  [bold]{DASHBOARD_BASE}/#/setup?{q}[/bold]")
    if not _is_running(state):
        console.print("[dim](start the core first with 'usm clash up')[/dim]")


# ---- autostart ----


@cli.command("enable", short_help="Autostart at login via a systemd user unit.")
def cmd_enable():
    state = load_state()
    _require_systemd()
    usm_bin = shutil.which("usm")
    if not usm_bin:
        raise click.ClickException("'usm' not found on PATH; install it first.")
    if not state.secret:
        import secrets

        state.secret = secrets.token_urlsafe(16)
    _kill_pid(state.pid)
    state.pid = None
    save_state(state)
    _write_runtime(state)
    _test_config()
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    _unit_path().write_text(_render_unit(usm_bin))
    _systemctl("daemon-reload", check=True)
    p = _systemctl("enable", "--now", UNIT_NAME)
    if p.returncode != 0:
        raise click.ClickException(p.stderr.strip() or "systemctl enable --now failed.")
    console.print(f"[green]✓[/green] enabled & started ({UNIT_NAME}).")
    if not _linger_enabled():
        console.print(
            "  [yellow]note:[/yellow] to start at boot before login, run "
            f"[bold]sudo loginctl enable-linger {_current_user()}[/bold]"
        )


@cli.command("disable", short_help="Remove the systemd user unit.")
def cmd_disable():
    if not _is_enabled():
        console.print("[dim]autostart is not enabled.[/dim]")
        return
    _require_systemd()
    _systemctl("disable", "--now", UNIT_NAME)
    _unit_path().unlink(missing_ok=True)
    _systemctl("daemon-reload")
    console.print("[green]✓[/green] autostart disabled.")


@cli.command("run", hidden=True, short_help="(internal) run mihomo in the foreground.")
def cmd_run():
    state = load_state()
    _write_runtime(state)
    mihomo = ensure_mihomo()
    state.pid = os.getpid()
    state.started_at = time.time()
    save_state(state)
    try:
        os.execvp(
            str(mihomo), [str(mihomo), "-d", str(ROOT), "-f", str(RUNTIME_CONFIG)]
        )
    except FileNotFoundError as exc:
        raise click.ClickException(f"{mihomo} not found.") from exc


@cli.command("install", short_help="Pre-download the mihomo binary.")
@click.option("--upgrade", is_flag=True, help="Re-download even if present.")
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
