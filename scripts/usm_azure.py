#!/usr/bin/env python3
"""Shared Azure plumbing for the usm scripts that talk to Blob Storage.

Imported by ``azsync.py`` and ``blobmount.py`` (declared as a ``modules``
entry in ``_config.json``, so it is fetched into the same directory and
resolves via Python's ``sys.path[0]``).

What lives here is the part both commands need and neither owns:

* **SAS lifecycle** — seven credential sources behind one protocol, with
  expiry taken from the token itself, an ``0600`` cache, and a freshness
  check. Neither azcopy nor blobfuse2 can rotate a credential that is
  already in flight, so both commands refresh *before* they need one.
* **Blob URL handling** — parsing, SAS splicing, redaction, and resolving a
  path inside a live blobfuse2 mount back to its ``https://`` URL.
* **Process plumbing** — liveness that isn't fooled by zombies, an advisory
  lock, atomic writes.
* **Service management** — systemd user units and launchd agents, so both
  commands get identical ``enable``/``disable``/``start`` semantics.

Command-specific machinery (azsync's trigger policy and azcopy engine,
blobmount's blobfuse2 handling) deliberately stays in the command.
"""

from __future__ import annotations

import fnmatch
import json
import os
import platform
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

USM_CACHE_DIR = Path.home() / ".cache" / "usm"
LOCAL_BIN_DIR = USM_CACHE_DIR / "bin"

SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
LAUNCHD_USER_DIR = Path.home() / "Library" / "LaunchAgents"

DEFAULT_SAS_TTL_HOURS = 168  # 7 days
DEFAULT_SAS_MIN_REMAINING = 1800.0  # refresh below 30 minutes remaining
SAS_PERMISSIONS = "racwdl"


# ==========================================================================
# Formatting, redaction, small IO
# ==========================================================================

_SIG_RE = re.compile(r"(sig=)[^&\s\"']+", re.I)


def redact(text: str) -> str:
    """Blank out the signature of any SAS token in *text*.

    Everything printed or logged goes through here; the token itself only
    ever lives in an ``0600`` file.
    """
    return _SIG_RE.sub(r"\1***", text or "")


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value or "").strip("-").lower()


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TiB"


def human_duration(secs: float | None) -> str:
    if secs is None:
        return "-"
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600:02d}h"


def compact_duration(secs: float | None) -> str:
    """One unit, at most four characters — for table columns.

    ``human_duration`` is precise but variable width, which is what makes a
    Rich column wrap to three lines. This never exceeds ``999d``.
    """
    if secs is None:
        return "-"
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def shorten_path(path) -> str:
    """Replace the home prefix with ``~``.

    Worth doing everywhere: a home directory like
    ``/home/first.last@example.com`` otherwise eats half an 80-column table.
    """
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


def short_blob_target(url: str) -> str:
    """``https://acct.blob.core.windows.net/c/a/b?sas`` → ``acct/c/a/b``.

    The scheme and endpoint are identical for every row, so showing them just
    pushes the part that differs off the edge.
    """
    base, _ = split_sas(url or "")
    parts = urllib.parse.urlsplit(base)
    if not parts.netloc:
        return base
    account = parts.netloc.split(":")[0].split(".")[0]
    path = parts.path.strip("/")
    return f"{account}/{path}" if path else account


def elide(text: str, width: int, *, keep: str = "tail") -> str:
    """Trim *text* to *width*, keeping whichever end carries the meaning.

    Paths and blob targets are distinguished by their tail, so that is what
    survives by default.
    """
    text = str(text)
    if width <= 0 or len(text) <= width:
        return text
    if width == 1:
        return "…"
    if keep == "tail":
        return "…" + text[-(width - 1) :]
    return text[: width - 1] + "…"


# -- tables ----------------------------------------------------------------


def new_table(*columns, **kwargs):
    """A table that stays inside the terminal instead of wrapping.

    Every column defaults to ``no_wrap`` with ellipsis overflow: one row is
    one line, always. Pass ``(header, {...})`` to override per column.
    """
    from rich import box
    from rich.table import Table

    kwargs.setdefault("box", box.SIMPLE_HEAD)
    kwargs.setdefault("header_style", "bold")
    kwargs.setdefault("pad_edge", False)
    kwargs.setdefault("expand", False)
    table = Table(**kwargs)
    for column in columns:
        header, opts = column if isinstance(column, tuple) else (column, {})
        opts = dict(opts)
        opts.setdefault("no_wrap", True)
        opts.setdefault("overflow", "ellipsis")
        table.add_column(header, **opts)
    return table


SECTION = object()  # marker: insert a blank line between kv_table groups


def kv_table(rows, **kwargs):
    """Key/value detail view. Use ``SECTION`` in *rows* to group.

    Values wrap rather than truncate here — unlike a list, a detail view has
    room, and silently cutting a path off is worse than a second line.
    """
    from rich.table import Table

    kwargs.setdefault("box", None)
    kwargs.setdefault("show_header", False)
    kwargs.setdefault("pad_edge", False)
    table = Table(**kwargs)
    table.add_column(style="dim", no_wrap=True)
    table.add_column(overflow="fold")
    for row in rows:
        if row is SECTION:
            table.add_row("", "")
            continue
        key, value = row
        table.add_row(key, "" if value is None else str(value))
    return table


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


# ==========================================================================
# Blob URL handling
# ==========================================================================


def is_https_blob(value: str) -> bool:
    if not value or not value.lower().startswith(("http://", "https://")):
        return False
    host = urllib.parse.urlparse(value).netloc.lower()
    return ".blob.core." in host or ".dfs.core." in host or ".file.core." in host


def has_sas(url: str) -> bool:
    return "sig=" in urllib.parse.urlparse(url).query.lower()


def parse_blob_url(url: str) -> tuple[str, str]:
    """Pull ``(account, container)`` out of a blob URL."""
    parsed = urllib.parse.urlparse(url)
    account = parsed.netloc.split(":")[0].split(".")[0]
    container = parsed.path.lstrip("/").split("/", 1)[0]
    if not account or not container:
        raise ValueError(f"cannot parse account/container from blob URL: {url}")
    return account, container


def split_sas(url: str) -> tuple[str, str | None]:
    """Return ``(url_without_sas, sas_token_or_None)``."""
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url, None
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(k.lower() == "sig" for k, _ in pairs):
        return url, None
    return urllib.parse.urlunsplit(parsed._replace(query="")), parsed.query


def join_sas(url: str, sas: str | None) -> str:
    if not sas:
        return url
    sas = sas.lstrip("?")
    sep = "&" if urllib.parse.urlsplit(url).query else "?"
    return f"{url}{sep}{sas}"


def container_url(account: str, container: str) -> str:
    return f"https://{account}.blob.core.windows.net/{container}"


def blobfuse_mounts() -> dict:
    """Discover live blobfuse2 mounts by reading each process's config file.

    Returns ``{mount_dir: {url, account_name, container_name, config_file,
    pid}}``. Used by ``usm cp``/``azsync`` to translate a local path back to
    its blob URL, and by ``usm blobmount ls`` to show what is mounted.
    """
    try:
        import psutil
        import yaml
    except ImportError:
        return {}

    mounts: dict = {}
    for proc in psutil.process_iter(["cmdline", "pid"]):
        try:
            cmdline = proc.info["cmdline"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not cmdline or "blobfuse2" not in cmdline[0] or len(cmdline) < 3:
            continue
        if cmdline[1] != "mount":
            continue
        mount_dir = cmdline[2]
        config_file = None
        for i, token in enumerate(cmdline):
            if token in ("--config-file", "-c") and i + 1 < len(cmdline):
                config_file = cmdline[i + 1]
            elif token.startswith("--config-file="):
                config_file = token.split("=", 1)[1]
        if not config_file:
            continue
        try:
            with open(config_file) as fh:
                config = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            continue
        storage = (config.get("azstorage") or {}) if isinstance(config, dict) else {}
        account = storage.get("account-name")
        container = storage.get("container")
        if not account or not container:
            continue
        mounts[mount_dir] = {
            "url": container_url(account, container) + "/",
            "account_name": account,
            "container_name": container,
            "config_file": config_file,
            "pid": proc.info.get("pid"),
        }
    return mounts


def resolve_blob_path(value: str, mounts: dict | None = None) -> str | None:
    """Translate an https blob URL or a path inside a mount into a blob URL."""
    if is_https_blob(value):
        return value
    mounts = blobfuse_mounts() if mounts is None else mounts
    try:
        resolved = str(Path(value).resolve())
    except OSError:  # pragma: no cover - unresolvable path
        return None
    for mount, info in mounts.items():
        base = mount.rstrip("/")
        if resolved == base or resolved.startswith(base + "/"):
            rel = resolved[len(base) :].lstrip("/")
            return info["url"] + urllib.parse.quote(rel, safe="/")
    return None


# ==========================================================================
# SAS lifecycle
#
# Neither azcopy nor blobfuse2 can swap a credential that is already in use:
# azcopy freezes it into the job plan, blobfuse2 reads it once at mount. So
# refreshing always means "get a good one *before* you need it", and both
# commands drive that through SasManager.
# ==========================================================================


class SasError(Exception):
    """The SAS could not be obtained or is unusable."""


@dataclass(frozen=True)
class SasToken:
    token: str  # query-string form, no leading '?'
    expires_at: float | None = None
    source: str = ""

    def remaining(self, now: float) -> float | None:
        return None if self.expires_at is None else self.expires_at - now

    def redacted(self) -> str:
        return redact(self.token)


def parse_iso8601(value: str) -> float | None:
    import datetime

    text = (value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for candidate in (text, text + "+00:00"):
        try:
            parsed = datetime.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.timestamp()
    return None


def parse_sas_expiry(token: str) -> float | None:
    """Read the authoritative expiry (``se=``) out of a SAS query string."""
    if not token:
        return None
    pairs = urllib.parse.parse_qsl(token.lstrip("?"), keep_blank_values=True)
    raw = next((v for k, v in pairs if k.lower() == "se"), None)
    return parse_iso8601(raw) if raw else None


def normalize_sas(raw: str) -> tuple[str, float | None]:
    """Accept a bare SAS, a full URL, or JSON; return ``(token, claimed_exp)``.

    External providers are inconsistent, so every shape is handled once here
    instead of in each provider.
    """
    text = (raw or "").strip()
    if not text:
        raise SasError("SAS provider returned nothing.")
    claimed: float | None = None
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SasError(f"SAS provider returned invalid JSON: {exc}") from exc
        for key in ("sas", "sas_token", "sasToken", "token", "url"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        else:
            raise SasError("SAS JSON has no 'sas'/'token'/'url' field.")
        for key in ("expires_at", "expiresAt", "expiry", "expires_on"):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                claimed = float(value)
                break
            if isinstance(value, str):
                claimed = parse_iso8601(value)
                if claimed:
                    break
    if "://" in text:  # a full URL was handed to us; keep only the query
        _, token = split_sas(text)
        if not token:
            raise SasError("SAS provider returned a URL without a 'sig=' token.")
        text = token
    text = text.lstrip("?").strip()
    if "sig=" not in text.lower():
        raise SasError("SAS provider returned a string without 'sig='.")
    return text, claimed


class SasProvider:
    """Obtain a SAS token. ``fetch`` is the only per-kind behaviour."""

    kind = "none"
    refreshable = True

    def fetch(self) -> tuple[str, float | None]:  # pragma: no cover - abstract
        raise NotImplementedError

    def resolve(self, now: float) -> SasToken:
        raw, claimed = self.fetch()
        token, from_payload = normalize_sas(raw)
        claimed = claimed if claimed is not None else from_payload
        embedded = parse_sas_expiry(token)
        # The token's own `se=` outranks any provider claim; when both exist,
        # trust whichever expires first, so a lying provider cannot cause a
        # mid-flight failure.
        candidates = [t for t in (claimed, embedded) if t is not None]
        expires_at = min(candidates) if candidates else None
        if expires_at is not None and expires_at <= now:
            raise SasError(
                f"{self.kind} provider returned a SAS that expired "
                f"{human_duration(now - expires_at)} ago."
            )
        return SasToken(token, expires_at, self.kind)


class AadProvider(SasProvider):
    """No SAS at all — the tool authenticates with Entra ID and self-refreshes."""

    kind = "aad"

    def resolve(self, now: float) -> SasToken:
        return SasToken("", None, self.kind)


class InlineProvider(SasProvider):
    """The SAS was pasted in by the user. Cannot be rotated."""

    kind = "inline"
    refreshable = False

    def __init__(self, token: str) -> None:
        self.token = token

    def fetch(self) -> tuple[str, float | None]:
        return self.token, None


class EnvProvider(SasProvider):
    kind = "env"

    def __init__(self, var: str) -> None:
        self.var = var

    def fetch(self) -> tuple[str, float | None]:
        value = os.environ.get(self.var)
        if not value:
            raise SasError(f"environment variable ${self.var} is empty or unset.")
        return value, None


class FileProvider(SasProvider):
    """Re-read on every refresh so an external rotator just works."""

    kind = "file"

    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()

    def fetch(self) -> tuple[str, float | None]:
        try:
            return self.path.read_text(), None
        except OSError as exc:
            raise SasError(f"cannot read SAS file {self.path}: {exc}") from exc


class ExecProvider(SasProvider):
    kind = "exec"

    def __init__(self, command: str, timeout: float = 60.0) -> None:
        self.command = command
        self.timeout = timeout

    def fetch(self) -> tuple[str, float | None]:
        try:
            proc = subprocess.run(
                self.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SasError(f"SAS command failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise SasError(
                f"SAS command exited {proc.returncode}: "
                f"{redact(detail[-1]) if detail else 'no output'}"
            )
        return proc.stdout, None


class HttpProvider(SasProvider):
    kind = "http"

    def __init__(
        self, url: str, headers: Iterable[str] = (), timeout: float = 30.0
    ) -> None:
        self.url = url
        self.headers = list(headers)
        self.timeout = timeout

    def fetch(self) -> tuple[str, float | None]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(self.url)
        for header in self.headers:
            if ":" not in header:
                raise SasError(f"invalid SAS header {header!r}; expected 'K: V'.")
            key, value = header.split(":", 1)
            request.add_header(key.strip(), value.strip())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace"), None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SasError(f"SAS endpoint {self.url} failed: {exc}") from exc


class AzCliProvider(SasProvider):
    """Mint a user-delegation SAS with the Azure CLI."""

    kind = "az"

    def __init__(
        self,
        account: str,
        container: str,
        ttl_hours: int = DEFAULT_SAS_TTL_HOURS,
        permissions: str = SAS_PERMISSIONS,
    ) -> None:
        self.account = account
        self.container = container
        self.ttl_hours = ttl_hours
        self.permissions = permissions

    def fetch(self) -> tuple[str, float | None]:
        import datetime

        expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=self.ttl_hours
        )
        argv = [
            "az",
            "storage",
            "container",
            "generate-sas",
            "--account-name",
            self.account,
            "--name",
            self.container,
            "--permissions",
            self.permissions,
            "--expiry",
            expiry.strftime("%Y-%m-%dT%H:%MZ"),
            "--auth-mode",
            "login",
            "--as-user",
            "--output",
            "tsv",
        ]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        except FileNotFoundError as exc:
            raise SasError(
                "the 'az' CLI is not on PATH; install it or pick another auth mode."
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise SasError(f"az generate-sas failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            raise SasError(
                "az generate-sas failed: "
                + (redact(detail[-1]) if detail else f"exit {proc.returncode}")
            )
        return proc.stdout.strip().strip('"'), expiry.timestamp()


AUTH_KINDS = ("aad", "az", "inline", "env", "file", "exec", "http")

# Which flag each external kind needs, for error messages.
AUTH_SPEC_FLAG = {
    "env": "--sas-env",
    "file": "--sas-file",
    "exec": "--sas-command",
    "http": "--sas-url",
}


def build_provider(
    auth: str,
    *,
    spec: str | None = None,
    headers: Iterable[str] = (),
    url: str | None = None,
    account: str | None = None,
    container: str | None = None,
    ttl_hours: int = DEFAULT_SAS_TTL_HOURS,
    permissions: str = SAS_PERMISSIONS,
) -> SasProvider:
    """Construct the provider named by *auth*.

    ``url`` is only used by the ``inline`` kind; ``account``/``container`` only
    by ``az`` (derived from the URL by the caller when it has one).
    """
    auth = (auth or "az").lower()
    if auth == "aad":
        return AadProvider()
    if auth == "inline":
        token = split_sas(url or "")[1]
        if not token:
            raise SasError("inline auth needs a 'sig=' SAS in the URL.")
        return InlineProvider(token)
    if auth in AUTH_SPEC_FLAG and not spec:
        raise SasError(f"{auth} auth needs {AUTH_SPEC_FLAG[auth]}.")
    if auth == "env":
        return EnvProvider(spec)
    if auth == "file":
        return FileProvider(spec)
    if auth == "exec":
        return ExecProvider(spec)
    if auth == "http":
        return HttpProvider(spec, headers)
    if auth == "az":
        if not account or not container:
            raise SasError("az auth needs an account and container.")
        return AzCliProvider(account, container, ttl_hours, permissions)
    raise SasError(f"unknown auth kind: {auth!r}")


class SasManager:
    """Cache a token on disk (0600) and keep it fresh enough for the next use.

    ``need`` is what the caller expects to require: azsync scales it with the
    last transfer's duration, blobmount uses its refresh interval. Either way
    the token is renewed *before* it is handed to a tool that cannot rotate
    it later.
    """

    def __init__(
        self,
        provider: SasProvider,
        cache_path: Path,
        *,
        min_remaining: float = DEFAULT_SAS_MIN_REMAINING,
    ) -> None:
        self.provider = provider
        self.cache_path = Path(cache_path)
        self.min_remaining = min_remaining
        self._token: SasToken | None = None

    @property
    def enabled(self) -> bool:
        """False when there is no SAS to manage (Entra ID auth)."""
        return self.provider.kind != "aad"

    def _load_cache(self) -> SasToken | None:
        if self._token is not None:
            return self._token
        raw = read_json(self.cache_path)
        if not isinstance(raw, dict):
            return None
        token = raw.get("token")
        if not isinstance(token, str) or not token:
            return None
        self._token = SasToken(
            token, raw.get("expires_at"), raw.get("source") or self.provider.kind
        )
        return self._token

    def _store(self, token: SasToken) -> None:
        self._token = token
        atomic_write(
            self.cache_path,
            json.dumps(
                {
                    "token": token.token,
                    "expires_at": token.expires_at,
                    "source": token.source,
                }
            ),
            mode=0o600,
        )

    def needed_lifetime(self, expected_use: float | None) -> float:
        """Ask for headroom proportional to how long the credential is used."""
        floor = max(self.min_remaining, 0.0)
        if not expected_use:
            return floor
        return max(floor, expected_use * 3)

    def ensure(self, now: float, *, need: float = 0.0, force: bool = False) -> SasToken:
        if not self.enabled:
            return SasToken("", None, self.provider.kind)
        need = max(need, self.min_remaining)
        token = None if force else self._load_cache()
        if token is not None:
            remaining = token.remaining(now)
            if remaining is None or remaining >= need:
                return token
            if not self.provider.refreshable:
                # A pasted-in SAS can't be rotated; hand it back and let the
                # caller surface the expiry warning.
                return token
        fresh = self.provider.resolve(now)
        if fresh.token:
            self._store(fresh)
        return fresh

    def current(self) -> SasToken | None:
        return self._load_cache() if self.enabled else None

    def invalidate(self) -> None:
        self._token = None
        self.cache_path.unlink(missing_ok=True)


# ==========================================================================
# Service management (systemd user units / launchd agents)
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
# Exclude patterns (shared between a watcher and azcopy's flags)
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

    def to_azcopy_flags(self) -> list[str]:
        """Render to azcopy's three (semicolon-separated) exclude flags.

        azcopy splits the job across flags with different matching rules:
        ``--exclude-path`` is a literal relative-path prefix with no wildcards,
        ``--exclude-pattern`` globs the *file name* only, and
        ``--exclude-regex`` matches a regex against the relative path.
        """
        paths: list[str] = []
        names: list[str] = []
        regexes: list[str] = []
        for pat in self.patterns:
            if pat.endswith("/"):
                stem = pat.rstrip("/")
                if _has_glob(stem):
                    regexes.append(_glob_segment_to_regex(stem))
                else:
                    # Prefix form covers `<dir>` at the root; the regex covers
                    # the same directory name nested anywhere below it.
                    paths.append(stem)
                    regexes.append(rf"(^|.*/){re.escape(stem)}/.*")
                continue
            if "/" in pat:
                if _has_glob(pat):
                    regexes.append(fnmatch.translate(pat).replace(r"\Z", "$"))
                else:
                    paths.append(pat)
                continue
            names.append(pat)

        flags: list[str] = []
        if paths:
            flags += ["--exclude-path", ";".join(dict.fromkeys(paths))]
        if names:
            flags += ["--exclude-pattern", ";".join(dict.fromkeys(names))]
        if regexes:
            flags += ["--exclude-regex", ";".join(dict.fromkeys(regexes))]
        return flags


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
