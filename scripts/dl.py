#!/usr/bin/env python3
"""Download large files with resume and verification.

Examples:
  usm dl https://example.com/model.bin
  usm dl https://example.com/model.bin ./weights/ --sha256 <hex>
  usm dl https://example.com/dataset.tar -c 8 --backend aria2c
"""

from __future__ import annotations

import hashlib
import ast
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import Message
from email.utils import collapse_rfc2231_value
from pathlib import Path
from typing import Any, BinaryIO

import click
import requests
from requests import Response, Session
from requests.exceptions import ConnectionError, RequestException, Timeout
from usmo import ui

CHUNK = 1024 * 1024
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
SIDE_VERSION = 1
MAX_RETRY_AFTER = 60.0
MAX_FILENAME = 255


def _safe(text: Any) -> str:
    redacted = ui.redact(text)
    redacted = re.sub(
        r"(Authorization:\s*(?:Bearer|Basic)?\s*)[^\s;]+",
        r"\1***",
        redacted,
        flags=re.I,
    )
    return redacted


class DownloadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        retry_after: float | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after
        self.status_code = status_code


@dataclass
class RemoteInfo:
    url: str
    final_url: str
    filename: str | None
    size: int | None
    etag: str | None
    last_modified: str | None
    accept_ranges: bool


@dataclass
class DownloadOptions:
    connections: int
    resume: bool
    headers: dict[str, str]
    insecure: bool
    timeout: float
    quiet: bool


@dataclass
class Progress:
    total: int | None
    quiet: bool
    label: str
    done: int = 0
    _start: float = field(default_factory=time.monotonic)
    _last: float = field(default=0.0)

    def update(self, n: int) -> None:
        self.done += n
        if self.quiet or ui.console().is_terminal:
            return
        now = time.monotonic()
        if now - self._last < 2 and (self.total is None or self.done < self.total):
            return
        self._last = now
        ui.hint(_progress_line(self.label, self.done, self.total, now - self._start))


def _progress_line(label: str, done: int, total: int | None, elapsed: float) -> str:
    rate = done / elapsed if elapsed > 0 else 0
    if total:
        pct = min(100.0, done * 100 / total)
        eta = (total - done) / rate if rate > 0 else None
        return f"{label}: {ui.human_bytes(done)}/{ui.human_bytes(total)} {pct:.1f}% {ui.human_bytes(rate)}/s eta {ui.human_duration(eta)}"
    return f"{label}: {ui.human_bytes(done)} {ui.human_bytes(rate)}/s"


def _parse_headers(values: tuple[str, ...]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in values:
        if ":" not in raw:
            raise click.BadParameter(f"invalid header {raw!r}: expected 'K: V'")
        key, value = raw.split(":", 1)
        key = key.strip()
        if not key:
            raise click.BadParameter(f"invalid header {raw!r}: empty name")
        headers[key] = value.strip()
    return headers


def _is_transient_exception(exc: RequestException) -> bool:
    return isinstance(exc, (Timeout, ConnectionError))


def _retry_after(response: Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(value)))
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            return min(
                MAX_RETRY_AFTER,
                max(0.0, parsedate_to_datetime(value).timestamp() - time.time()),
            )
        except (TypeError, ValueError, OSError):
            return None


def _request_with_retries(
    session: Session,
    method: str,
    url: str,
    *,
    retries: int,
    quiet: bool,
    **kwargs: Any,
) -> Response:
    attempt = 0
    while True:
        response: Response | None = None
        try:
            response = session.request(method, url, **kwargs)
            if response.status_code in TRANSIENT_STATUSES:
                raise DownloadError(
                    f"HTTP {response.status_code} from {_safe(url)}",
                    transient=True,
                    retry_after=_retry_after(response),
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                raise DownloadError(
                    f"HTTP {response.status_code} from {_safe(url)}",
                    status_code=response.status_code,
                )
            return response
        except DownloadError as exc:
            if not exc.transient or attempt >= retries:
                if response is not None:
                    response.close()
                raise
            delay = (
                exc.retry_after
                if exc.retry_after is not None
                else min(60.0, 2.0**attempt)
            )
            if not quiet:
                ui.warn(f"{exc}; retrying in {ui.human_duration(delay)}")
            if response is not None:
                response.close()
        except RequestException as exc:
            if not _is_transient_exception(exc) or attempt >= retries:
                raise DownloadError(
                    f"request failed for {_safe(url)}: {_safe(exc)}",
                    transient=_is_transient_exception(exc),
                ) from exc
            delay = min(60.0, 2.0**attempt)
            if not quiet:
                ui.warn(
                    f"request failed for {_safe(url)}; retrying in {ui.human_duration(delay)}"
                )
        attempt += 1
        time.sleep(delay)


def _content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    star = re.search(r"filename\*\s*=\s*(?:\"([^\"]+)\"|([^;]+))", value, re.I)
    if star:
        text = (star.group(1) or star.group(2)).strip()
        if "''" in text:
            _charset, _lang, encoded = text.split("'", 2)
            return urllib.parse.unquote(encoded)
        return urllib.parse.unquote(text)
    msg = Message()
    msg["content-disposition"] = value
    params = msg.get_params(header="content-disposition", unquote=True) or []
    for key, filename in params[1:]:
        if key.lower() == "filename*":
            if isinstance(filename, tuple) and len(filename) == 3:
                return collapse_rfc2231_value(filename)
            text = str(filename)
            if text.startswith("("):
                try:
                    parsed = ast.literal_eval(text)
                except (SyntaxError, ValueError):
                    parsed = None
                if isinstance(parsed, tuple) and len(parsed) == 3:
                    return collapse_rfc2231_value(parsed)
            if "''" in text:
                text = text.split("''", 1)[1]
            return urllib.parse.unquote(text)
    for key, filename in params[1:]:
        if key.lower() == "filename":
            return str(filename)
    return None


def _sanitize_filename(name: str) -> str:
    name = urllib.parse.unquote(name).strip()
    path = Path(name)
    if (
        path.is_absolute()
        or name in {"", ".", ".."}
        or any(part == ".." for part in path.parts)
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or "\n" in name
        or "\r" in name
        or len(name.encode()) > MAX_FILENAME
    ):
        raise DownloadError(f"unsafe filename from server: {name!r}")
    return name


def _filename_from_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    name = Path(urllib.parse.unquote(parsed.path)).name
    return _sanitize_filename(name or "download")


def resolve_remote(url: str, opts: DownloadOptions, *, retries: int = 0) -> RemoteInfo:
    session = requests.Session()
    response: Response | None = None
    try:
        response = _request_with_retries(
            session,
            "HEAD",
            url,
            retries=retries,
            quiet=opts.quiet,
            headers=opts.headers,
            timeout=opts.timeout,
            verify=not opts.insecure,
            allow_redirects=True,
        )
    except DownloadError as exc:
        if exc.status_code not in {405, 501}:
            raise
        response = _request_with_retries(
            session,
            "GET",
            url,
            retries=retries,
            quiet=opts.quiet,
            headers={**opts.headers, "Range": "bytes=0-0"},
            timeout=opts.timeout,
            verify=not opts.insecure,
            stream=True,
            allow_redirects=True,
        )
    try:
        size = _remote_size(response)
        cd_name = _content_disposition_filename(
            response.headers.get("Content-Disposition")
        )
        filename = (
            _sanitize_filename(cd_name)
            if cd_name is not None
            else _filename_from_url(response.url)
        )
        return RemoteInfo(
            url=url,
            final_url=response.url,
            filename=filename,
            size=size,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            accept_ranges=_supports_ranges(response),
        )
    finally:
        if response is not None:
            response.close()


def _remote_size(response: Response) -> int | None:
    content_range = response.headers.get("Content-Range")
    if content_range and "/" in content_range:
        tail = content_range.rsplit("/", 1)[1]
        if tail.isdigit():
            return int(tail)
    length = response.headers.get("Content-Length")
    if length and length.isdigit():
        return int(length)
    return None


def _supports_ranges(response: Response) -> bool:
    if response.status_code == 206:
        return True
    return response.headers.get("Accept-Ranges", "").lower() == "bytes"


def _target_path(output: str | None, positional: str | None, info: RemoteInfo) -> Path:
    if output and positional and Path(output) != Path(positional):
        raise DownloadError("use either positional OUTPUT or --output, not both")
    chosen = output or positional
    filename = info.filename or _filename_from_url(info.final_url)
    if not chosen:
        return Path(filename)
    path = Path(chosen)
    if str(chosen).endswith(os.sep) or (path.exists() and path.is_dir()):
        return path / filename
    return path


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".json")


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_sidecar(path: Path, info: RemoteInfo) -> None:
    path.write_text(
        json.dumps(
            {
                "version": SIDE_VERSION,
                "url": info.final_url,
                "etag": info.etag,
                "last_modified": info.last_modified,
                "size": info.size,
            },
            sort_keys=True,
        )
    )


def _metadata_changed(meta: dict[str, Any] | None, info: RemoteInfo) -> str | None:
    if not meta:
        return "missing resume metadata"
    old_url = meta.get("url")
    if old_url is not None and old_url != info.final_url:
        return "remote url changed"
    checks = [
        ("etag", info.etag),
        ("last_modified", info.last_modified),
        ("size", info.size),
    ]
    for key, current in checks:
        old = meta.get(key)
        if old is not None and current is not None and old != current:
            return f"remote {key.replace('_', '-')} changed"
    return None


def _content_range_start_total(value: str | None) -> tuple[int, int | None] | None:
    if not value:
        return None
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value.strip())
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    total = None if match.group(3) == "*" else int(match.group(3))
    if end < start:
        return None
    return start, total


def _iter_to_file(response: Response, fh: BinaryIO, progress: Progress) -> None:
    for chunk in response.iter_content(CHUNK):
        if not chunk:
            continue
        fh.write(chunk)
        progress.update(len(chunk))


class DownloadBackend(ABC):
    name: str

    @classmethod
    @abstractmethod
    def available(cls) -> bool: ...  # pragma: no cover

    @abstractmethod
    def download(
        self,
        url: str,
        dest: Path,
        info: RemoteInfo,
        opts: DownloadOptions,
        *,
        retries: int,
    ) -> None: ...  # pragma: no cover


class PythonBackend(DownloadBackend):
    name = "python"

    @classmethod
    def available(cls) -> bool:
        return True

    def download(
        self,
        url: str,
        dest: Path,
        info: RemoteInfo,
        opts: DownloadOptions,
        *,
        retries: int,
    ) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        side = _sidecar(part)
        resume_from = part.stat().st_size if opts.resume and part.exists() else 0
        if resume_from:
            reason = _metadata_changed(_read_sidecar(side), info)
            if reason:
                if not opts.quiet:
                    ui.warn(f"{reason}; restarting {ui.shorten_path(dest)}")
                part.unlink(missing_ok=True)
                side.unlink(missing_ok=True)
                resume_from = 0
        if resume_from and info.size is not None and resume_from > info.size:
            if not opts.quiet:
                ui.warn("partial file is larger than remote; restarting")
            part.unlink(missing_ok=True)
            side.unlink(missing_ok=True)
            resume_from = 0
        if resume_from and info.size is not None and resume_from == info.size:
            if not opts.quiet:
                ui.hint("partial file is already complete; verifying")
            part.replace(dest)
            side.unlink(missing_ok=True)
            return
        session = requests.Session()
        headers = dict(opts.headers)
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
        response = _request_with_retries(
            session,
            "GET",
            url,
            retries=retries,
            quiet=opts.quiet,
            headers=headers,
            timeout=opts.timeout,
            verify=not opts.insecure,
            stream=True,
            allow_redirects=True,
        )
        try:
            mode = "ab" if resume_from and response.status_code == 206 else "wb"
            if resume_from and response.status_code != 206:
                if not opts.quiet:
                    ui.warn("server ignored Range; restarting from zero")
                resume_from = 0
                side.unlink(missing_ok=True)
            elif resume_from:
                parsed = _content_range_start_total(
                    response.headers.get("Content-Range")
                )
                if parsed is None or parsed[0] != resume_from:
                    if not opts.quiet:
                        ui.warn("server returned an invalid Content-Range; restarting")
                    response.close()
                    resume_from = 0
                    mode = "wb"
                    side.unlink(missing_ok=True)
                    response = _request_with_retries(
                        session,
                        "GET",
                        url,
                        retries=retries,
                        quiet=opts.quiet,
                        headers=opts.headers,
                        timeout=opts.timeout,
                        verify=not opts.insecure,
                        stream=True,
                        allow_redirects=True,
                    )
                elif (
                    parsed[1] is not None
                    and info.size is not None
                    and parsed[1] != info.size
                ):
                    raise DownloadError(
                        "server Content-Range total does not match remote size"
                    )
            total = info.size
            progress = Progress(
                total, opts.quiet, ui.shorten_path(dest), done=resume_from
            )
            _write_sidecar(side, info)
            with part.open(mode + "") as fh:
                _iter_to_file(response, fh, progress)
            if total is not None and part.stat().st_size != total:
                raise DownloadError(
                    f"download incomplete: got {ui.human_bytes(part.stat().st_size)}, expected {ui.human_bytes(total)}",
                    transient=True,
                )
            part.replace(dest)
            side.unlink(missing_ok=True)
        finally:
            response.close()


class CurlBackend(DownloadBackend):  # pragma: no cover - external command wrapper
    name = "curl"

    @classmethod
    def available(cls) -> bool:
        return shutil.which("curl") is not None

    def download(
        self,
        url: str,
        dest: Path,
        info: RemoteInfo,
        opts: DownloadOptions,
        *,
        retries: int,
    ) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        argv = [
            "curl",
            "--fail",
            "--location",
            "--retry",
            str(retries),
            "--connect-timeout",
            str(opts.timeout),
            "--max-time",
            str(opts.timeout),
            "--output",
            str(part),
        ]
        if opts.resume:
            argv += ["-C", "-"]
        if opts.insecure:
            argv.append("--insecure")
        if opts.quiet:
            argv.append("--silent")
        for key, value in opts.headers.items():
            argv += ["--header", f"{key}: {value}"]
        argv.append(url)
        _run_backend(argv, url)
        if info.size is not None and part.stat().st_size != info.size:
            raise DownloadError("curl left an incomplete file", transient=True)
        part.replace(dest)


class Aria2cBackend(DownloadBackend):  # pragma: no cover - external command wrapper
    name = "aria2c"

    @classmethod
    def available(cls) -> bool:
        return shutil.which("aria2c") is not None

    def download(
        self,
        url: str,
        dest: Path,
        info: RemoteInfo,
        opts: DownloadOptions,
        *,
        retries: int,
    ) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        argv = [
            "aria2c",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--continue=true",
            "--max-tries",
            str(retries + 1),
            "--connect-timeout",
            str(int(opts.timeout)),
            "--timeout",
            str(int(opts.timeout)),
            "--max-connection-per-server",
            str(opts.connections),
            "--split",
            str(opts.connections),
            "--dir",
            str(part.parent),
            "--out",
            part.name,
        ]
        if opts.insecure:
            argv.append("--check-certificate=false")
        if opts.quiet:
            argv.append("--quiet=true")
        for key, value in opts.headers.items():
            argv.append(f"--header={key}: {value}")
        argv.append(url)
        _run_backend(argv, url)
        if info.size is not None and part.stat().st_size != info.size:
            raise DownloadError("aria2c left an incomplete file", transient=True)
        part.replace(dest)


def _run_backend(argv: list[str], url: str) -> None:  # pragma: no cover
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise DownloadError(f"backend failed for {_safe(url)}: {_safe(exc)}") from exc
    if result.returncode:
        msg = (
            (result.stderr or result.stdout or "download failed")
            .strip()
            .splitlines()[-1]
        )
        raise DownloadError(
            f"backend failed for {_safe(url)}: {_safe(msg)}", transient=True
        )


BACKENDS: dict[str, type[DownloadBackend]] = {
    "aria2c": Aria2cBackend,
    "curl": CurlBackend,
    "python": PythonBackend,
}


def select_backend(name: str) -> DownloadBackend:
    if name != "auto":
        cls = BACKENDS[name]
        if not cls.available():
            raise DownloadError(f"backend {name!r} is not available")
        return cls()
    for cls in (Aria2cBackend, CurlBackend, PythonBackend):
        if cls.available():
            return cls()
    return PythonBackend()


def _hash_file(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _checksum_from_file(
    source: str, filename: str, opts: DownloadOptions, *, retries: int
) -> tuple[str, str] | None:
    if source.startswith(("http://", "https://")):
        response = _request_with_retries(
            requests.Session(),
            "GET",
            source,
            retries=retries,
            quiet=opts.quiet,
            headers=opts.headers,
            timeout=opts.timeout,
            verify=not opts.insecure,
            allow_redirects=True,
        )
        try:
            text = response.text
        finally:
            response.close()
    else:
        text = Path(source).read_text()
    wanted = Path(filename).name
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s+", line, maxsplit=1)
        if len(parts) != 2:
            continue
        digest, name = parts[0].lower(), parts[1].lstrip("*").strip()
        if Path(name).name != wanted:
            continue
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            return ("sha256", digest)
        if re.fullmatch(r"[0-9a-f]{32}", digest):
            return ("md5", digest)
    return None


def _verify(
    dest: Path,
    sha256: str | None,
    md5: str | None,
    checksum_file: str | None,
    opts: DownloadOptions,
    *,
    retries: int,
) -> None:
    checks: list[tuple[str, str]] = []
    if checksum_file:
        found = _checksum_from_file(checksum_file, dest.name, opts, retries=retries)
        if not found:
            raise DownloadError(f"no checksum entry for {dest.name}")
        checks.append(found)
    if sha256:
        checks.append(("sha256", sha256.lower()))
    if md5:
        checks.append(("md5", md5.lower()))
    for algorithm, expected in checks:
        actual = _hash_file(dest, algorithm)
        if actual.lower() != expected.lower():
            bad = dest.with_name(dest.name + ".bad")
            bad.unlink(missing_ok=True)
            dest.replace(bad)
            raise DownloadError(
                f"{algorithm} mismatch for {ui.shorten_path(dest)}; saved corrupt file as {ui.shorten_path(bad)}"
            )


def _describe(info: RemoteInfo, dest: Path, backend: DownloadBackend) -> None:
    ui.print(
        ui.detail(
            [
                ("url", _safe(info.final_url)),
                ("output", ui.shorten_path(dest)),
                ("size", ui.human_bytes(info.size)),
                ("resume", "yes" if info.accept_ranges else "unknown/no"),
                ("etag", info.etag or "-"),
                ("backend", backend.name),
            ]
        )
    )


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Download URL with resume, mirrors, and optional checksums.",
)
@click.argument("url")
@click.argument("output_arg", required=False)
@click.option(
    "-o",
    "--output",
    "output_opt",
    type=click.Path(path_type=str),
    help="Target file or directory.",
)
@click.option(
    "-c",
    "--connections",
    default=1,
    show_default=True,
    type=click.IntRange(1),
    help="Parallel connections for capable backends.",
)
@click.option(
    "--resume/--no-resume",
    default=True,
    show_default=True,
    help="Resume an existing .part file when safe.",
)
@click.option("--sha256", help="Expected SHA-256 digest.")
@click.option("--md5", help="Expected MD5 digest.")
@click.option("--checksum-file", help="Local path or URL containing checksum lines.")
@click.option(
    "--retries",
    default=5,
    show_default=True,
    type=click.IntRange(0),
    help="Retries for transient failures.",
)
@click.option(
    "--header", "headers", multiple=True, help="Extra HTTP header, repeatable: 'K: V'."
)
@click.option("--insecure", is_flag=True, help="Disable TLS certificate verification.")
@click.option(
    "--timeout",
    default=30.0,
    show_default=True,
    type=click.FloatRange(0.1),
    help="Per-request timeout in seconds.",
)
@click.option("--quiet", is_flag=True, help="Suppress progress output.")
@click.option(
    "--mirror", "mirrors", multiple=True, help="Alternative URL to try after failure."
)
@click.option("--dry-run", is_flag=True, help="Resolve metadata but download nothing.")
@click.option(
    "--backend",
    type=click.Choice(["auto", "aria2c", "curl", "python"]),
    default="auto",
    show_default=True,
    help="Downloader backend.",
)
def cli(
    url: str,
    output_arg: str | None,
    output_opt: str | None,
    connections: int,
    resume: bool,
    sha256: str | None,
    md5: str | None,
    checksum_file: str | None,
    retries: int,
    headers: tuple[str, ...],
    insecure: bool,
    timeout: float,
    quiet: bool,
    mirrors: tuple[str, ...],
    dry_run: bool,
    backend: str,
) -> None:
    try:
        opts = DownloadOptions(
            connections, resume, _parse_headers(headers), insecure, timeout, quiet
        )
        selected = select_backend(backend)
        last_error: Exception | None = None
        for candidate in (url, *mirrors):
            try:
                info = resolve_remote(candidate, opts, retries=retries)
                dest = _target_path(output_opt, output_arg, info)
                if dry_run:
                    _describe(info, dest, selected)
                    return
                if not quiet:
                    ui.step(
                        f"downloading {_safe(info.final_url)} → {ui.shorten_path(dest)} with {selected.name}"
                    )
                selected.download(candidate, dest, info, opts, retries=retries)
                _verify(dest, sha256, md5, checksum_file, opts, retries=retries)
                if not quiet:
                    ui.ok(
                        f"downloaded {ui.shorten_path(dest)} ({ui.human_bytes(dest.stat().st_size)})"
                    )
                return
            except DownloadError as exc:
                last_error = exc
                if not quiet and candidate != (url, *mirrors)[-1]:
                    ui.warn(f"{exc}; trying next mirror")
        raise last_error or DownloadError("download failed")
    except (DownloadError, OSError) as exc:
        ui.fail(str(exc))
        raise click.exceptions.Exit(1) from exc


if __name__ == "__main__":  # pragma: no cover
    cli()
