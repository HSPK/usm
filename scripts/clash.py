#!/usr/bin/env python3
"""A ClashX-style command-line manager for the mihomo (Clash.Meta) core.

Subscriptions, profile switching, rule/global/direct mode, node selection,
latency tests, TUN, system-proxy, LAN sharing, live logs/traffic, and
connection inspection — all from the terminal. The mihomo binary is
auto-installed to ``~/.cache/usm/bin/mihomo`` (no sudo, no system packages).

Quick start
-----------
  usm clash sub add https://example.com/sub --name work   # add a subscription
  usm clash on                                             # start + set system proxy
  usm clash                                                # status dashboard
  usm clash node                                           # pick a node (interactive)
  usm clash sub use                                        # switch subscription (interactive)
  usm clash mode global                                    # rule | global | direct
  usm clash off                                            # clear system proxy
  usm clash core down                                      # stop the core

Most settings (mode, lan, tun, the active subscription) can be changed while
the core is running — the change is hot-applied immediately and remembered for
next time.

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
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
GROUP_TYPES = ("Selector", "URLTest", "Fallback", "LoadBalance")
NON_NODE_TYPES = (
    *GROUP_TYPES,
    "Direct",
    "Reject",
    "RejectDrop",
    "Pass",
    "PassRule",
    "Compatible",
)
CLASH_UA = f"clash.meta/usm mihomo/{MIHOMO_VERSION}"
READY_TIMEOUT = 45  # seconds to wait for the controller (first run fetches GeoIP)
CONFIG_TEST_TIMEOUT = 120  # safety cap on `mihomo -t`

# GeoIP / GeoSite data: usm fetches these itself (with a timeout + mirror) so a
# blocked GitHub never hangs the core. Override the source with $USM_CLASH_GEO_BASE.
GEO_DEFAULT_BASE = (
    "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest"
)
GEO_ENV = "USM_CLASH_GEO_BASE"
# rule type -> (release asset name, local filename mihomo looks for in its data dir)
GEO_RULE_FILES = {
    "GEOIP": ("geoip.dat", "GeoIP.dat"),
    "GEOSITE": ("geosite.dat", "GeoSite.dat"),
}

# Web dashboard (metacubexd) served locally by mihomo via `external-ui`.
UI_VERSION = "v1.251.3"
UI_DEFAULT_URL = (
    f"https://github.com/MetaCubeX/metacubexd/releases/download/{UI_VERSION}/"
    "compressed-dist.tgz"
)
UI_ENV = "USM_CLASH_UI_URL"
UI_DIR_NAME = "ui"  # relative to the data dir (ROOT); mihomo serves it at /ui/

try:
    _SIGKILL = signal.SIGKILL
except AttributeError:
    _SIGKILL = signal.SIGTERM

console = Console()


@contextmanager
def _status(message: str):
    """Spinner for a blocking step; silent when output is not a TTY."""
    if console.is_terminal:
        with console.status(message, spinner="dots"):
            yield
    else:
        console.print(f"[dim]{message}[/dim]")
        yield


def fmt_uptime(secs: float) -> str:
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def fmt_bytes(n: float) -> str:
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


def fmt_epoch(v: object) -> str:
    try:
        return datetime.fromtimestamp(int(v)).strftime("%Y-%m-%d")  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


# Mihomo binary -------------------------------------------------------------


class MihomoBinary:
    """Resolve (and, if needed, download) the pinned mihomo binary."""

    @staticmethod
    def _filename() -> str:
        return "mihomo.exe" if platform.system().lower() == "windows" else "mihomo"

    @classmethod
    def local_path(cls) -> Path:
        return LOCAL_BIN_DIR / cls._filename()

    @staticmethod
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

    @classmethod
    def _download(cls, url: str, dest: Path) -> None:
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
                        (n for n in zf.namelist() if n.lower().endswith("mihomo.exe")),
                        None,
                    ) or next((n for n in zf.namelist() if "mihomo" in n.lower()), None)
                    if not name:
                        raise click.ClickException("no mihomo binary inside the zip")
                    tmp.write_bytes(zf.read(name))
            else:
                tmp.write_bytes(payload)
        except (OSError, zipfile.BadZipFile, EOFError) as e:
            tmp.unlink(missing_ok=True)
            raise click.ClickException(f"failed to extract {url}: {e}") from e
        tmp.chmod(0o755)
        tmp.replace(dest)

    @classmethod
    def ensure(cls, *, upgrade: bool = False) -> Path:
        override = os.environ.get("USM_MIHOMO_BIN")
        if override and os.access(override, os.X_OK):
            return Path(override)
        local = cls.local_path()
        if not upgrade and local.exists():
            return local
        asset = cls._asset_name()
        with _status(f"downloading mihomo {MIHOMO_VERSION} ({asset}) …"):
            cls._download(MIHOMO_RELEASE + asset, local)
        console.print(f"[green]✓[/green] installed mihomo → {local}")
        return local

    @classmethod
    def has_net_admin(cls, path: Optional[Path] = None) -> bool:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return True
        if not shutil.which("getcap"):
            return False
        path = path or cls.ensure()
        out = subprocess.run(
            ["getcap", str(path)], text=True, capture_output=True
        ).stdout
        return "cap_net_admin" in out.lower()


# GeoIP / GeoSite data ------------------------------------------------------


class GeoData:
    """Fetches GeoIP/GeoSite databases so mihomo never has to.

    mihomo only needs these when a profile has GEOIP/GEOSITE rules, and it
    looks for ``GeoIP.dat`` / ``GeoSite.dat`` in its data dir. We download them
    ourselves (own timeout + progress + mirror) so a blocked GitHub fails fast
    with guidance instead of hanging the core.
    """

    @staticmethod
    def base() -> str:
        return os.environ.get(GEO_ENV, GEO_DEFAULT_BASE).rstrip("/")

    @staticmethod
    def geox_url(base: Optional[str] = None) -> dict:
        b = (base or GeoData.base()).rstrip("/")
        return {
            "geoip": f"{b}/geoip.dat",
            "geosite": f"{b}/geosite.dat",
            "mmdb": f"{b}/country.mmdb",
            "asn": f"{b}/GeoLite2-ASN.mmdb",
        }

    @staticmethod
    def needed(rules: list) -> list[tuple[str, str]]:
        types = {
            r.split(",", 1)[0].strip().upper() for r in rules if isinstance(r, str)
        }
        return [GEO_RULE_FILES[t] for t in GEO_RULE_FILES if t in types]

    @staticmethod
    def _download(url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with requests.get(url, stream=True, timeout=(10, 60)) as r:
                if r.status_code != 200:
                    raise click.ClickException(
                        GeoData._fail_msg(url, f"HTTP {r.status_code}")
                    )
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
        except requests.RequestException as e:
            tmp.unlink(missing_ok=True)
            raise click.ClickException(GeoData._fail_msg(url, str(e))) from e
        tmp.replace(dest)

    @staticmethod
    def _fail_msg(url: str, why: str) -> str:
        return (
            f"failed to download geo data ({why}): {url}\n"
            f"  GitHub may be blocked. Point {GEO_ENV} at a mirror and retry, e.g.\n"
            f'    export {GEO_ENV}="https://ghproxy.net/{GEO_DEFAULT_BASE}"\n'
            "  then run 'usm clash setup --force'. "
            "(Or remove GEOIP/GEOSITE rules from the subscription.)"
        )

    @classmethod
    def ensure(cls, rules: list, *, base: Optional[str] = None) -> None:
        b = (base or cls.base()).rstrip("/")
        missing = [
            (asset, local)
            for asset, local in cls.needed(rules)
            if not (ROOT / local).exists()
        ]
        if not missing:
            return
        labels = ", ".join(local for _, local in missing)
        with _status(f"downloading geo data ({labels}; first run, ~23 MB) …"):
            for asset, local in missing:
                cls._download(f"{b}/{asset}", ROOT / local)


class WebUI:
    """Downloads the metacubexd dashboard so mihomo can serve it locally."""

    @staticmethod
    def dir() -> Path:
        return ROOT / UI_DIR_NAME

    @classmethod
    def installed(cls) -> bool:
        d = cls.dir()
        return d.is_dir() and any(d.iterdir())

    @staticmethod
    def url() -> str:
        return os.environ.get(UI_ENV, UI_DEFAULT_URL)

    @classmethod
    def ensure(cls, *, force: bool = False) -> Path:
        d = cls.dir()
        if cls.installed() and not force:
            return d
        url = cls.url()
        with _status("downloading web dashboard (metacubexd) …"):
            try:
                payload = requests.get(url, timeout=(10, 60)).content
            except requests.RequestException as e:
                raise click.ClickException(cls._fail_msg(url, str(e))) from e
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
            try:
                import io
                import tarfile

                with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
                    tar.extractall(d)
            except (tarfile.TarError, OSError, EOFError) as e:
                raise click.ClickException(
                    cls._fail_msg(url, f"extract failed: {e}")
                ) from e
        return d

    @staticmethod
    def _fail_msg(url: str, why: str) -> str:
        return (
            f"failed to download the web dashboard ({why}): {url}\n"
            f"  GitHub may be blocked. Point {UI_ENV} at a mirror tgz, e.g.\n"
            f'    export {UI_ENV}="https://ghproxy.net/{UI_DEFAULT_URL}"'
        )


# State ---------------------------------------------------------------------


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


class StateStore:
    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path

    def load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return State()
        allowed = {f.name for f in fields(State)}
        return State(**{k: v for k, v in raw.items() if k in allowed})

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(state), indent=2))


# Profiles & subscriptions --------------------------------------------------


class ProfileRepo:
    def __init__(self, directory: Path = PROFILES_DIR) -> None:
        self.dir = directory

    def _yaml(self, name: str) -> Path:
        return self.dir / f"{name}.yaml"

    def _meta(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def exists(self, name: str) -> bool:
        return self._meta(name).exists()

    def config_path(self, name: str) -> Path:
        return self._yaml(name)

    def list(self) -> list[dict]:
        if not self.dir.exists():
            return []
        out = []
        for meta in sorted(self.dir.glob("*.json")):
            try:
                out.append(json.loads(meta.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def meta(self, name: str) -> dict:
        if not self.exists(name):
            raise click.ClickException(
                f"No subscription '{name}'. Add one with 'usm clash sub add <url>'."
            )
        return json.loads(self._meta(name).read_text())

    @staticmethod
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

    @staticmethod
    def _validate_clash_text(text: str) -> None:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            doc = None
        if not (isinstance(doc, dict) and "proxies" in doc):
            raise click.ClickException(
                "not a Clash config (no 'proxies:' key). Use the provider's "
                "Clash / Clash.Meta subscription link (raw base64 node lists "
                "are not supported)."
            )

    def fetch(self, url: str) -> tuple[str, dict]:
        with _status("fetching subscription …"):
            try:
                r = requests.get(url, headers={"User-Agent": CLASH_UA}, timeout=30)
            except requests.RequestException as e:
                raise click.ClickException(f"failed to fetch subscription: {e}") from e
        if r.status_code != 200:
            raise click.ClickException(f"subscription returned HTTP {r.status_code}")
        self._validate_clash_text(r.text)
        header = r.headers.get("Subscription-Userinfo") or r.headers.get(
            "subscription-userinfo"
        )
        return r.text, (self._parse_userinfo(header) if header else {})

    def save(
        self, name: str, url: str, kind: str, text: str, userinfo: dict, interval: int
    ) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._yaml(name).write_text(text)
        existing = {}
        if self.exists(name):
            try:
                existing = json.loads(self._meta(name).read_text())
            except (json.JSONDecodeError, OSError):
                existing = {}
        now = time.time()
        self._meta(name).write_text(
            json.dumps(
                {
                    "name": name,
                    "url": url,
                    "type": kind,
                    "added_at": existing.get("added_at", now),
                    "updated_at": now,
                    "interval_hours": interval,
                    "userinfo": userinfo or existing.get("userinfo") or {},
                },
                indent=2,
            )
        )

    def add(self, source: str, name: Optional[str], interval: int) -> str:
        is_remote = source.startswith(("http://", "https://"))
        if not name:
            if is_remote:
                host = urllib.parse.urlparse(source).hostname or "profile"
                name = host.split(":")[0].replace(".", "-")
            else:
                name = Path(source).stem
        if is_remote:
            text, userinfo = self.fetch(source)
            self.save(name, source, "remote", text, userinfo, interval)
        else:
            p = Path(source).expanduser()
            if not p.exists():
                raise click.ClickException(f"file not found: {source}")
            text = p.read_text()
            self._validate_clash_text(text)
            self.save(name, str(p), "local", text, {}, 0)
        return name

    def remove(self, name: str) -> None:
        if not self.exists(name):
            raise click.ClickException(f"No subscription '{name}'.")
        self._yaml(name).unlink(missing_ok=True)
        self._meta(name).unlink(missing_ok=True)

    def refresh(self, meta: dict) -> bool:
        if meta.get("type") != "remote":
            return False
        text, userinfo = self.fetch(meta["url"])
        self.save(
            meta["name"],
            meta["url"],
            "remote",
            text,
            userinfo,
            meta.get("interval_hours", 0),
        )
        return True

    def is_stale(self, name: str) -> bool:
        try:
            meta = self.meta(name)
        except click.ClickException:
            return False
        interval = meta.get("interval_hours") or 0
        if meta.get("type") != "remote" or interval <= 0:
            return False
        return time.time() - meta.get("updated_at", 0) >= interval * 3600


# Config composition --------------------------------------------------------


class ConfigComposer:
    def __init__(self, profiles: ProfileRepo, runtime: Path = RUNTIME_CONFIG) -> None:
        self.profiles = profiles
        self.runtime = runtime

    @staticmethod
    def _default_dns() -> dict:
        return {
            "enable": True,
            "ipv6": True,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "nameserver": ["https://dns.google/dns-query", "tls://8.8.8.8"],
            "fallback": ["https://1.1.1.1/dns-query", "tls://1.0.0.1"],
        }

    def compose(self, state: State) -> dict:
        if not state.active:
            raise click.ClickException(
                "No active subscription. Add one with 'usm clash sub add <url>' "
                "then 'usm clash use <name>'."
            )
        path = self.profiles.config_path(state.active)
        if not path.exists():
            raise click.ClickException(
                f"subscription file missing for '{state.active}'."
            )
        base = yaml.safe_load(path.read_text())
        if not isinstance(base, dict):
            raise click.ClickException(
                f"subscription '{state.active}' is not a valid Clash config."
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
        # We manage the geo databases ourselves (see GeoData); pin the source so
        # any fallback download mihomo does still honours the mirror, and don't
        # let it auto-update on a timer.
        base["geodata-mode"] = True
        base["geo-auto-update"] = False
        base["geox-url"] = GeoData.geox_url()
        # Serve the web dashboard locally (mihomo → /ui/) when it's installed.
        if WebUI.installed():
            base["external-ui"] = UI_DIR_NAME
        if state.tun:
            base["tun"] = {
                "enable": True,
                "stack": state.tun_stack,
                "auto-route": True,
                "auto-detect-interface": True,
                "dns-hijack": ["any:53"],
            }
            if not isinstance(base.get("dns"), dict) or not base["dns"].get("enable"):
                base["dns"] = self._default_dns()
        else:
            base["tun"] = {"enable": False}
        return base

    def write(self, state: State) -> dict:
        self.runtime.parent.mkdir(parents=True, exist_ok=True)
        cfg = self.compose(state)
        self.runtime.write_text(
            yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
        )
        return cfg

    def validate(self) -> None:
        mihomo = MihomoBinary.ensure()
        with _status("validating config …"):
            try:
                p = subprocess.run(
                    [str(mihomo), "-t", "-d", str(ROOT), "-f", str(self.runtime)],
                    text=True,
                    capture_output=True,
                    timeout=CONFIG_TEST_TIMEOUT,
                )
            except subprocess.TimeoutExpired as e:
                raise click.ClickException(
                    "config validation timed out — mihomo may be stuck fetching "
                    "geo data. Run 'usm clash geodata' (optionally with a mirror)."
                ) from e
        if p.returncode != 0:
            lines = [
                ln
                for ln in (p.stdout + p.stderr).splitlines()
                if "error" in ln.lower() or "fatal" in ln.lower()
            ]
            detail = lines[-1] if lines else (p.stderr.strip() or p.stdout.strip())
            raise click.ClickException(f"mihomo rejected the config: {detail}")


# RESTful API client --------------------------------------------------------


class ClashAPI:
    def __init__(self, controller: str, secret: str) -> None:
        host, _, port = controller.rpartition(":")
        if host in ("", "0.0.0.0", "::", "*"):
            host = "127.0.0.1"
        self.base = f"http://{host}:{port}"
        self.headers = {"Authorization": f"Bearer {secret}"} if secret else {}

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

    def ping(self) -> bool:
        try:
            r = requests.get(self.base + "/version", headers=self.headers, timeout=2)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def ui_ok(self) -> bool:
        """True if the core is currently serving the web dashboard at /ui/."""
        try:
            r = requests.get(self.base + "/ui/", headers=self.headers, timeout=3)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def version(self) -> Optional[str]:
        """Running mihomo version (e.g. ``v1.19.27``), or None."""
        try:
            r = requests.get(self.base + "/version", headers=self.headers, timeout=2)
            if r.status_code == 200:
                return r.json().get("version")
        except (requests.RequestException, ValueError):
            pass
        return None

    def ui_url(self) -> str:
        """Pre-filled metacubexd setup URL served by the core."""
        host, _, port = self.base[len("http://") :].partition(":")
        q = urllib.parse.urlencode(
            {
                "hostname": host,
                "port": port,
                "secret": self.headers.get("Authorization", "")[len("Bearer ") :],
            }
        )
        return f"{self.base}/ui/#/setup?{q}"

    def wait_ready(self, timeout: float = READY_TIMEOUT) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ping():
                return True
            time.sleep(0.3)
        return False

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
                f"failed to select '{name}' in '{group}': "
                f"{detail or f'HTTP {r.status_code}'}"
            )

    def delay(self, name: str, url: str, timeout_ms: int) -> Optional[int]:
        r = self._req(
            "GET",
            f"/proxies/{urllib.parse.quote(name)}/delay",
            params={"url": url, "timeout": timeout_ms},
            timeout=timeout_ms / 1000 + 5,
        )
        return r.json().get("delay") if r.status_code == 200 else None

    def group_delay(self, group: str, url: str, timeout_ms: int) -> dict:
        r = self._req(
            "GET",
            f"/group/{urllib.parse.quote(group)}/delay",
            params={"url": url, "timeout": timeout_ms},
            timeout=timeout_ms / 1000 + 10,
        )
        return r.json() if r.status_code == 200 else {}

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


# System proxy --------------------------------------------------------------


def _gsettings(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gsettings", *args], text=True, capture_output=True)


class SystemProxy:
    """Set/clear the OS HTTP/SOCKS proxy. Pure: never touches manager state."""

    @staticmethod
    def _write_env(host: str, port: int, enable: bool) -> None:
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
                "unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY "
                "all_proxy no_proxy\n"
            )
        ROOT.mkdir(parents=True, exist_ok=True)
        PROXY_ENV_PATH.write_text(body)

    @classmethod
    def enable(cls, port: int) -> tuple[list[str], Optional[dict]]:
        host = "127.0.0.1"
        notes: list[str] = []
        backup: Optional[dict] = None
        system = platform.system().lower()
        if system == "darwin" and shutil.which("networksetup"):
            for svc in cls._macos_services():
                subprocess.run(["networksetup", "-setwebproxy", svc, host, str(port)])
                subprocess.run(
                    ["networksetup", "-setsecurewebproxy", svc, host, str(port)]
                )
                subprocess.run(
                    ["networksetup", "-setsocksfirewallproxy", svc, host, str(port)]
                )
            notes.append("macOS network services proxy set.")
        elif shutil.which("gsettings"):
            prev = _gsettings("get", "org.gnome.system.proxy", "mode").stdout.strip()
            backup = {"gnome_mode": prev}
            _gsettings("set", "org.gnome.system.proxy", "mode", "manual")
            for scheme in ("http", "https"):
                _gsettings("set", f"org.gnome.system.proxy.{scheme}", "host", host)
                _gsettings("set", f"org.gnome.system.proxy.{scheme}", "port", str(port))
            _gsettings("set", "org.gnome.system.proxy.socks", "host", host)
            _gsettings("set", "org.gnome.system.proxy.socks", "port", str(port))
            notes.append("GNOME proxy set to manual.")
        elif system == "windows":
            cls._windows_set(True, f"{host}:{port}")
            notes.append("Windows proxy enabled.")
        cls._write_env(host, port, True)
        notes.append(
            f"shell exports written to {PROXY_ENV_PATH} (source for CLI apps)."
        )
        return notes, backup

    @classmethod
    def disable(cls, backup: Optional[dict]) -> list[str]:
        notes: list[str] = []
        system = platform.system().lower()
        if system == "darwin" and shutil.which("networksetup"):
            for svc in cls._macos_services():
                for kind in (
                    "-setwebproxystate",
                    "-setsecurewebproxystate",
                    "-setsocksfirewallproxystate",
                ):
                    subprocess.run(["networksetup", kind, svc, "off"])
            notes.append("macOS network services proxy cleared.")
        elif shutil.which("gsettings"):
            prev = (backup or {}).get("gnome_mode", "'none'").strip("'") or "none"
            _gsettings("set", "org.gnome.system.proxy", "mode", prev)
            notes.append("GNOME proxy restored.")
        elif system == "windows":
            cls._windows_set(False, None)
            notes.append("Windows proxy disabled.")
        cls._write_env("127.0.0.1", 0, False)
        return notes

    @staticmethod
    def _macos_services() -> list[str]:
        out = subprocess.run(
            ["networksetup", "-listallnetworkservices"], text=True, capture_output=True
        ).stdout.splitlines()[1:]
        return [s.lstrip("* ").strip() for s in out if s.strip()]

    @staticmethod
    def _windows_set(enable: bool, server: Optional[str]) -> None:
        import winreg  # type: ignore

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enable else 0)
        if enable and server:
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
        winreg.CloseKey(key)


# Supervisor (lifecycle strategy) -------------------------------------------


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


class Supervisor(ABC):
    """How the mihomo process is launched and tracked."""

    enabled = False

    @staticmethod
    def detect() -> "Supervisor":
        return SystemdSupervisor() if Systemd.is_enabled() else StandaloneSupervisor()

    @abstractmethod
    def is_running(self, state: State) -> bool: ...

    @abstractmethod
    def running_pid(self, state: State) -> Optional[int]: ...

    @abstractmethod
    def start(self, state: State) -> None: ...

    @abstractmethod
    def stop(self, state: State) -> bool: ...


class StandaloneSupervisor(Supervisor):
    def is_running(self, state: State) -> bool:
        return self.running_pid(state) is not None

    def running_pid(self, state: State) -> Optional[int]:
        if not state.pid:
            return None
        try:
            os.kill(state.pid, 0)
        except (OSError, ProcessLookupError):
            return None
        return state.pid

    def start(self, state: State) -> None:
        mihomo = MihomoBinary.ensure()
        argv = [str(mihomo), "-d", str(ROOT), "-f", str(RUNTIME_CONFIG)]
        log = open(LOG_PATH, "ab", buffering=0)
        log.write(f"\n--- start {time.strftime('%Y-%m-%dT%H:%M:%S')} ---\n".encode())
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
        proc = subprocess.Popen(argv, **kwargs)
        state.pid = proc.pid
        state.started_at = time.time()

    def stop(self, state: State) -> bool:
        was = _kill_pid(state.pid)
        state.pid = None
        state.started_at = None
        return was


class SystemdSupervisor(Supervisor):
    enabled = True

    def is_running(self, state: State) -> bool:
        return Systemd.is_active()

    def running_pid(self, state: State) -> Optional[int]:
        pid = Systemd.main_pid()
        return pid if pid and Systemd.is_active() else None

    def start(self, state: State) -> None:
        p = Systemd.ctl("start", UNIT_NAME)
        if p.returncode != 0:
            raise click.ClickException(p.stderr.strip() or "systemctl start failed.")

    def stop(self, state: State) -> bool:
        p = Systemd.ctl("stop", UNIT_NAME)
        if p.returncode != 0:
            raise click.ClickException(p.stderr.strip() or "systemctl stop failed.")
        return True


# systemd user-unit helpers -------------------------------------------------


class Systemd:
    @staticmethod
    def unit_path() -> Path:
        return SYSTEMD_USER_DIR / UNIT_NAME

    @classmethod
    def is_enabled(cls) -> bool:
        return cls.unit_path().exists()

    @staticmethod
    def ctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", "--user", *args], text=True, capture_output=True, check=check
        )

    @classmethod
    def require(cls) -> None:
        if os.name != "posix" or not shutil.which("systemctl"):
            raise click.ClickException(
                "Autostart needs systemd (user instance). Not available here."
            )
        if cls.ctl("--version").returncode != 0:
            raise click.ClickException("systemctl --user is not usable here.")

    @classmethod
    def is_active(cls) -> bool:
        return cls.ctl("is-active", UNIT_NAME).stdout.strip() == "active"

    @classmethod
    def main_pid(cls) -> Optional[int]:
        p = cls.ctl("show", "-p", "MainPID", "--value", UNIT_NAME)
        try:
            pid = int(p.stdout.strip())
        except ValueError:
            return None
        return pid or None

    @staticmethod
    def current_user() -> str:
        try:
            return os.getlogin()
        except OSError:
            import pwd

            return pwd.getpwuid(os.getuid()).pw_name

    @classmethod
    def linger_enabled(cls) -> bool:
        if not shutil.which("loginctl"):
            return False
        try:
            out = subprocess.check_output(
                [
                    "loginctl",
                    "show-user",
                    cls.current_user(),
                    "-p",
                    "Linger",
                    "--value",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, OSError):
            return False
        return out.lower() == "yes"

    @classmethod
    def render_unit(cls, usm_bin: str) -> str:
        uv_bin = shutil.which("uv")
        extra = [os.path.dirname(usm_bin)] + (
            [os.path.dirname(uv_bin)] if uv_bin else []
        )
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


# Application service (facade) ----------------------------------------------


@dataclass
class StatusReport:
    running: bool
    pid: Optional[int]
    enabled: bool
    state: State
    traffic: Optional[dict] = None
    node: Optional[str] = None
    group: Optional[str] = None
    dashboard: Optional[str] = None
    version: Optional[str] = None


class ClashManager:
    """Orchestrates binary, state, profiles, config, lifecycle, and the API."""

    def __init__(self) -> None:
        self.store = StateStore()
        self.state = self.store.load()
        self.profiles = ProfileRepo()
        self.composer = ConfigComposer(self.profiles)
        self.supervisor = Supervisor.detect()

    # -- persistence / api --
    def save(self) -> None:
        self.store.save(self.state)

    def api(self) -> ClashAPI:
        return ClashAPI(self.state.controller, self.state.secret)

    def is_running(self) -> bool:
        return self.supervisor.is_running(self.state)

    def running_pid(self) -> Optional[int]:
        return self.supervisor.running_pid(self.state)

    def require_running(self) -> ClashAPI:
        if not self.is_running():
            raise click.ClickException(
                "clash is not running. Start it with 'usm clash up'."
            )
        return self.api()

    def _ensure_secret(self) -> None:
        if not self.state.secret:
            import secrets

            self.state.secret = secrets.token_urlsafe(16)

    # -- the single "apply settings" path --
    def apply(self) -> None:
        """Persist state; if the core is running, regenerate config and reload."""
        self.save()
        if self.is_running():
            cfg = self.composer.write(self.state)
            GeoData.ensure(cfg.get("rules") or [])
            self.composer.validate()
            self.api().reload()

    # -- lifecycle --
    def up(self, *, wait: bool = True) -> None:
        self._ensure_secret()
        if self.profiles.is_stale(self.state.active or ""):
            try:
                self.profiles.refresh(self.profiles.meta(self.state.active))
                console.print(
                    f"[dim]refreshed subscription '{self.state.active}'.[/dim]"
                )
            except click.ClickException:
                pass
        cfg = self.composer.write(self.state)
        GeoData.ensure(cfg.get("rules") or [])
        self.composer.validate()
        # Persist settings *before* launching so the systemd `run` child reads
        # fresh state; save again after start to record the standalone pid.
        self.save()
        self.supervisor.start(self.state)
        self.save()
        if wait and not self._wait_ready_or_fail():
            return

    def _wait_ready_or_fail(self) -> bool:
        with _status("starting clash (this can take a moment on first run) …"):
            ready = self.api().wait_ready()
        if ready:
            return True
        # process may have died, or controller never came up
        if not self.is_running():
            self.supervisor.stop(self.state)
            self.save()
            console.print("[red]✗[/red] mihomo exited during startup. Recent log:")
            for line in _tail(LOG_PATH, 15):
                console.print(f"  [dim]{line}[/dim]")
            raise click.ClickException("clash failed to start.")
        console.print(
            "[yellow]⚠[/yellow] core is up but the controller did not respond in "
            f"{READY_TIMEOUT}s; check 'usm clash logs'."
        )
        return False

    def down(self) -> bool:
        if self.state.system_proxy:
            for note in SystemProxy.disable(self.state.sysproxy_backup):
                console.print(f"  [dim]{note}[/dim]")
            self.state.sysproxy_backup = None
        was = self.supervisor.stop(self.state)
        self.save()
        return was

    def restart(self) -> None:
        if self.supervisor.enabled:
            p = Systemd.ctl("restart", UNIT_NAME)
            if p.returncode != 0:
                raise click.ClickException(p.stderr.strip() or "restart failed.")
            return
        _kill_pid(self.state.pid)
        self.state.pid = None
        self.up()

    def status(self) -> StatusReport:
        pid = self.running_pid()
        running = pid is not None
        traffic = None
        node = group = dashboard = None
        version = f"v{MIHOMO_VERSION}"
        if running:
            api = self.api()
            try:
                traffic = api.connections()
                proxies = api.proxies()
                g = _primary_selector(proxies, self.state.mode)
                if g:
                    node, group = proxies[g].get("now"), g
                version = api.version() or version
                if WebUI.installed() and api.ui_ok():
                    dashboard = api.ui_url()
            except click.ClickException:
                pass
        return StatusReport(
            running=running,
            pid=pid,
            enabled=self.supervisor.enabled,
            state=self.state,
            traffic=traffic,
            node=node,
            group=group,
            dashboard=dashboard,
            version=version,
        )

    # -- settings (each persists + hot-applies if running) --
    def use_profile(self, name: str) -> None:
        if not self.profiles.exists(name):
            raise click.ClickException(f"No subscription '{name}'.")
        self.state.active = name
        self.apply()

    def set_mode(self, mode: str) -> None:
        self.state.mode = mode
        self.save()
        if self.is_running():
            self.api().set_mode(mode)

    def set_port(self, port: int) -> None:
        self.state.port = port
        was_sysproxy = self.state.system_proxy
        self.apply()
        if was_sysproxy and self.is_running():
            self._reapply_system_proxy()

    def set_lan(self, on: bool) -> None:
        self.state.allow_lan = on
        self.apply()

    def set_tun(self, on: bool) -> None:
        if on and not MihomoBinary.has_net_admin():
            self._tun_privilege_hint()
            raise click.ClickException("missing CAP_NET_ADMIN for TUN.")
        self.state.tun = on
        self.apply()

    def set_system_proxy(self, on: bool) -> list[str]:
        if on:
            notes = self._apply_system_proxy_on()
        else:
            notes = SystemProxy.disable(self.state.sysproxy_backup)
            self.state.sysproxy_backup = None
        self.state.system_proxy = on
        self.save()
        return notes

    def _apply_system_proxy_on(self) -> list[str]:
        """Turn the OS proxy on; capture the original setting only once."""
        notes, backup = SystemProxy.enable(self.state.port)
        if self.state.sysproxy_backup is None:
            self.state.sysproxy_backup = backup
        return notes

    def _reapply_system_proxy(self) -> None:
        self._apply_system_proxy_on()
        self.save()

    @staticmethod
    def _tun_privilege_hint() -> None:
        mihomo = MihomoBinary.ensure()
        console.print(
            "[yellow]TUN needs CAP_NET_ADMIN.[/yellow] Grant it once (no per-run sudo):"
        )
        console.print(
            f"  [bold]sudo setcap cap_net_admin,cap_net_bind_service+ep {mihomo}[/bold]"
        )
        console.print("  then re-run [bold]usm clash tun on[/bold].")

    # -- autostart --
    def enable_autostart(self) -> None:
        Systemd.require()
        usm_bin = shutil.which("usm")
        if not usm_bin:
            raise click.ClickException("'usm' not found on PATH; install it first.")
        self._ensure_secret()
        _kill_pid(self.state.pid)
        self.state.pid = None
        self.save()
        cfg = self.composer.write(self.state)
        GeoData.ensure(cfg.get("rules") or [])
        self.composer.validate()
        SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
        Systemd.unit_path().write_text(Systemd.render_unit(usm_bin))
        Systemd.ctl("daemon-reload", check=True)
        p = Systemd.ctl("enable", "--now", UNIT_NAME)
        if p.returncode != 0:
            raise click.ClickException(p.stderr.strip() or "enable --now failed.")
        self.supervisor = SystemdSupervisor()

    def disable_autostart(self) -> None:
        Systemd.require()
        Systemd.ctl("disable", "--now", UNIT_NAME)
        Systemd.unit_path().unlink(missing_ok=True)
        Systemd.ctl("daemon-reload")
        self.supervisor = StandaloneSupervisor()


# CLI -----------------------------------------------------------------------

# Logical grouping for the help / overview (keeps a 20-command CLI readable).
COMMAND_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("Run", ("on", "off", "core")),
    ("Subscriptions", ("sub",)),
    ("Proxy", ("node", "mode", "test")),
    ("Network", ("tun", "lan")),
    ("Observability", ("logs", "conns", "dash")),
    ("Setup", ("enable", "disable", "setup")),
]


class GroupedGroup(click.Group):
    """A click group that renders its commands in labelled sections."""

    def format_commands(self, ctx: click.Context, formatter) -> None:
        listed: set[str] = set()
        for title, names in COMMAND_SECTIONS:
            rows = []
            for name in names:
                cmd = self.get_command(ctx, name)
                if cmd is None or cmd.hidden:
                    continue
                listed.add(name)
                rows.append((name, cmd.get_short_help_str(78)))
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)
        extra = [
            (n, c)
            for n in sorted(self.list_commands(ctx))
            if n not in listed
            and (c := self.get_command(ctx, n)) is not None
            and not c.hidden
        ]
        if extra:
            with formatter.section("Other"):
                formatter.write_dl([(n, c.get_short_help_str(78)) for n, c in extra])


def _bind(state: State) -> str:
    return "0.0.0.0" if state.allow_lan else "127.0.0.1"


def _on_off(value: bool) -> str:
    return "[green]on[/green]" if value else "[dim]off[/dim]"


def _render_status(rep: StatusReport) -> None:
    s = rep.state
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 1))
    table.add_column(justify="right", style="dim", no_wrap=True)
    table.add_column(overflow="fold")
    dot = "[green]● running[/green]" if rep.running else "[dim]○ stopped[/dim]"
    table.add_row("status", dot + (f"  [dim](pid {rep.pid})[/dim]" if rep.pid else ""))
    if rep.version:
        table.add_row("mihomo", rep.version)
    table.add_row("subscription", s.active or "[dim]none[/dim]")
    if rep.node:
        suffix = f"  [dim]({rep.group})[/dim]" if rep.group else ""
        table.add_row("node", f"[bold]{rep.node}[/bold]{suffix}")
    table.add_row("mode", s.mode)
    table.add_row("proxy port", f"{_bind(s)}:{s.port}")
    table.add_row("system proxy", _on_off(s.system_proxy))
    table.add_row("tun", _on_off(s.tun))
    table.add_row("allow-lan", _on_off(s.allow_lan))
    table.add_row(
        "autostart", "[cyan]enabled[/cyan]" if rep.enabled else "[dim]off[/dim]"
    )
    if rep.running and s.started_at and not rep.enabled:
        table.add_row("uptime", fmt_uptime(time.time() - s.started_at))
    if rep.dashboard:
        table.add_row("dashboard", rep.dashboard)
    if rep.traffic is not None:
        t = rep.traffic
        table.add_row(
            "traffic",
            f"↓ {fmt_bytes(t.get('downloadTotal', 0))}  "
            f"↑ {fmt_bytes(t.get('uploadTotal', 0))}  "
            f"[dim]· {len(t.get('connections') or [])} conns[/dim]",
        )
    console.print(table)


@click.group(
    cls=GroupedGroup,
    invoke_without_command=True,
    help=__doc__.splitlines()[0],
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        _render_status(ClashManager().status())
        console.print("\n[dim]Run [bold]usm clash -h[/bold] for all commands.[/dim]")


def _print_endpoint(mgr: ClashManager) -> None:
    s = mgr.state
    console.print(
        f"  mixed proxy: http+socks://{_bind(s)}:{s.port}"
        f"  |  mode: {s.mode}  |  tun: {'on' if s.tun else 'off'}"
    )
    console.print(f"  [dim]point apps at[/dim] http://127.0.0.1:{s.port}")


# Interactive selection helpers ---------------------------------------------


# Node selection ------------------------------------------------------------


def _selectors(proxies: dict) -> list[str]:
    """All Selector-type groups (includes the built-in GLOBAL)."""
    return [k for k, v in proxies.items() if v.get("type") == "Selector"]


def _user_groups(proxies: dict, mode: str) -> list[str]:
    """Groups a user actually switches in: the built-in GLOBAL only matters in
    global mode; otherwise the subscription's own Selector groups."""
    sels = _selectors(proxies)
    if mode == "global":
        return ["GLOBAL"] if "GLOBAL" in sels else sels
    return [s for s in sels if s != "GLOBAL"]


def _primary_selector(proxies: dict, mode: str = "rule") -> Optional[str]:
    groups = _user_groups(proxies, mode)
    if not groups:
        return None
    for kw in ("proxy", "节点", "select"):
        for s in groups:
            if kw in s.lower():
                return s
    return groups[0]


def _node_delay(proxies: dict, name: str) -> Optional[int]:
    hist = (proxies.get(name, {}) or {}).get("history") or []
    return hist[-1].get("delay") if hist and hist[-1].get("delay") else None


class NodeSwitcher:
    """Domain logic for inspecting and switching proxy-group selections.

    Holds a single snapshot of ``/proxies`` and exposes the switchable groups,
    their members/latency, and the select action. Presentation (menus, tables)
    lives in the CLI layer.
    """

    def __init__(self, api: "ClashAPI", mode: str) -> None:
        self.api = api
        self.mode = mode
        self.proxies = api.proxies()

    @property
    def selectors(self) -> list[str]:
        return _selectors(self.proxies)

    @property
    def groups(self) -> list[str]:
        return _user_groups(self.proxies, self.mode)

    def members(self, group: str) -> list[str]:
        return self.proxies.get(group, {}).get("all", []) or []

    def current(self, group: str) -> Optional[str]:
        return self.proxies.get(group, {}).get("now")

    def delay(self, name: str) -> Optional[int]:
        return _node_delay(self.proxies, name)

    def group_delays(self, group: str) -> dict:
        return self.api.group_delay(group, DEFAULT_TEST_URL, 5000)

    def find_nodes(self, query: str) -> list[tuple[str, str]]:
        q = query.lower()
        return [(g, m) for g in self.groups for m in self.members(g) if q in m.lower()]

    def resolve_group(self, query: str) -> Optional[str]:
        exact = [g for g in self.groups if g.lower() == query.lower()]
        if exact:
            return exact[0]
        subs = [g for g in self.groups if query.lower() in g.lower()]
        return subs[0] if len(subs) == 1 else None

    def apply(self, group: str, node: str) -> None:
        self.api.select(group, node)


def _pick(title: str, options: list[tuple[str, object]], *, current=None):
    """Show a numbered menu and return the chosen value (or None if cancelled)."""
    if not options:
        return None
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 1))
    table.add_column(justify="right", style="bold green", no_wrap=True)
    table.add_column(overflow="fold")
    for i, (label, value) in enumerate(options, 1):
        dot = " [green]●[/green]" if current is not None and value == current else ""
        table.add_row(str(i), f"{label}{dot}")
    console.print(table)
    n = len(options)
    try:
        sel = click.prompt(
            f"{title} (1-{n}, 0 to cancel)",
            type=click.IntRange(0, n),
            default=0,
            show_default=False,
        )
    except click.Abort:
        return None
    return None if not sel else options[sel - 1][1]


def _resolve_one(query: str, candidates: list[str], kind: str) -> str:
    """Match *query* to exactly one candidate (exact, else unique substring)."""
    ci = query.lower()
    for c in candidates:
        if c.lower() == ci:
            return c
    subs = [c for c in candidates if ci in c.lower()]
    if len(subs) == 1:
        return subs[0]
    if len(subs) > 1:
        raise click.ClickException(f"ambiguous {kind} '{query}': {', '.join(subs)}")
    raise click.ClickException(f"no {kind} matching '{query}'.")


def _pick_node(sw: NodeSwitcher, group: str, do_test: bool) -> Optional[str]:
    members = sw.members(group)
    if not members:
        console.print(f"[dim]{group} has no members.[/dim]")
        return None
    delays: dict = {}
    if do_test:
        with _status(f"testing {group} …"):
            delays = sw.group_delays(group)
    options = []
    for m in members:
        d = delays.get(m) if do_test else sw.delay(m)
        tag = f"  [dim]{d}ms[/dim]" if d else ("  [red]✗[/red]" if do_test else "")
        options.append((f"{m}{tag}", m))
    return _pick(f"node in {group}", options, current=sw.current(group))


# ---- subscriptions ----


@cli.group("sub", short_help="Add / list / update / remove subscriptions.")
def sub() -> None:
    pass


@sub.command("add", short_help="Add a subscription URL or local config file.")
@click.argument("source")
@click.option("--name", help="Subscription name (default: derived from the URL/file).")
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
    help="Make this the active subscription.",
)
def cmd_sub_add(source, name, interval, use):
    mgr = ClashManager()
    saved = mgr.profiles.add(source, name, interval)
    console.print(f"[green]✓[/green] saved subscription [bold]{saved}[/bold].")
    if use:
        mgr.state.active = saved
        if mgr.is_running():
            mgr.apply()
            console.print(
                f"  active subscription → [bold]{saved}[/bold] (applied live)"
            )
        else:
            mgr.save()
            console.print(f"  active subscription → [bold]{saved}[/bold]")


@sub.command("ls", short_help="List subscriptions (numbered).")
def cmd_sub_ls():
    mgr = ClashManager()
    profiles = mgr.profiles.list()
    if not profiles:
        console.print(
            "[dim]No subscriptions. Add one with 'usm clash sub add <url>'.[/dim]"
        )
        return
    table = Table(show_header=True, header_style="bold")
    for col in (
        "#",
        "",
        "Name",
        "Type",
        "Updated",
        "Traffic (used / total)",
        "Expires",
    ):
        table.add_column(col)
    for i, meta in enumerate(profiles, 1):
        active = "[green]●[/green]" if meta["name"] == mgr.state.active else ""
        updated = datetime.fromtimestamp(meta.get("updated_at", 0)).strftime(
            "%Y-%m-%d %H:%M"
        )
        ui = meta.get("userinfo") or {}
        traffic = "-"
        total = _as_int(ui.get("total"))
        if total:
            used = _as_int(ui.get("upload")) + _as_int(ui.get("download"))
            traffic = f"{fmt_bytes(used)} / {fmt_bytes(total)}"
        expire = fmt_epoch(ui["expire"]) if ui.get("expire") else "-"
        table.add_row(
            str(i),
            active,
            meta["name"],
            meta.get("type", "?"),
            updated,
            traffic,
            expire,
        )
    console.print(table)
    console.print("[dim]switch with[/dim] usm clash sub use <#|name>")


@sub.command("update", short_help="Refresh remote subscriptions (NAME or all).")
@click.argument("name", required=False)
def cmd_sub_update(name):
    mgr = ClashManager()
    targets = mgr.profiles.list()
    if name:
        targets = [m for m in targets if m["name"] == name]
        if not targets:
            raise click.ClickException(f"No subscription '{name}'.")
    refreshed = 0
    for meta in targets:
        if meta.get("type") != "remote":
            console.print(f"  [dim]–[/dim] {meta['name']} (local, skipped)")
            continue
        try:
            mgr.profiles.refresh(meta)
            console.print(f"  [green]✓[/green] {meta['name']}")
            refreshed += 1
        except click.ClickException as e:
            console.print(f"  [red]✗[/red] {meta['name']}: {e.message}")
    if (
        refreshed
        and mgr.is_running()
        and mgr.state.active in {m["name"] for m in targets}
    ):
        mgr.apply()
        console.print("[dim]applied the refreshed config to the running core.[/dim]")


@sub.command("rm", short_help="Delete a subscription.")
@click.argument("name")
def cmd_sub_rm(name):
    mgr = ClashManager()
    mgr.profiles.remove(name)
    if mgr.state.active == name:
        mgr.state.active = None
        mgr.save()
    console.print(f"[green]✓[/green] removed subscription {name}")


@sub.command("use", short_help="Switch the active subscription (interactive).")
@click.argument("target", required=False)
def cmd_sub_use(target):
    mgr = ClashManager()
    names = [m["name"] for m in mgr.profiles.list()]
    if not names:
        raise click.ClickException(
            "no subscriptions yet. Add one with 'usm clash sub add <url>'."
        )
    if target is None:
        name = _pick("subscription", [(p, p) for p in names], current=mgr.state.active)
        if not name:
            return
    elif target.isdigit():
        i = int(target)
        if not (1 <= i <= len(names)):
            raise click.ClickException(f"index out of range (1-{len(names)}).")
        name = names[i - 1]
    else:
        name = _resolve_one(target, names, "subscription")
    mgr.use_profile(name)
    console.print(f"[green]✓[/green] subscription → [bold]{name}[/bold]")
    if mgr.is_running():
        console.print("  [dim]applied to the running core.[/dim]")


# ---- lifecycle ----


@cli.group("core", short_help="Start / stop / restart the mihomo core.")
def core() -> None:
    pass


@core.command("up", short_help="Start the core (or apply settings if already running).")
@click.argument("name", required=False)
@click.option("--tun/--no-tun", default=None, help="Enable/disable TUN.")
@click.option("--lan/--no-lan", default=None, help="Allow LAN connections.")
@click.option(
    "--system-proxy/--no-system-proxy",
    default=None,
    help="Set the OS proxy after start.",
)
@click.option("-p", "--port", type=int, help="Mixed HTTP+SOCKS port.")
def cmd_up(name, tun, lan, system_proxy, port):
    mgr = ClashManager()

    # Validate TUN privilege up front so we fail before changing anything.
    if tun and not MihomoBinary.has_net_admin():
        mgr._tun_privilege_hint()
        raise click.ClickException("missing CAP_NET_ADMIN for TUN.")

    if mgr.is_running():
        # Apply only what was asked, live, via the canonical setters.
        applied = False
        if name:
            mgr.use_profile(name)
            applied = True
        if port:
            mgr.set_port(port)
            applied = True
        if lan is not None:
            mgr.set_lan(lan)
            applied = True
        if tun is not None:
            mgr.set_tun(tun)
            applied = True
        if system_proxy is not None:
            mgr.set_system_proxy(system_proxy)
            applied = True
        console.print(
            "[green]✓[/green] already running — "
            + ("applied settings." if applied else "nothing to change.")
        )
        _print_endpoint(mgr)
        return

    if name:
        if not mgr.profiles.exists(name):
            raise click.ClickException(f"No subscription '{name}'.")
        mgr.state.active = name
    if port:
        mgr.state.port = port
    if lan is not None:
        mgr.state.allow_lan = lan
    if tun is not None:
        mgr.state.tun = tun
    if system_proxy is not None:
        mgr.state.system_proxy = system_proxy

    mgr.up()
    if not mgr.is_running():
        return
    console.print(
        f"[green]✓[/green] clash up (pid {mgr.running_pid()}) — "
        f"sub [bold]{mgr.state.active}[/bold]"
    )
    _print_endpoint(mgr)
    if mgr.state.system_proxy:
        for note in mgr.set_system_proxy(True):
            console.print(f"  [dim]{note}[/dim]")


@core.command("down", short_help="Stop the core.")
def cmd_down():
    mgr = ClashManager()
    was = mgr.down()
    console.print(f"[green]✓[/green] {'stopped' if was else 'already stopped'}.")


@core.command("restart", short_help="Restart the core.")
def cmd_restart():
    mgr = ClashManager()
    mgr.restart()
    if mgr.is_running():
        console.print(f"[green]✓[/green] restarted (pid {mgr.running_pid()}).")
        if mgr.state.system_proxy and not mgr.supervisor.enabled:
            mgr._reapply_system_proxy()


# ---- runtime control ----


@cli.command("mode", short_help="Get or set routing mode (rule|global|direct).")
@click.argument("mode", required=False, type=click.Choice(MODES))
def cmd_mode(mode):
    mgr = ClashManager()
    if not mode:
        console.print(f"mode: [bold]{mgr.state.mode}[/bold]")
        return
    mgr.set_mode(mode)
    console.print(f"[green]✓[/green] mode → [bold]{mode}[/bold]")


def _render_node_list(api, group: Optional[str]) -> None:
    proxies = api.proxies()
    groups = {k: v for k, v in proxies.items() if v.get("type") in GROUP_TYPES}
    if group:
        if group not in groups:
            raise click.ClickException(
                f"No proxy group '{group}'. Groups: {', '.join(groups) or '(none)'}"
            )
        groups = {group: groups[group]}
    else:
        groups = {k: v for k, v in groups.items() if k != "GLOBAL"}
    if not groups:
        console.print("[dim]This subscription has no groups.[/dim]")
        return
    for gname, g in groups.items():
        now = g.get("now")
        console.print(
            f"[bold cyan]{gname}[/bold cyan] [dim]({g.get('type')})[/dim]"
            + (f"  [dim]→[/dim] [bold]{now}[/bold]" if now else "")
        )
        inner = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
        inner.add_column(no_wrap=True)
        inner.add_column(justify="right", style="dim")
        for member in g.get("all", []):
            mark = "[green]●[/green]" if member == now else "[dim]·[/dim]"
            d = _node_delay(proxies, member)
            inner.add_row(f"   {mark} {member}", f"{d}ms" if d else "")
        console.print(inner)


@cli.command(
    "node",
    short_help="Switch the active node (interactive); -l lists, NAME switches.",
)
@click.argument("group", required=False)
@click.argument("node", required=False)
@click.option(
    "-l", "--list", "list_only", is_flag=True, help="List groups & nodes; don't switch."
)
@click.option(
    "-t", "--test", "do_test", is_flag=True, help="Latency-test before choosing."
)
def cmd_node(group, node, list_only, do_test):
    mgr = ClashManager()
    api = mgr.require_running()
    if list_only:
        _render_node_list(api, group)
        return

    sw = NodeSwitcher(api, mgr.state.mode)
    if not sw.groups:
        raise click.ClickException("this subscription has no selectable groups.")

    def _apply(g: str, n: str) -> None:
        sw.apply(g, n)
        console.print(f"[green]✓[/green] {g} → [bold]{n}[/bold]")

    # explicit group + node (substring-friendly; explicit GLOBAL allowed)
    if group and node:
        g = _resolve_one(group, sw.selectors, "group")
        _apply(g, _resolve_one(node, sw.members(g), "node"))
        return

    # single arg: a group to drill into, else a node to find
    if group:
        g = sw.resolve_group(group)
        if g:
            chosen = _pick_node(sw, g, do_test)
            if chosen:
                _apply(g, chosen)
            return
        hits = sw.find_nodes(group)
        if not hits:
            raise click.ClickException(f"no group or node matching '{group}'.")
        if len(hits) == 1:
            _apply(*hits[0])
            return
        # Same node name across groups: prefer the primary group if it has it.
        primary = _primary_selector(sw.proxies, sw.mode)
        primary_hits = [(g, m) for g, m in hits if g == primary]
        if primary_hits and len({m for _, m in hits}) == 1:
            _apply(*primary_hits[0])
            return
        chosen = _pick(
            f"node matching '{group}'",
            [(f"{m}   [dim]in {g}[/dim]", (g, m)) for g, m in hits],
        )
        if chosen:
            _apply(*chosen)
        return

    # no args: pick group (if several), then node
    g = sw.groups[0]
    if len(sw.groups) > 1:
        g = _pick(
            "group",
            [
                (
                    f"{s}  [dim]→ {sw.current(s)} ({len(sw.members(s))} nodes)[/dim]",
                    s,
                )
                for s in sw.groups
            ],
        )
        if not g:
            return
    chosen = _pick_node(sw, g, do_test)
    if chosen:
        _apply(g, chosen)


@cli.command("test", short_help="Latency-test a group, a node, or all nodes.")
@click.argument("target", required=False)
@click.option("--url", default=DEFAULT_TEST_URL, show_default=True, help="Test URL.")
@click.option(
    "--timeout", type=int, default=5000, show_default=True, help="Timeout (ms)."
)
def cmd_test(target, url, timeout):
    mgr = ClashManager()
    api = mgr.require_running()
    proxies = api.proxies()
    groups = {k for k, v in proxies.items() if v.get("type") in GROUP_TYPES}

    if target and target in groups:
        with _status(f"testing group {target} …"):
            result = api.group_delay(target, url, timeout)
        if not result:
            console.print("[yellow]no node responded.[/yellow]")
            return
        for node, delay in sorted(result.items(), key=lambda kv: kv[1]):
            console.print(f"  {node}: [green]{delay}ms[/green]")
        return

    if target:
        with _status(f"testing {target} …"):
            delay = api.delay(target, url, timeout)
        console.print(
            f"  {target}: [green]{delay}ms[/green]"
            if delay
            else f"  {target}: [red]timeout[/red]"
        )
        return

    nodes = [k for k, v in proxies.items() if v.get("type") not in NON_NODE_TYPES]
    if not nodes:
        console.print("[dim]No testable nodes.[/dim]")
        return
    results: dict[str, Optional[int]] = {}
    with _status(f"testing {len(nodes)} node(s) …"):
        with ThreadPoolExecutor(max_workers=16) as pool:
            for node, delay in zip(
                nodes, pool.map(lambda n: api.delay(n, url, timeout), nodes)
            ):
                results[node] = delay
    for node in sorted(results, key=lambda n: results[n] if results[n] else 1 << 30):
        delay = results[node]
        console.print(
            f"  {node}: [green]{delay}ms[/green]"
            if delay
            else f"  {node}: [red]timeout[/red]"
        )


# ---- system integration ----


@cli.command("tun", short_help="Toggle TUN (transparent system-wide proxy).")
@click.argument("action", type=click.Choice(["on", "off", "status"]))
def cmd_tun(action):
    mgr = ClashManager()
    if action == "status":
        console.print(f"tun: [bold]{'on' if mgr.state.tun else 'off'}[/bold]")
        return
    mgr.set_tun(action == "on")
    console.print(f"[green]✓[/green] tun → [bold]{action}[/bold]")
    if action == "on":
        console.print("  [dim]all system traffic is now captured by the core.[/dim]")


@cli.command("on", short_help="Set this machine's system proxy to clash (auto-starts).")
def cmd_on():
    mgr = ClashManager()
    if not mgr.is_running():
        mgr.up()
        if not mgr.is_running():
            return
        console.print(
            f"[green]✓[/green] clash up (pid {mgr.running_pid()}) — "
            f"sub [bold]{mgr.state.active}[/bold]"
        )
    for note in mgr.set_system_proxy(True):
        console.print(f"  [dim]{note}[/dim]")
    console.print(
        f"[green]✓[/green] system proxy → [bold]on[/bold] (127.0.0.1:{mgr.state.port})"
    )
    console.print(f"  [dim]for the current shell:[/dim] source {PROXY_ENV_PATH}")


@cli.command(
    "off", short_help="Clear this machine's system proxy (core keeps running)."
)
def cmd_off():
    mgr = ClashManager()
    for note in mgr.set_system_proxy(False):
        console.print(f"  [dim]{note}[/dim]")
    console.print("[green]✓[/green] system proxy → [bold]off[/bold]")


@cli.command("lan", short_help="Toggle LAN access (allow-lan).")
@click.argument("action", type=click.Choice(["on", "off", "status"]))
def cmd_lan(action):
    mgr = ClashManager()
    if action == "status":
        console.print(
            f"allow-lan: [bold]{'on' if mgr.state.allow_lan else 'off'}[/bold]"
        )
        return
    mgr.set_lan(action == "on")
    console.print(f"[green]✓[/green] allow-lan → [bold]{action}[/bold]")
    if mgr.state.allow_lan:
        console.print(
            f"  [dim]other devices can use[/dim] http://<this-host-ip>:{mgr.state.port}"
        )


# ---- observability ----


@cli.command("logs", short_help="Show recent logs, or stream live with -f/-w.")
@click.option(
    "-f",
    "--follow",
    "-w",
    "--watch",
    "follow",
    is_flag=True,
    help="Stream live logs via the API.",
)
@click.option("-n", "--lines", type=int, default=50, show_default=True)
@click.option(
    "--level",
    default="info",
    show_default=True,
    type=click.Choice(["debug", "info", "warning", "error", "silent"]),
)
def cmd_logs(follow, lines, level):
    mgr = ClashManager()
    if follow:
        api = mgr.require_running()
        console.print("[dim]following logs (Ctrl-C to stop) …[/dim]")
        try:
            for entry in api.stream("/logs", {"level": level}):
                lvl = entry.get("type", "info")
                color = {"warning": "yellow", "error": "red", "debug": "dim"}.get(
                    lvl, "white"
                )
                console.print(f"[{color}]{lvl:>7}[/{color}] {entry.get('payload', '')}")
        except KeyboardInterrupt:
            pass
        except click.ClickException as exc:
            console.print(f"[red]logs stream ended:[/red] {exc}")
        return
    if not LOG_PATH.exists():
        console.print("[dim]No logs yet.[/dim]")
        return
    for line in _tail(LOG_PATH, lines):
        click.echo(line)


def _conns_renderable(data: dict):
    from rich.console import Group

    conns = data.get("connections") or []
    header = (
        f"[dim]↓ {fmt_bytes(data.get('downloadTotal', 0))}  "
        f"↑ {fmt_bytes(data.get('uploadTotal', 0))}  |  {len(conns)} active[/dim]"
    )
    if not conns:
        return header
    table = Table(show_header=True, header_style="bold", expand=True)
    for col in ("Host", "Chain", "Rule", "↑", "↓"):
        table.add_column(col, justify="right" if col in ("↑", "↓") else "left")
    ordered = sorted(conns, key=lambda c: c.get("download", 0), reverse=True)
    for c in ordered[:40]:
        meta = c.get("metadata", {})
        host = meta.get("host") or meta.get("destinationIP", "")
        table.add_row(
            f"{host}:{meta.get('destinationPort', '')}",
            " → ".join(c.get("chains", [])[::-1]),
            c.get("rule", ""),
            fmt_bytes(c.get("upload", 0)),
            fmt_bytes(c.get("download", 0)),
        )
    return Group(header, table)


@cli.command("conns", short_help="Show active connections (-w to watch live).")
@click.option("-w", "--watch", is_flag=True, help="Refresh live until Ctrl-C.")
@click.option("--close", is_flag=True, help="Close all active connections.")
def cmd_conns(watch, close):
    mgr = ClashManager()
    api = mgr.require_running()
    if close:
        api.close_connections()
        console.print("[green]✓[/green] closed all connections.")
        return
    if watch:
        from rich.live import Live

        try:
            with Live(console=console, refresh_per_second=4, screen=True) as live:
                while True:
                    live.update(_conns_renderable(api.connections()))
                    time.sleep(1.5)
        except KeyboardInterrupt:
            pass
        except click.ClickException as exc:
            console.print(f"[red]{exc}[/red]")
        return
    console.print(_conns_renderable(api.connections()))


@cli.command("dash", short_help="Open the local web dashboard (metacubexd).")
@click.option(
    "--no-open", is_flag=True, help="Just print the URL; don't open a browser."
)
def cmd_dash(no_open):
    mgr = ClashManager()
    api = mgr.require_running()
    WebUI.ensure()
    # mihomo only mounts external-ui at startup, so a hot reload won't expose
    # /ui/. If it isn't being served yet, restart the core (the regenerated
    # config now includes external-ui because the UI is installed).
    if not api.ui_ok():
        with _status("enabling dashboard (restarting core) …"):
            mgr.restart()
            api = mgr.api()
            api.wait_ready()
        if not api.ui_ok():
            raise click.ClickException(
                "the core is running but isn't serving /ui/. Check 'usm clash logs'."
            )
    url = api.ui_url()
    console.print(f"[green]✓[/green] dashboard: [bold]{url}[/bold]")
    if not no_open:
        import webbrowser

        try:
            if webbrowser.open(url):
                console.print("[dim]opened in your browser.[/dim]")
        except Exception:
            pass


# ---- autostart ----


@cli.command("enable", short_help="Autostart at login via a systemd user unit.")
def cmd_enable():
    mgr = ClashManager()
    mgr.enable_autostart()
    console.print(f"[green]✓[/green] enabled & started ({UNIT_NAME}).")
    if not Systemd.linger_enabled():
        console.print(
            "  [yellow]note:[/yellow] to start at boot before login, run "
            f"[bold]sudo loginctl enable-linger {Systemd.current_user()}[/bold]"
        )


@cli.command("disable", short_help="Remove the systemd user unit.")
def cmd_disable():
    mgr = ClashManager()
    if not Systemd.is_enabled():
        console.print("[dim]autostart is not enabled.[/dim]")
        return
    mgr.disable_autostart()
    console.print("[green]✓[/green] autostart disabled.")


@cli.command("run", hidden=True, short_help="(internal) run mihomo in the foreground.")
def cmd_run():
    mgr = ClashManager()
    cfg = mgr.composer.write(mgr.state)
    GeoData.ensure(cfg.get("rules") or [])
    mihomo = MihomoBinary.ensure()
    mgr.state.pid = os.getpid()
    mgr.state.started_at = time.time()
    mgr.save()
    try:
        os.execvp(
            str(mihomo), [str(mihomo), "-d", str(ROOT), "-f", str(RUNTIME_CONFIG)]
        )
    except FileNotFoundError as exc:
        raise click.ClickException(f"{mihomo} not found.") from exc


@cli.command(
    "setup",
    short_help="Download the mihomo binary + GeoIP/GeoSite data + web dashboard.",
)
@click.option("--force", is_flag=True, help="Re-download everything, even if present.")
@click.option("--geo-mirror", help=f"Geo data base URL (overrides ${GEO_ENV}).")
def cmd_setup(force, geo_mirror):
    ROOT.mkdir(parents=True, exist_ok=True)
    path = MihomoBinary.ensure(upgrade=force)
    console.print(f"[green]✓[/green] mihomo {MIHOMO_VERSION} [dim]({path})[/dim]")

    base = (geo_mirror or GeoData.base()).rstrip("/")
    for asset, local in GEO_RULE_FILES.values():
        dest = ROOT / local
        if dest.exists() and not force:
            console.print(f"[green]✓[/green] {local} [dim](present)[/dim]")
            continue
        with _status(f"downloading {local} …"):
            GeoData._download(f"{base}/{asset}", dest)
        console.print(f"[green]✓[/green] {local} ({fmt_bytes(dest.stat().st_size)})")

    if WebUI.installed() and not force:
        console.print("[green]✓[/green] web dashboard [dim](present)[/dim]")
    else:
        WebUI.ensure(force=force)
        console.print("[green]✓[/green] web dashboard (metacubexd)")
    console.print("[bold green]Setup complete.[/bold green]")


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
