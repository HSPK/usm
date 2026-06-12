"""Pure SDK for the usm script catalog.

UI-free (no click, no rich): parses ``_config.json``, manages the local script
cache, builds argv, and raises typed exceptions. The ``usmo.cli`` module wraps
this with click + rich.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Callable, Iterable, Iterator

CACHE_DIR = Path.home() / ".cache" / "usm"
CACHE_SCRIPT_DIR = CACHE_DIR / "scripts"
CACHE_ENV_DIR = CACHE_DIR / "envs"
LAST_CHECK_FILE = CACHE_DIR / ".last_check"
CONFIG_FILENAME = "_config.json"
RESOURCE_BASE_URL = "https://raw.githubusercontent.com/hspk/usm/main/scripts/"
UV_INSTALL_HINT = "https://docs.astral.sh/uv/#installation"
LOCAL_BIN_DIR = Path.home() / ".local" / "bin"
ALIAS_SHIM_MARKER = "usm-managed alias shim"
AUTO_CHECK_ENV = "USM_AUTO_CHECK_INTERVAL"
DEFAULT_AUTO_CHECK_INTERVAL = 86400  # 24h, in seconds. 0 disables.
HASH_PREFIX = "sha256:"
ENV_MARKER_NAME = ".usm-env.json"

ProgressHook = Callable[[str], None]


def _null_hook(_filename: str) -> None:
    pass


# Errors --------------------------------------------------------------------


class UsmError(Exception):
    """Base class for SDK errors."""


class MissingUv(UsmError):
    def __init__(self, requirements: tuple[str, ...]) -> None:
        super().__init__("'uv' is required to satisfy script requirements.")
        self.requirements = requirements


class EnvBuildError(UsmError):
    """Building a script's virtualenv failed (often a network/index issue)."""

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(f"Failed to prepare the environment for '{name}'.")
        self.name = name
        self.detail = detail


class UnknownCommand(UsmError):
    def __init__(self, name: str, available: Iterable[str]) -> None:
        super().__init__(f"Unknown command '{name}'.")
        self.name = name
        self.available = sorted(available)


class DownloadError(UsmError):
    def __init__(self, filename: str, status: int) -> None:
        detail = "network error" if status == 0 else f"HTTP {status}"
        super().__init__(f"Failed to download {filename} ({detail}).")
        self.filename = filename
        self.status = status


class ForeignAlias(UsmError):
    """An alias target exists but was not installed by usm."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} exists and is not a usm-managed alias.")
        self.path = path


# Script model --------------------------------------------------------------


@dataclass(frozen=True)
class Script:
    """One entry parsed from ``_config.json``."""

    name: str
    path: str
    description: str = ""
    requirements: tuple[str, ...] = ()
    python: str | None = None
    version: str | None = None
    hash: str | None = None

    @classmethod
    def from_config(cls, name: str, raw: dict) -> Script:
        return cls(
            name=name,
            path=raw["path"],
            description=raw.get("description", ""),
            requirements=tuple(raw.get("requirements") or ()),
            python=raw.get("python"),
            version=raw.get("version"),
            hash=raw.get("hash"),
        )

    @property
    def is_python(self) -> bool:
        return self.path.lower().endswith(".py")

    @property
    def uses_uv(self) -> bool:
        return self.is_python and bool(self.requirements)

    @property
    def cached_path(self) -> Path:
        return CACHE_SCRIPT_DIR / self.path

    def local_path(self, *, debug: bool) -> Path:
        return Path.cwd() / "scripts" / self.path if debug else self.cached_path

    @property
    def env_dir(self) -> Path:
        """Directory of this script's persistent virtualenv."""
        return CACHE_ENV_DIR / self.name

    def interpreter_version(self) -> str:
        return self.python or f"{sys.version_info.major}.{sys.version_info.minor}"

    def build_argv(
        self, script_path: Path, args: Iterable[str], *, python: str
    ) -> list[str]:
        """Return the argv to run this script with the given *python* executable.

        Shell scripts run under ``bash``; Python scripts run under *python*
        (a per-script venv interpreter when the script has requirements, or the
        usm interpreter otherwise). No package resolution happens here.
        """
        runner = python if self.is_python else "bash"
        return [runner, str(script_path), *args]


Scripts = dict[str, Script]


# Remote fetch & cache ------------------------------------------------------


def download_file(filename: str, *, on_progress: ProgressHook = _null_hook) -> Path:
    """Download a single file from the remote scripts directory."""
    import requests

    on_progress(filename)
    try:
        response = requests.get(f"{RESOURCE_BASE_URL}{filename}", timeout=(5, 60))
    except requests.RequestException as exc:
        raise DownloadError(filename, 0) from exc
    if response.status_code != 200:
        raise DownloadError(filename, response.status_code)
    dest = CACHE_SCRIPT_DIR / filename
    CACHE_SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    if not filename.endswith(".json"):
        dest.chmod(dest.stat().st_mode | 0o111)
    return dest


def ensure_script_file(
    script: Script,
    *,
    force: bool = False,
    on_progress: ProgressHook = _null_hook,
) -> Path:
    """Return the cached script path, downloading on cache miss or ``force``."""
    if force or not script.cached_path.exists():
        return download_file(script.path, on_progress=on_progress)
    return script.cached_path


def load_scripts(
    *,
    debug: bool = False,
    force_download: bool = False,
    on_progress: ProgressHook = _null_hook,
) -> Scripts:
    """Return the parsed ``scripts`` mapping from ``_config.json``."""
    if debug:
        config_path = Path.cwd() / "scripts" / CONFIG_FILENAME
    else:
        config_path = CACHE_SCRIPT_DIR / CONFIG_FILENAME
        if force_download or not config_path.exists():
            config_path = download_file(CONFIG_FILENAME, on_progress=on_progress)

    raw = json.loads(config_path.read_text()).get("scripts", {})
    return {name: Script.from_config(name, info) for name, info in raw.items()}


# Maintenance actions -------------------------------------------------------


def clean_cache() -> Path | None:
    """Remove the script cache (and per-script envs); return the path if any existed."""
    existed = CACHE_SCRIPT_DIR.exists() or CACHE_ENV_DIR.exists()
    shutil.rmtree(CACHE_SCRIPT_DIR, ignore_errors=True)
    shutil.rmtree(CACHE_ENV_DIR, ignore_errors=True)
    return CACHE_SCRIPT_DIR if existed else None


def iter_updates(
    *,
    names: Iterable[str] | None = None,
    refresh_config: bool = True,
    on_progress: ProgressHook = _null_hook,
) -> Iterator[tuple[str, bool]]:
    """Re-download cached scripts (refreshing the config first by default).

    Yields ``(name, updated)`` per script; ``updated`` is True iff the script
    file was actually downloaded. When ``names`` is given, only those scripts
    are considered (and unknown names raise :class:`UnknownCommand`); scripts
    requested explicitly are downloaded even when not previously cached. Pass
    ``refresh_config=False`` when the caller already refreshed the manifest.
    """
    if refresh_config:
        download_file(CONFIG_FILENAME, on_progress=on_progress)
    scripts = load_scripts(on_progress=on_progress)
    if names is None:
        targets = list(scripts.items())
        force_missing = False
    else:
        wanted = list(names)
        unknown = [n for n in wanted if n not in scripts]
        if unknown:
            raise UnknownCommand(unknown[0], scripts.keys())
        targets = [(n, scripts[n]) for n in wanted]
        force_missing = True
    for name, script in targets:
        if script.cached_path.exists() or force_missing:
            download_file(script.path, on_progress=on_progress)
            yield name, True
        else:
            yield name, False


@dataclass(frozen=True)
class CatalogChange:
    """A script whose version/hash differs between the old and new manifest."""

    name: str
    old_version: str | None
    new_version: str | None
    old_hash: str | None
    new_hash: str | None

    @property
    def status(self) -> str:
        if self.old_version is None and self.old_hash is None:
            return "added"
        if self.new_version is None and self.new_hash is None:
            return "removed"
        return "changed"


def has_cached_config() -> bool:
    return (CACHE_SCRIPT_DIR / CONFIG_FILENAME).exists()


def read_catalog_meta(
    path: Path | None = None,
) -> dict[str, tuple[str | None, str | None]]:
    """Return ``{name: (version, hash)}`` from a manifest (default: cached)."""
    path = path or (CACHE_SCRIPT_DIR / CONFIG_FILENAME)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, tuple[str | None, str | None]] = {}
    for name, entry in data.get("scripts", {}).items():
        if isinstance(entry, dict):
            out[name] = (entry.get("version"), entry.get("hash"))
    return out


def short_hash(value: str | None) -> str:
    """Short form of a ``sha256:<hex>`` digest (7 hex chars)."""
    if not value:
        return "-"
    digest = value[len(HASH_PREFIX) :] if value.startswith(HASH_PREFIX) else value
    return digest[:7]


def update_config(*, on_progress: ProgressHook = _null_hook) -> list[CatalogChange]:
    """Re-download the ``_config.json`` manifest; return per-script changes.

    Compares the previously-cached manifest with the freshly-downloaded one and
    returns the version/hash differences (added / removed / changed scripts).
    """
    old = read_catalog_meta()
    download_file(CONFIG_FILENAME, on_progress=on_progress)
    new = read_catalog_meta()
    changes: list[CatalogChange] = []
    for name in sorted(set(old) | set(new)):
        ov, oh = old.get(name, (None, None))
        nv, nh = new.get(name, (None, None))
        if (ov, oh) != (nv, nh):
            changes.append(CatalogChange(name, ov, nv, oh, nh))
    return changes


# Alias shims (~/.local/bin) ------------------------------------------------


def local_bin_in_path() -> bool:
    """True if ``~/.local/bin`` is on ``$PATH``."""
    target = os.path.normcase(os.path.normpath(LOCAL_BIN_DIR))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        if os.path.normcase(os.path.normpath(os.path.expanduser(entry))) == target:
            return True
    return False


def alias_path(alias: str) -> Path:
    """Path of the shim file for *alias* (``.cmd`` suffix on Windows)."""
    name = alias
    if os.name == "nt" and not name.lower().endswith(".cmd"):
        name += ".cmd"
    return LOCAL_BIN_DIR / name


def alias_status(alias: str) -> tuple[Path, str]:
    """Return ``(path, status)`` where status is absent/ours/foreign."""
    path = alias_path(alias)
    if not path.exists():
        return path, "absent"
    try:
        owned = ALIAS_SHIM_MARKER in path.read_text(errors="ignore")
    except OSError:
        owned = False
    return path, ("ours" if owned else "foreign")


def install_alias(script: str, alias: str, *, usm_bin: str) -> Path:
    """Write an executable shim *alias* that runs ``usm <script>``.

    Overwrites whatever is at the target; the caller is responsible for
    resolving conflicts (see :func:`alias_status`).
    """
    LOCAL_BIN_DIR.mkdir(parents=True, exist_ok=True)
    path = alias_path(alias)
    if os.name == "nt":
        body = (
            "@echo off\r\n"
            f"rem {ALIAS_SHIM_MARKER}: {script}\r\n"
            f'"{usm_bin}" {script} %*\r\n'
        )
    else:
        body = (
            "#!/usr/bin/env bash\n"
            f"# {ALIAS_SHIM_MARKER}: {script}\n"
            f'exec "{usm_bin}" {script} "$@"\n'
        )
    path.write_text(body)
    if os.name != "nt":
        path.chmod(0o755)
    return path


def uninstall_alias(alias: str) -> Path | None:
    """Remove an alias shim we installed.

    Returns the removed path, or ``None`` if it didn't exist. Raises
    :class:`ForeignAlias` if the target exists but wasn't installed by usm.
    """
    path, status = alias_status(alias)
    if status == "absent":
        return None
    if status == "foreign":
        raise ForeignAlias(path)
    path.unlink()
    return path


def resolve_version() -> str:
    """Return the installed usmo version, or a sentinel when unknown."""
    try:
        from usmo._version import __version__ as ver

        return ver
    except ImportError:
        pass
    try:
        return pkg_version("usmo")
    except Exception:
        return "unknown (editable install without build)"


# Auto-check ----------------------------------------------------------------


@dataclass(frozen=True)
class VersionDiff:
    """A script whose remote version differs from the local cached one."""

    name: str
    local_version: str | None
    remote_version: str | None


def _script_versions(raw_scripts: dict) -> dict[str, str | None]:
    return {
        name: (entry.get("version") if isinstance(entry, dict) else None)
        for name, entry in raw_scripts.items()
    }


def _load_local_script_versions() -> dict[str, str | None] | None:
    """Return ``{name: version}`` from the cached _config.json, or None."""
    path = CACHE_SCRIPT_DIR / CONFIG_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return _script_versions(data.get("scripts", {}))


def fetch_remote_script_versions(timeout: float = 5.0) -> dict[str, str | None] | None:
    """Fetch the remote _config.json (in memory) and return per-script versions."""
    import requests

    try:
        r = requests.get(f"{RESOURCE_BASE_URL}{CONFIG_FILENAME}", timeout=timeout)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = json.loads(r.text)
    except json.JSONDecodeError:
        return None
    return _script_versions(data.get("scripts", {}))


def auto_check_interval() -> int:
    """Read ``USM_AUTO_CHECK_INTERVAL`` (seconds). 0 disables. Invalid → default."""
    raw = os.environ.get(AUTO_CHECK_ENV)
    if raw is None or raw == "":
        return DEFAULT_AUTO_CHECK_INTERVAL
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_AUTO_CHECK_INTERVAL


def should_auto_check() -> bool:
    """Return True if the auto-check interval has elapsed (or never ran)."""
    interval = auto_check_interval()
    if interval <= 0:
        return False
    try:
        last = LAST_CHECK_FILE.stat().st_mtime
    except OSError:
        return True
    return time.time() - last >= interval


def mark_checked() -> None:
    """Touch the last-check timestamp file."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_CHECK_FILE.touch()
        os.utime(LAST_CHECK_FILE, None)
    except OSError:
        pass


def check_for_update(*, force: bool = False) -> list[VersionDiff] | None:
    """If due, fetch the remote manifest and diff per-script versions.

    Returns the list of changed scripts (possibly empty if everything matches),
    or ``None`` when no check was run (interval not due, cold cache, or
    network failure). Always touches the last-check timestamp on completion.
    """
    if not force and not should_auto_check():
        return None
    local = _load_local_script_versions()
    if local is None:
        mark_checked()
        return None
    remote = fetch_remote_script_versions()
    mark_checked()
    if remote is None:
        return None
    diffs: list[VersionDiff] = []
    for name in sorted(set(local) | set(remote)):
        lv, rv = local.get(name), remote.get(name)
        if lv != rv:
            diffs.append(VersionDiff(name=name, local_version=lv, remote_version=rv))
    return diffs


# Manifest hashing & version bump (for pre-commit) --------------------------


def compute_script_hash(path: Path) -> str:
    """Return ``sha256:<hex>`` for the bytes of *path*."""
    return HASH_PREFIX + hashlib.sha256(path.read_bytes()).hexdigest()


def _bump_version(version: str | None, level: str = "patch") -> str:
    """Bump *version* by *level* ('patch'/'minor'/'major').

    Missing or non-semver versions return '1.0.0'.
    """
    if not version:
        return "1.0.0"
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return "1.0.0"
    major, minor, patch = (int(p) for p in parts)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


@dataclass(frozen=True)
class HashChange:
    name: str
    old_hash: str | None
    new_hash: str
    old_version: str | None
    new_version: str


def audit_manifest(
    config_path: Path,
    scripts_dir: Path | None = None,
    *,
    names: Iterable[str] | None = None,
    bump: str = "patch",
    force: bool = False,
) -> tuple[dict, list[HashChange]]:
    """Inspect the manifest and return ``(updated_data, changes)``.

    Each script entry is hashed and compared with its declared ``hash``;
    drift (or a missing ``version``) triggers a version bump. ``names``
    restricts the operation to the given script keys, and ``force=True``
    bumps even when the hash already matches (used when the user explicitly
    requests a version bump).

    Raises ``KeyError`` if any name in *names* is not declared in the
    manifest.
    """
    scripts_dir = scripts_dir or config_path.parent
    data = json.loads(config_path.read_text())
    entries = data.get("scripts", {})
    targets = set(names) if names else None

    if targets is not None:
        unknown = targets - set(entries)
        if unknown:
            raise KeyError(
                f"Unknown script(s): {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(entries))}"
            )

    changes: list[HashChange] = []
    for name, entry in entries.items():
        if targets is not None and name not in targets:
            continue
        if not isinstance(entry, dict) or "path" not in entry:
            continue
        target = scripts_dir / entry["path"]
        if not target.exists():
            continue

        new_hash = compute_script_hash(target)
        old_hash = entry.get("hash")
        old_version = entry.get("version")
        hash_matches = new_hash == old_hash

        if hash_matches and old_version and not force:
            continue

        new_version = _bump_version(old_version, bump)
        entry["version"] = new_version
        entry["hash"] = new_hash
        changes.append(HashChange(name, old_hash, new_hash, old_version, new_version))

    return data, changes


def sync_manifest(
    config_path: Path,
    scripts_dir: Path | None = None,
    *,
    names: Iterable[str] | None = None,
    bump: str = "patch",
    force: bool = False,
    check_only: bool = False,
) -> list[HashChange]:
    """Update the manifest in place; return the list of changes.

    With ``check_only=True``, the file is not touched. Other keyword args
    forward to :func:`audit_manifest`.
    """
    data, changes = audit_manifest(
        config_path, scripts_dir, names=names, bump=bump, force=force
    )
    if changes and not check_only:
        config_path.write_text(json.dumps(data, indent=2) + "\n")
    return changes


# Script execution ----------------------------------------------------------


def resolve_script_path(
    script: Script,
    *,
    debug: bool,
    upgrade: bool,
    on_progress: ProgressHook = _null_hook,
) -> Path:
    """Resolve the on-disk location used to execute *script*."""
    if debug:
        return script.local_path(debug=True)
    return ensure_script_file(script, force=upgrade, on_progress=on_progress)


# Per-script virtualenvs ----------------------------------------------------


def _env_python(env_dir: Path) -> Path:
    if os.name == "nt":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _env_spec(script: Script) -> dict:
    return {
        "requirements": list(script.requirements),
        "python": script.interpreter_version(),
    }


def env_ready(script: Script) -> bool:
    """True if the script's venv exists and matches its current requirements."""
    if not script.uses_uv:
        return True
    py = _env_python(script.env_dir)
    if not py.exists():
        return False
    try:
        marker = json.loads((script.env_dir / ENV_MARKER_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return marker == _env_spec(script)


def _build_env(script: Script, *, on_progress: ProgressHook = _null_hook) -> Path:
    """Create the script's venv and install its requirements (needs network once)."""
    env_dir = script.env_dir
    on_progress(script.name)
    env_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(env_dir, ignore_errors=True)
    py_ver = script.interpreter_version()
    try:
        subprocess.run(
            ["uv", "venv", "--python", py_ver, str(env_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(_env_python(env_dir)),
                *script.requirements,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(env_dir, ignore_errors=True)
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise EnvBuildError(script.name, detail) from exc
    (env_dir / ENV_MARKER_NAME).write_text(json.dumps(_env_spec(script)))
    return _env_python(env_dir)


def ensure_env(
    script: Script,
    *,
    upgrade: bool = False,
    on_progress: ProgressHook = _null_hook,
) -> str:
    """Return a Python executable that satisfies *script*'s requirements.

    Scripts without requirements use the usm interpreter. Scripts with
    requirements get a persistent venv under ``~/.cache/usm/envs/<name>`` that
    is built once (and rebuilt only when requirements change or ``upgrade``);
    afterwards runs need no network or package resolution.

    Raises :class:`MissingUv` if uv is required but absent, or
    :class:`EnvBuildError` if building the venv fails.
    """
    if not script.uses_uv:
        return sys.executable
    if not shutil.which("uv"):
        raise MissingUv(script.requirements)
    if not upgrade and env_ready(script):
        return str(_env_python(script.env_dir))
    return str(_build_env(script, on_progress=on_progress))


def run_script(
    script: Script,
    args: Iterable[str],
    *,
    debug: bool = False,
    upgrade: bool = False,
    on_progress: ProgressHook = _null_hook,
    on_setup: ProgressHook = _null_hook,
) -> None:
    """Execute *script* with *args*.

    Raises ``MissingUv`` if uv is required but missing, ``EnvBuildError`` if the
    per-script venv can't be built, and ``subprocess.CalledProcessError`` /
    ``OSError`` if the script subprocess itself fails.
    """
    script_path = resolve_script_path(
        script, debug=debug, upgrade=upgrade, on_progress=on_progress
    )
    python = ensure_env(script, upgrade=upgrade, on_progress=on_setup)
    argv = script.build_argv(script_path, args, python=python)
    subprocess.run(argv, check=True, text=True)
