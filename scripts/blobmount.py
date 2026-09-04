#!/usr/bin/env python3
"""Mount Azure Blob containers with blobfuse2.

The shell version this replaces minted a 6-day SAS once at mount time; when
it expired the mount stayed up but every read started failing. Here a small
supervisor re-mints the token and rewrites the config before it lapses, so a
mount survives indefinitely.

Examples
--------
  usm blobmount mount /mnt/data myaccount mycontainer
  usm blobmount mount /mnt/data myaccount mycontainer --auth fic
  usm blobmount ls                       # what is mounted, and is it healthy
  usm blobmount status data              # detail + SAS clock + probe result
  usm blobmount refresh data             # re-mint the SAS right now
  usm blobmount enable data              # remount at boot, keep refreshing
  usm blobmount umount data

Shape
-----
Everything Azure-shaped (SAS providers and rotation, blob URLs, service
units, locking, redaction) comes from ``usm_azure``, shared with
``usm azsync``. What lives here is blobfuse2: installing it, rendering its
config, mounting, probing a mountpoint's health, and the refresh loop.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import click
from usm_cli import grouped_class
import yaml
from rich.console import Console

from usm_fic import workload_identity_from_env
from usm_azure import (
    AUTH_KINDS,
    AUTH_SPEC_FLAG,
    DEFAULT_SAS_MIN_REMAINING,
    DEFAULT_SAS_TTL_HOURS,
    SECTION,
    USM_CACHE_DIR,
    FileLock,
    SasError,
    SasManager,
    ServiceManager,
    atomic_write,
    blobfuse_mounts,
    build_provider,
    compact_duration,
    container_url,
    human_duration,
    kv_table,
    new_table,
    is_https_blob,
    parse_blob_url,
    pid_alive,
    read_json,
    redact,
    run,
    shorten_path,
    sleep_until,
    slugify,
    split_sas,
    usm_bin,
)

console = Console(stderr=True)
BLOBMOUNT_AUTH_KINDS = (*AUTH_KINDS, "fic")

STATE_DIR = Path(
    os.environ.get("USM_BLOBMOUNT_STATE_DIR") or (USM_CACHE_DIR / "blobmount")
)
CACHE_ROOT = Path(
    os.environ.get("USM_BLOBMOUNT_CACHE_DIR") or (USM_CACHE_DIR / "blobfuse2")
)
CONFIG_ROOT = Path(
    os.environ.get("USM_BLOBMOUNT_CONFIG_DIR") or (Path.home() / ".config" / "blobfuse")
)

SERVICE = ServiceManager("usm-blobmount-", "com.github.hspk.usm.blobmount.")
SUPERVISE_ENV = "USM_BLOBMOUNT_SUPERVISE_ID"

# blobfuse2 reads the SAS once, at mount. Rotation therefore means "rewrite
# the config and remount", so the token is renewed well ahead of expiry.
DEFAULT_REFRESH_INTERVAL = 6 * 3600.0
DEFAULT_PROBE_INTERVAL = 60.0
MOUNT_TIMEOUT = 120.0
UNMOUNT_TIMEOUT = 60.0
STARTUP_GRACE_SECS = 2.0

BLOBFUSE_RELEASE = "blobfuse2-2.5.4"
BLOBFUSE_RELEASE_URL = (
    f"https://github.com/Azure/azure-storage-fuse/releases/download/{BLOBFUSE_RELEASE}"
)

# Health states a mountpoint can be in.
OK = "ok"
STALE = "stale"  # blobfuse2 gone but the kernel still has the mount
UNMOUNTED = "unmounted"
DENIED = "denied"  # mounted, but I/O fails (usually an expired SAS)
MISSING = "missing"  # the mount directory doesn't exist


class BlobmountError(click.ClickException):
    """Anything the user can act on."""


# ==========================================================================
# blobfuse2 binary
# ==========================================================================


def find_blobfuse2() -> str | None:
    override = os.environ.get("USM_BLOBFUSE2_BIN")
    if override and os.access(override, os.X_OK):
        return override
    return shutil.which("blobfuse2")


def ensure_blobfuse2() -> str:
    path = find_blobfuse2()
    if path:
        return path
    raise BlobmountError(
        "blobfuse2 not found. Install it with `usm blobmount install`, "
        "or set $USM_BLOBFUSE2_BIN to an existing binary."
    )


def blobfuse_deb_url() -> str:
    """Return the release asset matching this Debian-family host."""
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if arch is None:
        raise BlobmountError(f"unsupported blobfuse2 architecture: {machine}")

    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key] = value.strip().strip('"')
    except OSError as exc:
        raise BlobmountError(f"cannot read /etc/os-release: {exc}") from exc
    distro = values.get("ID", "").lower()
    version = values.get("VERSION_ID", "")
    major = version.partition(".")[0]

    if distro == "ubuntu":
        release = version if version in {"18.04", "20.04", "22.04"} else "22.04"
        asset = f"{BLOBFUSE_RELEASE}-Ubuntu-{release}.{arch}.deb"
    elif (
        distro == "debian"
        and arch == "x86_64"
        and major in {"9", "10", "11", "12", "13"}
    ):
        asset = f"{BLOBFUSE_RELEASE}-Debian-{major}.0.x86_64.deb"
    else:
        raise BlobmountError(
            f"no blobfuse2 {BLOBFUSE_RELEASE} .deb for "
            f"{distro or 'unknown'} {version or 'unknown'} {arch}"
        )
    return f"{BLOBFUSE_RELEASE_URL}/{asset}"


def install_blobfuse2(*, assume_yes: bool = False) -> str:
    """Install blobfuse2 from the upstream .deb (Debian/Ubuntu only)."""
    if platform.system().lower() != "linux":
        raise BlobmountError(
            "automatic install only supports Debian/Ubuntu. See "
            "https://github.com/Azure/azure-storage-fuse for other platforms."
        )
    if not shutil.which("dpkg"):
        raise BlobmountError(
            "this looks like a non-Debian system (no dpkg). Install blobfuse2 "
            "from your package manager and set $USM_BLOBFUSE2_BIN if needed."
        )
    sudo = [] if os.geteuid() == 0 else ["sudo"]
    if sudo and not shutil.which("sudo"):
        raise BlobmountError("need root (or sudo) to install blobfuse2.")

    workdir = USM_CACHE_DIR / "downloads"
    workdir.mkdir(parents=True, exist_ok=True)
    url = blobfuse_deb_url()
    deb = workdir / Path(url).name
    console.print(f"[dim]Downloading {url}[/dim]")
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=300) as response:
            deb.write_bytes(response.read())
    except (urllib.error.URLError, OSError) as exc:
        raise BlobmountError(f"download failed: {exc}") from exc

    steps = [
        (sudo + ["apt-get", "update"], False),
        (sudo + ["apt-get", "install", "-y", "fuse3"], True),
        (sudo + ["dpkg", "-i", str(deb)], True),
        (sudo + ["apt-get", "install", "-f", "-y"], False),
    ]
    for argv, required in steps:
        proc = run(argv)
        if proc.returncode != 0 and required:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise BlobmountError(
                f"{argv[-1]} failed: {detail[-1] if detail else proc.returncode}"
            )
    _allow_other()
    path = find_blobfuse2()
    if not path:
        raise BlobmountError("blobfuse2 still not on PATH after installing.")
    return path


def _allow_other() -> None:
    """`--allow-other` needs user_allow_other in /etc/fuse.conf."""
    conf = Path("/etc/fuse.conf")
    try:
        text = conf.read_text()
    except OSError:
        return
    if "user_allow_other" in text and not text.strip().startswith("#"):
        for line in text.splitlines():
            if line.strip() == "user_allow_other":
                return
    sudo = [] if os.geteuid() == 0 else ["sudo"]
    run(sudo + ["sed", "-i", "1i user_allow_other", str(conf)])


# ==========================================================================
# Data model
# ==========================================================================


@dataclass
class Mount:
    """A managed mount. Written by the CLI, read by the supervisor."""

    id: str
    mount_dir: str
    account: str
    container: str

    auth: str = "az"
    sas_spec: str | None = None
    sas_headers: list[str] = field(default_factory=list)
    sas_ttl_hours: int = DEFAULT_SAS_TTL_HOURS
    sas_min_remaining: float = DEFAULT_SAS_MIN_REMAINING
    refresh_interval: float = DEFAULT_REFRESH_INTERVAL
    probe_interval: float = DEFAULT_PROBE_INTERVAL

    allow_other: bool = True
    read_only: bool = False
    cache_dir: str | None = None
    cache_size_mb: int | None = None
    attr_timeout: int = 7200
    file_cache_timeout: int = 120
    log_level: str = "log_warning"
    extra_args: list[str] = field(default_factory=list)

    def url(self) -> str:
        return container_url(self.account, self.container)

    def endpoint(self) -> str:
        return f"https://{self.account}.blob.core.windows.net/"

    def config_path(self) -> Path:
        return CONFIG_ROOT / f"{self.account}-{self.container}.yaml"

    def cache_path(self) -> Path:
        if self.cache_dir:
            return Path(self.cache_dir)
        return CACHE_ROOT / f"{self.account}-{self.container}"

    def route(self) -> str:
        return f"{self.url()} → {self.mount_dir}"


@dataclass
class MountState:
    """Supervisor-owned runtime. Never written by the CLI."""

    state: str = "stopped"  # stopped|mounted|remounting|failed
    supervisor_pid: int | None = None
    mounted_at: float | None = None
    blobfuse_pid: int | None = None
    health: str | None = None
    health_detail: str | None = None
    checked_at: float | None = None
    sas_expires_at: float | None = None
    last_refresh_at: float | None = None
    next_refresh_at: float | None = None
    refreshes: int = 0
    remounts: int = 0
    failures: int = 0
    last_error: str | None = None


# ==========================================================================
# Store
# ==========================================================================


def _def_path(ident: str) -> Path:
    return STATE_DIR / f"{ident}.json"


def _state_path(ident: str) -> Path:
    return STATE_DIR / f"{ident}.state.json"


def sas_path(ident: str) -> Path:
    return STATE_DIR / f"{ident}.sas"


def log_path(ident: str) -> Path:
    return STATE_DIR / f"{ident}.log"


def lock_path(ident: str) -> Path:
    return STATE_DIR / f"{ident}.lock"


def _from_dict(cls, raw: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})


def save_mount(mount: Mount) -> None:
    atomic_write(_def_path(mount.id), json.dumps(asdict(mount), indent=2))


def load_mount(ident: str) -> Mount:
    raw = read_json(_def_path(ident))
    if raw is None:
        raise KeyError(ident)
    return _from_dict(Mount, raw)


def list_mounts() -> list[Mount]:
    if not STATE_DIR.exists():
        return []
    out = []
    for path in sorted(STATE_DIR.glob("*.json")):
        if path.name.endswith(".state.json"):
            continue
        raw = read_json(path)
        if isinstance(raw, dict) and raw.get("id"):
            try:
                out.append(_from_dict(Mount, raw))
            except TypeError:
                continue
    return out


def save_state(ident: str, state: MountState) -> None:
    atomic_write(_state_path(ident), json.dumps(asdict(state), indent=2))


def load_state(ident: str) -> MountState:
    raw = read_json(_state_path(ident))
    if not isinstance(raw, dict):
        return MountState()
    try:
        return _from_dict(MountState, raw)
    except TypeError:
        return MountState()


def delete_mount(ident: str) -> None:
    for path in (
        _def_path(ident),
        _state_path(ident),
        sas_path(ident),
        log_path(ident),
        lock_path(ident),
    ):
        path.unlink(missing_ok=True)


def make_mount_id(mount_dir: Path, container: str, custom: str | None = None) -> str:
    if custom:
        return slugify(custom) or "mount"
    base = slugify(mount_dir.name) or slugify(container) or "mount"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    used = {p.stem for p in STATE_DIR.glob("*.json")}
    if base not in used:
        return base
    for i in range(2, 1000):
        candidate = f"{base}-{i}"
        if candidate not in used:
            return candidate
    return f"{base}-{int(time.time())}"  # pragma: no cover


def mount_provider(mount: Mount):
    if mount.auth == "fic":
        workload_identity_from_env()
        return build_provider("aad")
    return build_provider(
        mount.auth,
        spec=mount.sas_spec,
        headers=mount.sas_headers,
        url=mount.sas_spec if mount.auth == "inline" else None,
        account=mount.account,
        container=mount.container,
        ttl_hours=mount.sas_ttl_hours,
    )


def mount_sas_manager(mount: Mount) -> SasManager:
    return SasManager(
        mount_provider(mount),
        sas_path(mount.id),
        min_remaining=mount.sas_min_remaining,
    )


# ==========================================================================
# blobfuse2 config + mounting
# ==========================================================================


def render_config(mount: Mount, sas: str | None) -> str:
    """Render blobfuse2 YAML for SAS, Azure CLI or Workload Identity."""
    azstorage: dict = {
        "type": "block",
        "account-name": mount.account,
        "endpoint": mount.endpoint(),
        "container": mount.container,
    }
    if mount.auth == "fic":
        identity = workload_identity_from_env()
        azstorage.update(
            {
                "mode": "spn",
                "tenantid": identity.tenant_id,
                "clientid": identity.client_id,
                "oauth-token-path": identity.token_file,
            }
        )
    elif sas:
        azstorage["mode"] = "sas"
        azstorage["sas"] = sas.lstrip("?")
    else:
        # No SAS: blobfuse2 picks up the Azure CLI / MSI identity itself.
        azstorage["mode"] = "azcli"

    file_cache: dict = {
        "path": str(mount.cache_path()),
        "timeout-sec": mount.file_cache_timeout,
    }
    if mount.cache_size_mb:
        file_cache["max-size-mb"] = mount.cache_size_mb

    config = {
        "allow-other": mount.allow_other,
        "read-only": mount.read_only,
        "logging": {"type": "syslog", "level": mount.log_level},
        "components": ["libfuse", "file_cache", "attr_cache", "azstorage"],
        "libfuse": {
            "attribute-expiration-sec": 120,
            "entry-expiration-sec": 120,
            "negative-entry-expiration-sec": 240,
        },
        "file_cache": file_cache,
        "attr_cache": {"timeout-sec": mount.attr_timeout},
        "azstorage": azstorage,
    }
    return yaml.safe_dump(config, sort_keys=False, default_flow_style=False)


def write_config(mount: Mount, sas: str | None) -> Path:
    path = mount.config_path()
    # 0600: the SAS lives in here.
    atomic_write(path, render_config(mount, sas), mode=0o600)
    return path


def is_mountpoint(path: Path) -> bool:
    """True when *path* is a mount root (works for a dead FUSE mount too)."""
    try:
        return os.path.ismount(str(path))
    except OSError:
        # A stale FUSE mount raises ENOTCONN from stat; that still means the
        # kernel has something mounted there.
        return True


def probe_mount(mount: Mount, *, timeout: float = 10.0) -> tuple[str, str]:
    """Classify the mountpoint's health as ``(state, detail)``.

    Distinguishes "not mounted" from "mounted but broken", which is what an
    expired SAS looks like: the kernel mount is fine, I/O is not.
    """
    path = Path(mount.mount_dir)
    if not path.parent.exists():
        return MISSING, f"parent of {shorten_path(path)} does not exist"
    if not is_mountpoint(path):
        return (
            (UNMOUNTED, "not a mountpoint")
            if path.exists()
            else (
                MISSING,
                f"{path} does not exist",
            )
        )
    try:
        # Cheap read against the mount root; an expired SAS fails here.
        with _time_limit(timeout):
            os.listdir(str(path))
    except TimeoutError:
        return DENIED, f"listing timed out after {timeout:g}s"
    except OSError as exc:
        import errno

        if exc.errno == errno.ENOTCONN:
            return STALE, "transport endpoint is not connected"
        return DENIED, f"{exc.strerror or exc}"
    return OK, "readable"


class _time_limit:
    """SIGALRM-based timeout; a hung FUSE mount can block listdir forever."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._previous = None

    def __enter__(self):
        if self.seconds <= 0 or not hasattr(signal, "SIGALRM"):
            return self
        if threading.current_thread() is not threading.main_thread():
            return self  # signals only work on the main thread

        def _raise(_sig, _frame):
            raise TimeoutError("timed out")

        self._previous = signal.signal(signal.SIGALRM, _raise)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, *exc):
        if self._previous is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._previous)
        return False


def build_mount_argv(mount: Mount, binary: str, config: Path) -> list[str]:
    argv = [binary, "mount", str(mount.mount_dir), "--config-file", str(config)]
    if mount.allow_other:
        argv.append("--allow-other")
    if mount.read_only:
        argv.append("--read-only")
    argv += list(mount.extra_args)
    return argv


def do_mount(
    mount: Mount, sas: str | None, *, binary: str | None = None
) -> tuple[Path, str]:
    """Write the config and mount. Returns ``(config_path, stdout+stderr)``."""
    binary = binary or ensure_blobfuse2()
    parent = Path(mount.mount_dir).parent
    if not os.access(parent, os.W_OK):
        raise BlobmountError(f"{parent} is not writable; cannot create the mountpoint.")
    Path(mount.mount_dir).mkdir(parents=True, exist_ok=True)
    mount.cache_path().mkdir(parents=True, exist_ok=True)
    config = write_config(mount, sas)

    env = os.environ.copy()
    if mount.auth == "fic":
        env["AZCOPY_AUTO_LOGIN_TYPE"] = "WORKLOAD"
    else:
        env.setdefault("AZCOPY_AUTO_LOGIN_TYPE", "AZCLI")
    proc = run(build_mount_argv(mount, binary, config), env=env, timeout=MOUNT_TIMEOUT)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise BlobmountError(
            f"blobfuse2 mount failed: {redact(output) or proc.returncode}"
        )
    return config, redact(output)


def do_unmount(mount: Mount, *, binary: str | None = None, lazy: bool = False) -> bool:
    """Unmount, falling back to fusermount when blobfuse2 declines."""
    target = str(mount.mount_dir)
    binary = binary or find_blobfuse2()
    attempts: list[list[str]] = []
    if binary:
        attempts.append([binary, "unmount", target])
    if shutil.which("fusermount3"):
        attempts.append(["fusermount3", "-u"] + (["-z"] if lazy else []) + [target])
    elif shutil.which("fusermount"):
        attempts.append(["fusermount", "-u"] + (["-z"] if lazy else []) + [target])
    for argv in attempts:
        proc = run(argv, timeout=UNMOUNT_TIMEOUT)
        if proc.returncode == 0:
            return True
    return not is_mountpoint(Path(target))


# ==========================================================================
# Supervisor: keep the SAS fresh for as long as the mount lives
# ==========================================================================


class MountSupervisor:
    """Re-mint the SAS and remount before the credential lapses.

    blobfuse2 cannot be told about a new SAS in place, so a refresh is
    unmount + rewrite config + mount. That is disruptive, hence it only
    happens when the token is actually close to expiry (or the probe says
    the mount has already gone bad).
    """

    def __init__(
        self,
        mount: Mount,
        *,
        sas: SasManager | None = None,
        binary: str | None = None,
        clock=time.time,
        log=None,
    ) -> None:
        self.mount = mount
        self.sas = sas if sas is not None else mount_sas_manager(mount)
        self.binary = binary
        self.clock = clock
        self.state = MountState()
        self._log = log or self._default_log
        self._stop = threading.Event()
        # Set by both stop and refresh so a sleeping loop reacts at once
        # instead of waiting out its current slice.
        self._wake = threading.Event()
        self._forced = False

    def _default_log(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"{stamp} {redact(message)}", flush=True)

    def log(self, message: str) -> None:
        self._log(message)

    def _persist(self) -> None:
        token = self.sas.current() if self.sas.enabled else None
        self.state.sas_expires_at = token.expires_at if token else None
        save_state(self.mount.id, self.state)

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request_refresh(self) -> None:
        """Ask for a rotation now (signal-safe: only sets flags)."""
        self._forced = True
        self._wake.set()

    # -- decisions ---------------------------------------------------------

    def needs_refresh(self, now: float) -> tuple[bool, str]:
        """Should the mount be re-established right now, and why."""
        if self._forced:
            return True, "manual"
        if self.state.health in (STALE, DENIED, UNMOUNTED, MISSING):
            return True, f"unhealthy ({self.state.health})"
        if not self.sas.enabled:
            return False, ""
        token = self.sas.current()
        if token is None:
            return True, "no cached SAS"
        remaining = token.remaining(now)
        if remaining is None:
            return False, ""
        if remaining <= self.mount.sas_min_remaining:
            return True, f"SAS expires in {human_duration(remaining)}"
        return False, ""

    def next_deadline(self, now: float) -> float:
        """When to wake: the earlier of the probe tick and the SAS deadline."""
        deadlines = [now + self.mount.probe_interval]
        if self.sas.enabled:
            token = self.sas.current()
            if token is not None and token.expires_at is not None:
                deadlines.append(token.expires_at - self.mount.sas_min_remaining)
        deadlines.append(now + self.mount.refresh_interval)
        return max(now + 1.0, min(deadlines))

    # -- actions -----------------------------------------------------------

    def check_health(self) -> str:
        health, detail = probe_mount(self.mount)
        self.state.health = health
        self.state.health_detail = detail
        self.state.checked_at = self.clock()
        return health

    def remount(self, reason: str) -> bool:
        """Unmount (if needed), mint a fresh SAS, mount again."""
        mount = self.mount
        now = self.clock()
        self.state.state = "remounting"
        self._persist()
        self.log(f"remount ({reason})")
        try:
            token = self.sas.ensure(
                now, need=self.sas.needed_lifetime(mount.refresh_interval), force=True
            )
        except SasError as exc:
            self.state.failures += 1
            self.state.last_error = str(exc)
            self.state.state = "failed"
            self.log(f"SAS refresh failed: {exc}")
            self._persist()
            return False

        if is_mountpoint(Path(mount.mount_dir)):
            if not do_unmount(mount, binary=self.binary, lazy=True):
                self.log("warning: unmount reported failure; mounting anyway")
        try:
            do_mount(mount, token.token or None, binary=self.binary)
        except BlobmountError as exc:
            self.state.failures += 1
            self.state.last_error = str(exc)
            self.state.state = "failed"
            self.log(str(exc))
            self._persist()
            return False

        self.state.state = "mounted"
        self.state.mounted_at = now
        self.state.remounts += 1
        self.state.refreshes += 1
        self.state.last_refresh_at = now
        self.state.last_error = None
        self.check_health()
        self._forced = False
        self.log(f"mounted {mount.mount_dir} (health: {self.state.health})")
        self._persist()
        return True

    def tick(self) -> str | None:
        """One supervision round. Returns the reason it acted, if it did."""
        now = self.clock()
        self.check_health()
        needed, reason = self.needs_refresh(now)
        self.state.next_refresh_at = self.next_deadline(now)
        if needed:
            self.remount(reason)
            return reason
        if self.state.state != "mounted" and self.state.health == OK:
            self.state.state = "mounted"
        self._persist()
        return None

    def run(self) -> int:
        mount = self.mount
        self.state.supervisor_pid = os.getpid()
        self.state.state = "starting"
        self._persist()
        self.log(f"supervising {mount.route()} [auth={mount.auth}]")

        if self.check_health() != OK:
            if not self.remount("initial mount"):
                self.log("initial mount failed")
                self.state.state = "failed"
                self.state.supervisor_pid = None
                self._persist()
                return 1
        else:
            self.state.state = "mounted"
            self.state.mounted_at = self.clock()
            self._persist()

        try:
            while not self._stop.is_set():
                sleep_until(self.next_deadline(self.clock()), self._wake)
                self._wake.clear()
                if self._stop.is_set():
                    break
                self.tick()
        finally:
            self.state.state = "stopped"
            self.state.supervisor_pid = None
            self._persist()
            self.log("supervisor stopped")
        return 0


# ==========================================================================
# Daemon lifecycle
# ==========================================================================


def spawn_supervisor(mount: Mount) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(log_path(mount.id), "ab", buffering=0)
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "env": {**os.environ, SUPERVISE_ENV: mount.id},
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # pragma: no cover - windows
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    argv = [sys.executable, str(Path(__file__).resolve()), "up", mount.id]
    return subprocess.Popen(argv, **kwargs).pid


def supervisor_running(ident: str) -> bool:
    if SERVICE.enabled_kind(ident):
        return SERVICE.is_active(ident)
    return pid_alive(load_state(ident).supervisor_pid)


def stop_supervisor(ident: str, *, timeout: float = 20.0) -> bool:
    if SERVICE.enabled_kind(ident):
        proc = SERVICE.stop(ident)
        return proc is not None and proc.returncode == 0
    pid = load_state(ident).supervisor_pid
    if not pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    try:  # pragma: no cover - only if it ignores SIGTERM
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass
    return True


def poke_supervisor(ident: str) -> bool:
    pid = load_state(ident).supervisor_pid
    if pid_alive(pid) and hasattr(signal, "SIGUSR1"):
        try:
            os.kill(pid, signal.SIGUSR1)
            return True
        except OSError:
            return False
    return False


def run_supervisor(ident: str) -> int:
    mount = load_mount(ident)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = FileLock(lock_path(ident))
    if not lock.acquire():
        console.print(f"[red]✗[/red] {ident} is already supervised.")
        return 1
    supervisor = MountSupervisor(mount)
    if os.name == "posix":
        signal.signal(signal.SIGTERM, lambda *_: supervisor.request_stop())
        signal.signal(signal.SIGINT, lambda *_: supervisor.request_stop())
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, lambda *_: supervisor.request_refresh())
    try:
        return supervisor.run()
    finally:
        lock.release()


# ==========================================================================
# CLI
# ==========================================================================


def _require(ident: str) -> Mount:
    try:
        return load_mount(ident)
    except KeyError:
        known = ", ".join(m.id for m in list_mounts()) or "none"
        raise BlobmountError(f"unknown mount {ident!r}. Defined: {known}") from None


HEALTH_STYLE = {
    OK: "[green]ok[/green]",
    STALE: "[red]stale[/red]",
    DENIED: "[red]denied[/red]",
    UNMOUNTED: "[yellow]unmounted[/yellow]",
    MISSING: "[dim]missing[/dim]",
}


def _health_label(health: str | None) -> str:
    return HEALTH_STYLE.get(health or "", "[dim]-[/dim]")


def _sas_label(mount: Mount, *, compact: bool = False) -> str:
    """Time left on the credential, coloured by urgency."""
    if mount.auth in {"aad", "fic"}:
        return "[dim]workload[/dim]" if mount.auth == "fic" else "[dim]entra[/dim]"
    expires = load_state(mount.id).sas_expires_at
    if not expires:
        return "[dim]-[/dim]"
    remaining = expires - time.time()
    if remaining <= 0:
        return "[red]expired[/red]"
    colour = "yellow" if remaining < mount.sas_min_remaining else "green"
    render = compact_duration if compact else human_duration
    return f"[{colour}]{render(remaining)}[/{colour}]"


def _auth_options(fn):
    for decorator in reversed(
        [
            click.option("--name", help="Explicit id instead of a derived slug."),
            click.option(
                "--auth",
                type=click.Choice(BLOBMOUNT_AUTH_KINDS),
                default=None,
                help="Credential source. 'fic' uses Azure Workload Identity, "
                "'aad' uses your Azure CLI login, 'az' mints a rotating SAS; "
                "the rest read a SAS from an external provider.",
            ),
            click.option("--sas-env", help="[--auth env] environment variable name."),
            click.option(
                "--sas-file",
                type=click.Path(),
                help="[--auth file] file re-read on every refresh.",
            ),
            click.option("--sas-command", help="[--auth exec] command printing a SAS."),
            click.option("--sas-url", help="[--auth http] endpoint returning a SAS."),
            click.option(
                "--sas-header",
                multiple=True,
                help="[--auth http] extra request header 'Key: Value'.",
            ),
            click.option(
                "--sas-ttl-hours",
                type=int,
                default=DEFAULT_SAS_TTL_HOURS,
                show_default=True,
                help="[--auth az] lifetime of each minted SAS.",
            ),
            click.option(
                "--sas-min-remaining",
                type=float,
                default=DEFAULT_SAS_MIN_REMAINING,
                show_default=True,
                help="Remount to rotate the SAS below this many seconds left.",
            ),
            click.option(
                "--refresh-interval",
                type=float,
                default=DEFAULT_REFRESH_INTERVAL,
                show_default=True,
                help="Upper bound between credential refreshes.",
            ),
            click.option(
                "--probe-interval",
                type=float,
                default=DEFAULT_PROBE_INTERVAL,
                show_default=True,
                help="How often to check that the mount still reads.",
            ),
            click.option("--cache-dir", type=click.Path(), help="file_cache location."),
            click.option("--cache-size-mb", type=int, help="Cap the file cache."),
            click.option(
                "--read-only", is_flag=True, help="Mount the container read-only."
            ),
            click.option(
                "--no-allow-other",
                is_flag=True,
                help="Don't pass --allow-other (skips the /etc/fuse.conf need).",
            ),
            click.option(
                "--log-level",
                default="log_warning",
                show_default=True,
                type=click.Choice(
                    ["log_off", "log_err", "log_warning", "log_info", "log_debug"]
                ),
            ),
        ]
    ):
        fn = decorator(fn)
    return fn


def _mount_from_options(
    mount_dir: Path, account: str, container: str, opts: dict
) -> Mount:
    auth = opts.get("auth")
    provided = [
        ("env", opts.get("sas_env")),
        ("file", opts.get("sas_file")),
        ("exec", opts.get("sas_command")),
        ("http", opts.get("sas_url")),
    ]
    given = [(kind, value) for kind, value in provided if value]
    if len(given) > 1:
        raise BlobmountError(
            "pick one SAS source: " + ", ".join(AUTH_SPEC_FLAG[k] for k, _ in given)
        )
    spec = None
    if given:
        inferred, spec = given[0]
        if auth and auth != inferred:
            raise BlobmountError(
                f"--auth {auth} conflicts with the --sas-* flag you passed."
            )
        auth = inferred
    if auth is None:
        auth = "az"
    if auth in AUTH_SPEC_FLAG and not spec:
        raise BlobmountError(f"--auth {auth} needs {AUTH_SPEC_FLAG[auth]}.")

    return Mount(
        id="",
        mount_dir=str(mount_dir),
        account=account,
        container=container,
        auth=auth,
        sas_spec=spec,
        sas_headers=list(opts.get("sas_header") or []),
        sas_ttl_hours=opts["sas_ttl_hours"],
        sas_min_remaining=opts["sas_min_remaining"],
        refresh_interval=opts["refresh_interval"],
        probe_interval=opts["probe_interval"],
        cache_dir=opts.get("cache_dir"),
        cache_size_mb=opts.get("cache_size_mb"),
        read_only=opts["read_only"],
        allow_other=not opts["no_allow_other"],
        log_level=opts["log_level"],
    )


def parse_target(account: str | None, container: str | None) -> tuple[str, str]:
    """Accept ``<account> <container>`` or a single blob URL."""
    if container is None and account and is_https_blob(account):
        base, _ = split_sas(account)
        return parse_blob_url(base)
    if not account or not container:
        raise BlobmountError(
            "give either '<account> <container>' or a container URL "
            "(https://<account>.blob.core.windows.net/<container>)."
        )
    return account, container


BLOBMOUNT_SECTIONS = (
    ("Mount", ("mount", "umount", "refresh", "check")),
    ("Inspect", ("ls", "status", "config", "logs")),
    ("Lifecycle", ("start", "stop", "rm")),
    ("Boot", ("enable", "disable")),
    ("Setup", ("install",)),
    ("Internal", ("up",)),
)
BlobmountGroup = grouped_class(BLOBMOUNT_SECTIONS, name="BlobmountGroup")


@click.group(
    cls=BlobmountGroup,
    help="Mount Azure Blob containers with blobfuse2, keeping the SAS fresh.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def cli() -> None:
    pass


@cli.command("mount", short_help="Mount a container and keep it alive.")
@click.argument("mount_dir", type=click.Path())
@click.argument("account")
@click.argument("container", required=False)
@click.option("--no-supervise", is_flag=True, help="Mount once, don't keep refreshing.")
@click.option("--force", is_flag=True, help="Remount even if already mounted.")
@_auth_options
def cmd_mount(mount_dir, account, container, no_supervise, force, **opts):
    account, container = parse_target(account, container)
    target = Path(mount_dir).expanduser()
    mount = _mount_from_options(target, account, container, opts)
    mount.id = make_mount_id(target, container, opts.get("name"))

    existing = next((m for m in list_mounts() if m.mount_dir == str(target)), None)
    if existing is not None and not force:
        raise BlobmountError(
            f"{target} is already managed as {existing.id!r}. "
            f"Use `usm blobmount refresh {existing.id}` or pass --force."
        )
    try:
        mount_provider(mount)
    except SasError as exc:
        raise BlobmountError(str(exc)) from exc

    if is_mountpoint(target) and not force:
        raise BlobmountError(
            f"{target} is already a mountpoint. Unmount it first or pass --force."
        )
    save_mount(mount)
    save_state(mount.id, MountState())

    if no_supervise:
        manager = mount_sas_manager(mount)
        try:
            token = manager.ensure(time.time(), need=mount.refresh_interval)
        except SasError as exc:
            raise BlobmountError(str(exc)) from exc
        if is_mountpoint(target):
            do_unmount(mount, lazy=True)
        do_mount(mount, token.token or None)
        console.print(f"[green]✓[/green] {mount.id}: mounted {mount.route()}")
        console.print(
            "[dim]No supervisor: the SAS will not be refreshed. Run "
            f"`usm blobmount start {mount.id}` to keep it fresh.[/dim]"
        )
        return
    _start(mount)


def _start(mount: Mount) -> None:
    if supervisor_running(mount.id):
        raise BlobmountError(f"{mount.id} is already supervised.")
    if SERVICE.enabled_kind(mount.id):
        proc = SERVICE.start(mount.id)
        if proc is not None and proc.returncode != 0:
            raise BlobmountError(proc.stderr.strip() or "service start failed")
        console.print(f"[green]✓[/green] Started {mount.id} (service).")
        return
    pid = spawn_supervisor(mount)
    time.sleep(STARTUP_GRACE_SECS)
    state = load_state(mount.id)
    if not pid_alive(state.supervisor_pid or pid) and state.state != "mounted":
        raise BlobmountError(
            f"{mount.id} exited during startup.\n"
            + "\n".join(_tail(log_path(mount.id), 15))
        )
    console.print(
        f"[green]✓[/green] {mount.id}: {mount.route()} "
        f"[dim](health: {state.health or 'checking'})[/dim]"
    )


def _tail(path: Path, n: int) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


@cli.command("ls", short_help="List mounts (managed and unmanaged).")
@click.option(
    "--all", "show_all", is_flag=True, help="Include unmanaged blobfuse2 mounts."
)
def cmd_ls(show_all):
    mounts = list_mounts()
    live = blobfuse_mounts()
    width = console.width or 100
    # Narrow terminals lose the least useful columns first; `status` has it all.
    show_boot = width >= 90
    show_sas = width >= 80

    columns = [
        ("ID", {"min_width": 6}),
        ("mount", {"min_width": 12, "ratio": 1}),
        ("container", {"min_width": 12, "ratio": 1}),
        ("supervisor", {"min_width": 10}),
        ("health", {"min_width": 9}),
    ]
    if show_sas:
        columns.append(("SAS", {"justify": "right", "min_width": 4}))
    if show_boot:
        columns.append(("boot", {"justify": "center", "min_width": 4}))
    table = new_table(*columns)

    managed_dirs = set()
    for mount in mounts:
        managed_dirs.add(mount.mount_dir)
        state = load_state(mount.id)
        health, _ = probe_mount(mount)
        if health != state.health:
            state.health = health
            save_state(mount.id, state)
        row = [
            mount.id,
            shorten_path(mount.mount_dir),
            f"{mount.account}/{mount.container}",
            "[green]running[/green]"
            if supervisor_running(mount.id)
            else "[dim]stopped[/dim]",
            _health_label(health),
        ]
        if show_sas:
            row.append(_sas_label(mount, compact=True))
        if show_boot:
            row.append(
                "[cyan]✓[/cyan]" if SERVICE.enabled_kind(mount.id) else "[dim]-[/dim]"
            )
        table.add_row(*row)

    external = 0
    if show_all:
        for mount_dir, info in sorted(live.items()):
            if mount_dir in managed_dirs:
                continue
            external += 1
            row = [
                "[dim]-[/dim]",
                shorten_path(mount_dir),
                f"{info['account_name']}/{info['container_name']}",
                "[dim]external[/dim]",
                _health_label(OK if is_mountpoint(Path(mount_dir)) else UNMOUNTED),
            ]
            if show_sas:
                row.append("[dim]-[/dim]")
            if show_boot:
                row.append("[dim]-[/dim]")
            table.add_row(*row)

    if not mounts and not external:
        hint = "" if show_all else " (use --all to include unmanaged ones)"
        console.print(
            f"[dim]No mounts{hint}. Create one with "
            "`usm blobmount mount <dir> <account> <container>`.[/dim]"
        )
        return
    console.print(table)
    unmanaged = set(live) - managed_dirs
    if unmanaged and not show_all:
        console.print(
            f"[dim]{len(unmanaged)} other blobfuse2 mount(s) are not managed "
            "here; `usm blobmount ls --all` shows them.[/dim]"
        )


@cli.command("status", short_help="Show one mount in detail.")
@click.argument("ident")
def cmd_status(ident):
    mount = _require(ident)
    state = load_state(ident)
    health, detail = probe_mount(mount)
    now = time.time()

    rows = [
        ("mount dir", shorten_path(mount.mount_dir)),
        ("container", f"{mount.account}/{mount.container}"),
        SECTION,
        ("health", f"{_health_label(health)}  [dim]{detail}[/dim]"),
        ("supervisor", "running" if supervisor_running(ident) else "stopped"),
        ("state", state.state),
        (
            "mounted for",
            human_duration(now - state.mounted_at) if state.mounted_at else "-",
        ),
        SECTION,
        (
            "credential",
            mount.auth
            + (f" ({mount.sas_spec})" if mount.sas_spec else "")
            + (
                f" · expires in {_sas_label(mount)}"
                if mount.auth not in {"aad", "fic"}
                else " · nothing to rotate"
            ),
        ),
        (
            "last refresh",
            f"{human_duration(now - state.last_refresh_at)} ago"
            if state.last_refresh_at
            else "never",
        ),
        (
            "next refresh",
            f"in {human_duration(state.next_refresh_at - now)}"
            if state.next_refresh_at and state.next_refresh_at > now
            else "-",
        ),
        (
            "counters",
            f"{state.refreshes} refreshes · {state.remounts} remounts · "
            f"{state.failures} failures",
        ),
        SECTION,
        ("read-only", "yes" if mount.read_only else "no"),
        ("config", shorten_path(mount.config_path())),
        ("cache", shorten_path(mount.cache_path())),
        ("boot", SERVICE.enabled_kind(ident) or "-"),
    ]
    console.print(kv_table(rows))
    if state.last_error:
        console.print(f"\n[red]last error[/red]  {redact(state.last_error)[:600]}")


@cli.command("check", short_help="Probe a mount's health; exit non-zero if bad.")
@click.argument("ident", required=False)
def cmd_check(ident):
    targets = [_require(ident)] if ident else list_mounts()
    if not targets:
        raise BlobmountError("no mounts defined.")
    bad = 0
    for mount in targets:
        health, detail = probe_mount(mount)
        marker = "[green]✓[/green]" if health == OK else "[red]✗[/red]"
        console.print(f"{marker} {mount.id}: {health} [dim]({detail})[/dim]")
        if health != OK:
            bad += 1
    if bad:
        raise SystemExit(1)


@cli.command("refresh", short_help="Re-mint the SAS and remount now.")
@click.argument("ident")
def cmd_refresh(ident):
    mount = _require(ident)
    if supervisor_running(ident):
        if poke_supervisor(ident):
            console.print(f"[green]✓[/green] Asked {ident} to refresh now.")
            return
        console.print(f"[yellow]![/yellow] {ident} did not acknowledge; doing it here.")
    lock = FileLock(lock_path(ident))
    if not lock.acquire():
        raise BlobmountError(f"{ident} is busy (another operation holds the lock).")
    try:
        supervisor = MountSupervisor(
            mount, log=lambda m: console.print(f"[dim]{m}[/dim]")
        )
        if not supervisor.remount("manual"):
            raise BlobmountError(
                redact(supervisor.state.last_error or "remount failed")
            )
    finally:
        lock.release()
    console.print(f"[green]✓[/green] {ident}: remounted with a fresh SAS.")


@cli.command("umount", short_help="Unmount and stop supervising.")
@click.argument("ident")
@click.option("--lazy", is_flag=True, help="Detach even if the mount is busy.")
def cmd_umount(ident, lazy):
    mount = _require(ident)
    stop_supervisor(ident)
    if not is_mountpoint(Path(mount.mount_dir)):
        console.print(f"[dim]{ident} was not mounted.[/dim]")
        return
    if do_unmount(mount, lazy=lazy):
        console.print(f"[green]✓[/green] Unmounted {mount.mount_dir}.")
    else:
        raise BlobmountError(
            f"could not unmount {mount.mount_dir}; something is using it "
            "(try --lazy, or `lsof +D <dir>`)."
        )


@cli.command("start", short_help="Start supervising an existing definition.")
@click.argument("ident")
def cmd_start(ident):
    _start(_require(ident))


@cli.command("stop", short_help="Stop the supervisor (leaves it mounted).")
@click.argument("ident")
def cmd_stop(ident):
    _require(ident)
    stopped = stop_supervisor(ident)
    console.print(
        f"[green]✓[/green] {ident}: "
        f"{'supervisor stopped' if stopped else 'was not supervised'}"
    )


@cli.command("rm", short_help="Unmount and forget a definition.")
@click.argument("ident")
@click.option("-y", "--yes", is_flag=True)
@click.option("--keep-mounted", is_flag=True, help="Leave the filesystem mounted.")
def cmd_rm(ident, yes, keep_mounted):
    mount = _require(ident)
    if not yes and not click.confirm(f"Remove {ident} ({mount.mount_dir})?"):
        return
    if SERVICE.enabled_kind(ident):
        SERVICE.disable(ident)
    stop_supervisor(ident)
    if not keep_mounted and is_mountpoint(Path(mount.mount_dir)):
        do_unmount(mount, lazy=True)
    delete_mount(ident)
    console.print(f"[green]✓[/green] Removed {ident}.")


@cli.command("enable", short_help="Mount at boot and keep refreshing.")
@click.argument("ident")
def cmd_enable(ident):
    mount = _require(ident)
    binary = usm_bin()
    try:
        kind = SERVICE.enable(
            ident,
            [binary, "blobmount", "up", ident],
            description=f"usm blobmount {ident}: {mount.route()}",
            binary=binary,
            log_path=log_path(ident),
        )
    except RuntimeError as exc:
        raise BlobmountError(str(exc)) from exc
    if kind == "systemd":
        console.print(
            f"[green]✓[/green] {ident} mounts at boot (systemd user unit).\n"
            "[dim]Tip: `loginctl enable-linger $USER` keeps it mounted when "
            "you log out.[/dim]"
        )
    else:
        console.print(f"[green]✓[/green] {ident} mounts at login (launchd agent).")


@cli.command("disable", short_help="Don't mount at boot.")
@click.argument("ident")
def cmd_disable(ident):
    _require(ident)
    if not SERVICE.enabled_kind(ident):
        console.print(f"[dim]{ident} was not enabled.[/dim]")
        return
    SERVICE.disable(ident)
    console.print(f"[green]✓[/green] {ident} no longer mounts at boot.")


@cli.command("logs", short_help="Tail the supervisor log.")
@click.argument("ident")
@click.option("-n", "--lines", type=int, default=40, show_default=True)
@click.option("-f", "--follow", is_flag=True)
def cmd_logs(ident, lines, follow):
    _require(ident)
    path = log_path(ident)
    if not path.exists():
        raise BlobmountError(f"no log at {path}")
    for line in _tail(path, lines):
        click.echo(redact(line))
    if not follow:
        return
    with open(path, errors="replace") as fh:
        fh.seek(0, os.SEEK_END)
        try:
            while True:
                line = fh.readline()
                if line:
                    click.echo(redact(line.rstrip()))
                else:
                    time.sleep(0.3)
        except KeyboardInterrupt:  # pragma: no cover
            pass


@cli.command("config", short_help="Show the rendered blobfuse2 config.")
@click.argument("ident")
def cmd_config(ident):
    mount = _require(ident)
    click.echo(redact(render_config(mount, "sv=REDACTED&sig=REDACTED")))


@cli.command("install", short_help="Install blobfuse2.")
def cmd_install():
    path = install_blobfuse2()
    proc = run([path, "--version"])
    console.print(
        f"[green]✓[/green] blobfuse2 at {path} [dim]{(proc.stdout or '').strip()}[/dim]"
    )


@cli.command("up", short_help="Run the supervisor in the foreground.")
@click.argument("ident")
def cmd_up(ident):
    _require(ident)
    raise SystemExit(run_supervisor(ident))


def main() -> None:
    supervise_id = os.environ.get(SUPERVISE_ENV)
    if supervise_id and len(sys.argv) == 1:  # pragma: no cover - spawn path
        raise SystemExit(run_supervisor(supervise_id))
    cli()


if __name__ == "__main__":
    main()
