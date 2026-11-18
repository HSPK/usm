"""Discover blobfuse2 storage targets without retaining mount credentials."""

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def _option(cmdline: list[str], *names: str) -> str | None:
    value = None
    for i, token in enumerate(cmdline):
        if token in names and i + 1 < len(cmdline):
            value = cmdline[i + 1]
        elif any(token.startswith(name + "=") for name in names):
            value = token.split("=", 1)[1]
    return value


def blobfuse_mounts() -> dict:
    """Read each mount's CLI, environment and YAML settings, in that order."""
    try:
        import psutil
        import yaml
    except ImportError:
        return {}

    mounts = {}
    for proc in psutil.process_iter(["cmdline", "pid"]):
        try:
            info = proc.info
            cmdline = info["cmdline"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if (
            not cmdline
            or "blobfuse2" not in cmdline[0]
            or len(cmdline) < 3
            or cmdline[1] != "mount"
        ):
            continue

        config_file = _option(cmdline, "--config-file", "-c")
        storage = {}
        if config_file:
            try:
                path = Path(config_file)
                if not path.is_absolute():
                    path = Path(proc.cwd()) / path
                with path.open() as fh:
                    config = yaml.safe_load(fh)
                if isinstance(config, dict) and isinstance(
                    config.get("azstorage"), dict
                ):
                    storage = config["azstorage"]
            except (OSError, yaml.YAMLError, psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            env = proc.environ()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            env = {}
        account = env.get("AZURE_STORAGE_ACCOUNT") or storage.get("account-name")
        container = (
            _option(cmdline, "--container-name")
            or env.get("AZURE_STORAGE_ACCOUNT_CONTAINER")
            or storage.get("container")
        )
        if not isinstance(account, str) or not account:
            continue
        if not isinstance(container, str) or not container:
            continue
        endpoint = env.get("AZURE_STORAGE_BLOB_ENDPOINT") or storage.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            endpoint = f"https://{account}.blob.core.windows.net"
        parsed = urlsplit(endpoint)
        endpoint = urlunsplit(parsed._replace(query="", fragment="")).rstrip("/")
        mounts[cmdline[2]] = {
            "url": f"{endpoint}/{container}/",
            "account_name": account,
            "container_name": container,
            "config_file": config_file,
            "pid": info.get("pid"),
        }
    return mounts
