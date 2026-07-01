"""Tests for scripts/openai_proxy.py.

Unit tests for pure helpers + async integration tests using
``httpx.AsyncClient(transport=ASGITransport(app=app))`` so the entire
proxy runs in-process. Upstream is a real fake HTTP server on loopback
so streaming timing can be observed.
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
import re
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

import httpx
import pytest
import uvicorn
from asgi_lifespan import LifespanManager
from openai_proxy import (
    DEFAULT_LOG_MAX_BYTES,
    PrettyAccessFormatter,
    build_app,
    build_uvicorn_log_config,
    check_api_key,
    resolve_url,
)


# --- Unit: resolve_url -----------------------------------------------------


class TestResolveUrl:
    def _u(self, **kwargs):
        d = dict(
            path_qs="/v1/chat/completions",
            body_obj={"model": "gpt-4"},
            base="https://x/openai",
            api_version="2024-10-21",
            default_dep=None,
        )
        d.update(kwargs)
        return resolve_url(**d)

    def test_chat_with_model_body(self):
        assert self._u() == (
            "https://x/openai/deployments/gpt-4/chat/completions?api-version=2024-10-21"
        )

    def test_no_v1_prefix_still_works(self):
        assert "/deployments/gpt-4/chat/completions" in self._u(
            path_qs="/chat/completions"
        )

    def test_models_no_deployment(self):
        assert self._u(path_qs="/v1/models", body_obj=None) == (
            "https://x/openai/models?api-version=2024-10-21"
        )

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/files/abc",
            "/v1/fine_tuning/jobs",
            "/v1/batches",
            "/v1/threads/x",
            "/v1/assistants/y",
        ],
    )
    def test_other_no_deployment_paths(self, path):
        url = self._u(path_qs=path, body_obj=None)
        assert "/deployments" not in url
        assert path.replace("/v1", "") in url

    def test_default_deployment_when_body_missing_model(self):
        assert "/deployments/default-d/" in self._u(
            body_obj={}, default_dep="default-d"
        )

    def test_missing_model_returns_none(self):
        assert self._u(body_obj={}) is None

    def test_non_dict_body(self):
        assert self._u(body_obj="not a dict") is None

    def test_query_preserved_with_default_api_version(self):
        url = self._u(path_qs="/v1/chat/completions?stream=true")
        assert "stream=true" in url and "api-version=2024-10-21" in url

    def test_query_api_version_overrides_default(self):
        url = self._u(path_qs="/v1/chat/completions?api-version=custom")
        assert "api-version=custom" in url and "api-version=2024-10-21" not in url

    def test_deployment_url_encoded(self):
        assert "gpt%204o" in self._u(body_obj={"model": "gpt 4o"})

    def test_model_field_preferred_over_deployment_field(self):
        url = self._u(body_obj={"model": "m1", "deployment": "m2"})
        assert "/deployments/m1/" in url and "/m2/" not in url

    def test_falls_back_to_deployment_field(self):
        assert "/deployments/m2/" in self._u(body_obj={"deployment": "m2"})

    @pytest.mark.parametrize(
        "evil_path",
        [
            "/v1/chat/../../../../../etc/admin-only",
            "/v1/chat/../management/secrets",
            "/v1/x/%2e%2e/%2e%2e/etc/passwd",
            "/v1/x/..%2fadmin",
            "/v1/..",
            "/v1/foo/..",
        ],
    )
    def test_rejects_path_traversal(self, evil_path):
        # Authenticated clients must not be able to escape the deployment
        # scope via `..` segments — httpx.Request would RFC-3986-normalize
        # them away and leak the AAD bearer token to arbitrary upstream
        # endpoints. resolve_url must return None for any such input.
        assert self._u(path_qs=evil_path) is None


# --- Unit: check_api_key ---------------------------------------------------


class TestCheckApiKey:
    def test_no_expected_always_passes(self):
        assert check_api_key({}, None) is True
        assert check_api_key({"Authorization": "Bearer wrong"}, None) is True

    @pytest.mark.parametrize(
        "headers",
        [
            {"Authorization": "Bearer sekret"},
            {"Authorization": "bearer sekret"},
            {"api-key": "sekret"},
            {"Api-Key": "sekret"},
        ],
    )
    def test_match(self, headers):
        assert check_api_key(headers, "sekret") is True

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Authorization": "Bearer wrong"},
            {"api-key": "wrong"},
            {"Authorization": "Basic sekret"},
        ],
    )
    def test_no_match(self, headers):
        assert check_api_key(headers, "sekret") is False


# --- Unit: access logging --------------------------------------------------


def _reset_logger(name: str) -> None:
    logger = logging.getLogger(name)
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


class TestAccessLogging:
    def test_pretty_access_formatter_aligns_fields(self):
        formatter = PrettyAccessFormatter(
            fmt=(
                "%(asctime)s | %(levelname)-7s | %(client_ip)-39s | "
                "%(method)-6s | %(path)-72s | %(status_code)s"
            ),
            datefmt="%Y-%m-%d %H:%M:%S",
            use_colors=False,
        )
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            0,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:12345", "POST", "/v1/chat/completions", "1.1", 200),
            None,
        )

        line = formatter.format(record)

        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", line)
        assert "127.0.0.1" in line
        assert "POST  " in line
        assert "/v1/chat/completions" in line
        assert "200 OK" in line

    def test_log_config_routes_uvicorn_access_to_rotating_file(self, tmp_path):
        log_file = tmp_path / "proxy.log"
        config = build_uvicorn_log_config("info", log_file, max_bytes=120)
        logging.config.dictConfig(config)
        try:
            logger = logging.getLogger("uvicorn.access")
            logger.info(
                '%s - "%s %s HTTP/%s" %d',
                "203.0.113.10:0",
                "GET",
                "/health",
                "1.1",
                200,
            )
            for handler in logger.handlers:
                handler.flush()

            text = log_file.read_text()
        finally:
            _reset_logger("uvicorn.access")

        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text)
        assert "INFO" in text
        assert "203.0.113.10" in text
        assert "GET" in text
        assert "/health" in text
        assert "200 OK" in text

    def test_log_file_handler_rotates_by_day_and_size(self, tmp_path):
        log_file = tmp_path / "proxy.log"
        config = build_uvicorn_log_config("info", log_file, max_bytes=DEFAULT_LOG_MAX_BYTES)
        logging.config.dictConfig(config)

        try:
            handlers = logging.getLogger("uvicorn.access").handlers
            file_handlers = [
                h
                for h in handlers
                if h.__class__.__name__ == "ConcurrentTimedRotatingFileHandler"
            ]
        finally:
            _reset_logger("uvicorn.access")

        assert len(file_handlers) == 1
        assert file_handlers[0].when == "MIDNIGHT"
        assert file_handlers[0].clh.maxBytes == DEFAULT_LOG_MAX_BYTES


# --- Fake upstream HTTP server --------------------------------------------


class _Upstream(BaseHTTPRequestHandler):
    captured: list[dict] = []
    responses: dict[str, Callable] = {}

    def log_message(self, *a, **k):
        return

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.captured.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        h = self.responses.get(self.path.split("?")[0])
        if h is None:
            payload = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        else:
            h(self, body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


class _ThreadedHTTP(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def upstream():
    """Spin up a fake upstream on a free port. Yields (base_url, captured, responses)."""
    port = _free_port()
    _Upstream.captured = []
    _Upstream.responses = {}
    server = _ThreadedHTTP(("127.0.0.1", port), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", _Upstream.captured, _Upstream.responses
    finally:
        server.shutdown()
        server.server_close()


async def _fake_token() -> str:
    return "TEST-TOKEN"


def _build_for(
    upstream_url: str, *, api_key: str | None = None, default_dep: str | None = None
) -> tuple[object, dict]:
    """Return (app, cfg) wired against *upstream_url*."""
    cfg = {
        "endpoint": upstream_url,
        "instance": "openai",
        "base": f"{upstream_url}/openai",
        "api_version": "2024-10-21",
        "default_dep": default_dep,
        "api_key": api_key,
        "timeout": 10,
        "skip_warmup": True,
    }
    return build_app(cfg, token_provider=_fake_token), cfg


@pytest.fixture
async def client(upstream):
    upstream_url, _, _ = upstream
    app, _ = _build_for(upstream_url)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            timeout=10,
        ) as ac:
            yield ac


@pytest.fixture
async def live_proxy(upstream):
    """Boot uvicorn in the test's event loop so streaming timing is preserved.

    ``httpx.ASGITransport`` buffers all response chunks before yielding, so
    it cannot validate progressive delivery. Tests that care about real
    streaming timing should use this fixture instead of ``client``.
    """
    upstream_url, _, responses = upstream
    app, _ = _build_for(upstream_url)
    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", responses
    finally:
        server.should_exit = True
        await task


# --- Built-in endpoints ---------------------------------------------------


class TestBuiltinEndpoints:
    async def test_health(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    async def test_status(self, client):
        r = await client.get("/status")
        assert r.status_code == 200
        d = r.json()
        assert d["api_version"] == "2024-10-21"
        assert d["api_key_required"] is False
        assert "endpoint" in d and "instance" in d

    async def test_options_preflight(self, client):
        r = await client.options(
            "/v1/chat/completions",
            headers={"Access-Control-Request-Headers": "X-Custom"},
        )
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == "*"
        assert "X-Custom" in r.headers["access-control-allow-headers"]


# --- End-to-end proxying --------------------------------------------------


class TestEndToEnd:
    async def test_post_with_model_injects_token(self, client, upstream):
        _, captured, responses = upstream

        def echo(h, _body):
            payload = b'{"echo": "ok"}'
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(payload)))
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(payload)

        responses["/openai/deployments/gpt-4/chat/completions"] = echo

        r = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        assert r.json() == {"echo": "ok"}
        req = captured[-1]
        assert "/deployments/gpt-4/chat/completions" in req["path"]
        assert "api-version=2024-10-21" in req["path"]
        assert req["headers"]["authorization"] == "Bearer TEST-TOKEN"

    async def test_get_models_no_deployment(self, client, upstream):
        _, captured, responses = upstream

        def listm(h, _body):
            payload = b'{"data": [{"id": "fake"}]}'
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(payload)))
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(payload)

        responses["/openai/models"] = listm

        r = await client.get("/v1/models")
        assert r.status_code == 200
        assert r.json() == {"data": [{"id": "fake"}]}
        assert "/deployments" not in captured[-1]["path"]
        assert "/openai/models" in captured[-1]["path"]

    async def test_missing_model_returns_400(self, client):
        r = await client.post("/v1/chat/completions", json={"messages": []})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "missing_model"

    async def test_client_auth_headers_stripped(self, client, upstream):
        _, captured, responses = upstream

        def ok(h, _body):
            h.send_response(200)
            h.send_header("Content-Length", "0")
            h.send_header("Connection", "close")
            h.end_headers()

        responses["/openai/deployments/m/chat/completions"] = ok

        await client.post(
            "/v1/chat/completions",
            json={"model": "m"},
            headers={"Authorization": "Bearer client-secret", "api-key": "client-key"},
        )
        hdrs = captured[-1]["headers"]
        assert hdrs["authorization"] == "Bearer TEST-TOKEN"
        assert "api-key" not in hdrs

    async def test_upstream_unreachable_502(self):
        cfg = {
            "endpoint": "http://127.0.0.1:1",
            "instance": "openai",
            "base": "http://127.0.0.1:1/openai",
            "api_version": "v",
            "default_dep": None,
            "api_key": None,
            "timeout": 2,
            "skip_warmup": True,
        }
        app = build_app(cfg, token_provider=_fake_token)
        async with LifespanManager(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://test",
                timeout=5,
            ) as c:
                r = await c.post("/v1/chat/completions", json={"model": "m"})
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "upstream_unreachable"


# --- API-key gate ---------------------------------------------------------


class TestApiKeyGate:
    @pytest.fixture
    async def gated(self, upstream):
        upstream_url, _, _ = upstream
        app, _ = _build_for(upstream_url, api_key="sekret")
        async with LifespanManager(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://test",
                timeout=10,
            ) as ac:
                yield ac

    async def test_no_key_401(self, gated):
        r = await gated.post("/v1/chat/completions", json={})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_api_key"

    async def test_wrong_key_401(self, gated):
        r = await gated.post(
            "/v1/chat/completions",
            json={},
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401

    async def test_right_bearer_passes(self, gated):
        r = await gated.post(
            "/v1/chat/completions",
            json={},
            headers={"Authorization": "Bearer sekret"},
        )
        assert r.status_code == 400  # passes gate, then missing_model

    async def test_right_api_key_header_passes(self, gated):
        r = await gated.post(
            "/v1/chat/completions",
            json={},
            headers={"api-key": "sekret"},
        )
        assert r.status_code == 400


# --- SSE streaming relay --------------------------------------------------


class TestStreaming:
    def _sse_handler(self, n_chunks: int, sleep_s: float):
        def handler(h, _body):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for i in range(n_chunks):
                chunk = f"data: chunk{i}\n\n".encode()
                size = f"{len(chunk):x}\r\n".encode()
                h.wfile.write(size + chunk + b"\r\n")
                h.wfile.flush()
                time.sleep(sleep_s)
            h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()

        return handler

    async def test_all_chunks_preserved(self, client, upstream):
        _, _, responses = upstream
        responses["/openai/deployments/m/chat/completions"] = self._sse_handler(3, 0.0)
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "m", "stream": True},
        ) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers.get("content-type", "")
            combined = ""
            async for piece in r.aiter_text():
                combined += piece
        for i in range(3):
            assert f"data: chunk{i}" in combined

    async def test_anti_buffering_headers_present(self, client, upstream):
        _, _, responses = upstream
        responses["/openai/deployments/m/chat/completions"] = self._sse_handler(1, 0.0)
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "m", "stream": True},
        ) as r:
            assert r.headers.get("cache-control") == "no-cache"
            assert r.headers.get("x-accel-buffering") == "no"
            await r.aread()

    async def test_chunks_arrive_incrementally(self, live_proxy):
        """Upstream sleeps 100 ms between chunks; client must see them at
        that cadence (not all-at-end if buffered).

        Uses real uvicorn (not ASGITransport, which buffers).
        """
        base, responses = live_proxy
        responses["/openai/deployments/m/chat/completions"] = self._sse_handler(3, 0.1)
        ts: list[float] = []
        async with httpx.AsyncClient(timeout=10) as c:
            async with c.stream(
                "POST",
                f"{base}/v1/chat/completions",
                json={"model": "m", "stream": True},
            ) as r:
                async for _ in r.aiter_raw():
                    ts.append(time.time())
        span = ts[-1] - ts[0]
        assert span >= 0.18, f"chunks appear buffered (span={span:.3f}s)"


# --- Concurrency: many simultaneous SSE streams ---------------------------


class TestConcurrency:
    async def test_many_concurrent_sse_streams(self, client, upstream):
        """Single async worker should handle dozens of overlapping streams."""
        _, _, responses = upstream

        def slow_sse(h, _body):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for i in range(2):
                chunk = f"data: chunk{i}\n\n".encode()
                size = f"{len(chunk):x}\r\n".encode()
                h.wfile.write(size + chunk + b"\r\n")
                h.wfile.flush()
                time.sleep(0.05)
            h.wfile.write(b"0\r\n\r\n")

        responses["/openai/deployments/m/chat/completions"] = slow_sse

        async def one_stream(i):
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={"model": "m", "stream": True},
            ) as r:
                body = ""
                async for piece in r.aiter_text():
                    body += piece
                return i, r.status_code, body

        results = await asyncio.gather(*(one_stream(i) for i in range(20)))
        assert all(rc == 200 for _, rc, _ in results)
        assert all("chunk0" in b and "chunk1" in b for _, _, b in results)
