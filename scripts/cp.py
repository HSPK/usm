import datetime
import io
import os
import platform
import shlex
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import click
import yaml

USM_CACHE_DIR = Path.home() / ".cache" / "usm"
LOCAL_BIN_DIR = USM_CACHE_DIR / "bin"

# aka.ms links always redirect to the latest azcopy v10 release asset.
AZCOPY_DOWNLOADS = {
    ("linux", "amd64"): "https://aka.ms/downloadazcopy-v10-linux",
    ("linux", "arm64"): "https://aka.ms/downloadazcopy-v10-linux-arm64",
    ("darwin", "amd64"): "https://aka.ms/downloadazcopy-v10-mac",
    ("darwin", "arm64"): "https://aka.ms/downloadazcopy-v10-mac-arm64",
    ("windows", "amd64"): "https://aka.ms/downloadazcopy-v10-windows",
    ("windows", "arm64"): "https://aka.ms/downloadazcopy-v10-windows-arm64",
}


# Binary install (azcopy) ---------------------------------------------------


def _azcopy_filename() -> str:
    return "azcopy.exe" if platform.system().lower() == "windows" else "azcopy"


def _local_azcopy() -> Path:
    return LOCAL_BIN_DIR / _azcopy_filename()


def _normalize_arch(machine: str) -> str:
    m = machine.lower()
    if m in ("x86_64", "amd64", "x64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m


def _azcopy_download_url() -> str:
    system = platform.system().lower()
    arch = _normalize_arch(platform.machine())
    url = AZCOPY_DOWNLOADS.get((system, arch))
    if not url:
        raise click.ClickException(
            f"no azcopy build mapped for {platform.system()}/{platform.machine()}; "
            "install it manually and set $USM_AZCOPY_BIN. See "
            "https://learn.microsoft.com/azure/storage/common/storage-use-azcopy-v10"
        )
    return url


def _is_azcopy_member(name: str) -> bool:
    return name.rstrip("/").rsplit("/", 1)[-1] in ("azcopy", "azcopy.exe")


def _extract_azcopy_binary(payload: bytes, url: str) -> bytes:
    buf = io.BytesIO(payload)
    if payload[:2] == b"\x1f\x8b":  # gzip -> tar.gz (linux)
        with tarfile.open(fileobj=buf, mode="r:gz") as tf:
            member = next(
                (m for m in tf.getmembers() if _is_azcopy_member(m.name)), None
            )
            if member is None:
                raise click.ClickException("no azcopy binary inside the archive")
            extracted = tf.extractfile(member)
            if extracted is None:
                raise click.ClickException("failed to read azcopy from the archive")
            return extracted.read()
    if payload[:2] == b"PK":  # zip (mac, windows)
        with zipfile.ZipFile(buf) as zf:
            name = next((n for n in zf.namelist() if _is_azcopy_member(n)), None)
            if name is None:
                raise click.ClickException("no azcopy binary inside the archive")
            return zf.read(name)
    raise click.ClickException(f"unexpected azcopy archive format from {url}")


def _download_azcopy(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=180) as r:
            payload = r.read()
    except (urllib.error.URLError, OSError) as e:
        raise click.ClickException(f"azcopy download failed: {url}: {e}") from e
    data = _extract_azcopy_binary(payload, url)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.chmod(0o755)
    tmp.replace(dest)


def _find_azcopy() -> str | None:
    """Locate a usable azcopy without downloading."""
    override = os.environ.get("USM_AZCOPY_BIN")
    if override and os.access(override, os.X_OK):
        return override
    found = shutil.which("azcopy")
    if found:
        return found
    local = _local_azcopy()
    if local.exists():
        return str(local)
    return None


def ensure_azcopy(*, upgrade: bool = False) -> str:
    """Resolve a usable azcopy binary, downloading the pinned-latest release if needed.

    Order: ``$USM_AZCOPY_BIN`` -> azcopy on ``PATH`` -> managed binary in
    ``~/.cache/usm/bin`` -> download into the managed location. ``upgrade=True``
    skips the lookups and always (re)installs the managed binary.
    """
    if not upgrade:
        existing = _find_azcopy()
        if existing:
            return existing
    local = _local_azcopy()
    click.echo(f"Installing azcopy -> {local} ...")
    _download_azcopy(_azcopy_download_url(), local)
    return str(local)


# Azure blob path helpers ---------------------------------------------------


def _is_https_blob(s: str) -> bool:
    if not s.lower().startswith(("http://", "https://")):
        return False
    host = urllib.parse.urlparse(s).netloc.lower()
    return ".blob.core." in host or ".dfs.core." in host


def _has_sas(url: str) -> bool:
    return "sig=" in urllib.parse.urlparse(url).query.lower()


def _parse_blob_url(url: str) -> tuple[str, str]:
    """Pull (account, container) out of an https://<account>.blob.../<container>/... URL."""
    parsed = urllib.parse.urlparse(url)
    account = parsed.netloc.split(":")[0].split(".")[0]
    container = parsed.path.lstrip("/").split("/", 1)[0]
    if not account or not container:
        raise click.ClickException(
            f"cannot parse account/container from blob URL: {url}"
        )
    return account, container


def _config_file_from_cmdline(cmdline: list[str]) -> str | None:
    """Pick the blobfuse2 --config-file value out of a cmdline, in either form."""
    it = iter(enumerate(cmdline))
    for i, tok in it:
        if tok == "--config-file" and i + 1 < len(cmdline):
            return cmdline[i + 1]
        if tok.startswith("--config-file="):
            return tok.split("=", 1)[1]
        if tok in ("-c",) and i + 1 < len(cmdline):
            return cmdline[i + 1]
    return None


def _mount_dir_from_cmdline(cmdline: list[str]) -> str | None:
    """blobfuse2 `mount <DIR> [opts]` — DIR is the first positional after the subcommand."""
    if len(cmdline) < 3:
        return None
    if cmdline[1] != "mount":
        return None
    return cmdline[2]


def check_blobfuse2_mountpoints():
    import psutil

    mountpoints = {}
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info["cmdline"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not cmdline or "blobfuse2" not in cmdline[0]:
            continue
        try:
            mount_dir = _mount_dir_from_cmdline(cmdline)
            config_file = _config_file_from_cmdline(cmdline)
            if not mount_dir or not config_file:
                continue
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
            azstorage = (
                (config.get("azstorage") or {}) if isinstance(config, dict) else {}
            )
            account_name = azstorage.get("account-name")
            container_name = azstorage.get("container")
            if not account_name or not container_name:
                continue
            mountpoints[mount_dir] = {
                "url": f"https://{account_name}.blob.core.windows.net/{container_name}/",
                "account_name": account_name,
                "container_name": container_name,
            }
        except (OSError, yaml.YAMLError, KeyError, IndexError):
            continue
    return mountpoints


def generate_sas_token(account_name, container_name, expiry_days: int = 7):
    expiry_date = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=expiry_days)
    ).strftime("%Y-%m-%dT%H:%MZ")
    command = [
        "az",
        "storage",
        "container",
        "generate-sas",
        "--account-name",
        account_name,
        "--name",
        container_name,
        "--permissions",
        "rwdlac",
        "--expiry",
        expiry_date,
        "--auth-mode",
        "login",
        "--as-user",
    ]

    result = subprocess.run(command, capture_output=True, text=True, check=True)
    if result.returncode == 0:
        sas_token = result.stdout.strip().strip('"')
        return sas_token
    else:
        raise Exception(f"Failed to generate SAS token: {result.stderr}")


@click.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    )
)
@click.argument("args", nargs=-1, type=str)
@click.option(
    "--use-az",
    is_flag=True,
    default=False,
    help="Authenticate azcopy via Azure CLI login instead of generating SAS tokens.",
)
@click.option(
    "--dry-run",
    "-d",
    is_flag=True,
    default=False,
    help="Only print the commands that would be executed, without actually running them.",
)
@click.option(
    "--install",
    is_flag=True,
    default=False,
    help="Download and install the azcopy binary (linux/mac/windows), then exit.",
)
def copy(
    args,
    use_az: bool = False,
    dry_run: bool = False,
    install: bool = False,
):
    if install:
        path = ensure_azcopy(upgrade=True)
        version = ""
        try:
            out = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=30
            )
            version = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        click.echo(f"azcopy installed at {path}" + (f" ({version})" if version else ""))
        return

    def run_cmd(cmd):
        if dry_run:
            click.echo(shlex.join(str(c) for c in cmd))
        else:
            subprocess.run(cmd)

    if len(args) < 2:
        click.echo("Usage: usm cp [SOURCE] [DESTINATION]")
        return

    mountpoints = check_blobfuse2_mountpoints()

    def match_mount(resolved: str) -> str | None:
        for mp in mountpoints:
            base = mp.rstrip("/")
            if resolved == base or resolved.startswith(base + "/"):
                return mp
        return None

    def classify(arg: str):
        # (kind, value[, mountpoint]) where kind is "https" | "blobfuse" | "local".
        if _is_https_blob(arg):
            return ("https", arg)
        resolved = str(Path(arg).resolve())
        mp = match_mount(resolved)
        if mp is not None:
            return ("blobfuse", resolved, mp)
        return ("local", resolved)

    def is_blob(item) -> bool:
        return item[0] in ("https", "blobfuse")

    def to_azcopy_url(item) -> str:
        kind = item[0]
        if kind == "https":
            url = item[1]
            if not use_az and not _has_sas(url):
                account, container = _parse_blob_url(url)
                sas = generate_sas_token(account, container)
                url += ("&" if urllib.parse.urlparse(url).query else "?") + sas
            return url
        if kind == "blobfuse":
            resolved, mp = item[1], item[2]
            info = mountpoints[mp]
            relative_path = resolved[len(mp.rstrip("/")) :].lstrip("/")
            url = info["url"] + urllib.parse.quote(relative_path, safe="/")
            if not use_az:
                sas = generate_sas_token(info["account_name"], info["container_name"])
                url += "?" + sas
            return url
        return item[1]  # local resolved path

    items = [classify(a) for a in args]

    if not any(is_blob(it) for it in items):
        click.echo(
            "No Azure blob paths detected in the provided paths. Handing over to native cp."
        )
        run_cmd(["cp", "-r"] + list(args))
        return

    # A blob path is involved: make sure azcopy is available (auto-install).
    if dry_run:
        azcopy = _find_azcopy() or "azcopy"
    else:
        azcopy = ensure_azcopy()
    if use_az:
        os.environ["AZCOPY_AUTO_LOGIN_TYPE"] = "AZCLI"

    sources = items[:-1]
    destination = items[-1]

    if is_blob(destination):
        click.echo("Copying files using azcopy...")
        for src in sources:
            run_cmd(
                [
                    azcopy,
                    "copy",
                    to_azcopy_url(src),
                    to_azcopy_url(destination),
                    "--recursive",
                ]
            )
    else:
        for src in sources:
            if is_blob(src):
                click.echo(
                    f"Copying from blob {src[1]} to local path {destination[1]} using azcopy..."
                )
                run_cmd(
                    [
                        azcopy,
                        "copy",
                        to_azcopy_url(src),
                        destination[1],
                        "--recursive",
                    ]
                )
            else:
                run_cmd(["cp", "-r", src[1], destination[1]])


if __name__ == "__main__":
    copy()
