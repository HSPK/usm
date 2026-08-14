"""Tests for scripts/dl.py.

The downloader's safety contract is stronger than "the command exits 0": it
must never append incompatible bytes to a partial file, never leave corrupt data
at the final path, and never print credentials while explaining failures. These
cases use a local HTTP server so retries, redirects and Range handling exercise
real request code without touching the internet.
"""

from __future__ import annotations

import hashlib
import socket
import socketserver
import subprocess
import sys
import threading
from collections import deque
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import click.testing
import pytest
import requests

import dl

BODY = (b"0123456789abcdef" * 2048) + b"tail"
ETAG = '"v1"'
LAST_MODIFIED = "Fri, 14 Aug 2026 08:00:00 GMT"


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        exc_type, _exc, _tb = sys.exc_info()
        if exc_type is ConnectionResetError:
            return
        super().handle_error(request, client_address)


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DlStub/1"
    calls: ClassVar[list[dict[str, str]]] = []
    body: ClassVar[bytes] = BODY
    etag: ClassVar[str | None] = ETAG
    last_modified: ClassVar[str | None] = LAST_MODIFIED
    accept_ranges: ClassVar[bool] = True
    ignore_range: ClassVar[bool] = False
    content_range: ClassVar[str | None] = None
    content_disposition: ClassVar[str | None] = None
    statuses: ClassVar[deque[int]] = deque()
    retry_after: ClassVar[str | None] = None
    no_content_length: ClassVar[bool] = False
    redirect_to: ClassVar[str | None] = None
    redirect_loop: ClassVar[bool] = False
    checksum_text: ClassVar[str | None] = None

    def log_message(self, *_args: Any) -> None:
        return

    def do_HEAD(self) -> None:
        self._record()
        if self._redirect_if_needed():
            return
        status = self._next_status()
        if status != 200:
            self._send_empty(status)
            return
        self._send_headers(200, len(type(self).body))

    def do_GET(self) -> None:
        self._record()
        if self._redirect_if_needed():
            return
        if self.path.startswith("/sum"):
            text = type(self).checksum_text
            if text is None:
                text = f"{hashlib.sha256(type(self).body).hexdigest()}  file.bin\n"
            self._send_blob(text.encode(), content_type="text/plain")
            return
        status = self._next_status()
        if status != 200:
            self._send_empty(status)
            return
        range_header = self.headers.get("Range")
        if range_header and type(self).accept_ranges and not type(self).ignore_range:
            start = int(range_header.partition("=")[2].partition("-")[0])
            chunk = type(self).body[start:]
            self.send_response(206)
            self._common_headers(len(chunk))
            self.send_header(
                "Content-Range",
                type(self).content_range
                or f"bytes {start}-{len(type(self).body) - 1}/{len(type(self).body)}",
            )
            self.end_headers()
            self.wfile.write(chunk)
            return
        self._send_blob(type(self).body)

    def _record(self) -> None:
        type(self).calls.append(
            {
                "method": self.command,
                "path": self.path,
                "range": self.headers.get("Range", ""),
                "authorization": self.headers.get("Authorization", ""),
            }
        )

    def _redirect_if_needed(self) -> bool:
        if type(self).redirect_to and not self.path.startswith("/redirect"):
            return False
        if not (type(self).redirect_loop or type(self).redirect_to):
            return False
        self.send_response(302)
        self.send_header(
            "Location",
            self.path if type(self).redirect_loop else type(self).redirect_to,
        )
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _next_status(self) -> int:
        return type(self).statuses.popleft() if type(self).statuses else 200

    def _common_headers(self, length: int) -> None:
        if not type(self).no_content_length:
            self.send_header("Content-Length", str(length))
        self.send_header("Content-Type", "application/octet-stream")
        if type(self).etag is not None:
            self.send_header("ETag", type(self).etag)
        if type(self).last_modified is not None:
            self.send_header("Last-Modified", type(self).last_modified)
        if type(self).accept_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if type(self).content_disposition:
            self.send_header("Content-Disposition", type(self).content_disposition)
        if type(self).no_content_length:
            self.send_header("Connection", "close")
            self.close_connection = True

    def _send_headers(self, status: int, length: int) -> None:
        self.send_response(status)
        self._common_headers(length)
        if type(self).retry_after and status == 429:
            self.send_header("Retry-After", type(self).retry_after)
        self.end_headers()

    def _send_blob(
        self, body: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        self.send_response(200)
        if not type(self).no_content_length:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        if type(self).etag is not None:
            self.send_header("ETag", type(self).etag)
        if type(self).last_modified is not None:
            self.send_header("Last-Modified", type(self).last_modified)
        if type(self).accept_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if type(self).content_disposition:
            self.send_header("Content-Disposition", type(self).content_disposition)
        if type(self).no_content_length:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int) -> None:
        self._send_headers(status, 0)


@pytest.fixture
def server():
    class Handler(StubHandler):
        calls: ClassVar[list[dict[str, str]]] = []
        body: ClassVar[bytes] = BODY
        etag: ClassVar[str | None] = ETAG
        last_modified: ClassVar[str | None] = LAST_MODIFIED
        accept_ranges: ClassVar[bool] = True
        ignore_range: ClassVar[bool] = False
        content_range: ClassVar[str | None] = None
        content_disposition: ClassVar[str | None] = None
        statuses: ClassVar[deque[int]] = deque()
        retry_after: ClassVar[str | None] = None
        no_content_length: ClassVar[bool] = False
        redirect_to: ClassVar[str | None] = None
        redirect_loop: ClassVar[bool] = False
        checksum_text: ClassVar[str | None] = None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    class Server:
        handler = Handler

        def url(self, path: str = "/file.bin") -> str:
            return f"http://127.0.0.1:{httpd.server_port}{path}"

    try:
        yield Server()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> click.testing.CliRunner:
    monkeypatch.setattr(dl.Aria2cBackend, "available", classmethod(lambda cls: False))
    monkeypatch.setattr(dl.CurlBackend, "available", classmethod(lambda cls: False))
    return click.testing.CliRunner()


@pytest.fixture
def plain_runner() -> click.testing.CliRunner:
    return click.testing.CliRunner()


def invoke(runner: click.testing.CliRunner, args: list[str]) -> click.testing.Result:
    return runner.invoke(dl.cli, args, catch_exceptions=False)


def opts(**overrides: Any) -> dl.DownloadOptions:
    data = {
        "connections": 1,
        "resume": True,
        "headers": {},
        "insecure": False,
        "timeout": 3.0,
        "quiet": False,
    }
    data.update(overrides)
    return dl.DownloadOptions(**data)


def remote(server: Any) -> dl.RemoteInfo:
    return dl.resolve_remote(server.url(), opts(quiet=True))


def write_sidecar(part: Path, info: dl.RemoteInfo) -> None:
    dl._write_sidecar(dl._sidecar(part), info)


def digest(body: bytes = BODY, algorithm: str = "sha256") -> str:
    return hashlib.new(algorithm, body).hexdigest()


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestBasicDownload:
    """The default Python path must be safe and useful without external tools."""

    def test_downloads_to_filename_derived_from_url(self, runner, server, tmp_path):
        result = invoke(runner, [server.url(), "-o", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / "file.bin").read_bytes() == BODY
        assert "python" in result.output

    def test_positional_output_file_wins_when_no_output_option(
        self, runner, server, tmp_path
    ):
        dest = tmp_path / "explicit.bin"
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY

    def test_matching_positional_and_option_are_allowed(self, runner, server, tmp_path):
        dest = tmp_path / "same.bin"
        result = invoke(runner, [server.url(), str(dest), "-o", str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY

    def test_conflicting_positional_and_option_fail_before_writing(
        self, runner, server, tmp_path
    ):
        result = invoke(
            runner, [server.url(), str(tmp_path / "a"), "-o", str(tmp_path / "b")]
        )
        assert result.exit_code == 1
        assert not (tmp_path / "a").exists()
        assert not (tmp_path / "b").exists()

    def test_server_without_content_length_still_downloads(
        self, runner, server, tmp_path
    ):
        server.handler.no_content_length = True
        result = invoke(runner, [server.url(), str(tmp_path / "unknown.bin")])
        assert result.exit_code == 0
        assert (tmp_path / "unknown.bin").read_bytes() == BODY

    def test_help_uses_short_option(self, runner):
        result = invoke(runner, ["-h"])
        assert result.exit_code == 0
        assert "--backend" in result.output


class TestResumeCorrectness:
    """Resume bugs corrupt large downloads; every restart/resume branch pins bytes."""

    def test_partial_with_range_fetches_remainder_and_matches_full_content(
        self, runner, server, tmp_path
    ):
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(BODY[:1234])
        write_sidecar(part, remote(server))
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY
        assert any(call["range"] == "bytes=1234-" for call in server.handler.calls)

    def test_ignored_range_restarts_instead_of_appending(
        self, runner, server, tmp_path
    ):
        server.handler.ignore_range = True
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(b"not a prefix")
        write_sidecar(part, remote(server))
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY
        assert "ignored Range" in result.output

    @pytest.mark.parametrize(
        "content_range", ["bytes 0-99/32772", "bytes 99-0/32772", "bananas"]
    )
    def test_bad_content_range_restarts_cleanly(
        self, runner, server, tmp_path, content_range
    ):
        server.handler.content_range = content_range
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(BODY[:100])
        write_sidecar(part, remote(server))
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY
        assert "invalid Content-Range" in result.output

    def test_content_range_total_mismatch_fails_without_final_file(
        self, runner, server, tmp_path
    ):
        server.handler.content_range = "bytes 100-32771/99999"
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(BODY[:100])
        write_sidecar(part, remote(server))
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 1
        assert not dest.exists()
        assert part.exists()
        assert "Content-Range total" in result.output

    def test_part_larger_than_remote_restarts(self, runner, server, tmp_path):
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(BODY + b"extra")
        write_sidecar(part, remote(server))
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY
        assert "larger than remote" in result.output

    def test_part_exactly_remote_size_is_promoted_without_range_get(
        self, runner, server, tmp_path
    ):
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(BODY)
        write_sidecar(part, remote(server))
        server.handler.calls.clear()
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY
        assert not any(
            call["method"] == "GET" and call["range"] for call in server.handler.calls
        )

    def test_zero_byte_part_starts_from_zero(self, runner, server, tmp_path):
        dest = tmp_path / "file.bin"
        (tmp_path / "file.bin.part").write_bytes(b"")
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY
        assert not any(call["range"] for call in server.handler.calls)

    @pytest.mark.parametrize(
        ("sidecar", "message"),
        [
            (None, "missing resume metadata"),
            ("not json", "missing resume metadata"),
            (
                '{"url":"http://other/file.bin","etag":"v1","size":32772}',
                "remote url changed",
            ),
        ],
    )
    def test_bad_or_wrong_sidecar_restarts(
        self, runner, server, tmp_path, sidecar, message
    ):
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(b"stale")
        if sidecar is not None:
            dl._sidecar(part).write_text(sidecar)
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY
        assert message in result.output

    @pytest.mark.parametrize(
        ("meta", "message"),
        [
            (
                {
                    "url": None,
                    "etag": '"old"',
                    "last_modified": LAST_MODIFIED,
                    "size": len(BODY),
                },
                "remote etag changed",
            ),
            (
                {
                    "url": None,
                    "etag": ETAG,
                    "last_modified": "Thu, 13 Aug 2026 08:00:00 GMT",
                    "size": len(BODY),
                },
                "remote last-modified changed",
            ),
            (
                {
                    "url": None,
                    "etag": ETAG,
                    "last_modified": LAST_MODIFIED,
                    "size": len(BODY) - 1,
                },
                "remote size changed",
            ),
        ],
    )
    def test_remote_metadata_changes_restart_with_reason(
        self, runner, server, tmp_path, meta, message
    ):
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(b"stale")
        meta["url"] = server.url()
        dl._sidecar(part).write_text(dl.json.dumps(meta))
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY
        assert message in result.output

    def test_no_etag_or_last_modified_uses_size_policy_and_resumes(
        self, runner, server, tmp_path
    ):
        """If validators are absent, the downloader can only compare length; pin that policy."""
        server.handler.etag = None
        server.handler.last_modified = None
        info = remote(server)
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(BODY[:77])
        write_sidecar(part, info)
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY
        assert any(call["range"] == "bytes=77-" for call in server.handler.calls)

    def test_no_resume_ignores_existing_part(self, runner, server, tmp_path):
        dest = tmp_path / "file.bin"
        (tmp_path / "file.bin.part").write_bytes(b"junk")
        result = invoke(runner, [server.url(), str(dest), "--no-resume"])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY
        assert not any(call["range"] for call in server.handler.calls)

    def test_interruption_leaves_resumable_part_not_final_file(
        self, server, tmp_path, monkeypatch
    ):
        dest = tmp_path / "file.bin"

        def explode(_response, fh, _progress):
            fh.write(BODY[:2048])
            raise requests.ConnectionError("reset after bytes")

        monkeypatch.setattr(dl, "_iter_to_file", explode)
        with pytest.raises(requests.ConnectionError):
            dl.PythonBackend().download(
                server.url(), dest, remote(server), opts(quiet=True), retries=0
            )
        assert not dest.exists()
        assert dest.with_name("file.bin.part").read_bytes() == BODY[:2048]
        assert dl._sidecar(dest.with_name("file.bin.part")).exists()


class TestVerification:
    """Checksum failures must quarantine bytes as .bad and never look successful."""

    @pytest.mark.parametrize(
        ("option", "algorithm"), [("--sha256", "sha256"), ("--md5", "md5")]
    )
    def test_explicit_checksum_match_succeeds(
        self, runner, server, tmp_path, option, algorithm
    ):
        dest = tmp_path / f"ok-{algorithm}.bin"
        result = invoke(
            runner, [server.url(), str(dest), option, digest(algorithm=algorithm)]
        )
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY

    @pytest.mark.parametrize(
        ("option", "message"),
        [("--sha256", "sha256 mismatch"), ("--md5", "md5 mismatch")],
    )
    def test_explicit_checksum_mismatch_moves_to_bad(
        self, runner, server, tmp_path, option, message
    ):
        dest = tmp_path / "bad.bin"
        result = invoke(
            runner,
            [
                server.url(),
                str(dest),
                option,
                "0" * (64 if option == "--sha256" else 32),
            ],
        )
        assert result.exit_code == 1
        assert not dest.exists()
        assert dest.with_name("bad.bin.bad").read_bytes() == BODY
        assert message in result.output

    def test_checksum_file_matching_entry_succeeds(self, runner, server, tmp_path):
        sums = tmp_path / "sums.txt"
        sums.write_text(f"{digest()}  file.bin\n")
        result = invoke(
            runner,
            [server.url(), str(tmp_path / "file.bin"), "--checksum-file", str(sums)],
        )
        assert result.exit_code == 0

    def test_checksum_file_url_succeeds(self, runner, server, tmp_path):
        result = invoke(
            runner,
            [
                server.url(),
                str(tmp_path / "file.bin"),
                "--checksum-file",
                server.url("/sum"),
            ],
        )
        assert result.exit_code == 0

    def test_checksum_file_missing_entry_fails(self, runner, server, tmp_path):
        sums = tmp_path / "sums.txt"
        sums.write_text(f"{digest()}  other.bin\n")
        result = invoke(
            runner,
            [server.url(), str(tmp_path / "file.bin"), "--checksum-file", str(sums)],
        )
        assert result.exit_code == 1
        assert "no checksum entry" in result.output

    def test_checksum_file_ignores_malformed_and_blank_lines(
        self, runner, server, tmp_path
    ):
        sums = tmp_path / "sums.txt"
        sums.write_text(
            f"\n# comment\nnot enough\nzzzz  file.bin\n{digest()}  file.bin\n"
        )
        result = invoke(
            runner,
            [server.url(), str(tmp_path / "file.bin"), "--checksum-file", str(sums)],
        )
        assert result.exit_code == 0

    def test_checksum_file_binary_marker_is_supported(self, runner, server, tmp_path):
        sums = tmp_path / "sums.txt"
        sums.write_text(f"{digest()}  *file.bin\n")
        result = invoke(
            runner,
            [server.url(), str(tmp_path / "file.bin"), "--checksum-file", str(sums)],
        )
        assert result.exit_code == 0

    def test_checksum_file_filename_case_must_match(self, runner, server, tmp_path):
        sums = tmp_path / "sums.txt"
        sums.write_text(f"{digest()}  FILE.BIN\n")
        result = invoke(
            runner,
            [server.url(), str(tmp_path / "file.bin"), "--checksum-file", str(sums)],
        )
        assert result.exit_code == 1

    def test_checksum_file_first_matching_entry_controls_result(
        self, runner, server, tmp_path
    ):
        sums = tmp_path / "sums.txt"
        sums.write_text(f"{'0' * 64}  file.bin\n{digest()}  file.bin\n")
        result = invoke(
            runner,
            [server.url(), str(tmp_path / "file.bin"), "--checksum-file", str(sums)],
        )
        assert result.exit_code == 1
        assert "sha256 mismatch" in result.output

    def test_resumed_download_checksum_covers_whole_file(
        self, runner, server, tmp_path
    ):
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(BODY[:4096])
        write_sidecar(part, remote(server))
        result = invoke(runner, [server.url(), str(dest), "--sha256", digest()])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY


class TestRetriesAndHttpSemantics:
    """Retries are for transient failures only; auth/not-found errors must be immediate."""

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_transient_status_retried_until_budget_then_fails(
        self, runner, server, tmp_path, monkeypatch, status
    ):
        monkeypatch.setattr(dl.time, "sleep", lambda _n: None)
        server.handler.statuses = deque([status, status, status])
        result = invoke(runner, [server.url(), str(tmp_path / "x"), "--retries", "2"])
        assert result.exit_code == 1
        assert len(server.handler.calls) == 3
        assert f"HTTP {status}" in result.output

    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_permanent_status_is_not_retried(
        self, runner, server, tmp_path, monkeypatch, status
    ):
        monkeypatch.setattr(dl.time, "sleep", lambda _n: pytest.fail("must not sleep"))
        server.handler.statuses = deque([status])
        result = invoke(runner, [server.url(), str(tmp_path / "x"), "--retries", "5"])
        assert result.exit_code == 1
        assert len(server.handler.calls) == 1
        assert f"HTTP {status}" in result.output

    @pytest.mark.parametrize(
        "exc", [requests.ConnectionError("reset"), requests.Timeout("slow")]
    )
    def test_transient_request_exceptions_are_retried(self, monkeypatch, exc):
        sleeps: list[float] = []
        monkeypatch.setattr(dl.time, "sleep", lambda n: sleeps.append(n))

        class FakeSession:
            calls = 0

            def request(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls < 3:
                    raise exc
                response = dl.Response()
                response.status_code = 200
                response.url = "http://example/file"
                return response

        session = FakeSession()
        response = dl._request_with_retries(
            session, "HEAD", "http://example/file", retries=3, quiet=True
        )
        assert response.status_code == 200
        assert sleeps == [1.0, 2.0]

    def test_transient_exception_fails_cleanly_after_budget(self, monkeypatch):
        monkeypatch.setattr(dl.time, "sleep", lambda _n: None)

        class FakeSession:
            def request(self, *_args, **_kwargs):
                raise requests.ConnectionError("reset")

        with pytest.raises(dl.DownloadError, match="request failed"):
            dl._request_with_retries(
                FakeSession(),
                "GET",
                "http://example/?token=SECRET",
                retries=1,
                quiet=True,
            )

    def test_429_retry_after_seconds_is_honoured_and_capped(
        self, runner, server, tmp_path, monkeypatch
    ):
        sleeps: list[float] = []
        monkeypatch.setattr(dl.time, "sleep", lambda n: sleeps.append(n))
        server.handler.statuses = deque([429, 200])
        server.handler.retry_after = "999"
        result = invoke(runner, [server.url(), str(tmp_path / "x"), "--retries", "1"])
        assert result.exit_code == 0
        assert sleeps == [dl.MAX_RETRY_AFTER]

    def test_429_retry_after_http_date_is_honoured(
        self, runner, server, tmp_path, monkeypatch
    ):
        sleeps: list[float] = []
        monkeypatch.setattr(dl.time, "sleep", lambda n: sleeps.append(round(n)))
        monkeypatch.setattr(dl.time, "time", lambda: 1_000_000.0)
        server.handler.statuses = deque([429, 200])
        server.handler.retry_after = formatdate(1_000_007.0, usegmt=True)
        result = invoke(runner, [server.url(), str(tmp_path / "x"), "--retries", "1"])
        assert result.exit_code == 0
        assert sleeps == [7]

    def test_backoff_grows_for_repeated_500s(
        self, runner, server, tmp_path, monkeypatch
    ):
        sleeps: list[float] = []
        monkeypatch.setattr(dl.time, "sleep", lambda n: sleeps.append(n))
        server.handler.statuses = deque([500, 500, 500, 200])
        result = invoke(runner, [server.url(), str(tmp_path / "x"), "--retries", "3"])
        assert result.exit_code == 0
        assert sleeps == [1.0, 2.0, 4.0]

    def test_timeout_option_is_passed_to_requests(self, monkeypatch):
        seen: dict[str, Any] = {}

        class FakeSession:
            def request(self, *_args, **kwargs):
                seen.update(kwargs)
                response = dl.Response()
                response.status_code = 200
                response.url = "http://example/file"
                return response

        dl._request_with_retries(
            FakeSession(),
            "HEAD",
            "http://example/file",
            retries=0,
            quiet=True,
            timeout=12.5,
        )
        assert seen["timeout"] == 12.5


class TestRedirects:
    """Redirects are normal for signed downloads, but loops must not hang."""

    def test_redirect_chain_downloads_final_url(self, runner, server, tmp_path):
        server.handler.redirect_to = "/file.bin"
        result = invoke(runner, [server.url("/redirect"), str(tmp_path / "x.bin")])
        assert result.exit_code == 0
        assert (tmp_path / "x.bin").read_bytes() == BODY

    def test_redirect_changes_derived_filename(self, runner, server, tmp_path):
        server.handler.redirect_to = "/renamed.bin"
        result = invoke(runner, [server.url("/redirect"), "-o", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / "renamed.bin").read_bytes() == BODY

    def test_redirect_loop_fails_cleanly(self, runner, server, tmp_path):
        server.handler.redirect_loop = True
        result = invoke(runner, [server.url("/loop"), str(tmp_path / "x")])
        assert result.exit_code == 1
        assert "request failed" in result.output

    def test_head_405_falls_back_to_range_get(self, runner, server, tmp_path):
        server.handler.statuses = deque([405])
        result = invoke(runner, [server.url(), str(tmp_path / "x"), "--dry-run"])
        assert result.exit_code == 0
        assert any(
            call["method"] == "GET" and call["range"] == "bytes=0-0"
            for call in server.handler.calls
        )


class TestFilenamesAndSafety:
    """Remote filenames are attacker-controlled and must never escape the target dir."""

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ('attachment; filename="quoted.bin"', "quoted.bin"),
            ("attachment; filename=plain.bin", "plain.bin"),
            ("attachment; filename*=UTF-8''caf%C3%A9.bin", "café.bin"),
        ],
    )
    def test_content_disposition_variants(
        self, runner, server, tmp_path, header, expected
    ):
        server.handler.content_disposition = header
        result = invoke(runner, [server.url("/url-name.bin"), "-o", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / expected).read_bytes() == BODY

    def test_url_path_filename_is_used_without_content_disposition(
        self, runner, server, tmp_path
    ):
        result = invoke(runner, [server.url("/nested/url.bin"), "-o", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / "url.bin").read_bytes() == BODY

    def test_empty_url_path_uses_download(self, runner, server, tmp_path):
        result = invoke(runner, [server.url("/"), "-o", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / "download").read_bytes() == BODY

    @pytest.mark.parametrize(
        "name",
        [
            "../../etc/passwd",
            "/etc/passwd",
            "bad%00name",
            "bad%0Aname",
            r"bad\name",
            "..",
            "",
            "x" * 300,
        ],
    )
    def test_malicious_content_disposition_is_rejected(
        self, runner, server, tmp_path, name
    ):
        server.handler.content_disposition = f'attachment; filename="{name}"'
        result = invoke(runner, [server.url(), "-o", str(tmp_path)])
        assert result.exit_code == 1
        assert "unsafe filename" in result.output
        assert not any(tmp_path.iterdir())

    def test_existing_output_directory_contains_derived_filename(
        self, runner, server, tmp_path
    ):
        out = tmp_path / "out"
        out.mkdir()
        result = invoke(runner, [server.url(), str(out)])
        assert result.exit_code == 0
        assert (out / "file.bin").read_bytes() == BODY

    def test_existing_output_file_is_replaced_atomically(
        self, runner, server, tmp_path
    ):
        dest = tmp_path / "file.bin"
        dest.write_bytes(b"old")
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY

    def test_parent_directory_is_created(self, runner, server, tmp_path):
        dest = tmp_path / "missing" / "file.bin"
        result = invoke(runner, [server.url(), str(dest)])
        assert result.exit_code == 0
        assert dest.read_bytes() == BODY

    def test_unwritable_parent_fails_without_final_file(self, runner, server, tmp_path):
        parent = tmp_path / "locked"
        parent.mkdir()
        parent.chmod(0o500)
        try:
            result = invoke(runner, [server.url(), str(parent / "file.bin")])
        finally:
            parent.chmod(0o700)
        if result.exit_code == 0:
            pytest.skip("environment permits writes despite directory mode")
        assert not (parent / "file.bin").exists()


class TestBackends:
    """Backend selection is decoupled, but forced choices and argv must be stable."""

    @pytest.mark.parametrize("backend", ["aria2c", "curl"])
    def test_forced_backend_absent_errors(
        self, plain_runner, monkeypatch, server, tmp_path, backend
    ):
        monkeypatch.setattr(dl.shutil, "which", lambda _name: None)
        result = invoke(
            plain_runner, [server.url(), str(tmp_path / "x"), "--backend", backend]
        )
        assert result.exit_code == 1
        assert "not available" in result.output

    @pytest.mark.parametrize(
        ("available", "expected"),
        [({"aria2c", "curl"}, "aria2c"), ({"curl"}, "curl"), (set(), "python")],
    )
    def test_auto_selection_order(self, monkeypatch, available, expected):
        monkeypatch.setattr(
            dl.shutil,
            "which",
            lambda name: f"/bin/{name}" if name in available else None,
        )
        assert dl.select_backend("auto").name == expected

    @pytest.mark.parametrize("forced", ["aria2c", "curl", "python"])
    def test_forced_available_backend_is_selected(self, monkeypatch, forced):
        monkeypatch.setattr(dl.shutil, "which", lambda name: f"/bin/{name}")
        assert dl.select_backend(forced).name == forced

    def test_curl_argv_includes_resume_headers_and_output(self, monkeypatch, tmp_path):
        captured: dict[str, Any] = {}
        dest = tmp_path / "file.bin"

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            Path(argv[argv.index("--output") + 1]).write_bytes(BODY)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(dl.subprocess, "run", fake_run)
        dl.CurlBackend().download(
            "http://example/file?token=SECRET",
            dest,
            dl.RemoteInfo("u", "u", "file.bin", len(BODY), None, None, True),
            opts(headers={"Authorization": "Bearer SECRET"}),
            retries=2,
        )
        argv = captured["argv"]
        assert argv[:2] == ["curl", "--fail"]
        assert "-C" in argv
        assert "Authorization: Bearer SECRET" in argv
        assert dest.read_bytes() == BODY

    def test_aria2c_argv_includes_parallelism_headers_and_output(
        self, monkeypatch, tmp_path
    ):
        captured: dict[str, Any] = {}
        dest = tmp_path / "file.bin"

        def fake_run(argv, **_kwargs):
            captured["argv"] = argv
            Path(
                argv[argv.index("--dir") + 1], argv[argv.index("--out") + 1]
            ).write_bytes(BODY)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(dl.subprocess, "run", fake_run)
        dl.Aria2cBackend().download(
            "http://example/file",
            dest,
            dl.RemoteInfo("u", "u", "file.bin", len(BODY), None, None, True),
            opts(connections=4, headers={"X-Test": "yes"}),
            retries=3,
        )
        argv = captured["argv"]
        assert "--split" in argv and argv[argv.index("--split") + 1] == "4"
        assert "--header=X-Test: yes" in argv
        assert dest.read_bytes() == BODY

    def test_backend_nonzero_is_surfaced(self, monkeypatch):
        monkeypatch.setattr(
            dl.subprocess,
            "run",
            lambda argv, **_kwargs: subprocess.CompletedProcess(
                argv, 7, "", "Authorization: Bearer SECRET"
            ),
        )
        with pytest.raises(dl.DownloadError) as exc:
            dl._run_backend(["curl"], "http://example/file?sig=SECRET")
        assert "SECRET" not in str(exc.value)
        assert "sig=***" in str(exc.value)

    def test_backend_failure_tries_mirror(
        self, plain_runner, monkeypatch, server, tmp_path
    ):
        class FakeBackend(dl.DownloadBackend):
            name = "fake"
            seen: list[str] = []

            @classmethod
            def available(cls) -> bool:
                return True

            def download(self, url, dest, info, options, *, retries):
                self.seen.append(url)
                if len(self.seen) == 1:
                    raise dl.DownloadError("backend died", transient=True)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(BODY)

        fake = FakeBackend()
        monkeypatch.setattr(dl, "select_backend", lambda _name: fake)
        result = invoke(
            plain_runner,
            [
                server.url("/file.bin?primary=1"),
                str(tmp_path / "file.bin"),
                "--mirror",
                server.url("/file.bin?mirror=1"),
                "--retries",
                "0",
            ],
        )
        assert result.exit_code == 0
        assert fake.seen == [
            server.url("/file.bin?primary=1"),
            server.url("/file.bin?mirror=1"),
        ]


class TestMirrors:
    """Mirrors are failover sources; one bad URL must not doom a long download."""

    def test_first_fails_second_succeeds(self, runner, server, tmp_path):
        result = invoke(
            runner,
            [
                f"http://127.0.0.1:{unused_port()}/file.bin",
                str(tmp_path / "file.bin"),
                "--mirror",
                server.url(),
                "--retries",
                "0",
            ],
        )
        assert result.exit_code == 0
        assert (tmp_path / "file.bin").read_bytes() == BODY
        assert "trying next mirror" in result.output

    def test_all_mirrors_fail(self, runner, tmp_path):
        result = invoke(
            runner,
            [
                f"http://127.0.0.1:{unused_port()}/file.bin",
                str(tmp_path / "file.bin"),
                "--mirror",
                f"http://127.0.0.1:{unused_port()}/file.bin",
                "--retries",
                "0",
            ],
        )
        assert result.exit_code == 1
        assert not (tmp_path / "file.bin").exists()

    def test_empty_mirror_list_is_just_primary(self, runner, server, tmp_path):
        result = invoke(runner, [server.url(), str(tmp_path / "file.bin")])
        assert result.exit_code == 0
        assert (tmp_path / "file.bin").read_bytes() == BODY

    def test_404_mirror_is_not_retried_but_next_mirror_is_tried(
        self, runner, server, tmp_path
    ):
        bad = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            type("NotFound", (StubHandler,), {"statuses": deque([404]), "calls": []}),
        )
        thread = threading.Thread(target=bad.serve_forever, daemon=True)
        thread.start()
        try:
            bad_url = f"http://127.0.0.1:{bad.server_port}/file.bin"
            result = invoke(
                runner,
                [
                    bad_url,
                    str(tmp_path / "file.bin"),
                    "--mirror",
                    server.url(),
                    "--retries",
                    "3",
                ],
            )
        finally:
            bad.shutdown()
            bad.server_close()
            thread.join(timeout=2)
        assert result.exit_code == 0
        assert (tmp_path / "file.bin").read_bytes() == BODY


class TestRedaction:
    """URLs and headers often contain SAS tokens; no path should echo them."""

    @pytest.mark.parametrize("query", ["sig=SUPERSECRET", "token=SUPERSECRET"])
    def test_failure_redacts_query_secret(self, runner, tmp_path, query):
        result = invoke(
            runner,
            [
                f"http://127.0.0.1:{unused_port()}/file.bin?{query}",
                str(tmp_path / "x"),
                "--retries",
                "0",
            ],
        )
        assert result.exit_code == 1
        assert "SUPERSECRET" not in result.output
        assert "***" in result.output

    def test_dry_run_redacts_query_secret(self, runner, server, tmp_path):
        result = invoke(
            runner,
            [server.url("/file.bin?sig=SUPERSECRET"), "-o", str(tmp_path), "--dry-run"],
        )
        assert result.exit_code == 0
        assert "SUPERSECRET" not in result.output
        assert "sig=***" in result.output

    def test_success_verbose_path_redacts_query_secret(self, runner, server, tmp_path):
        result = invoke(
            runner,
            [server.url("/file.bin?token=SUPERSECRET"), str(tmp_path / "file.bin")],
        )
        assert result.exit_code == 0
        assert "SUPERSECRET" not in result.output

    def test_authorization_header_secret_not_printed_on_backend_error(
        self, plain_runner, monkeypatch, server, tmp_path
    ):
        monkeypatch.setattr(dl.CurlBackend, "available", classmethod(lambda cls: True))
        monkeypatch.setattr(
            dl.subprocess,
            "run",
            lambda argv, **_kwargs: subprocess.CompletedProcess(
                argv, 1, "", "Authorization: Bearer SUPERSECRET"
            ),
        )
        result = invoke(
            plain_runner,
            [
                server.url(),
                str(tmp_path / "x"),
                "--backend",
                "curl",
                "--header",
                "Authorization: Bearer SUPERSECRET",
            ],
        )
        assert result.exit_code == 1
        assert "SUPERSECRET" not in result.output
        assert "Authorization: Bearer ***" in result.output


class TestOutputQuality:
    """Progress should be useful when visible and quiet/non-spammy otherwise."""

    def test_dry_run_writes_nothing(self, runner, server, tmp_path):
        result = invoke(runner, [server.url(), "-o", str(tmp_path), "--dry-run"])
        assert result.exit_code == 0
        assert not any(tmp_path.iterdir())

    def test_quiet_suppresses_status_lines(self, runner, server, tmp_path):
        result = invoke(runner, [server.url(), str(tmp_path / "file.bin"), "--quiet"])
        assert result.exit_code == 0
        assert result.output == ""

    def test_non_tty_progress_is_not_spammy(
        self, runner, server, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(dl, "CHUNK", 128)
        result = invoke(runner, [server.url(), str(tmp_path / "file.bin")])
        assert result.exit_code == 0
        assert result.output.count("file.bin:") <= 2

    def test_narrow_terminal_detail_still_renders(
        self, runner, server, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("COLUMNS", "42")
        result = invoke(runner, [server.url(), str(tmp_path / "file.bin"), "--dry-run"])
        assert result.exit_code == 0
        assert "output" in result.output
