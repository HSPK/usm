"""Tests for scripts/openai_proxy.py.

Unit tests for the pure helpers (resolve_url, check_api_key) plus end-to-end
integration tests with a fake TRAPI upstream — including SSE streaming
relay verification.
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

import httpx
import pytest

import openai_proxy as oxp
from openai_proxy import _Server, check_api_key, make_handler, resolve_url


# --- Unit: resolve_url -----------------------------------------------------

class TestResolveUrl:
    def _u(self, **kwargs):
        defaults = dict(
            path_qs="/v1/chat/completions",
            body_obj={"model": "gpt-4"},
            base="https://x/openai",
            api_version="2024-10-21",
            default_dep=None,
        )
        defaults.update(kwargs)
        return resolve_url(**defaults)

    def test_chat_with_model_body(self):
        assert self._u() == (
            "https://x/openai/deployments/gpt-4/chat/completions?api-version=2024-10-21"
        )

    def test_no_v1_prefix_still_works(self):
        url = self._u(path_qs="/chat/completions")
        assert "/deployments/gpt-4/chat/completions" in url

    def test_models_no_deployment(self):
        url = self._u(path_qs="/v1/models", body_obj=None)
        assert url == "https://x/openai/models?api-version=2024-10-21"

    @pytest.mark.parametrize("path", [
        "/v1/files/abc", "/v1/fine_tuning/jobs", "/v1/batches",
        "/v1/threads/x", "/v1/assistants/y",
    ])
    def test_other_no_deployment_paths(self, path):
        url = self._u(path_qs=path, body_obj=None)
        assert "/deployments" not in url
        assert path.replace("/v1", "") in url

    def test_default_deployment_when_body_missing_model(self):
        url = self._u(body_obj={}, default_dep="default-d")
        assert "/deployments/default-d/" in url

    def test_missing_model_returns_none(self):
        assert self._u(body_obj={}) is None

    def test_non_dict_body(self):
        assert self._u(body_obj="not a dict") is None

    def test_query_preserved_with_default_api_version(self):
        url = self._u(path_qs="/v1/chat/completions?stream=true")
        assert "stream=true" in url
        assert "api-version=2024-10-21" in url

    def test_query_api_version_overrides_default(self):
        url = self._u(path_qs="/v1/chat/completions?api-version=custom")
        assert "api-version=custom" in url
        assert "api-version=2024-10-21" not in url

    def test_deployment_url_encoded(self):
        url = self._u(body_obj={"model": "gpt 4o"})
        assert "gpt%204o" in url

    def test_model_field_preferred_over_deployment_field(self):
        url = self._u(body_obj={"model": "m1", "deployment": "m2"})
        assert "/deployments/m1/" in url
        assert "/m2/" not in url

    def test_falls_back_to_deployment_field(self):
        url = self._u(body_obj={"deployment": "m2"})
        assert "/deployments/m2/" in url


# --- Unit: check_api_key ---------------------------------------------------

class TestCheckApiKey:
    def test_no_expected_always_passes(self):
        assert check_api_key({}, None) is True
        assert check_api_key({"Authorization": "Bearer wrong"}, None) is True

    @pytest.mark.parametrize("headers", [
        {"Authorization": "Bearer sekret"},
        {"Authorization": "bearer sekret"},  # case-insensitive prefix
        {"api-key": "sekret"},
        {"Api-Key": "sekret"},
    ])
    def test_match(self, headers):
        assert check_api_key(headers, "sekret") is True

    @pytest.mark.parametrize("headers", [
        {},
        {"Authorization": "Bearer wrong"},
        {"api-key": "wrong"},
        {"Authorization": "Basic sekret"},  # not Bearer
    ])
    def test_no_match(self, headers):
        assert check_api_key(headers, "sekret") is False


# --- Integration: fake upstream + proxy infra ------------------------------

class _Upstream(BaseHTTPRequestHandler):
    """Records every inbound and dispatches to a registered response."""

    captured: list[dict] = []
    responses: dict[str, Callable] = {}  # path → fn(handler, body) → None

    def log_message(self, *a, **k): return

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.captured.append({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        key = self.path.split("?")[0]
        handler = self.responses.get(key)
        if handler is None:
            payload = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        else:
            handler(self, body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


class _ThreadedHTTP(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


@pytest.fixture
def upstream():
    """Spin up a fake upstream. Yields (base_url, captured_requests, responses)."""
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


def _boot_proxy(upstream_base: str, *, api_key: str | None = None,
                default_dep: str | None = None) -> tuple[str, dict, _ThreadedHTTP]:
    port = _free_port()
    cfg = {
        "token_provider": lambda: "TEST-TOKEN",
        "api_key": api_key,
        "client": httpx.Client(timeout=10),
        "base": f"{upstream_base}/openai",
        "api_version": "2024-10-21",
        "default_dep": default_dep,
    }
    server = _Server(("127.0.0.1", port), make_handler(cfg))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.05)
    return f"http://127.0.0.1:{port}", cfg, server


@pytest.fixture
def proxy(upstream):
    upstream_url, _, _ = upstream
    base, cfg, server = _boot_proxy(upstream_url)
    try:
        yield base, cfg
    finally:
        server.shutdown()
        server.server_close()
        cfg["client"].close()


class TestEndToEnd:
    def test_post_with_model_injects_token(self, proxy, upstream):
        base, _ = proxy
        _, captured, responses = upstream

        def echo(h, body):
            payload = b'{"echo": "ok"}'
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(payload)))
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(payload)
        responses["/openai/deployments/gpt-4/chat/completions"] = echo

        r = httpx.post(
            f"{base}/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        assert r.json() == {"echo": "ok"}
        req = captured[-1]
        assert req["method"] == "POST"
        assert "/deployments/gpt-4/chat/completions" in req["path"]
        assert "api-version=2024-10-21" in req["path"]
        assert req["headers"]["authorization"] == "Bearer TEST-TOKEN"

    def test_get_models_no_deployment(self, proxy, upstream):
        base, _ = proxy
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

        r = httpx.get(f"{base}/v1/models")
        assert r.status_code == 200
        assert r.json() == {"data": [{"id": "fake"}]}
        assert "/deployments" not in captured[-1]["path"]
        assert "/openai/models" in captured[-1]["path"]

    def test_missing_model_returns_400(self, proxy):
        base, _ = proxy
        r = httpx.post(f"{base}/v1/chat/completions", json={"messages": []})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "missing_model"

    def test_client_auth_headers_stripped(self, proxy, upstream):
        base, _ = proxy
        _, captured, responses = upstream

        def ok(h, _body):
            h.send_response(200); h.send_header("Content-Length", "0")
            h.send_header("Connection", "close"); h.end_headers()
        responses["/openai/deployments/m/chat/completions"] = ok

        httpx.post(
            f"{base}/v1/chat/completions",
            json={"model": "m"},
            headers={
                "Authorization": "Bearer client-secret",
                "api-key": "client-key",
            },
        )
        hdrs = captured[-1]["headers"]
        assert hdrs["authorization"] == "Bearer TEST-TOKEN"  # ours
        assert "api-key" not in hdrs                          # client's was dropped

    def test_options_preflight(self, proxy):
        base, _ = proxy
        r = httpx.options(
            f"{base}/v1/chat/completions",
            headers={"Access-Control-Request-Headers": "X-Custom"},
        )
        assert r.status_code == 204
        assert r.headers["Access-Control-Allow-Origin"] == "*"
        assert "X-Custom" in r.headers["Access-Control-Allow-Headers"]

    def test_upstream_unreachable_502(self):
        # Point the proxy at a port nothing is listening on.
        port = _free_port()
        cfg = {
            "token_provider": lambda: "T",
            "api_key": None,
            "client": httpx.Client(timeout=2),
            "base": "http://127.0.0.1:1/openai",  # nothing on port 1
            "api_version": "v",
            "default_dep": None,
        }
        server = _Server(("127.0.0.1", port), make_handler(cfg))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(0.05)
        try:
            r = httpx.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json={"model": "m"},
                timeout=5,
            )
            assert r.status_code == 502
            assert r.json()["error"]["code"] == "upstream_unreachable"
        finally:
            server.shutdown(); server.server_close(); cfg["client"].close()


class TestApiKeyGate:
    @pytest.fixture
    def gated(self, upstream):
        upstream_url, _, _ = upstream
        base, cfg, server = _boot_proxy(upstream_url, api_key="sekret")
        try:
            yield base
        finally:
            server.shutdown(); server.server_close(); cfg["client"].close()

    def test_no_key_401(self, gated):
        r = httpx.post(f"{gated}/v1/chat/completions", json={})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_api_key"

    def test_wrong_key_401(self, gated):
        r = httpx.post(
            f"{gated}/v1/chat/completions",
            json={}, headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401

    def test_right_bearer_passes(self, gated):
        # passes gate, then hits missing_model
        r = httpx.post(
            f"{gated}/v1/chat/completions",
            json={}, headers={"Authorization": "Bearer sekret"},
        )
        assert r.status_code == 400

    def test_right_api_key_header_passes(self, gated):
        r = httpx.post(
            f"{gated}/v1/chat/completions",
            json={}, headers={"api-key": "sekret"},
        )
        assert r.status_code == 400


class TestStreaming:
    """Verify SSE chunks flow through the proxy progressively, not buffered."""

    def _sse_handler(self, n_chunks: int, sleep_s: float):
        """Build a fake upstream that writes chunked SSE with sleeps between."""
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

    def test_all_chunks_preserved(self, proxy, upstream):
        base, _ = proxy
        _, _, responses = upstream
        responses["/openai/deployments/m/chat/completions"] = self._sse_handler(3, 0.0)

        with httpx.stream(
            "POST", f"{base}/v1/chat/completions",
            json={"model": "m", "stream": True}, timeout=10,
        ) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers.get("content-type", "")
            combined = "".join(r.iter_text())
        for i in range(3):
            assert f"data: chunk{i}" in combined

    def test_anti_buffering_headers_present(self, proxy, upstream):
        base, _ = proxy
        _, _, responses = upstream
        responses["/openai/deployments/m/chat/completions"] = self._sse_handler(1, 0.0)

        with httpx.stream(
            "POST", f"{base}/v1/chat/completions",
            json={"model": "m", "stream": True}, timeout=10,
        ) as r:
            assert r.headers.get("cache-control") == "no-cache"
            assert r.headers.get("x-accel-buffering") == "no"
            r.read()

    def test_chunks_arrive_incrementally(self, proxy, upstream):
        """Upstream sleeps 100 ms between chunks; client must see them at
        roughly that cadence (not all at once at the end)."""
        base, _ = proxy
        _, _, responses = upstream
        responses["/openai/deployments/m/chat/completions"] = self._sse_handler(3, 0.1)

        timestamps: list[float] = []
        with httpx.stream(
            "POST", f"{base}/v1/chat/completions",
            json={"model": "m", "stream": True}, timeout=10,
        ) as r:
            for _ in r.iter_text():
                timestamps.append(time.time())

        # 3 chunks with 100 ms sleeps → total span should be > 0.18s if
        # genuinely streamed. If buffered, all timestamps cluster within
        # ~10 ms at the end.
        span = timestamps[-1] - timestamps[0]
        assert span >= 0.18, f"chunks appear buffered (span={span:.3f}s)"
