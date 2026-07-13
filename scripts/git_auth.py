#!/usr/bin/env python3
"""Manage directory-aware Git identities and SSH keys.

All managed data lives below ``~/.config/usm/git``.  Profiles are selected
with Git's native ``includeIf gitdir:`` support; shell integration adds the
generated file to Git's runtime configuration without replacing the user's
normal global config.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator, Sequence

import click


SCHEMA_VERSION = 1
ROOT = Path(os.environ.get("USM_GIT_AUTH_HOME", "~/.config/usm/git")).expanduser()
ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]+$")
GIT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*\.[A-Za-z][A-Za-z0-9-]*$")
BEGIN_MARKER = "## __USM_GIT_AUTH_BEGIN__"
END_MARKER = "## __USM_GIT_AUTH_END__"
SUPPORTED_SHELLS = ("bash", "zsh")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _root() -> Path:
    return ROOT.expanduser()


def _profiles_dir() -> Path:
    return _root() / "profiles"


def _profile_dir(alias: str) -> Path:
    return _profiles_dir() / alias


def _profile_json(alias: str) -> Path:
    return _profile_dir(alias) / "profile.json"


def _profile_gitconfig(alias: str) -> Path:
    return _profile_dir(alias) / "gitconfig"


def _generated_path() -> Path:
    return _root() / "generated.gitconfig"


def _config_path() -> Path:
    return _root() / "config.json"


def _mappings_path() -> Path:
    return _root() / "mappings.json"


def _shell_dir() -> Path:
    return _root() / "shell"


def _ensure_root() -> None:
    _root().mkdir(parents=True, exist_ok=True, mode=0o700)
    _profiles_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    _shell_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(_root(), 0o700)
        os.chmod(_profiles_dir(), 0o700)
        os.chmod(_shell_dir(), 0o700)


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Serialize mutations on POSIX; still provide a harmless lock on Windows."""
    _ensure_root()
    lock_path = _root() / ".lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        with contextlib.suppress(OSError):
            os.chmod(lock_path, 0o600)
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _write_json(path: Path, data: Any) -> None:
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"cannot read {path}: {exc}") from exc


def _load_config() -> dict[str, Any]:
    data = _read_json(
        _config_path(),
        {"schema_version": SCHEMA_VERSION, "installations": []},
    )
    _check_schema(data, _config_path())
    data.setdefault("installations", [])
    return data


def _load_mappings() -> dict[str, Any]:
    data = _read_json(
        _mappings_path(),
        {"schema_version": SCHEMA_VERSION, "mappings": []},
    )
    _check_schema(data, _mappings_path())
    data.setdefault("mappings", [])
    if not isinstance(data["mappings"], list):
        raise click.ClickException(f"mappings must be a list in {_mappings_path()}")
    seen: set[str] = set()
    for item in data["mappings"]:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(field), str) for field in ("path", "real_path", "alias")
        ):
            raise click.ClickException(f"invalid mapping entry in {_mappings_path()}")
        real_path = item["real_path"]
        if real_path in seen:
            raise click.ClickException(
                f"duplicate mapping path {real_path} in {_mappings_path()}"
            )
        seen.add(real_path)
    return data


def _check_schema(data: Any, path: Path) -> None:
    if not isinstance(data, dict):
        raise click.ClickException(f"{path} must contain a JSON object")
    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise click.ClickException(
            f"unsupported schema_version {version!r} in {path}; expected {SCHEMA_VERSION}"
        )


def _validate_alias(alias: str) -> str:
    if not ALIAS_RE.fullmatch(alias):
        raise click.BadParameter(
            "alias may contain only letters, digits, '.', '_' and '-'",
            param_hint="alias",
        )
    return alias


def _validate_name(name: str) -> str:
    if not name.strip() or any(ord(char) < 32 for char in name):
        raise click.BadParameter("name cannot be empty or contain control characters")
    return name


def _validate_email(email: str) -> str:
    if (
        email.count("@") != 1
        or email.startswith("@")
        or email.endswith("@")
        or any(char.isspace() or ord(char) < 32 for char in email)
    ):
        raise click.BadParameter(f"invalid email address: {email!r}")
    return email


def _list_aliases() -> list[str]:
    if not _profiles_dir().exists():
        return []
    return sorted(
        path.name
        for path in _profiles_dir().iterdir()
        if path.is_dir() and (path / "profile.json").is_file()
    )


def _load_profile(alias: str) -> dict[str, Any]:
    _validate_alias(alias)
    path = _profile_json(alias)
    if not path.exists():
        available = ", ".join(_list_aliases()) or "none"
        raise click.ClickException(
            f"unknown profile '{alias}' (available: {available})"
        )
    data = _read_json(path, {})
    _check_schema(data, path)
    if data.get("alias") != alias:
        raise click.ClickException(f"profile alias mismatch in {path}")
    for field in ("name", "email"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise click.ClickException(f"profile field {field!r} is missing in {path}")
    try:
        _validate_name(data["name"])
        _validate_email(data["email"])
    except click.BadParameter as exc:
        raise click.ClickException(
            f"invalid profile {path}: {exc.format_message()}"
        ) from exc
    if not isinstance(data.get("ssh", {}), dict) or not isinstance(
        data.get("git", {}), dict
    ):
        raise click.ClickException(f"profile ssh/git fields must be objects in {path}")
    return data


def _save_profile(profile: dict[str, Any]) -> None:
    alias = _validate_alias(str(profile["alias"]))
    directory = _profile_dir(alias)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)
    _write_json(_profile_json(alias), profile)


def _git_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def _gitdir_pattern(path: Path, *, descendants: bool) -> str:
    value = path.as_posix()
    # gitdir conditions are globs. Escape metacharacters so directory names are
    # interpreted literally. Tree mappings intentionally match only normal
    # worktree ``.git`` directories. A broad trailing-slash pattern would also
    # match ``.git/worktrees/<name>`` and leak a profile into linked worktrees
    # located outside the mapped filesystem tree.
    for char in ("\\", "*", "?", "["):
        value = value.replace(char, "\\" + char)
    return value.rstrip("/") + ("/**/.git" if descendants else "")


def _identity_path(profile: dict[str, Any]) -> Path | None:
    identity = profile.get("ssh", {}).get("identity_file")
    if not identity:
        return None
    path = Path(str(identity))
    if not path.is_absolute():
        path = _profile_dir(str(profile["alias"])) / path
    return path


def _ssh_command(profile: dict[str, Any]) -> str | None:
    ssh = profile.get("ssh", {})
    identity = _identity_path(profile)
    if identity is None:
        return None
    argv = ["ssh", "-i", str(identity)]
    if ssh.get("identities_only", True):
        argv.extend(["-o", "IdentitiesOnly=yes"])
    for key, value in sorted(ssh.get("options", {}).items()):
        argv.extend(["-o", f"{key}={value}"])
    return shlex.join(argv)


def _render_profile(profile: dict[str, Any]) -> str:
    lines = [
        "# Generated by usm git-auth. Do not edit; edit profile.json instead.",
        "[user]",
        f"\tname = {_git_quote(str(profile['name']))}",
        f"\temail = {_git_quote(str(profile['email']))}",
    ]
    ssh_command = _ssh_command(profile)
    if ssh_command:
        lines.extend(["", "[core]", f"\tsshCommand = {_git_quote(ssh_command)}"])

    grouped: dict[str, list[tuple[str, str]]] = {}
    for key, value in sorted(profile.get("git", {}).items()):
        section, name = key.split(".", 1)
        grouped.setdefault(section, []).append((name, str(value)))
    for section, values in grouped.items():
        lines.extend(["", f"[{section}]"])
        for name, value in values:
            lines.append(f"\t{name} = {_git_quote(value)}")
    return "\n".join(lines) + "\n"


def _render_generated(mappings_data: dict[str, Any] | None = None) -> str:
    data = mappings_data or _load_mappings()
    mappings = sorted(
        data.get("mappings", []),
        key=lambda item: (len(Path(item["real_path"]).parts), item["real_path"]),
    )
    lines = [
        "# Generated by usm git-auth. Do not edit.",
        "# More-specific directory mappings appear later and take precedence.",
    ]
    for mapping in mappings:
        alias = mapping["alias"]
        include_path = _profile_gitconfig(alias)
        candidates: list[tuple[Path, bool]] = []
        for raw in (mapping.get("path"), mapping.get("real_path")):
            if raw:
                candidate = Path(raw)
                if all(existing != candidate for existing, _ in candidates):
                    candidates.append((candidate, True))
        exact_git_dirs = (
            mapping.get("git_dirs", [])
            if mapping.get("scope") in {"repository", "worktree"}
            else []
        )
        for raw in exact_git_dirs:
            candidate = Path(raw)
            if all(existing != candidate for existing, _ in candidates):
                candidates.append((candidate, False))
        for candidate, descendants in candidates:
            patterns = [_gitdir_pattern(candidate, descendants=descendants)]
            if descendants:
                literal_base = _gitdir_pattern(candidate, descendants=False)
                patterns.append(literal_base + "/**/.git/modules/**")
            for pattern in patterns:
                lines.extend(
                    [
                        "",
                        f"# {mapping['path']} -> {alias}",
                        f"[includeIf {_git_quote('gitdir:' + pattern)}]",
                        f"\tpath = {_git_quote(str(include_path))}",
                    ]
                )
    return "\n".join(lines) + "\n"


def _sync_unlocked() -> None:
    _ensure_root()
    if not _config_path().exists():
        _write_json(
            _config_path(),
            {"schema_version": SCHEMA_VERSION, "installations": []},
        )
    if not _mappings_path().exists():
        _write_json(
            _mappings_path(),
            {"schema_version": SCHEMA_VERSION, "mappings": []},
        )
    aliases = set(_list_aliases())
    dangling = {
        item["alias"]
        for item in _load_mappings()["mappings"]
        if item["alias"] not in aliases
    }
    if dangling:
        raise click.ClickException(
            f"mappings reference missing profile(s): {', '.join(sorted(dangling))}"
        )
    for alias in sorted(aliases):
        profile = _load_profile(alias)
        identity = _identity_path(profile)
        if identity is not None:
            try:
                identity.resolve().relative_to(_profile_dir(alias).resolve())
            except ValueError as exc:
                raise click.ClickException(
                    f"profile '{alias}' references an SSH key outside {_profile_dir(alias)}"
                ) from exc
        _atomic_write(_profile_gitconfig(alias), _render_profile(profile))
    _atomic_write(_generated_path(), _render_generated())
    _write_shell_helpers()


def _commit_mappings(data: dict[str, Any], original: dict[str, Any]) -> None:
    _write_json(_mappings_path(), data)
    try:
        _sync_unlocked()
    except BaseException:
        _write_json(_mappings_path(), original)
        with contextlib.suppress(Exception):
            _sync_unlocked()
        raise


def _shell_helper(shell: str) -> str:
    generated = str(_generated_path())
    marker = str(_root())
    return f"""# Generated by usm git-auth. Source this file; do not edit.
if [ "${{USM_GIT_AUTH_LOADED:-}}" != {shlex.quote(marker)} ]; then
  _usm_git_auth_index="${{GIT_CONFIG_COUNT:-0}}"
  _usm_git_auth_value={shlex.quote(generated)}
  export "GIT_CONFIG_KEY_${{_usm_git_auth_index}}=include.path"
  export "GIT_CONFIG_VALUE_${{_usm_git_auth_index}}=${{_usm_git_auth_value}}"
  export GIT_CONFIG_COUNT="$((_usm_git_auth_index + 1))"
  export USM_GIT_AUTH_LOADED={shlex.quote(marker)}
  unset _usm_git_auth_index _usm_git_auth_value
fi
"""


def _write_shell_helpers() -> None:
    for shell in SUPPORTED_SHELLS:
        _atomic_write(_shell_dir() / f"git-auth.{shell}", _shell_helper(shell), 0o600)


def _profile_target(shell: str) -> Path:
    if shell == "zsh":
        zdotdir = os.environ.get("ZDOTDIR")
        if zdotdir:
            path = Path(zdotdir).expanduser()
            if not path.is_absolute():
                path = Path.home() / path
            return path / ".zshrc"
        return Path.home() / ".zshrc"
    if sys.platform == "darwin":
        bash_profile = Path.home() / ".bash_profile"
        if bash_profile.exists() or not (Path.home() / ".bashrc").exists():
            return bash_profile
    return Path.home() / ".bashrc"


def _detect_shell() -> str:
    name = Path(os.environ.get("SHELL", "")).name
    return name if name in SUPPORTED_SHELLS else "bash"


def _managed_block(shell: str) -> str:
    helper = _shell_dir() / f"git-auth.{shell}"
    return f"{BEGIN_MARKER}\nsource {shlex.quote(str(helper))}\n{END_MARKER}\n"


def _strip_managed_block(content: str) -> tuple[str, bool]:
    lines = content.splitlines()
    kept: list[str] = []
    inside = False
    found = False
    for line in lines:
        if line == BEGIN_MARKER:
            if inside:
                raise click.ClickException("nested git-auth shell markers found")
            inside = True
            found = True
            continue
        if line == END_MARKER:
            if not inside:
                raise click.ClickException("git-auth end marker has no begin marker")
            inside = False
            continue
        if not inside:
            kept.append(line)
    if inside:
        raise click.ClickException("incomplete git-auth shell marker block")
    return "\n".join(kept).rstrip("\n"), found


def _install_shell_block(path: Path, shell: str) -> str:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    cleaned, existed = _strip_managed_block(original)
    content = (
        f"{cleaned}\n\n{_managed_block(shell)}" if cleaned else _managed_block(shell)
    )
    _atomic_write(path, content, mode)
    return "updated" if existed else "installed"


def _remove_shell_block(path: Path) -> bool:
    if not path.exists():
        return False
    cleaned, existed = _strip_managed_block(path.read_text(encoding="utf-8"))
    if existed:
        mode = stat.S_IMODE(path.stat().st_mode)
        _atomic_write(path, cleaned + ("\n" if cleaned else ""), mode)
    return existed


def _canonical_dir(path: Path) -> tuple[Path, Path]:
    display = path.expanduser().absolute()
    if not display.exists() or not display.is_dir():
        raise click.BadParameter(
            f"directory does not exist: {display}", param_hint="path"
        )
    return display, display.resolve()


def _relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_mapping(
    path: Path, data: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    real = path.expanduser().resolve()
    matches = [
        item
        for item in (data or _load_mappings()).get("mappings", [])
        if _relative_to(real, Path(item["real_path"]))
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(Path(item["real_path"]).parts))


def _repo_context(path: Path) -> dict[str, Any] | None:
    if shutil.which("git") is None:
        return None
    git_dir_result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--absolute-git-dir"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if git_dir_result.returncode:
        return None
    bare_result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-bare-repository"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    bare = bare_result.stdout.strip() == "true"
    top_level: Path | None = None
    if not bare:
        top_result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if top_result.returncode == 0:
            top_level = Path(top_result.stdout.strip()).resolve()
    return {
        "git_dir": Path(git_dir_result.stdout.strip()).resolve(),
        "top_level": top_level,
        "bare": bare,
    }


def _bool_value(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise click.BadParameter(f"expected a boolean, got {value!r}")


def _validate_private_key(path: Path) -> None:
    if not path.is_file():
        raise click.BadParameter(f"SSH private key does not exist: {path}")
    try:
        head = path.read_bytes()[:4096]
    except OSError as exc:
        raise click.BadParameter(f"cannot read SSH private key: {exc}") from exc
    if b"PRIVATE KEY-----" not in head:
        raise click.BadParameter(f"file does not look like an SSH private key: {path}")
    if shutil.which("ssh-keygen") is None:
        raise click.ClickException("ssh-keygen is required to validate private keys")
    _ensure_root()
    fd, temp_name = tempfile.mkstemp(prefix=".key-validation.", dir=_root())
    os.close(fd)
    validation_copy = Path(temp_name)
    try:
        shutil.copyfile(path, validation_copy)
        os.chmod(validation_copy, 0o600)
        result = subprocess.run(
            ["ssh-keygen", "-lf", str(validation_copy)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            validation_copy.unlink()
    if result.returncode:
        raise click.BadParameter(
            f"invalid or unreadable SSH private key {path}: {result.stderr.strip()}"
        )


def _copy_identity_to(directory: Path, source: Path) -> None:
    source = source.expanduser().resolve()
    _validate_private_key(source)
    destination = directory / "identity"
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o600)
    public_source = Path(str(source) + ".pub")
    public_destination = directory / "identity.pub"
    if public_source.is_file():
        shutil.copyfile(public_source, public_destination)
        os.chmod(public_destination, 0o644)
    else:
        with contextlib.suppress(FileNotFoundError):
            public_destination.unlink()
        # Derive a public key for unencrypted private keys. Encrypted keys are
        # still valid; avoid prompting for their passphrase during import.
        result = subprocess.run(
            ["ssh-keygen", "-y", "-P", "", "-f", str(source)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0 and result.stdout.strip():
            _atomic_write(public_destination, result.stdout.strip() + "\n", 0o644)


def _validate_rendered_gitconfig(path: Path) -> None:
    if shutil.which("git") is None:
        return
    result = subprocess.run(
        ["git", "config", "--file", str(path), "--list"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        raise click.ClickException(
            f"generated Git config is invalid: {result.stderr.strip()}"
        )


def _commit_profile(
    profile: dict[str, Any],
    *,
    identity_source: Path | None = None,
    remove_identity: bool = False,
    fresh: bool = False,
) -> None:
    """Stage and atomically swap one complete profile directory."""
    alias = _validate_alias(str(profile["alias"]))
    final = _profile_dir(alias)
    container = Path(tempfile.mkdtemp(prefix=f".{alias}.transaction.", dir=_root()))
    staged = container / "profile"
    backup = container / "backup"
    had_final = final.exists()
    try:
        if had_final and not fresh:
            shutil.copytree(final, staged)
        else:
            staged.mkdir(mode=0o700)
        if remove_identity:
            for name in ("identity", "identity.pub"):
                with contextlib.suppress(FileNotFoundError):
                    (staged / name).unlink()
        if identity_source is not None:
            _copy_identity_to(staged, identity_source)
        _write_json(staged / "profile.json", profile)
        rendered = _render_profile(profile)
        _atomic_write(staged / "gitconfig", rendered)
        _validate_rendered_gitconfig(staged / "gitconfig")

        if had_final:
            os.replace(final, backup)
        try:
            os.replace(staged, final)
            _sync_unlocked()
        except BaseException:
            if final.exists():
                shutil.rmtree(final)
            if backup.exists():
                os.replace(backup, final)
            with contextlib.suppress(Exception):
                _sync_unlocked()
            raise
    finally:
        shutil.rmtree(container, ignore_errors=True)


def _fingerprint(path: Path | None) -> str:
    if path is None or not path.exists() or shutil.which("ssh-keygen") is None:
        return "-"
    result = subprocess.run(
        ["ssh-keygen", "-lf", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode:
        return "unavailable"
    fields = result.stdout.strip().split()
    return fields[1] if len(fields) > 1 else result.stdout.strip()


def _set_profile_key(profile: dict[str, Any], key: str, value: str) -> None:
    if key == "name":
        profile["name"] = _validate_name(value)
    elif key == "email":
        profile["email"] = _validate_email(value)
    elif key in {"ssh.identity", "ssh.key"}:
        source = Path(value).expanduser().resolve()
        _validate_private_key(source)
        profile.setdefault("ssh", {})["identity_file"] = "identity"
    elif key == "ssh.identities-only":
        profile.setdefault("ssh", {})["identities_only"] = _bool_value(value)
    elif key.startswith("ssh.option."):
        option = key.removeprefix("ssh.option.")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", option):
            raise click.BadParameter(f"invalid SSH option name: {option!r}")
        profile.setdefault("ssh", {}).setdefault("options", {})[option] = value
    elif key.startswith("git."):
        git_key = key.removeprefix("git.")
        if not GIT_KEY_RE.fullmatch(git_key):
            raise click.BadParameter(
                "Git keys must use git.<section>.<name> with letters, digits or '-'"
            )
        if git_key.lower() in {"user.name", "user.email", "core.sshcommand"}:
            raise click.BadParameter(f"{git_key} is managed by git-auth")
        profile.setdefault("git", {})[git_key] = value
    else:
        raise click.BadParameter(f"unsupported profile key: {key!r}")
    profile["updated_at"] = _now()


def _unset_profile_key(profile: dict[str, Any], key: str) -> bool:
    if key in {"name", "email"}:
        raise click.BadParameter(f"required field {key!r} cannot be unset")
    if key in {"ssh.identity", "ssh.key"}:
        changed = bool(profile.setdefault("ssh", {}).pop("identity_file", None))
    elif key == "ssh.identities-only":
        changed = profile.setdefault("ssh", {}).pop("identities_only", None) is not None
    elif key.startswith("ssh.option."):
        changed = (
            profile.setdefault("ssh", {})
            .setdefault("options", {})
            .pop(key.removeprefix("ssh.option."), None)
            is not None
        )
    elif key.startswith("git."):
        changed = (
            profile.setdefault("git", {}).pop(key.removeprefix("git."), None)
            is not None
        )
    else:
        raise click.BadParameter(f"unsupported profile key: {key!r}")
    if changed:
        profile["updated_at"] = _now()
    return changed


def _json_echo(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=False))


def _tabulate(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    rendered = [[str(value) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in rendered:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    click.echo("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    click.echo("  ".join("-" * width for width in widths))
    for row in rendered:
        click.echo("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))


def _git_value(
    path: Path, key: str, *, include_generated: bool
) -> tuple[str | None, str | None]:
    if shutil.which("git") is None:
        return None, None
    probe = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode:
        return None, None
    command = ["git", "-C", str(path)]
    if include_generated:
        command.extend(["-c", f"include.path={_generated_path()}"])
    command.extend(["config", "--show-origin", "--get", key])
    result = subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    if result.returncode or not result.stdout.strip():
        return None, None
    line = result.stdout.rstrip("\n")
    parts = line.split(None, 1)
    return (parts[1] if len(parts) > 1 else "", parts[0])


def _runtime_generated_include() -> bool:
    try:
        count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        return False
    target = str(_generated_path())
    for index in range(count):
        if (
            os.environ.get(f"GIT_CONFIG_KEY_{index}") == "include.path"
            and os.environ.get(f"GIT_CONFIG_VALUE_{index}") == target
        ):
            return True
    return False


def _global_generated_include() -> bool:
    if shutil.which("git") is None:
        return False
    result = subprocess.run(
        ["git", "config", "--global", "--get-all", "include.path"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return str(_generated_path()) in result.stdout.splitlines()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Automatically select Git identity and SSH key by directory."""


@cli.command("enable")
@click.option(
    "--shell",
    "shell_name",
    type=click.Choice((*SUPPORTED_SHELLS, "all")),
    default=None,
    help="Shell profile to update (default: detect $SHELL).",
)
@click.option(
    "--file",
    "profile_file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Explicit shell profile file; requires one shell syntax.",
)
@click.option(
    "--global",
    "global_mode",
    is_flag=True,
    help="Install through Git's global include path for IDE/GUI support.",
)
def cmd_enable(
    shell_name: str | None, profile_file: Path | None, global_mode: bool
) -> None:
    """Enable git-auth without deleting existing Git configuration."""
    if global_mode and (shell_name or profile_file):
        raise click.UsageError("--global cannot be combined with --shell or --file")
    with _locked():
        _sync_unlocked()
        config = _load_config()
        installations = config["installations"]
        if global_mode:
            if shutil.which("git") is None:
                raise click.ClickException("git is not installed")
            path = str(_generated_path())
            existing = subprocess.run(
                ["git", "config", "--global", "--get-all", "include.path"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ).stdout.splitlines()
            previous = next(
                (
                    item
                    for item in installations
                    if item.get("type") == "global" and item.get("path") == path
                ),
                None,
            )
            added = previous.get("added", True) if previous else path not in existing
            if path not in existing:
                try:
                    subprocess.run(
                        ["git", "config", "--global", "--add", "include.path", path],
                        check=True,
                    )
                except subprocess.CalledProcessError as exc:
                    raise click.ClickException(
                        "failed to update Git global configuration"
                    ) from exc
            entry = {"type": "global", "path": path, "added": added}
            installations[:] = [
                item
                for item in installations
                if not (item.get("type") == "global" and item.get("path") == path)
            ]
            installations.append(entry)
            try:
                _write_json(_config_path(), config)
            except BaseException:
                if added and path not in existing:
                    subprocess.run(
                        [
                            "git",
                            "config",
                            "--global",
                            "--fixed-value",
                            "--unset-all",
                            "include.path",
                            path,
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                raise
            click.echo(f"Enabled git-auth through Git global config: {path}")
            return

        shells = (
            list(SUPPORTED_SHELLS)
            if shell_name == "all"
            else [shell_name or _detect_shell()]
        )
        if profile_file is not None and len(shells) != 1:
            raise click.UsageError("--file requires exactly one --shell")
        snapshots: dict[Path, tuple[str | None, int | None]] = {}
        messages: list[str] = []
        try:
            for shell in shells:
                target = (
                    profile_file.expanduser()
                    if profile_file
                    else _profile_target(shell)
                )
                snapshots[target] = (
                    target.read_text(encoding="utf-8") if target.exists() else None,
                    stat.S_IMODE(target.stat().st_mode) if target.exists() else None,
                )
                action = _install_shell_block(target, shell)
                entry = {"type": "shell", "shell": shell, "file": str(target)}
                installations[:] = [
                    item
                    for item in installations
                    if not (
                        item.get("type") == "shell" and item.get("file") == str(target)
                    )
                ]
                installations.append(entry)
                messages.append(
                    f"{action.capitalize()} {shell} integration in {target}"
                )
            _write_json(_config_path(), config)
        except BaseException:
            for target, (content, mode) in snapshots.items():
                if content is None:
                    with contextlib.suppress(FileNotFoundError):
                        target.unlink()
                else:
                    _atomic_write(target, content, mode or 0o644)
            raise
        for message in messages:
            click.echo(message)
    click.echo("Restart the shell or source the updated profile to activate it.")


@cli.command("disable")
@click.option(
    "--shell",
    "shell_name",
    type=click.Choice((*SUPPORTED_SHELLS, "all")),
    default=None,
    help="Only remove integration for this shell.",
)
@click.option(
    "--global", "global_mode", is_flag=True, help="Remove the Git global include."
)
def cmd_disable(shell_name: str | None, global_mode: bool) -> None:
    """Disable integration but keep profiles and mappings."""
    with _locked():
        config = _load_config()
        installations = config["installations"]
        removed = 0
        kept: list[dict[str, Any]] = []
        errors: list[str] = []
        for entry in installations:
            if entry.get("type") == "global":
                if not global_mode and shell_name is not None:
                    kept.append(entry)
                    continue
                if entry.get("added", True):
                    if shutil.which("git") is None:
                        kept.append(entry)
                        errors.append(
                            "git is not installed; global include was not removed"
                        )
                        continue
                    existing = subprocess.run(
                        ["git", "config", "--global", "--get-all", "include.path"],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    ).stdout.splitlines()
                    if entry["path"] in existing:
                        result = subprocess.run(
                            [
                                "git",
                                "config",
                                "--global",
                                "--fixed-value",
                                "--unset-all",
                                "include.path",
                                entry["path"],
                            ],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        if result.returncode:
                            kept.append(entry)
                            errors.append(
                                result.stderr.strip()
                                or "failed to remove Git global include"
                            )
                            continue
                removed += 1
                continue
            shell = entry.get("shell")
            selected = not global_mode and (
                shell_name in {None, "all"} or shell_name == shell
            )
            if selected and _remove_shell_block(Path(entry["file"])):
                removed += 1
            if not selected:
                kept.append(entry)
        config["installations"] = kept
        _write_json(_config_path(), config)
    if errors:
        raise click.ClickException("; ".join(errors))
    if removed:
        click.echo(f"Disabled {removed} git-auth integration(s).")
        click.echo(
            "Restart the shell to clear the integration from the current environment."
        )
    else:
        click.echo("No matching git-auth integration was enabled.")


@cli.command("add")
@click.argument("alias")
@click.argument("name", required=False)
@click.argument("email", required=False)
@click.argument("ssh_key", required=False, type=click.Path(path_type=Path))
@click.option("-i", "--interactive", is_flag=True, help="Prompt for missing values.")
@click.option("--force", is_flag=True, help="Replace an existing profile.")
def cmd_add(
    alias: str,
    name: str | None,
    email: str | None,
    ssh_key: Path | None,
    interactive: bool,
    force: bool,
) -> None:
    """Add a Git identity and optional imported SSH private key."""
    _validate_alias(alias)
    if interactive:
        name = click.prompt("Name", default=name or "", show_default=False)
        email = click.prompt("Email", default=email or "", show_default=False)
        if ssh_key is None:
            raw = click.prompt(
                "SSH private key (optional)", default="", show_default=False
            )
            ssh_key = Path(raw) if raw else None
    if not name or not email:
        raise click.UsageError("usage: git-auth add <alias> <name> <email> [ssh-key]")
    _validate_name(name)
    _validate_email(email)
    if ssh_key is not None:
        _validate_private_key(ssh_key.expanduser().resolve())
    with _locked():
        collision = next(
            (
                existing
                for existing in _list_aliases()
                if existing.casefold() == alias.casefold() and existing != alias
            ),
            None,
        )
        if collision:
            raise click.ClickException(
                f"profile alias '{alias}' conflicts with existing '{collision}' on case-insensitive filesystems"
            )
        if _profile_json(alias).exists() and not force:
            raise click.ClickException(f"profile '{alias}' already exists; use --force")
        profile: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "alias": alias,
            "name": name,
            "email": email,
            "ssh": {"identity_file": None, "identities_only": True, "options": {}},
            "git": {},
            "created_at": _now(),
            "updated_at": _now(),
        }
        if ssh_key is not None:
            profile["ssh"]["identity_file"] = "identity"
        _commit_profile(
            profile,
            identity_source=ssh_key,
            fresh=True,
        )
    click.echo(f"Added profile '{alias}' ({name} <{email}>).")
    if ssh_key:
        click.echo(f"Imported SSH key: {_identity_path(profile)}")


@cli.command("rm")
@click.argument("alias")
@click.option(
    "--force", is_flag=True, help="Also remove mappings that use this profile."
)
def cmd_rm(alias: str, force: bool) -> None:
    """Delete a profile."""
    with _locked():
        _load_profile(alias)
        data = _load_mappings()
        used = [item for item in data["mappings"] if item["alias"] == alias]
        if used and not force:
            paths = ", ".join(item["path"] for item in used)
            raise click.ClickException(
                f"profile '{alias}' is used by: {paths}; use --force to remove mappings"
            )
        original_data = copy.deepcopy(data)
        if used:
            data["mappings"] = [
                item for item in data["mappings"] if item["alias"] != alias
            ]
        transaction = Path(tempfile.mkdtemp(prefix=f".{alias}.remove.", dir=_root()))
        removed_profile = transaction / "profile"
        os.replace(_profile_dir(alias), removed_profile)
        try:
            _write_json(_mappings_path(), data)
            _sync_unlocked()
        except BaseException:
            os.replace(removed_profile, _profile_dir(alias))
            _write_json(_mappings_path(), original_data)
            with contextlib.suppress(Exception):
                _sync_unlocked()
            raise
        finally:
            shutil.rmtree(transaction, ignore_errors=True)
    click.echo(f"Removed profile '{alias}' and {len(used)} mapping(s).")


@cli.command("set")
@click.argument("alias")
@click.option(
    "--key",
    "pairs",
    type=(str, str),
    multiple=True,
    required=True,
    metavar="FIELD VALUE",
    help="Set a profile field; may be repeated.",
)
def cmd_set(alias: str, pairs: tuple[tuple[str, str], ...]) -> None:
    """Set one or more profile fields."""
    with _locked():
        profile = copy.deepcopy(_load_profile(alias))
        identity_source: Path | None = None
        for key, value in pairs:
            _set_profile_key(profile, key, value)
            if key in {"ssh.identity", "ssh.key"}:
                identity_source = Path(value)
        _commit_profile(profile, identity_source=identity_source)
    click.echo(f"Updated {len(pairs)} field(s) in profile '{alias}'.")


@cli.command("unset")
@click.argument("alias")
@click.option("--key", "keys", multiple=True, required=True, help="Field to remove.")
def cmd_unset(alias: str, keys: tuple[str, ...]) -> None:
    """Remove optional fields from a profile."""
    with _locked():
        profile = copy.deepcopy(_load_profile(alias))
        changed = sum(_unset_profile_key(profile, key) for key in keys)
        _commit_profile(
            profile,
            remove_identity=bool({"ssh.identity", "ssh.key"}.intersection(keys)),
        )
    click.echo(f"Removed {changed} field(s) from profile '{alias}'.")


@cli.command("rename")
@click.argument("old_alias")
@click.argument("new_alias")
def cmd_rename(old_alias: str, new_alias: str) -> None:
    """Rename a profile and update every mapping that references it."""
    _validate_alias(new_alias)
    with _locked():
        profile = _load_profile(old_alias)
        original_profile = copy.deepcopy(profile)
        collision = next(
            (
                existing
                for existing in _list_aliases()
                if existing.casefold() == new_alias.casefold()
                and existing not in {old_alias, new_alias}
            ),
            None,
        )
        if collision:
            raise click.ClickException(
                f"profile alias '{new_alias}' conflicts with existing '{collision}'"
            )
        if _profile_dir(new_alias).exists():
            raise click.ClickException(f"profile '{new_alias}' already exists")
        data = _load_mappings()
        original_data = copy.deepcopy(data)
        changed = 0
        for mapping in data["mappings"]:
            if mapping["alias"] == old_alias:
                mapping["alias"] = new_alias
                changed += 1
        _profile_dir(old_alias).rename(_profile_dir(new_alias))
        try:
            profile["alias"] = new_alias
            profile["updated_at"] = _now()
            _save_profile(profile)
            _write_json(_mappings_path(), data)
            _sync_unlocked()
        except BaseException:
            if (
                _profile_dir(new_alias).exists()
                and not _profile_dir(old_alias).exists()
            ):
                _profile_dir(new_alias).rename(_profile_dir(old_alias))
            _save_profile(original_profile)
            _write_json(_mappings_path(), original_data)
            with contextlib.suppress(Exception):
                _sync_unlocked()
            raise
    click.echo(
        f"Renamed '{old_alias}' to '{new_alias}' and updated {changed} mapping(s)."
    )


@cli.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def cmd_list(as_json: bool) -> None:
    """List all profiles."""
    mappings = _load_mappings().get("mappings", [])
    profiles = []
    for alias in _list_aliases():
        profile = _load_profile(alias)
        identity = _identity_path(profile)
        profiles.append(
            {
                "alias": alias,
                "name": profile["name"],
                "email": profile["email"],
                "ssh_key": str(identity) if identity else None,
                "fingerprint": _fingerprint(identity),
                "mappings": sum(item["alias"] == alias for item in mappings),
            }
        )
    if as_json:
        _json_echo(profiles)
    elif not profiles:
        click.echo("No git-auth profiles configured.")
    else:
        _tabulate(
            ("ALIAS", "NAME", "EMAIL", "SSH", "MAPPINGS"),
            [
                (
                    item["alias"],
                    item["name"],
                    item["email"],
                    item["fingerprint"],
                    item["mappings"],
                )
                for item in profiles
            ],
        )


@cli.command("show")
@click.argument("alias")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def cmd_show(alias: str, as_json: bool) -> None:
    """Show one profile and the directories that use it."""
    profile = _load_profile(alias)
    identity = _identity_path(profile)
    result = dict(profile)
    result["ssh_key"] = str(identity) if identity else None
    result["fingerprint"] = _fingerprint(identity)
    result["mappings"] = [
        item for item in _load_mappings()["mappings"] if item["alias"] == alias
    ]
    if as_json:
        _json_echo(result)
        return
    click.echo(f"Profile:     {alias}")
    click.echo(f"Name:        {profile['name']}")
    click.echo(f"Email:       {profile['email']}")
    click.echo(f"SSH key:     {identity or '-'}")
    click.echo(f"Fingerprint: {_fingerprint(identity)}")
    click.echo(f"Mappings:    {len(result['mappings'])}")
    for mapping in result["mappings"]:
        click.echo(f"  {mapping['path']}")


@cli.command("mappings")
@click.option("--alias", "alias_filter", help="Only show mappings for this profile.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def cmd_mappings(alias_filter: str | None, as_json: bool) -> None:
    """List directory-to-profile mappings."""
    if alias_filter:
        _load_profile(alias_filter)
    items = [
        item
        for item in _load_mappings()["mappings"]
        if alias_filter is None or item["alias"] == alias_filter
    ]
    items.sort(key=lambda item: item["path"])
    if as_json:
        _json_echo(items)
    elif not items:
        click.echo("No matching directory mappings.")
    else:
        _tabulate(
            ("DIRECTORY", "PROFILE", "REAL PATH"),
            [(item["path"], item["alias"], item["real_path"]) for item in items],
        )


@cli.command("resolve")
@click.argument(
    "path", required=False, type=click.Path(path_type=Path), default=Path(".")
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def cmd_resolve(path: Path, as_json: bool) -> None:
    """Explain every directory rule that participates in profile selection."""
    display, real = _canonical_dir(path)
    data = _load_mappings()
    matches = [
        item for item in data["mappings"] if _relative_to(real, Path(item["real_path"]))
    ]
    matches.sort(key=lambda item: len(Path(item["real_path"]).parts))
    context = _repo_context(real)
    result = {
        "directory": str(display),
        "real_path": str(real),
        "matches": matches,
        "selected": matches[-1] if matches else None,
        "repository": (
            {
                "git_dir": str(context["git_dir"]),
                "top_level": str(context["top_level"])
                if context and context["top_level"]
                else None,
                "bare": context["bare"],
            }
            if context
            else None
        ),
    }
    if as_json:
        _json_echo(result)
        return
    click.echo(f"Directory: {display}")
    if not matches:
        click.echo("No matching directory rules.")
    else:
        for index, mapping in enumerate(matches, start=1):
            click.echo(
                f"{index}. {mapping['path']} -> {mapping['alias']} "
                f"({mapping.get('scope', 'tree')})"
            )
        click.echo(f"Selected: {matches[-1]['alias']}")
    if context:
        click.echo(f"Git dir:  {context['git_dir']}")
        if context["top_level"]:
            click.echo(f"Worktree: {context['top_level']}")


@cli.command("test")
@click.argument("alias")
@click.argument("host", required=False, default="github.com")
@click.option("--user", default="git", show_default=True, help="SSH user name.")
@click.option(
    "--connect", is_flag=True, help="Make an actual BatchMode SSH connection."
)
def cmd_test(alias: str, host: str, user: str, connect: bool) -> None:
    """Validate a profile's SSH command, optionally connecting to a host."""
    profile = _load_profile(alias)
    identity = _identity_path(profile)
    if identity is None:
        raise click.ClickException(f"profile '{alias}' has no SSH key")
    _validate_private_key(identity)
    argv = ["ssh", "-i", str(identity)]
    ssh = profile.get("ssh", {})
    if ssh.get("identities_only", True):
        argv.extend(["-o", "IdentitiesOnly=yes"])
    for key, value in sorted(ssh.get("options", {}).items()):
        argv.extend(["-o", f"{key}={value}"])
    target = f"{user}@{host}"
    if connect:
        result = subprocess.run([*argv, "-o", "BatchMode=yes", "-T", target])
        raise SystemExit(result.returncode)
    result = subprocess.run(
        [*argv, "-G", target],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise click.ClickException(result.stderr.strip() or "ssh configuration failed")
    identity_files = [
        line.split(None, 1)[1]
        for line in result.stdout.splitlines()
        if line.lower().startswith("identityfile ")
    ]
    click.echo(f"Profile:     {alias}")
    click.echo(f"Target:      {target}")
    click.echo(f"Fingerprint: {_fingerprint(identity)}")
    click.echo(f"Identity:    {identity_files[0] if identity_files else identity}")
    click.echo("SSH configuration is valid. Use --connect to test authentication.")


@cli.command("use")
@click.argument("alias")
@click.argument(
    "path", required=False, type=click.Path(path_type=Path), default=Path(".")
)
@click.option(
    "--force", is_flag=True, help="Replace an existing mapping on this directory."
)
def cmd_use(alias: str, path: Path, force: bool) -> None:
    """Use a profile for a directory and all repositories below it."""
    _load_profile(alias)
    display, real = _canonical_dir(path)
    context = _repo_context(real)
    scope = "tree"
    git_dirs: list[str] = []
    if context:
        top_level = context["top_level"]
        if top_level is not None and real != top_level:
            raise click.ClickException(
                f"{display} is inside repository {top_level}. Bind the repository "
                "root instead; profiles cannot change between subdirectories of one repository."
            )
        scope = "repository" if context["bare"] else "worktree"
        git_dir = context["git_dir"]
        normal_git_dir = top_level / ".git" if top_level is not None else None
        if context["bare"] or normal_git_dir is None or git_dir != normal_git_dir:
            git_dirs = [str(git_dir)]
    with _locked():
        data = _load_mappings()
        original_data = copy.deepcopy(data)
        existing = next(
            (item for item in data["mappings"] if Path(item["real_path"]) == real),
            None,
        )
        if existing and existing["alias"] != alias and not force:
            raise click.ClickException(
                f"{display} already uses '{existing['alias']}'; pass --force to replace it"
            )
        entry = {
            "path": str(display),
            "real_path": str(real),
            "alias": alias,
            "scope": scope,
            "git_dirs": git_dirs,
            "created_at": existing.get("created_at", _now()) if existing else _now(),
            "updated_at": _now(),
        }
        if existing:
            data["mappings"][data["mappings"].index(existing)] = entry
        else:
            data["mappings"].append(entry)
        _commit_mappings(data, original_data)
    click.echo(f"{display} and its child repositories now use profile '{alias}'.")


@cli.command("reset")
@click.argument(
    "path", required=False, type=click.Path(path_type=Path), default=Path(".")
)
def cmd_reset(path: Path) -> None:
    """Remove the mapping defined exactly on a directory."""
    display, real = _canonical_dir(path)
    with _locked():
        data = _load_mappings()
        original_data = copy.deepcopy(data)
        exact = next(
            (item for item in data["mappings"] if Path(item["real_path"]) == real),
            None,
        )
        if exact is None:
            inherited = _resolve_mapping(real, data)
            if inherited:
                click.echo(
                    f"No mapping is defined directly on {display}. "
                    f"Inherited profile '{inherited['alias']}' from {inherited['path']}."
                )
            else:
                click.echo(f"No mapping is defined on {display}.")
            return
        data["mappings"].remove(exact)
        _commit_mappings(data, original_data)
    click.echo(f"Removed mapping {display} -> {exact['alias']}.")
    inherited = _resolve_mapping(real)
    if inherited:
        click.echo(
            f"Effective profile is now inherited from {inherited['path']}: {inherited['alias']}"
        )


@cli.command("status")
@click.argument(
    "path", required=False, type=click.Path(path_type=Path), default=Path(".")
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def cmd_status(path: Path, as_json: bool) -> None:
    """Compare selected, actual, and expected Git configuration."""
    display, real = _canonical_dir(path)
    mapping = _resolve_mapping(real)
    config = _load_config()
    configured = bool(config.get("installations"))
    active_modes = []
    if _runtime_generated_include():
        active_modes.append("shell")
    if _global_generated_include():
        active_modes.append("global")
    result: dict[str, Any] = {
        "enabled": bool(active_modes),
        "configured": configured,
        "active_modes": active_modes,
        "precedence": (
            "profile"
            if "shell" in active_modes
            else ("repository-local" if "global" in active_modes else None)
        ),
        "installations": config.get("installations", []),
        "directory": str(display),
        "real_path": str(real),
        "mapping": mapping,
        "profile": None,
        "actual": {},
        "expected": {},
        "effective": {},
    }
    if mapping:
        profile = _load_profile(mapping["alias"])
        identity = _identity_path(profile)
        result["profile"] = {
            "alias": profile["alias"],
            "name": profile["name"],
            "email": profile["email"],
            "ssh_key": str(identity) if identity else None,
            "fingerprint": _fingerprint(identity),
            "ssh_key_exists": bool(identity and identity.exists()),
        }
    if _repo_context(real):
        for key in ("user.name", "user.email", "core.sshCommand"):
            actual_value, actual_origin = _git_value(real, key, include_generated=False)
            expected_value, expected_origin = _git_value(
                real, key, include_generated=True
            )
            result["actual"][key] = {
                "value": actual_value,
                "origin": actual_origin,
            }
            result["expected"][key] = {
                "value": expected_value,
                "origin": expected_origin,
            }
        result["effective"] = result["actual"]
    result["matches_expected"] = result["actual"] == result["expected"]
    if as_json:
        _json_echo(result)
        return
    state = (
        "active"
        if result["enabled"]
        else ("configured, not active" if configured else "disabled")
    )
    click.echo(f"git-auth:      {state}")
    if result["precedence"]:
        click.echo(f"Precedence:    {result['precedence']}")
    click.echo(f"Directory:     {display}")
    if not mapping:
        click.echo("Profile:       - (no matching mapping)")
        return
    profile_result = result["profile"]
    click.echo(f"Matched path:  {mapping['path']}")
    click.echo(f"Profile:       {profile_result['alias']}")
    click.echo(f"Name:          {profile_result['name']}")
    click.echo(f"Email:         {profile_result['email']}")
    click.echo(f"SSH key:       {profile_result['ssh_key'] or '-'}")
    click.echo(f"Fingerprint:   {profile_result['fingerprint']}")
    for key, item in result["actual"].items():
        if item["value"] is not None:
            click.echo(f"Actual {key}:   {item['value']} ({item['origin']})")
    if not result["matches_expected"]:
        click.echo(
            "WARNING: actual Git configuration does not match the selected git-auth profile."
        )
        for key, item in result["expected"].items():
            if item["value"] is not None:
                click.echo(f"Expected {key}: {item['value']} ({item['origin']})")


def _runtime_env_for(alias: str) -> dict[str, str]:
    with _locked():
        profile = _load_profile(alias)
        # Ensure a manually edited profile has an up-to-date derived config.
        _atomic_write(_profile_gitconfig(alias), _render_profile(profile))
    env = os.environ.copy()
    try:
        index = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError as exc:
        raise click.ClickException("GIT_CONFIG_COUNT is not an integer") from exc
    env[f"GIT_CONFIG_KEY_{index}"] = "include.path"
    env[f"GIT_CONFIG_VALUE_{index}"] = str(_profile_gitconfig(alias))
    env["GIT_CONFIG_COUNT"] = str(index + 1)
    return env


@cli.command(
    "exec",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("alias")
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
def cmd_exec(alias: str, command: tuple[str, ...]) -> None:
    """Run a command with a specific profile (useful for git clone)."""
    args = list(command)
    if args and args[0] == "--":
        args.pop(0)
    if not args:
        raise click.UsageError("usage: git-auth exec <alias> -- <command> [args...]")
    result = subprocess.run(args, env=_runtime_env_for(alias))
    raise SystemExit(result.returncode)


@cli.command(
    "clone",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("alias")
@click.argument("repository")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def cmd_clone(alias: str, repository: str, args: tuple[str, ...]) -> None:
    """Clone an SSH repository using a specific profile."""
    if shutil.which("git") is None:
        raise click.ClickException("git is not installed")
    result = subprocess.run(
        ["git", "clone", repository, *args], env=_runtime_env_for(alias)
    )
    raise SystemExit(result.returncode)


@cli.command("keygen")
@click.argument("alias")
@click.option(
    "--type",
    "key_type",
    type=click.Choice(("ed25519", "rsa")),
    default="ed25519",
    show_default=True,
)
@click.option(
    "--comment", default=None, help="Public-key comment (default: profile email)."
)
@click.option("--protect", is_flag=True, help="Let ssh-keygen prompt for a passphrase.")
@click.option("--force", is_flag=True, help="Replace an existing managed key.")
def cmd_keygen(
    alias: str, key_type: str, comment: str | None, protect: bool, force: bool
) -> None:
    """Generate a new SSH key inside the profile directory."""
    if shutil.which("ssh-keygen") is None:
        raise click.ClickException("ssh-keygen is not installed")
    with _locked():
        profile = copy.deepcopy(_load_profile(alias))
        identity = _profile_dir(alias) / "identity"
        if identity.exists() and not force:
            raise click.ClickException(
                f"managed SSH key already exists: {identity}; use --force"
            )
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{alias}.keygen.", dir=_root()))
        generated = temp_dir / "identity"
        argv = [
            "ssh-keygen",
            "-q",
            "-t",
            key_type,
            "-f",
            str(generated),
            "-C",
            comment or profile["email"],
        ]
        if not protect:
            argv.extend(["-N", ""])
        try:
            result = subprocess.run(argv)
            if result.returncode:
                raise click.ClickException(
                    "ssh-keygen failed; the existing key was kept"
                )
            profile.setdefault("ssh", {})["identity_file"] = "identity"
            profile["updated_at"] = _now()
            _commit_profile(profile, identity_source=generated)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    click.echo(f"Generated {key_type} key: {identity}")
    click.echo(f"Fingerprint: {_fingerprint(identity)}")
    click.echo(f"Public key: {identity.with_name('identity.pub')}")


@cli.command("sync")
def cmd_sync() -> None:
    """Regenerate all derived Git and shell configuration files."""
    with _locked():
        _sync_unlocked()
    click.echo(f"Regenerated {_generated_path()} and {_shell_dir()}.")


def _doctor_audit() -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    aliases = set(_list_aliases())
    config = _load_config()
    mappings = _load_mappings()
    installation_types = {item.get("type") for item in config.get("installations", [])}
    if {"shell", "global"}.issubset(installation_types):
        issues.append(
            (
                "warning",
                "shell and global modes coexist: terminal Git enforces the profile, "
                "while GUI Git allows repository-local overrides",
            )
        )
    for mapping in mappings["mappings"]:
        if mapping.get("alias") not in aliases:
            issues.append(
                (
                    "error",
                    f"{mapping.get('path')} references missing profile {mapping.get('alias')!r}",
                )
            )
        real_path = Path(mapping.get("real_path", ""))
        if not real_path.exists():
            issues.append(
                ("warning", f"mapped directory no longer exists: {mapping.get('path')}")
            )
        else:
            context = _repo_context(real_path)
            if (
                context
                and context["top_level"] is not None
                and real_path.resolve() != context["top_level"]
            ):
                issues.append(
                    (
                        "error",
                        f"mapping is inside a repository instead of at its root: {mapping.get('path')}",
                    )
                )
        if mapping.get("git_dirs") and mapping.get("scope") not in {
            "repository",
            "worktree",
        }:
            issues.append(
                (
                    "warning",
                    f"legacy exact gitdir metadata is ignored for {mapping.get('path')}; run use again",
                )
            )
    for alias in sorted(aliases):
        profile = _load_profile(alias)
        identity = _identity_path(profile)
        if identity:
            try:
                identity.resolve().relative_to(_profile_dir(alias).resolve())
            except ValueError:
                issues.append(
                    (
                        "error",
                        f"{alias}: SSH key is outside its managed profile: {identity}",
                    )
                )
            if not identity.is_file():
                issues.append(("error", f"{alias}: SSH key is missing: {identity}"))
            else:
                mode = stat.S_IMODE(identity.stat().st_mode)
                if mode & 0o077:
                    issues.append(
                        (
                            "error",
                            f"{alias}: SSH key permissions are {mode:o}, expected 600",
                        )
                    )
                try:
                    _validate_private_key(identity)
                except (click.ClickException, click.BadParameter) as exc:
                    issues.append(("error", f"{alias}: {exc.format_message()}"))
                public_key = identity.with_name("identity.pub")
                if public_key.exists():
                    private_fingerprint = _fingerprint(identity)
                    public_fingerprint = _fingerprint(public_key)
                    if (
                        private_fingerprint not in {"-", "unavailable"}
                        and public_fingerprint != private_fingerprint
                    ):
                        issues.append(
                            (
                                "error",
                                f"{alias}: identity.pub does not match the private key",
                            )
                        )
        rendered_path = _profile_gitconfig(alias)
        rendered_actual = (
            rendered_path.read_text(encoding="utf-8")
            if rendered_path.exists()
            else None
        )
        if rendered_actual != _render_profile(profile):
            issues.append(
                ("warning", f"{alias}: rendered gitconfig is missing or stale")
            )
    expected = _render_generated(mappings)
    actual = (
        _generated_path().read_text(encoding="utf-8")
        if _generated_path().exists()
        else None
    )
    if actual != expected:
        issues.append(("warning", "generated.gitconfig is missing or stale"))
    for entry in config.get("installations", []):
        if entry.get("type") == "shell":
            path = Path(entry["file"])
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            helper = str(_shell_dir() / f"git-auth.{entry.get('shell')}")
            if (
                content.count(BEGIN_MARKER) != 1
                or content.count(END_MARKER) != 1
                or helper not in content
            ):
                issues.append(
                    ("warning", f"shell integration is missing or stale in {path}")
                )
        elif entry.get("type") == "global" and not _global_generated_include():
            issues.append(
                ("warning", "Git global include is recorded but not installed")
            )
    if shutil.which("git"):
        for alias in sorted(aliases):
            result = subprocess.run(
                ["git", "config", "--file", str(_profile_gitconfig(alias)), "--list"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode:
                issues.append(
                    (
                        "error",
                        f"{alias}: invalid generated gitconfig: {result.stderr.strip()}",
                    )
                )
    else:
        issues.append(
            ("warning", "git is not installed; generated config was not parsed")
        )
    return issues


def _doctor_fix() -> None:
    config = _load_config()
    config_changed = False
    for alias in _list_aliases():
        identity = _identity_path(_load_profile(alias))
        if identity and identity.is_file():
            with contextlib.suppress(OSError):
                os.chmod(identity, 0o600)
            public_key = identity.with_name("identity.pub")
            if public_key.exists() and _fingerprint(public_key) != _fingerprint(
                identity
            ):
                result = subprocess.run(
                    ["ssh-keygen", "-y", "-P", "", "-f", str(identity)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode == 0 and result.stdout.strip():
                    _atomic_write(public_key, result.stdout.strip() + "\n", 0o644)
    _sync_unlocked()
    for entry in config.get("installations", []):
        if entry.get("type") == "shell":
            _install_shell_block(Path(entry["file"]), entry["shell"])
        elif entry.get("type") == "global" and not _global_generated_include():
            if shutil.which("git") is None:
                continue
            result = subprocess.run(
                [
                    "git",
                    "config",
                    "--global",
                    "--add",
                    "include.path",
                    str(_generated_path()),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode:
                raise click.ClickException(
                    result.stderr.strip() or "failed to restore Git global include"
                )
            entry["added"] = True
            config_changed = True
    if config_changed:
        _write_json(_config_path(), config)


@cli.command("doctor")
@click.option(
    "--fix", is_flag=True, help="Repair safe permission and generated-file issues."
)
def cmd_doctor(fix: bool) -> None:
    """Check profiles, mappings, keys, integration and generated config."""
    if fix:
        initial_issues = _doctor_audit()
        blockers = [
            (level, message)
            for level, message in initial_issues
            if level == "error"
            and "permissions are" not in message
            and "identity.pub does not match" not in message
        ]
        if blockers:
            for level, message in initial_issues:
                click.echo(f"{level.upper()}: {message}")
            click.echo("No changes were made because unfixable errors remain.")
            raise SystemExit(1)
        with _locked():
            _doctor_fix()
    issues = _doctor_audit()
    if issues:
        for level, message in issues:
            click.echo(f"{level.upper()}: {message}")
        if fix:
            click.echo(
                "Repairs were applied; the issues above still require attention."
            )
        if any(level == "error" for level, _ in issues):
            raise SystemExit(1)
    else:
        click.echo(
            "git-auth configuration was repaired and is healthy."
            if fix
            else "git-auth configuration is healthy."
        )


if __name__ == "__main__":
    cli()
