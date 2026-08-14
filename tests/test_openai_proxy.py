"""Tests for scripts/openai_proxy.py.

Unit tests for pure helpers + async integration tests using
``httpx.AsyncClient(transport=ASGITransport(app=app))`` so the entire
proxy runs in-process. Upstream is a real fake HTTP server on loopback
so streaming timing can be observed.
"""

from __future__ import annotations

import asyncio
import json
import socket
import socketserver
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import httpx
import pytest
import uvicorn
import websockets
from asgi_lifespan import LifespanManager
from openai_proxy import (
    AnthropicError,
    AnthropicStreamTranslator,
    ResponsesError,
    ResponsesStreamTranslator,
    anthropic_messages_to_chat,
    anthropic_tool_choice_to_chat,
    anthropic_tools_to_chat,
    build_app,
    build_chat_request,
    build_chat_request_from_messages,
    chat_to_anthropic_message,
    chat_to_responses,
    check_api_key,
    is_reasoning_model,
    openai_models_to_anthropic,
    parse_body_metadata,
    plan_retry,
    resolve_chat_url,
    resolve_native_api_url,
    resolve_native_responses_url,
    resolve_realtime_url,
    resolve_url,
    responses_input_to_messages,
    responses_text_to_chat,
    responses_tool_choice_to_chat,
    responses_tools_to_chat,
    retry_after_seconds,
    thinking_to_reasoning_effort,
    token_limit_field,
)
from websockets.datastructures import Headers
from websockets.http11 import Response as WebSocketResponse

# --- Unit: resolve_url -----------------------------------------------------


class TestResolveUrl:
    def _u(self, **kwargs):
        d = {
            "path_qs": "/v1/chat/completions",
            "body_obj": {"model": "gpt-4"},
            "base": "https://x/openai",
            "api_version": "2024-10-21",
            "default_dep": None,
        }
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


class TestResolveNativeResponsesUrl:
    BASE = "https://x/instance/openai"

    def test_v1_collection(self):
        assert resolve_native_responses_url("/v1/responses", self.BASE) == (
            "https://x/instance/openai/v1/responses"
        )

    def test_root_collection_is_normalized_to_v1(self):
        assert resolve_native_responses_url("/responses", self.BASE) == (
            "https://x/instance/openai/v1/responses"
        )

    def test_stored_response_path_and_query_are_preserved(self):
        assert (
            resolve_native_responses_url(
                "/v1/responses/resp_1?include=output", self.BASE
            )
            == "https://x/instance/openai/v1/responses/resp_1?include=output"
        )

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/chat/completions",
            "/models",
            "/v1/responses/../models",
            "/responses/%2e%2e/models",
        ],
    )
    def test_unrelated_or_traversal_paths_are_rejected(self, path):
        assert resolve_native_responses_url(path, self.BASE) is None


class TestResolveNativeApiUrl:
    BASE = "https://x/instance/openai"

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            (
                "/v1/audio/speech",
                "https://x/instance/openai/v1/audio/speech",
            ),
            (
                "/videos?limit=10",
                "https://x/instance/openai/v1/videos?limit=10",
            ),
            (
                "/v1/images/edits",
                "https://x/instance/openai/v1/images/edits",
            ),
            (
                "/v1/videos/video_1/content?model=sora",
                "https://x/instance/openai/v1/videos/video_1/content?model=sora",
            ),
        ],
    )
    def test_native_media_paths(self, path, expected):
        assert resolve_native_api_url(path, self.BASE) == expected

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/audio/transcriptions",
            "/v1/images/generations",
            "/v1/videos/../models",
        ],
    )
    def test_other_or_traversal_paths_rejected(self, path):
        assert resolve_native_api_url(path, self.BASE) is None


class TestResolveRealtimeUrl:
    BASE = "https://x/instance/openai"

    def test_v1_path_and_query(self):
        assert resolve_realtime_url(
            "/v1/realtime?model=gpt-realtime&intent=transcription",
            self.BASE,
            None,
        ) == (
            "wss://x/instance/openai/v1/realtime"
            "?model=gpt-realtime&intent=transcription"
        )

    def test_root_path_and_default_model(self):
        assert resolve_realtime_url("/realtime", self.BASE, "realtime-default") == (
            "wss://x/instance/openai/v1/realtime?model=realtime-default"
        )

    @pytest.mark.parametrize(
        "path",
        ["/v1/realtime", "/v1/chat/completions?model=m", "/v1/../realtime?model=m"],
    )
    def test_missing_model_unrelated_or_traversal_rejected(self, path):
        assert resolve_realtime_url(path, self.BASE, None) is None


class TestParseBodyMetadata:
    def test_json_body(self):
        assert parse_body_metadata(
            b'{"model":"gpt-4o","input":"hi"}', "application/json"
        ) == {"model": "gpt-4o", "input": "hi"}

    def test_urlencoded_body(self):
        assert parse_body_metadata(
            b"model=whisper_001&response_format=json",
            "application/x-www-form-urlencoded",
        ) == {"model": "whisper_001"}

    def test_multipart_body_ignores_file_contents(self):
        boundary = "test-boundary"
        raw = (
            b"--test-boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
            b"Content-Type: audio/wav\r\n\r\n"
            b"RIFF-not-json\r\n"
            b"--test-boundary\r\n"
            b'Content-Disposition: form-data; name="model"\r\n\r\n'
            b"whisper_001\r\n"
            b"--test-boundary--\r\n"
        )
        assert parse_body_metadata(
            raw, f'multipart/form-data; boundary="{boundary}"'
        ) == {"model": "whisper_001"}

    @pytest.mark.parametrize(
        ("raw", "content_type"),
        [
            (b"{bad", "application/json"),
            (b"file=data", "application/x-www-form-urlencoded"),
            (b"--bad", "multipart/form-data; boundary=missing"),
        ],
    )
    def test_missing_or_malformed_model(self, raw, content_type):
        assert parse_body_metadata(raw, content_type) is None


class TestRetryAfterSeconds:
    def test_retry_after_seconds(self):
        assert retry_after_seconds({"Retry-After": "2.5"}) == 2.5

    def test_retry_after_http_date(self):
        assert (
            retry_after_seconds(
                {"Retry-After": "Tue, 14 Nov 2023 22:13:32 GMT"},
                now=1_700_000_000,
            )
            == 12
        )

    def test_millisecond_header(self):
        assert retry_after_seconds({"x-ms-retry-after-ms": "750"}) == 0.75

    def test_uses_longest_reset_duration(self):
        assert (
            retry_after_seconds(
                {
                    "x-ratelimit-reset-requests": "1m30s",
                    "x-ratelimit-reset-tokens": "500ms",
                }
            )
            == 90
        )

    def test_parses_error_body(self):
        assert (
            retry_after_seconds({}, b"Rate Limit Exceeded, retry after 7 seconds") == 7
        )

    def test_exponential_fallback_is_bounded(self):
        assert retry_after_seconds({}, retry_number=1) == 1
        assert retry_after_seconds({}, retry_number=4) == 8
        assert retry_after_seconds({}, retry_number=8) == 8


class TestResolveChatUrl:
    def test_regular_deployment_keeps_azure_style_route(self):
        assert resolve_chat_url(
            {"model": "gpt-4o"}, "https://x/openai", "2024-10-21", None
        ) == (
            "https://x/openai/deployments/gpt-4o/chat/completions"
            "?api-version=2024-10-21"
        )

    def test_slash_model_uses_native_body_routing(self):
        assert (
            resolve_chat_url(
                {"model": "Qwen/Qwen3.5-27B"},
                "https://x/openai",
                "2024-10-21",
                None,
            )
            == "https://x/openai/v1/chat/completions"
        )

    def test_slash_default_deployment_uses_native_body_routing(self):
        assert (
            resolve_chat_url(
                {}, "https://x/openai", "2024-10-21", "unsloth/gemma-3-4b-it"
            )
            == "https://x/openai/v1/chat/completions"
        )

    def test_missing_model_still_fails(self):
        assert resolve_chat_url({}, "https://x/openai", "2024-10-21", None) is None


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


# --- Fake upstream HTTP server --------------------------------------------


class _Upstream(BaseHTTPRequestHandler):
    captured: ClassVar[list[dict]] = []
    responses: ClassVar[dict[str, Callable]] = {}

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
    # The default listen backlog is 5. The concurrency tests open 20 sockets
    # at once, and the accept loop is serial, so on a busy or slow machine
    # (CI) the excess connections get refused and the test fails for reasons
    # that have nothing to do with the proxy.
    request_queue_size = 128


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
    upstream_url: str,
    *,
    api_key: str | None = None,
    default_dep: str | None = None,
    responses_mode: str = "translate",
    anthropic_mode: str = "translate",
    token_field: str | None = None,
    retry_429: int = 0,
    retry_max_wait: float = 30.0,
    retry_sleep=None,
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
        "responses_mode": responses_mode,
        "anthropic_mode": anthropic_mode,
        "token_field": token_field,
        "retry_429": retry_429,
        "retry_max_wait": retry_max_wait,
    }
    return (
        build_app(
            cfg,
            token_provider=_fake_token,
            retry_sleep=retry_sleep,
            retry_random=lambda: 0.0,
        ),
        cfg,
    )


@pytest.fixture
async def client(upstream):
    upstream_url, _, _ = upstream
    app, _ = _build_for(upstream_url)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            timeout=10,
        ) as ac,
    ):
        yield ac


@pytest.fixture
async def auto_client(upstream):
    upstream_url, _, _ = upstream
    app, _ = _build_for(upstream_url, responses_mode="auto")
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            timeout=10,
        ) as ac,
    ):
        yield ac


@pytest.fixture
async def retry_client(upstream):
    upstream_url, _, _ = upstream
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    app, _ = _build_for(
        upstream_url,
        retry_429=2,
        retry_max_wait=30,
        retry_sleep=fake_sleep,
    )
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            timeout=10,
        ) as ac,
    ):
        yield ac, sleeps


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


@pytest.fixture
async def realtime_pair():
    captured = {}

    async def realtime_upstream(ws):
        captured["path"] = ws.request.path
        captured["authorization"] = ws.request.headers["authorization"]
        await ws.send(json.dumps({"type": "session.created"}))
        async for message in ws:
            await ws.send(message)

    upstream_port = _free_port()
    async with websockets.serve(realtime_upstream, "127.0.0.1", upstream_port):
        app, _ = _build_for(f"http://127.0.0.1:{upstream_port}")
        proxy_port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=proxy_port,
                log_level="error",
                lifespan="on",
                access_log=False,
            )
        )
        task = asyncio.create_task(server.serve())
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        try:
            yield f"ws://127.0.0.1:{proxy_port}", captured
        finally:
            server.should_exit = True
            await task


@pytest.fixture
async def realtime_retry_pair():
    attempts = {"count": 0}
    sleeps = []

    async def process_request(_connection, _request):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            return WebSocketResponse(
                429,
                "Too Many Requests",
                Headers(
                    {
                        "Content-Type": "application/json",
                        "Retry-After": "0.05",
                    }
                ),
                b'{"error":{"message":"busy"}}',
            )
        return None

    async def realtime_upstream(ws):
        await ws.send(json.dumps({"type": "session.created"}))

    async def fake_sleep(delay):
        sleeps.append(delay)

    upstream_port = _free_port()
    async with websockets.serve(
        realtime_upstream,
        "127.0.0.1",
        upstream_port,
        process_request=process_request,
    ):
        app, _ = _build_for(
            f"http://127.0.0.1:{upstream_port}",
            retry_429=2,
            retry_sleep=fake_sleep,
        )
        proxy_port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=proxy_port,
                log_level="error",
                lifespan="on",
                access_log=False,
            )
        )
        task = asyncio.create_task(server.serve())
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        try:
            yield f"ws://127.0.0.1:{proxy_port}", attempts, sleeps
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

    async def test_generic_proxy_preserves_rate_limit_headers(self, client, upstream):
        _, _, responses = upstream
        responses["/openai/deployments/embed/embeddings"] = _error_reply(
            429,
            {"error": {"message": "retry later"}},
            headers={
                "Retry-After": "11",
                "X-APIM-Remaining-Requests": "0",
            },
        )

        r = await client.post(
            "/v1/embeddings", json={"model": "embed", "input": "hello"}
        )

        assert r.status_code == 429
        assert r.headers["retry-after"] == "11"
        assert r.headers["x-apim-remaining-requests"] == "0"

    async def test_multipart_model_routes_audio_upload(self, client, upstream):
        _, captured, responses = upstream
        responses["/openai/deployments/whisper_001/audio/transcriptions"] = _json_reply(
            {"text": "hello"}
        )

        r = await client.post(
            "/v1/audio/transcriptions",
            data={"model": "whisper_001", "response_format": "json"},
            files={"file": ("sample.wav", b"RIFF-audio", "audio/wav")},
        )

        assert r.status_code == 200
        assert r.json() == {"text": "hello"}
        assert captured[-1]["path"].startswith(
            "/openai/deployments/whisper_001/audio/transcriptions?"
        )
        assert b"RIFF-audio" in captured[-1]["body"]

    async def test_speech_uses_native_body_routing(self, client, upstream):
        _, captured, responses = upstream
        responses["/openai/v1/audio/speech"] = _json_reply({"audio": "ok"})

        r = await client.post(
            "/v1/audio/speech",
            json={"model": "gpt-4o-mini-tts", "voice": "alloy", "input": "hi"},
        )

        assert r.status_code == 200
        assert captured[-1]["path"] == "/openai/v1/audio/speech"

    async def test_image_edit_uses_native_body_routing(self, client, upstream):
        _, captured, responses = upstream
        responses["/openai/v1/images/edits"] = _json_reply(
            {"data": [{"b64_json": "image"}]}
        )

        r = await client.post(
            "/v1/images/edits",
            data={"model": "gpt-image-1", "prompt": "make it blue"},
            files={"image": ("input.png", b"PNG-image", "image/png")},
        )

        assert r.status_code == 200
        assert captured[-1]["path"] == "/openai/v1/images/edits"

    async def test_video_lifecycle_remembers_model(self, client, upstream):
        _, captured, responses = upstream
        responses["/openai/v1/videos"] = _json_reply(
            {"id": "video_1", "object": "video", "model": "sora-2"}
        )
        responses["/openai/v1/videos/video_1"] = _json_reply(
            {"id": "video_1", "status": "completed", "model": "sora-2"}
        )

        created = await client.post(
            "/v1/videos", json={"model": "sora-2", "prompt": "blue circle"}
        )
        retrieved = await client.get("/v1/videos/video_1")

        assert created.status_code == 200
        assert retrieved.status_code == 200
        assert captured[-2]["path"] == "/openai/v1/videos"
        assert captured[-1]["path"] == "/openai/v1/videos/video_1?model=sora-2"

    async def test_video_query_model_works_without_cache(self, client, upstream):
        _, captured, responses = upstream
        responses["/openai/v1/videos/video_old/content"] = _json_reply({"ok": True})

        r = await client.get("/v1/videos/video_old/content", params={"model": "sora-2"})

        assert r.status_code == 200
        assert captured[-1]["path"] == (
            "/openai/v1/videos/video_old/content?model=sora-2"
        )

    async def test_video_resource_without_model_returns_clear_error(self, client):
        r = await client.get("/v1/videos/video_unknown")
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
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://test",
                timeout=5,
            ) as c,
        ):
            r = await c.post("/v1/chat/completions", json={"model": "m"})
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "upstream_unreachable"


class TestRealtimeWebSocket:
    async def test_bidirectional_relay_and_aad_auth(self, realtime_pair):
        base, captured = realtime_pair
        async with websockets.connect(
            f"{base}/v1/realtime?model=gpt-realtime-mini"
        ) as ws:
            created = json.loads(await ws.recv())
            assert created["type"] == "session.created"
            await ws.send(json.dumps({"type": "session.update", "session": {}}))
            echoed = json.loads(await ws.recv())

        assert echoed["type"] == "session.update"
        assert captured["path"] == "/openai/v1/realtime?model=gpt-realtime-mini"
        assert captured["authorization"] == "Bearer " + await _fake_token()

    async def test_missing_model_returns_error_event(self, realtime_pair):
        base, _ = realtime_pair
        async with websockets.connect(f"{base}/v1/realtime") as ws:
            error = json.loads(await ws.recv())
            assert error["type"] == "error"
            assert error["error"]["code"] == "missing_model"
            with pytest.raises(websockets.ConnectionClosed) as closed:
                await ws.recv()
        assert closed.value.rcvd.code == 1008

    async def test_handshake_retries_429(self, realtime_retry_pair):
        base, attempts, sleeps = realtime_retry_pair

        async with websockets.connect(
            f"{base}/v1/realtime?model=gpt-realtime-mini"
        ) as ws:
            created = json.loads(await ws.recv())

        assert created["type"] == "session.created"
        assert attempts["count"] == 3
        assert sleeps == [0.05, 0.05]


# --- API-key gate ---------------------------------------------------------


class TestApiKeyGate:
    @pytest.fixture
    async def gated(self, upstream):
        upstream_url, _, _ = upstream
        app, _ = _build_for(upstream_url, api_key="sekret")
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://test",
                timeout=10,
            ) as ac,
        ):
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
        async with (
            httpx.AsyncClient(timeout=10) as c,
            c.stream(
                "POST",
                f"{base}/v1/chat/completions",
                json={"model": "m", "stream": True},
            ) as r,
        ):
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


# --- Unit: Responses ⇄ chat completions adapter ---------------------------


def _ids():
    """Deterministic id factory for translation assertions."""
    counters: dict[str, int] = {}

    def factory(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}_{counters[prefix]}"

    return factory


class TestTokenLimitField:
    """max_tokens vs max_completion_tokens is model-dependent."""

    @pytest.mark.parametrize(
        "model", ["o1", "o1-mini", "o3_2025-04-16", "o4-mini", "gpt-5", "codex-mini"]
    )
    def test_reasoning_models_use_max_completion_tokens(self, model):
        assert is_reasoning_model(model) is True
        assert token_limit_field(model) == "max_completion_tokens"

    @pytest.mark.parametrize(
        "model", ["gpt-4o", "gpt-4.1", "gpt-35-turbo", "text-embedding-3-large", ""]
    )
    def test_classic_models_use_max_tokens(self, model):
        assert is_reasoning_model(model) is False
        assert token_limit_field(model) == "max_tokens"

    def test_override_wins(self):
        assert token_limit_field("o3", "max_tokens") == "max_tokens"
        assert token_limit_field("gpt-4o", "max_completion_tokens") == (
            "max_completion_tokens"
        )

    def test_bogus_override_falls_back_to_auto(self):
        assert token_limit_field("gpt-4o", "nonsense") == "max_tokens"

    def test_deployment_prefix_with_vendor_path(self):
        assert is_reasoning_model("azure/o3-mini") is True

    def test_request_sends_only_one_token_field(self):
        for model, expected in [
            ("gpt-4o", "max_tokens"),
            ("o3", "max_completion_tokens"),
        ]:
            payload = build_chat_request(
                {"model": model, "input": "hi", "max_output_tokens": 128}
            ).payload
            other = ({"max_tokens", "max_completion_tokens"} - {expected}).pop()
            assert payload[expected] == 128
            assert other not in payload

    def test_max_output_tokens_omitted_when_absent(self):
        payload = build_chat_request({"model": "gpt-4o", "input": "hi"}).payload
        assert "max_tokens" not in payload
        assert "max_completion_tokens" not in payload


class TestResponsesInputToMessages:
    def test_plain_string_input(self):
        assert responses_input_to_messages("hello") == [
            {"role": "user", "content": "hello"}
        ]

    def test_instructions_become_leading_system_message(self):
        msgs = responses_input_to_messages("hi", "be terse")
        assert msgs[0] == {"role": "system", "content": "be terse"}
        assert msgs[1]["content"] == "hi"

    def test_instructions_role_is_configurable(self):
        msgs = responses_input_to_messages("hi", "x", instructions_role="developer")
        assert msgs[0]["role"] == "developer"

    def test_reasoning_model_gets_developer_instructions(self):
        plan = build_chat_request({"model": "o3", "input": "hi", "instructions": "x"})
        assert plan.payload["messages"][0]["role"] == "developer"

    def test_message_items_with_typed_content(self):
        msgs = responses_input_to_messages(
            [
                {"role": "user", "content": [{"type": "input_text", "text": "a"}]},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "b"}],
                },
            ]
        )
        assert msgs == [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]

    def test_input_image_becomes_image_url_part(self):
        msgs = responses_input_to_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "what is this"},
                        {
                            "type": "input_image",
                            "image_url": "https://x/y.png",
                            "detail": "low",
                        },
                    ],
                }
            ]
        )
        assert msgs[0]["content"] == [
            {"type": "text", "text": "what is this"},
            {
                "type": "image_url",
                "image_url": {"url": "https://x/y.png", "detail": "low"},
            },
        ]

    def test_input_file_becomes_file_part(self):
        msgs = responses_input_to_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": "f-1"},
                        {"type": "input_text", "text": "summarise"},
                    ],
                }
            ]
        )
        assert {"type": "file", "file": {"file_id": "f-1"}} in msgs[0]["content"]

    def test_function_call_roundtrip(self):
        msgs = responses_input_to_messages(
            [
                {"role": "user", "content": "weather?"},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": '{"city":"SH"}',
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "20C"},
            ]
        )
        assert msgs[1] == {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city":"SH"}',
                    },
                }
            ],
        }
        assert msgs[2] == {"role": "tool", "tool_call_id": "call_1", "content": "20C"}

    def test_parallel_function_calls_merge_into_one_assistant_message(self):
        msgs = responses_input_to_messages(
            [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "a",
                    "arguments": "{}",
                },
                {
                    "type": "function_call",
                    "call_id": "c2",
                    "name": "b",
                    "arguments": "{}",
                },
            ]
        )
        assert len(msgs) == 1
        assert [c["id"] for c in msgs[0]["tool_calls"]] == ["c1", "c2"]

    def test_non_string_function_output_is_json_encoded(self):
        msgs = responses_input_to_messages(
            [{"type": "function_call_output", "call_id": "c", "output": {"t": 1}}]
        )
        assert msgs[0]["content"] == '{"t": 1}'

    def test_reasoning_items_are_skipped(self):
        msgs = responses_input_to_messages(
            [{"type": "reasoning", "id": "rs_1", "summary": []}, "hi"]
        )
        assert msgs == [{"role": "user", "content": "hi"}]

    def test_unknown_item_type_raises(self):
        with pytest.raises(ResponsesError):
            responses_input_to_messages([{"type": "totally_new_item"}])


class TestResponsesToolTranslation:
    def test_flat_function_tool_is_nested(self):
        tools, dropped = responses_tools_to_chat(
            [
                {
                    "type": "function",
                    "name": "get_weather",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": True,
                }
            ]
        )
        assert dropped == []
        assert tools == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": True,
                },
            }
        ]

    def test_chat_shaped_tool_passes_through(self):
        tools, _ = responses_tools_to_chat(
            [{"type": "function", "function": {"name": "x", "parameters": {}}}]
        )
        assert tools[0]["function"]["name"] == "x"

    def test_hosted_tools_are_dropped_not_fatal(self):
        tools, dropped = responses_tools_to_chat(
            [{"type": "web_search"}, {"type": "function", "name": "f"}]
        )
        assert dropped == ["web_search"]
        assert len(tools) == 1

    def test_missing_parameters_defaults_to_empty_object_schema(self):
        tools, _ = responses_tools_to_chat([{"type": "function", "name": "f"}])
        assert tools[0]["function"]["parameters"] == {
            "type": "object",
            "properties": {},
        }

    def test_tools_must_be_an_array(self):
        with pytest.raises(ResponsesError):
            responses_tools_to_chat({"type": "function"})

    @pytest.mark.parametrize("choice", ["auto", "none", "required"])
    def test_string_tool_choice(self, choice):
        assert responses_tool_choice_to_chat(choice) == choice

    def test_named_tool_choice_is_nested(self):
        assert responses_tool_choice_to_chat({"type": "function", "name": "f"}) == {
            "type": "function",
            "function": {"name": "f"},
        }

    def test_hosted_tool_choice_degrades_to_auto(self):
        assert responses_tool_choice_to_chat({"type": "web_search"}) == "auto"

    def test_tool_choice_only_sent_with_tools(self):
        plan = build_chat_request(
            {"model": "gpt-4o", "input": "hi", "tool_choice": "required"}
        )
        assert "tool_choice" not in plan.payload


class TestResponsesTextFormat:
    def test_json_schema_becomes_response_format(self):
        fmt, verbosity = responses_text_to_chat(
            {
                "format": {
                    "type": "json_schema",
                    "name": "Out",
                    "schema": {"type": "object"},
                    "strict": True,
                }
            }
        )
        assert verbosity is None
        assert fmt == {
            "type": "json_schema",
            "json_schema": {
                "name": "Out",
                "schema": {"type": "object"},
                "strict": True,
            },
        }

    def test_json_object_and_text(self):
        assert responses_text_to_chat({"format": {"type": "json_object"}})[0] == {
            "type": "json_object"
        }
        assert responses_text_to_chat({"format": {"type": "text"}})[0] == {
            "type": "text"
        }

    def test_verbosity_extracted(self):
        assert responses_text_to_chat({"verbosity": "low"})[1] == "low"

    def test_unknown_format_raises(self):
        with pytest.raises(ResponsesError):
            responses_text_to_chat({"format": {"type": "xml"}})


class TestBuildChatRequest:
    def test_minimal_request(self):
        plan = build_chat_request({"model": "gpt-4o", "input": "hi"})
        assert plan.payload["model"] == "gpt-4o"
        assert plan.payload["messages"] == [{"role": "user", "content": "hi"}]
        assert plan.stream is False
        assert "stream" not in plan.payload

    def test_stream_requests_usage_in_final_chunk(self):
        plan = build_chat_request({"model": "gpt-4o", "input": "hi", "stream": True})
        assert plan.stream is True
        assert plan.payload["stream"] is True
        assert plan.payload["stream_options"] == {"include_usage": True}

    def test_reasoning_effort_mapped(self):
        plan = build_chat_request(
            {"model": "o3", "input": "hi", "reasoning": {"effort": "high"}}
        )
        assert plan.payload["reasoning_effort"] == "high"
        assert plan.echo["reasoning"]["effort"] == "high"

    def test_sampling_params_passed_through(self):
        plan = build_chat_request(
            {"model": "gpt-4o", "input": "hi", "temperature": 0.2, "top_p": 0.9}
        )
        assert plan.payload["temperature"] == 0.2
        assert plan.payload["top_p"] == 0.9

    def test_top_logprobs_enables_logprobs(self):
        plan = build_chat_request({"model": "gpt-4o", "input": "hi", "top_logprobs": 3})
        assert plan.payload["logprobs"] is True

    def test_default_deployment_used_when_model_missing(self):
        plan = build_chat_request({"input": "hi"}, default_model="dep-1")
        assert plan.payload["model"] == "dep-1"

    def test_missing_model_raises(self):
        with pytest.raises(ResponsesError) as exc:
            build_chat_request({"input": "hi"})
        assert exc.value.param == "model"

    def test_missing_input_raises(self):
        with pytest.raises(ResponsesError) as exc:
            build_chat_request({"model": "gpt-4o"})
        assert exc.value.param == "input"

    @pytest.mark.parametrize(
        "field,value",
        [
            ("previous_response_id", "resp_1"),
            ("conversation", "conv_1"),
            ("background", True),
            ("prompt", {"id": "p"}),
        ],
    )
    def test_stateful_fields_rejected(self, field, value):
        with pytest.raises(ResponsesError) as exc:
            build_chat_request({"model": "gpt-4o", "input": "hi", field: value})
        assert exc.value.code == "unsupported_parameter"
        assert exc.value.param == field

    def test_store_is_always_false_in_echo(self):
        plan = build_chat_request({"model": "gpt-4o", "input": "hi", "store": True})
        assert plan.echo["store"] is False

    def test_non_object_body_raises(self):
        with pytest.raises(ResponsesError):
            build_chat_request(["nope"])


class TestPlanRetry:
    def test_swaps_max_tokens_for_max_completion_tokens(self):
        body = json.dumps(
            {
                "error": {
                    "message": (
                        "Unsupported parameter: 'max_tokens' is not supported with "
                        "this model. Use 'max_completion_tokens' instead."
                    ),
                    "param": "max_tokens",
                }
            }
        )
        new, note = plan_retry(400, body, {"model": "o3", "max_tokens": 50})
        assert new == {"model": "o3", "max_completion_tokens": 50}
        assert "max_completion_tokens" in note

    def test_swaps_back_when_upstream_is_too_old(self):
        body = json.dumps(
            {
                "error": {
                    "message": (
                        "Unrecognized request argument supplied: max_completion_tokens"
                    )
                }
            }
        )
        new, _ = plan_retry(400, body, {"model": "m", "max_completion_tokens": 7})
        assert new == {"model": "m", "max_tokens": 7}

    def test_drops_unsupported_optional_param(self):
        body = json.dumps(
            {
                "error": {
                    "message": "Unsupported parameter: 'temperature'.",
                    "param": "temperature",
                }
            }
        )
        new, note = plan_retry(400, body, {"model": "o3", "temperature": 0.5})
        assert new == {"model": "o3"}
        assert note == "dropped temperature"

    def test_drops_unsupported_stream_options(self):
        body = json.dumps({"error": {"param": "stream_options", "message": "unknown"}})
        new, _ = plan_retry(
            400, body, {"model": "m", "stream_options": {"include_usage": True}}
        )
        assert "stream_options" not in new

    def test_non_400_is_not_retried(self):
        assert plan_retry(500, "boom", {"max_tokens": 1}) is None

    def test_unrelated_400_is_not_retried(self):
        body = json.dumps({"error": {"message": "content filter", "param": "prompt"}})
        assert plan_retry(400, body, {"model": "m", "max_tokens": 1}) is None

    def test_param_not_in_payload_is_not_retried(self):
        body = json.dumps({"error": {"param": "temperature", "message": "unsupported"}})
        assert plan_retry(400, body, {"model": "m"}) is None

    def test_non_json_error_body_still_parsed(self):
        new, _ = plan_retry(
            400,
            "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead",
            {"max_tokens": 3},
        )
        assert new == {"max_completion_tokens": 3}


class TestChatToResponses:
    ECHO: ClassVar = {"model": "gpt-4o", "tools": [], "store": False}

    def _chat(self, **overrides):
        chat = {
            "id": "chatcmpl-abc",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "gpt-4o-2024-11-20",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        chat.update(overrides)
        return chat

    def test_basic_shape(self):
        out = chat_to_responses(self._chat(), self.ECHO, id_factory=_ids())
        assert out["object"] == "response"
        assert out["id"] == "resp_chatcmplabc"
        assert out["created_at"] == 1700000000
        assert out["status"] == "completed"
        assert out["model"] == "gpt-4o-2024-11-20"
        assert out["store"] is False

    def test_text_output_item(self):
        out = chat_to_responses(self._chat(), self.ECHO, id_factory=_ids())
        assert out["output"] == [
            {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "hello", "annotations": []}
                ],
            }
        ]

    def test_usage_mapping(self):
        out = chat_to_responses(self._chat(), self.ECHO, id_factory=_ids())
        assert out["usage"] == {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 15,
        }

    def test_reasoning_tokens_surfaced(self):
        chat = self._chat(
            usage={
                "prompt_tokens": 1,
                "completion_tokens": 9,
                "total_tokens": 10,
                "completion_tokens_details": {"reasoning_tokens": 7},
                "prompt_tokens_details": {"cached_tokens": 1},
            }
        )
        out = chat_to_responses(chat, self.ECHO, id_factory=_ids())
        assert out["usage"]["output_tokens_details"]["reasoning_tokens"] == 7
        assert out["usage"]["input_tokens_details"]["cached_tokens"] == 1

    def test_length_finish_reason_marks_incomplete(self):
        chat = self._chat(
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "trunc"},
                    "finish_reason": "length",
                }
            ]
        )
        out = chat_to_responses(chat, self.ECHO, id_factory=_ids())
        assert out["status"] == "incomplete"
        assert out["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_tool_calls_become_function_call_items(self):
        chat = self._chat(
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_9",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"SH"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        )
        out = chat_to_responses(chat, self.ECHO, id_factory=_ids())
        assert out["output"] == [
            {
                "id": "fc_1",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_9",
                "name": "get_weather",
                "arguments": '{"city":"SH"}',
            }
        ]

    def test_refusal_content_part(self):
        chat = self._chat(
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "refusal": "no",
                    },
                    "finish_reason": "stop",
                }
            ]
        )
        out = chat_to_responses(chat, self.ECHO, id_factory=_ids())
        assert out["output"][0]["content"] == [{"type": "refusal", "refusal": "no"}]

    def test_empty_choices_yields_empty_output(self):
        out = chat_to_responses(self._chat(choices=[]), self.ECHO, id_factory=_ids())
        assert out["output"] == []


class TestResponsesStreamTranslator:
    ECHO: ClassVar = {"model": "gpt-4o", "tools": [], "store": False}

    def _run(self, chunks: list[dict]) -> list[dict]:
        t = ResponsesStreamTranslator(self.ECHO, id_factory=_ids())
        frames = list(t.start())
        for chunk in chunks:
            frames += t.feed_line("data: " + json.dumps(chunk))
        frames += t.feed_line("data: [DONE]")
        events = []
        for frame in frames:
            text = frame.decode()
            head, _, payload = text.partition("\ndata: ")
            assert head.startswith("event: ")
            events.append(json.loads(payload.strip()))
        return events

    def _delta(self, **delta):
        return {
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }

    def test_text_stream_event_order(self):
        events = self._run(
            [
                self._delta(role="assistant", content=""),
                self._delta(content="Hel"),
                self._delta(content="lo"),
                {
                    "id": "c",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
                {
                    "id": "c",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 3,
                        "total_tokens": 5,
                    },
                },
            ]
        )
        assert [e["type"] for e in events] == [
            "response.created",
            "response.in_progress",
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        ]

    def test_sequence_numbers_are_monotonic(self):
        events = self._run([self._delta(content="a")])
        assert [e["sequence_number"] for e in events] == list(range(len(events)))

    def test_deltas_and_final_text_agree(self):
        events = self._run([self._delta(content="Hel"), self._delta(content="lo")])
        deltas = [
            e["delta"] for e in events if e["type"] == "response.output_text.delta"
        ]
        done = next(e for e in events if e["type"] == "response.output_text.done")
        assert deltas == ["Hel", "lo"]
        assert done["text"] == "Hello"

    def test_completed_carries_full_output_and_usage(self):
        events = self._run(
            [
                self._delta(content="hi"),
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 2,
                        "total_tokens": 3,
                    },
                },
            ]
        )
        final = events[-1]["response"]
        assert final["status"] == "completed"
        assert final["output"][0]["content"][0]["text"] == "hi"
        assert final["usage"]["input_tokens"] == 1
        assert final["usage"]["output_tokens"] == 2

    def test_created_event_has_empty_output(self):
        events = self._run([self._delta(content="hi")])
        assert events[0]["response"]["status"] == "in_progress"
        assert events[0]["response"]["output"] == []

    def test_streamed_tool_call(self):
        events = self._run(
            [
                self._delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "f", "arguments": ""},
                        }
                    ]
                ),
                self._delta(
                    tool_calls=[{"index": 0, "function": {"arguments": '{"a":'}}]
                ),
                self._delta(tool_calls=[{"index": 0, "function": {"arguments": "1}"}}]),
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
        types = [e["type"] for e in events]
        assert "response.function_call_arguments.delta" in types
        done = next(
            e for e in events if e["type"] == "response.function_call_arguments.done"
        )
        assert done["arguments"] == '{"a":1}'
        item = events[-1]["response"]["output"][0]
        assert item["type"] == "function_call"
        assert item["call_id"] == "call_1"
        assert item["name"] == "f"
        assert item["status"] == "completed"

    def test_parallel_tool_calls_get_distinct_output_indexes(self):
        events = self._run(
            [
                self._delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {"name": "a", "arguments": "{}"},
                        },
                        {
                            "index": 1,
                            "id": "c2",
                            "function": {"name": "b", "arguments": "{}"},
                        },
                    ]
                ),
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
        output = events[-1]["response"]["output"]
        assert [i["call_id"] for i in output] == ["c1", "c2"]
        added = [e for e in events if e["type"] == "response.output_item.added"]
        assert [e["output_index"] for e in added] == [0, 1]

    def test_text_then_tool_call_indexes(self):
        events = self._run(
            [
                self._delta(content="thinking"),
                self._delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {"name": "a", "arguments": "{}"},
                        }
                    ]
                ),
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
        output = events[-1]["response"]["output"]
        assert [i["type"] for i in output] == ["message", "function_call"]

    def test_length_finish_emits_response_incomplete(self):
        events = self._run(
            [
                self._delta(content="cut"),
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
            ]
        )
        assert events[-1]["type"] == "response.incomplete"
        assert events[-1]["response"]["incomplete_details"] == {
            "reason": "max_output_tokens"
        }

    def test_upstream_error_chunk_fails_the_response(self):
        events = self._run([{"error": {"code": "content_filter", "message": "no"}}])
        assert [e["type"] for e in events[-2:]] == ["error", "response.failed"]
        assert events[-1]["response"]["error"]["code"] == "content_filter"

    def test_done_is_idempotent(self):
        t = ResponsesStreamTranslator(self.ECHO, id_factory=_ids())
        t.start()
        t.feed_line("data: [DONE]")
        assert t.finish() == []

    def test_ignores_comments_and_garbage_lines(self):
        t = ResponsesStreamTranslator(self.ECHO, id_factory=_ids())
        t.start()
        assert t.feed_line(": ping") == []
        assert t.feed_line("") == []
        assert t.feed_line("data: not-json") == []


# --- End-to-end: /v1/responses -------------------------------------------


def _json_reply(payload: dict, headers: dict[str, str] | None = None):
    raw = json.dumps(payload).encode()

    def handler(h, _body):
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            h.send_header(key, value)
        h.send_header("Content-Length", str(len(raw)))
        h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.write(raw)

    return handler


def _error_reply(
    status: int,
    payload: dict,
    counter: list | None = None,
    headers: dict[str, str] | None = None,
):
    raw = json.dumps(payload).encode()

    def handler(h, _body):
        if counter is not None:
            counter.append(1)
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            h.send_header(key, value)
        h.send_header("Content-Length", str(len(raw)))
        h.send_header("Connection", "close")
        h.end_headers()
        h.wfile.write(raw)

    return handler


def _sequence_replies(*handlers):
    state = {"calls": 0}

    def handler(h, body):
        index = min(state["calls"], len(handlers) - 1)
        state["calls"] += 1
        handlers[index](h, body)

    return handler, state


CHAT_OK = {
    "id": "chatcmpl-e2e",
    "object": "chat.completion",
    "created": 1700000001,
    "model": "gpt-4o",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "pong"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


class TestRateLimitRetry:
    async def test_generic_proxy_retries_then_succeeds(self, retry_client, upstream):
        client, sleeps = retry_client
        _, captured, responses_map = upstream
        handler, state = _sequence_replies(
            _error_reply(
                429,
                {"error": {"message": "slow down"}},
                headers={"Retry-After": "0.5"},
            ),
            _error_reply(
                429,
                {"error": {"message": "still busy"}},
                headers={"x-ms-retry-after-ms": "250"},
            ),
            _json_reply({"object": "list", "data": [{"embedding": [1.0]}]}),
        )
        responses_map["/openai/deployments/embed/embeddings"] = handler

        r = await client.post(
            "/v1/embeddings", json={"model": "embed", "input": "hello"}
        )

        assert r.status_code == 200
        assert state["calls"] == 3
        assert sleeps == [0.5, 0.25]
        assert len(captured) == 3

    async def test_translated_responses_retries_from_error_body(
        self, retry_client, upstream
    ):
        client, sleeps = retry_client
        _, _, responses_map = upstream
        handler, state = _sequence_replies(
            _error_reply(
                429,
                {"error": {"message": "Rate Limit Exceeded, retry after 3 seconds"}},
            ),
            _json_reply(CHAT_OK),
        )
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = handler

        r = await client.post("/v1/responses", json={"model": "gpt-4o", "input": "hi"})

        assert r.status_code == 200
        assert state["calls"] == 2
        assert sleeps == [3]

    async def test_anthropic_retries_before_translating_response(
        self, retry_client, upstream
    ):
        client, sleeps = retry_client
        _, _, responses_map = upstream
        handler, state = _sequence_replies(
            _error_reply(
                429,
                {"error": {"message": "busy"}},
                headers={"Retry-After": "1"},
            ),
            _json_reply(
                {
                    **CHAT_OK,
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": 4,
                    },
                }
            ),
        )
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = handler

        r = await client.post(
            "/v1/messages",
            json={
                "model": "gpt-4o",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert r.status_code == 200
        assert state["calls"] == 2
        assert sleeps == [1]

    async def test_stream_retries_before_first_event(self, retry_client, upstream):
        client, sleeps = retry_client
        _, _, responses_map = upstream

        def sse(h, _body):
            frame = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Content-Length", str(len(frame)))
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(frame)

        handler, state = _sequence_replies(
            _error_reply(
                429,
                {"error": {"message": "busy"}},
                headers={"Retry-After": "0.1"},
            ),
            sse,
        )
        responses_map["/openai/deployments/m/chat/completions"] = handler

        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "m", "messages": [], "stream": True},
        ) as r:
            body = await r.aread()

        assert r.status_code == 200
        assert state["calls"] == 2
        assert sleeps == [0.1]
        assert b'"content":"ok"' in body

    async def test_exhaustion_returns_final_429_headers(self, retry_client, upstream):
        client, sleeps = retry_client
        _, _, responses_map = upstream
        calls = []
        responses_map["/openai/deployments/embed/embeddings"] = _error_reply(
            429,
            {"error": {"message": "busy"}},
            calls,
            headers={"Retry-After": "1", "X-APIM-Remaining-Requests": "0"},
        )

        r = await client.post(
            "/v1/embeddings", json={"model": "embed", "input": "hello"}
        )

        assert r.status_code == 429
        assert len(calls) == 3
        assert sleeps == [1, 1]
        assert r.headers["retry-after"] == "1"
        assert r.headers["x-apim-remaining-requests"] == "0"

    async def test_wait_over_budget_does_not_retry(self, upstream):
        upstream_url, _, responses_map = upstream
        calls = []
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        responses_map["/openai/deployments/embed/embeddings"] = _error_reply(
            429,
            {"error": {"message": "busy"}},
            calls,
            headers={"Retry-After": "2"},
        )
        app, _ = _build_for(
            upstream_url,
            retry_429=2,
            retry_max_wait=0.5,
            retry_sleep=fake_sleep,
        )
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://test",
                timeout=10,
            ) as client,
        ):
            r = await client.post(
                "/v1/embeddings", json={"model": "embed", "input": "hello"}
            )

        assert r.status_code == 429
        assert len(calls) == 1
        assert sleeps == []


class TestResponsesEndpoint:
    async def test_non_streaming_roundtrip(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _json_reply(
            CHAT_OK
        )

        r = await client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": "ping", "max_output_tokens": 64},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "response"
        assert body["status"] == "completed"
        assert body["output"][0]["content"][0]["text"] == "pong"
        assert body["usage"]["input_tokens"] == 3

        sent = json.loads(captured[-1]["body"])
        assert sent["messages"] == [{"role": "user", "content": "ping"}]
        assert sent["max_tokens"] == 64
        assert "max_completion_tokens" not in sent
        assert "/deployments/gpt-4o/chat/completions" in captured[-1]["path"]

    async def test_reasoning_model_uses_max_completion_tokens(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/o3/chat/completions"] = _json_reply(CHAT_OK)

        r = await client.post(
            "/v1/responses",
            json={"model": "o3", "input": "ping", "max_output_tokens": 32},
        )
        assert r.status_code == 200
        sent = json.loads(captured[-1]["body"])
        assert sent["max_completion_tokens"] == 32
        assert "max_tokens" not in sent

    async def test_retries_once_with_the_other_token_field(self, client, upstream):
        _, captured, responses_map = upstream
        path = "/openai/deployments/gpt-4o/chat/completions"
        state = {"calls": 0}
        ok = _json_reply(CHAT_OK)
        err = json.dumps(
            {
                "error": {
                    "message": (
                        "Unsupported parameter: 'max_tokens' is not supported "
                        "with this model. Use 'max_completion_tokens' instead."
                    ),
                    "param": "max_tokens",
                    "type": "invalid_request_error",
                }
            }
        ).encode()

        def handler(h, body):
            state["calls"] += 1
            if state["calls"] == 1:
                h.send_response(400)
                h.send_header("Content-Type", "application/json")
                h.send_header("Content-Length", str(len(err)))
                h.send_header("Connection", "close")
                h.end_headers()
                h.wfile.write(err)
            else:
                ok(h, body)

        responses_map[path] = handler

        r = await client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": "hi", "max_output_tokens": 8},
        )
        assert r.status_code == 200
        assert state["calls"] == 2
        first, second = (
            json.loads(captured[-2]["body"]),
            json.loads(captured[-1]["body"]),
        )
        assert first["max_tokens"] == 8
        assert second["max_completion_tokens"] == 8
        assert "max_tokens" not in second

    async def test_unfixable_400_is_relayed(self, client, upstream):
        _, _, responses_map = upstream
        calls: list = []
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _error_reply(
            400, {"error": {"message": "content filter", "param": "prompt"}}, calls
        )
        r = await client.post("/v1/responses", json={"model": "gpt-4o", "input": "hi"})
        assert r.status_code == 400
        assert len(calls) == 1  # not retried
        assert r.json()["error"]["message"] == "content filter"

    async def test_upstream_500_is_relayed(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _error_reply(
            500, {"error": {"message": "boom"}}
        )
        r = await client.post("/v1/responses", json={"model": "gpt-4o", "input": "hi"})
        assert r.status_code == 500

    async def test_tools_translated_to_chat_shape(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _json_reply(
            CHAT_OK
        )
        await client.post(
            "/v1/responses",
            json={
                "model": "gpt-4o",
                "input": "hi",
                "tools": [
                    {"type": "function", "name": "f", "parameters": {"type": "object"}},
                    {"type": "web_search"},
                ],
                "tool_choice": "required",
            },
        )
        sent = json.loads(captured[-1]["body"])
        assert sent["tools"] == [
            {
                "type": "function",
                "function": {"name": "f", "parameters": {"type": "object"}},
            }
        ]
        assert sent["tool_choice"] == "required"

    async def test_missing_model_400(self, client):
        r = await client.post("/v1/responses", json={"input": "hi"})
        assert r.status_code == 400
        assert r.json()["error"]["param"] == "model"

    async def test_previous_response_id_400(self, client):
        r = await client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": "hi", "previous_response_id": "resp_1"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "unsupported_parameter"

    async def test_invalid_json_400(self, client):
        r = await client.post(
            "/v1/responses",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400

    async def test_default_deployment_fills_model(self, upstream):
        upstream_url, captured, responses_map = upstream
        responses_map["/openai/deployments/dep-x/chat/completions"] = _json_reply(
            CHAT_OK
        )
        app, _ = _build_for(upstream_url, default_dep="dep-x")
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            r = await c.post("/v1/responses", json={"input": "hi"})
        assert r.status_code == 200
        assert "/deployments/dep-x/" in captured[-1]["path"]

    async def test_token_field_override_from_config(self, upstream):
        upstream_url, captured, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _json_reply(
            CHAT_OK
        )
        app, _ = _build_for(upstream_url, token_field="max_completion_tokens")
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            r = await c.post(
                "/v1/responses",
                json={"model": "gpt-4o", "input": "hi", "max_output_tokens": 5},
            )
        assert r.status_code == 200
        assert json.loads(captured[-1]["body"])["max_completion_tokens"] == 5

    async def test_client_auth_header_not_forwarded(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _json_reply(
            CHAT_OK
        )
        await client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": "hi"},
            headers={"Authorization": "Bearer client-token"},
        )
        assert captured[-1]["headers"]["authorization"] == "Bearer TEST-TOKEN"

    async def test_api_key_gate_applies(self, upstream):
        upstream_url, _, _ = upstream
        app, _ = _build_for(upstream_url, api_key="sekret")
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            r = await c.post("/v1/responses", json={"model": "m", "input": "hi"})
        assert r.status_code == 401

    async def test_stored_response_operations_404(self, client):
        for method, path in [
            ("GET", "/v1/responses/resp_1"),
            ("DELETE", "/v1/responses/resp_1"),
            ("POST", "/v1/responses/resp_1/cancel"),
        ]:
            r = await client.request(method, path)
            assert r.status_code == 404, path
            assert r.json()["error"]["code"] == "not_found"

    async def test_status_reports_responses_mode(self, client):
        d = (await client.get("/status")).json()
        assert d["responses_mode"] == "translate"
        assert d["token_limit_field"] == "auto"

    async def test_passthrough_mode_forwards_verbatim(self, upstream):
        upstream_url, captured, responses_map = upstream
        responses_map["/openai/v1/responses"] = _json_reply({"ok": 1})
        app, _ = _build_for(upstream_url, responses_mode="passthrough")
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            r = await c.post("/v1/responses", json={"model": "gpt-4o", "input": "hi"})
        assert r.status_code == 200
        assert r.json() == {"ok": 1}
        assert captured[-1]["path"] == "/openai/v1/responses"
        assert json.loads(captured[-1]["body"]) == {"model": "gpt-4o", "input": "hi"}

    async def test_passthrough_root_path_normalizes_to_native_v1(self, upstream):
        upstream_url, captured, responses_map = upstream
        responses_map["/openai/v1/responses"] = _json_reply({"ok": 1})
        app, _ = _build_for(upstream_url, responses_mode="passthrough")
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            r = await c.post(
                "/responses?trace=yes", json={"model": "gpt-4o", "input": "hi"}
            )
        assert r.status_code == 200
        assert captured[-1]["path"] == "/openai/v1/responses?trace=yes"

    async def test_passthrough_stored_response_get_needs_no_model(self, upstream):
        upstream_url, captured, responses_map = upstream
        responses_map["/openai/v1/responses/resp_1"] = _json_reply({"id": "resp_1"})
        app, _ = _build_for(upstream_url, responses_mode="passthrough")
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            r = await c.get("/v1/responses/resp_1")
        assert r.status_code == 200
        assert r.json() == {"id": "resp_1"}
        assert captured[-1]["path"] == "/openai/v1/responses/resp_1"

    async def test_passthrough_stream_is_relayed_verbatim(self, upstream):
        upstream_url, captured, responses_map = upstream

        def native_sse(h, _body):
            frames = [
                b'event: response.created\ndata: {"type":"response.created"}\n\n',
                b'event: response.completed\ndata: {"type":"response.completed"}\n\n',
            ]
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for frame in frames:
                h.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
            h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()

        responses_map["/openai/v1/responses"] = native_sse
        app, _ = _build_for(upstream_url, responses_mode="passthrough")
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
            c.stream(
                "POST",
                "/v1/responses",
                json={"model": "gpt-4o", "input": "hi", "stream": True},
            ) as r,
        ):
            body = "".join([part async for part in r.aiter_text()])
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        assert r.headers["cache-control"] == "no-cache"
        assert "event: response.created" in body
        assert "event: response.completed" in body
        assert captured[-1]["path"] == "/openai/v1/responses"

    async def test_translate_preserves_upstream_rate_limit_headers(self, upstream):
        upstream_url, _, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _error_reply(
            429,
            {"error": {"message": "retry after 7 seconds"}},
            headers={
                "Retry-After": "7",
                "X-RateLimit-Reset-Requests": "7",
                "X-APIM-Remaining-Requests": "0",
            },
        )
        app, _ = _build_for(upstream_url)
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            r = await c.post("/v1/responses", json={"model": "gpt-4o", "input": "hi"})
        assert r.status_code == 429
        assert r.headers["retry-after"] == "7"
        assert r.headers["x-ratelimit-reset-requests"] == "7"
        assert r.headers["x-apim-remaining-requests"] == "0"

    async def test_translate_preserves_success_rate_limit_headers(self, upstream):
        upstream_url, _, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _json_reply(
            CHAT_OK,
            headers={
                "X-RateLimit-Remaining-Requests": "41",
                "X-RateLimit-Reset-Requests": "2",
            },
        )
        app, _ = _build_for(upstream_url)
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            r = await c.post("/v1/responses", json={"model": "gpt-4o", "input": "hi"})
        assert r.status_code == 200
        assert r.headers["x-ratelimit-remaining-requests"] == "41"
        assert r.headers["x-ratelimit-reset-requests"] == "2"

    async def test_translate_slash_model_uses_native_chat_route(self, upstream):
        upstream_url, captured, responses_map = upstream
        responses_map["/openai/v1/chat/completions"] = _json_reply(CHAT_OK)
        app, _ = _build_for(upstream_url)
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            r = await c.post(
                "/v1/responses",
                json={"model": "Qwen/Qwen3.5-27B", "input": "hi"},
            )
        assert r.status_code == 200
        assert captured[-1]["path"] == "/openai/v1/chat/completions"
        assert json.loads(captured[-1]["body"])["model"] == "Qwen/Qwen3.5-27B"


class TestResponsesAutoMode:
    @staticmethod
    def _catalogue(model: str, responses):
        return {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "capabilities": {"responses": responses},
                }
            ],
        }

    @pytest.mark.parametrize("capability", [True, "true", "TRUE"])
    async def test_supported_model_uses_native(self, auto_client, upstream, capability):
        _, captured, responses_map = upstream
        responses_map["/openai/models"] = _json_reply(
            self._catalogue("gpt-native", capability)
        )
        responses_map["/openai/v1/responses"] = _json_reply(
            {"id": "resp_native", "status": "completed"}
        )

        r = await auto_client.post(
            "/v1/responses", json={"model": "gpt-native", "input": "hi"}
        )

        assert r.status_code == 200
        assert captured[-1]["path"] == "/openai/v1/responses"

    @pytest.mark.parametrize(
        "capability",
        [
            False,
            "false",
            None,
        ],
    )
    async def test_unsupported_model_uses_translation(
        self, auto_client, upstream, capability
    ):
        _, captured, responses_map = upstream
        responses_map["/openai/models"] = _json_reply(
            self._catalogue("gpt-chat", capability)
        )
        responses_map["/openai/deployments/gpt-chat/chat/completions"] = _json_reply(
            CHAT_OK
        )

        r = await auto_client.post(
            "/v1/responses", json={"model": "gpt-chat", "input": "hi"}
        )

        assert r.status_code == 200
        assert captured[-1]["path"].startswith(
            "/openai/deployments/gpt-chat/chat/completions?"
        )

    async def test_unknown_model_uses_translation(self, auto_client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/models"] = _json_reply(
            self._catalogue("some-other-model", True)
        )
        responses_map["/openai/deployments/unknown/chat/completions"] = _json_reply(
            CHAT_OK
        )

        r = await auto_client.post(
            "/v1/responses", json={"model": "unknown", "input": "hi"}
        )

        assert r.status_code == 200
        assert "/deployments/unknown/chat/completions" in captured[-1]["path"]

    async def test_catalogue_failure_falls_back_to_translation(
        self, auto_client, upstream
    ):
        _, captured, responses_map = upstream
        responses_map["/openai/models"] = _error_reply(
            503, {"error": {"message": "catalogue unavailable"}}
        )
        responses_map["/openai/deployments/gpt-chat/chat/completions"] = _json_reply(
            CHAT_OK
        )

        r = await auto_client.post(
            "/v1/responses", json={"model": "gpt-chat", "input": "hi"}
        )

        assert r.status_code == 200
        assert [entry["path"].split("?")[0] for entry in captured] == [
            "/openai/models",
            "/openai/deployments/gpt-chat/chat/completions",
        ]

    async def test_catalogue_refresh_retries_429(self, upstream):
        upstream_url, _, responses_map = upstream
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        catalogue, state = _sequence_replies(
            _error_reply(
                429,
                {"error": {"message": "busy"}},
                headers={"Retry-After": "0.1"},
            ),
            _json_reply(self._catalogue("gpt-native", True)),
        )
        responses_map["/openai/models"] = catalogue
        responses_map["/openai/v1/responses"] = _json_reply(
            {"id": "resp_native", "status": "completed"}
        )
        app, _ = _build_for(
            upstream_url,
            responses_mode="auto",
            retry_429=1,
            retry_sleep=fake_sleep,
        )
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://test",
                timeout=10,
            ) as client,
        ):
            r = await client.post(
                "/v1/responses", json={"model": "gpt-native", "input": "hi"}
            )

        assert r.status_code == 200
        assert state["calls"] == 2
        assert sleeps == [0.1]

    async def test_catalogue_is_cached(self, auto_client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/models"] = _json_reply(
            self._catalogue("gpt-chat", False)
        )
        responses_map["/openai/deployments/gpt-chat/chat/completions"] = _json_reply(
            CHAT_OK
        )

        for _ in range(2):
            r = await auto_client.post(
                "/v1/responses", json={"model": "gpt-chat", "input": "hi"}
            )
            assert r.status_code == 200

        paths = [entry["path"].split("?")[0] for entry in captured]
        assert paths.count("/openai/models") == 1
        assert paths.count("/openai/deployments/gpt-chat/chat/completions") == 2

    async def test_concurrent_requests_share_one_catalogue_refresh(
        self, auto_client, upstream
    ):
        _, captured, responses_map = upstream
        catalogue_reply = _json_reply(self._catalogue("gpt-chat", False))

        def slow_catalogue(h, body):
            time.sleep(0.05)
            catalogue_reply(h, body)

        responses_map["/openai/models"] = slow_catalogue
        responses_map["/openai/deployments/gpt-chat/chat/completions"] = _json_reply(
            CHAT_OK
        )

        replies = await asyncio.gather(
            *[
                auto_client.post(
                    "/v1/responses", json={"model": "gpt-chat", "input": str(index)}
                )
                for index in range(10)
            ]
        )

        assert all(reply.status_code == 200 for reply in replies)
        paths = [entry["path"].split("?")[0] for entry in captured]
        assert paths.count("/openai/models") == 1

    async def test_default_deployment_is_injected_for_native_request(self, upstream):
        upstream_url, captured, responses_map = upstream
        responses_map["/openai/models"] = _json_reply(
            self._catalogue("gpt-default", True)
        )
        responses_map["/openai/v1/responses"] = _json_reply(
            {"id": "resp_native", "status": "completed"}
        )
        app, _ = _build_for(
            upstream_url,
            responses_mode="auto",
            default_dep="gpt-default",
        )
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://test",
                timeout=10,
            ) as client,
        ):
            r = await client.post("/v1/responses", json={"input": "hi"})

        assert r.status_code == 200
        assert json.loads(captured[-1]["body"])["model"] == "gpt-default"

    async def test_model_capability_wins_over_request_features(
        self, auto_client, upstream
    ):
        _, captured, responses_map = upstream
        responses_map["/openai/models"] = _json_reply(
            self._catalogue("gpt-chat", False)
        )
        responses_map["/openai/deployments/gpt-chat/chat/completions"] = _json_reply(
            CHAT_OK
        )

        r = await auto_client.post(
            "/v1/responses",
            json={"model": "gpt-chat", "input": "hi", "store": True},
        )

        assert r.status_code == 200
        assert [entry["path"].split("?")[0] for entry in captured] == [
            "/openai/models",
            "/openai/deployments/gpt-chat/chat/completions",
        ]

    async def test_stored_operations_use_native_passthrough(
        self, auto_client, upstream
    ):
        _, captured, responses_map = upstream
        responses_map["/openai/v1/responses/resp_1"] = _json_reply({"id": "resp_1"})

        r = await auto_client.get("/v1/responses/resp_1")

        assert r.status_code == 200
        assert captured[-1]["path"] == "/openai/v1/responses/resp_1"

    async def test_status_reports_auto(self, auto_client):
        status = (await auto_client.get("/status")).json()
        assert status["responses_mode"] == "auto"


class TestResponsesStreamingEndpoint:
    def _chat_sse(self, chunks: list[str], headers: dict[str, str] | None = None):
        def handler(h, _body):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            for key, value in (headers or {}).items():
                h.send_header(key, value)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for payload in chunks:
                frame = f"data: {payload}\n\n".encode()
                h.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
                h.wfile.flush()
            h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()

        return handler

    def _parse(self, text: str) -> list[dict]:
        events = []
        for block in text.split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return events

    async def test_stream_translates_to_responses_events(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = self._chat_sse(
            [
                json.dumps(
                    {
                        "id": "chatcmpl-s",
                        "model": "gpt-4o",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "He"},
                                "finish_reason": None,
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "id": "chatcmpl-s",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "y"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "id": "chatcmpl-s",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 4,
                            "completion_tokens": 2,
                            "total_tokens": 6,
                        },
                    }
                ),
                "[DONE]",
            ],
            headers={"X-RateLimit-Remaining-Tokens": "1234"},
        )

        async with client.stream(
            "POST",
            "/v1/responses",
            json={"model": "gpt-4o", "input": "hi", "stream": True},
        ) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            assert r.headers["cache-control"] == "no-cache"
            assert r.headers["x-ratelimit-remaining-tokens"] == "1234"
            text = "".join([piece async for piece in r.aiter_text()])

        events = self._parse(text)
        kinds = [e["type"] for e in events]
        assert kinds[0] == "response.created"
        assert kinds[-1] == "response.completed"
        assert "response.output_text.delta" in kinds
        assert "event: response.completed" in text

        final = events[-1]["response"]
        assert final["output"][0]["content"][0]["text"] == "Hey"
        assert final["usage"]["total_tokens"] == 6

        sent = json.loads(captured[-1]["body"])
        assert sent["stream"] is True
        assert sent["stream_options"] == {"include_usage": True}

    async def test_stream_error_before_first_chunk_is_json(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _error_reply(
            403, {"error": {"message": "denied"}}
        )
        r = await client.post(
            "/v1/responses", json={"model": "gpt-4o", "input": "hi", "stream": True}
        )
        assert r.status_code == 403
        assert r.json()["error"]["message"] == "denied"

    async def test_stream_chunks_arrive_incrementally(self, live_proxy):
        base, responses_map = live_proxy

        def handler(h, _body):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for i in range(3):
                payload = json.dumps(
                    {
                        "id": "c",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": f"t{i}"},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                frame = f"data: {payload}\n\n".encode()
                h.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
                h.wfile.flush()
                time.sleep(0.1)
            h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()

        responses_map["/openai/deployments/gpt-4o/chat/completions"] = handler

        ts: list[float] = []
        async with (
            httpx.AsyncClient(timeout=10) as c,
            c.stream(
                "POST",
                f"{base}/v1/responses",
                json={"model": "gpt-4o", "input": "hi", "stream": True},
            ) as r,
        ):
            async for _ in r.aiter_raw():
                ts.append(time.time())
        assert ts[-1] - ts[0] >= 0.15, "responses stream appears buffered"


# --- Unit: Anthropic Messages ⇄ chat completions adapter ------------------


class TestAnthropicMessagesToChat:
    def test_string_content(self):
        assert anthropic_messages_to_chat([{"role": "user", "content": "hi"}]) == [
            {"role": "user", "content": "hi"}
        ]

    def test_system_becomes_leading_message(self):
        msgs = anthropic_messages_to_chat(
            [{"role": "user", "content": "hi"}], "be terse"
        )
        assert msgs[0] == {"role": "system", "content": "be terse"}

    def test_system_blocks_are_flattened(self):
        msgs = anthropic_messages_to_chat(
            [{"role": "user", "content": "hi"}],
            [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        )
        assert msgs[0]["content"] == "ab"

    def test_system_role_is_configurable(self):
        msgs = anthropic_messages_to_chat(
            [{"role": "user", "content": "hi"}], "x", system_role="developer"
        )
        assert msgs[0]["role"] == "developer"

    def test_reasoning_model_gets_developer_system(self):
        plan = build_chat_request_from_messages(
            {
                "model": "o3",
                "max_tokens": 1,
                "system": "x",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        assert plan.payload["messages"][0]["role"] == "developer"

    def test_text_blocks_collapse(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "text", "text": "b"},
                    ],
                }
            ]
        )
        assert msgs[0]["content"] == "ab"

    def test_base64_image_becomes_data_url(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": "QUJD",
                            },
                        },
                    ],
                }
            ]
        )
        assert msgs[0]["content"][1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,QUJD"},
        }

    def test_url_image(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "url", "url": "https://x"}}
                    ],
                }
            ]
        )
        assert msgs[0]["content"] == [
            {"type": "image_url", "image_url": {"url": "https://x"}}
        ]

    def test_pdf_document_becomes_file_part(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "title": "r.pdf",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": "QUJD",
                            },
                        }
                    ],
                }
            ]
        )
        assert msgs[0]["content"] == [
            {
                "type": "file",
                "file": {
                    "filename": "r.pdf",
                    "file_data": "data:application/pdf;base64,QUJD",
                },
            }
        ]

    def test_plain_text_document_becomes_text(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "text", "data": "notes"},
                        }
                    ],
                }
            ]
        )
        assert msgs[0]["content"] == "notes"

    def test_tool_use_becomes_tool_calls(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "checking"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "get_weather",
                            "input": {"city": "SH"},
                        },
                    ],
                }
            ]
        )
        assert msgs[0]["content"] == "checking"
        assert msgs[0]["tool_calls"] == [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city": "SH"}',
                },
            }
        ]

    def test_tool_result_splits_into_tool_message(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "20C",
                        },
                        {"type": "text", "text": "thanks"},
                    ],
                }
            ]
        )
        assert msgs == [
            {"role": "tool", "tool_call_id": "toolu_1", "content": "20C"},
            {"role": "user", "content": "thanks"},
        ]

    def test_tool_result_only_emits_no_user_message(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t", "content": "ok"}
                    ],
                }
            ]
        )
        assert msgs == [{"role": "tool", "tool_call_id": "t", "content": "ok"}]

    def test_tool_result_block_content_is_flattened(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": [{"type": "text", "text": "20C"}],
                        }
                    ],
                }
            ]
        )
        assert msgs[0]["content"] == "20C"

    def test_tool_result_error_is_marked(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": "boom",
                            "is_error": True,
                        }
                    ],
                }
            ]
        )
        assert msgs[0]["content"] == "Error: boom"

    def test_multiple_tool_results_in_one_turn(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "a", "content": "1"},
                        {"type": "tool_result", "tool_use_id": "b", "content": "2"},
                    ],
                }
            ]
        )
        assert [m["tool_call_id"] for m in msgs] == ["a", "b"]

    def test_thinking_blocks_are_skipped(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "hmm", "signature": "s"},
                        {"type": "text", "text": "answer"},
                    ],
                }
            ]
        )
        assert msgs[0]["content"] == "answer"

    def test_bad_role_raises(self):
        with pytest.raises(AnthropicError):
            anthropic_messages_to_chat([{"role": "system", "content": "x"}])

    def test_messages_must_be_array(self):
        with pytest.raises(AnthropicError):
            anthropic_messages_to_chat({"role": "user"})


class TestAnthropicTools:
    def test_input_schema_becomes_parameters(self):
        tools, dropped = anthropic_tools_to_chat(
            [
                {
                    "name": "get_weather",
                    "description": "d",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ]
        )
        assert dropped == []
        assert tools == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {"type": "object", "properties": {}},
                    "description": "d",
                },
            }
        ]

    def test_server_side_tools_dropped(self):
        tools, dropped = anthropic_tools_to_chat(
            [
                {"type": "web_search_20250305", "name": "web_search"},
                {"name": "f", "input_schema": {"type": "object"}},
            ]
        )
        assert dropped == ["web_search_20250305"]
        assert len(tools) == 1

    def test_tools_must_be_array(self):
        with pytest.raises(AnthropicError):
            anthropic_tools_to_chat({"name": "f"})

    @pytest.mark.parametrize(
        "choice,expected",
        [
            ({"type": "auto"}, "auto"),
            ({"type": "any"}, "required"),
            ({"type": "none"}, "none"),
        ],
    )
    def test_tool_choice_modes(self, choice, expected):
        assert anthropic_tool_choice_to_chat(choice)[0] == expected

    def test_named_tool_choice(self):
        assert anthropic_tool_choice_to_chat({"type": "tool", "name": "f"})[0] == {
            "type": "function",
            "function": {"name": "f"},
        }

    def test_disable_parallel_tool_use(self):
        _, parallel = anthropic_tool_choice_to_chat(
            {"type": "auto", "disable_parallel_tool_use": True}
        )
        assert parallel is False

    def test_tool_choice_reaches_payload(self):
        plan = build_chat_request_from_messages(
            {
                "model": "gpt-4o",
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"name": "f", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
            }
        )
        assert plan.payload["tool_choice"] == "required"
        assert plan.payload["parallel_tool_calls"] is False


class TestThinkingBudget:
    @pytest.mark.parametrize(
        "budget,effort",
        [(1024, "low"), (2048, "low"), (4096, "medium"), (30000, "high")],
    )
    def test_budget_maps_to_effort(self, budget, effort):
        assert (
            thinking_to_reasoning_effort({"type": "enabled", "budget_tokens": budget})
            == effort
        )

    def test_disabled_thinking_is_none(self):
        assert thinking_to_reasoning_effort({"type": "disabled"}) is None
        assert thinking_to_reasoning_effort(None) is None

    def test_enabled_without_budget_defaults_medium(self):
        assert thinking_to_reasoning_effort({"type": "enabled"}) == "medium"


class TestBuildChatRequestFromMessages:
    def _plan(self, **overrides):
        body = {
            "model": "gpt-4o",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        }
        body.update(overrides)
        return build_chat_request_from_messages(body)

    def test_max_tokens_is_required(self):
        with pytest.raises(AnthropicError) as exc:
            build_chat_request_from_messages(
                {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
            )
        assert exc.value.param == "max_tokens"

    def test_count_tokens_mode_allows_missing_max_tokens(self):
        plan = build_chat_request_from_messages(
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            require_max_tokens=False,
        )
        assert "max_tokens" not in plan.payload

    def test_anthropic_max_tokens_maps_to_the_right_field(self):
        assert self._plan().payload["max_tokens"] == 100
        reasoning = build_chat_request_from_messages(
            {
                "model": "o3",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        assert reasoning.payload["max_completion_tokens"] == 100
        assert "max_tokens" not in reasoning.payload

    def test_token_field_override(self):
        plan = build_chat_request_from_messages(
            {
                "model": "gpt-4o",
                "max_tokens": 7,
                "messages": [{"role": "user", "content": "hi"}],
            },
            token_field_override="max_completion_tokens",
        )
        assert plan.payload["max_completion_tokens"] == 7

    def test_stop_sequences(self):
        assert self._plan(stop_sequences=["END"]).payload["stop"] == ["END"]

    def test_metadata_user_id(self):
        assert self._plan(metadata={"user_id": "u1"}).payload["user"] == "u1"

    def test_sampling_passthrough(self):
        payload = self._plan(temperature=0.3, top_p=0.8).payload
        assert payload["temperature"] == 0.3 and payload["top_p"] == 0.8

    def test_top_k_is_not_forwarded(self):
        assert "top_k" not in self._plan(top_k=5).payload

    def test_thinking_maps_to_reasoning_effort(self):
        payload = self._plan(
            thinking={"type": "enabled", "budget_tokens": 20000}
        ).payload
        assert payload["reasoning_effort"] == "high"

    def test_stream_requests_usage(self):
        plan = self._plan(stream=True)
        assert plan.stream is True
        assert plan.payload["stream_options"] == {"include_usage": True}

    def test_default_deployment(self):
        plan = build_chat_request_from_messages(
            {"max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            default_model="dep-1",
        )
        assert plan.payload["model"] == "dep-1"

    def test_missing_model_raises(self):
        with pytest.raises(AnthropicError) as exc:
            build_chat_request_from_messages(
                {"max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            )
        assert exc.value.param == "model"

    def test_missing_messages_raises(self):
        with pytest.raises(AnthropicError) as exc:
            build_chat_request_from_messages({"model": "gpt-4o", "max_tokens": 1})
        assert exc.value.param == "messages"

    def test_non_object_body_raises(self):
        with pytest.raises(AnthropicError):
            build_chat_request_from_messages("nope")


class TestChatToAnthropicMessage:
    ECHO: ClassVar = {"model": "gpt-4o"}

    def _chat(self, **overrides):
        chat = {
            "id": "chatcmpl-abc",
            "model": "gpt-4o-2024-11-20",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        chat.update(overrides)
        return chat

    def test_basic_shape(self):
        out = chat_to_anthropic_message(self._chat(), self.ECHO, id_factory=_ids())
        assert out["id"] == "msg_chatcmplabc"
        assert out["type"] == "message"
        assert out["role"] == "assistant"
        assert out["model"] == "gpt-4o-2024-11-20"
        assert out["content"] == [{"type": "text", "text": "hello"}]
        assert out["stop_reason"] == "end_turn"
        assert out["stop_sequence"] is None

    def test_usage_excludes_cached_from_input(self):
        chat = self._chat(
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 4},
            }
        )
        out = chat_to_anthropic_message(chat, self.ECHO, id_factory=_ids())
        assert out["usage"] == {
            "input_tokens": 6,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 4,
        }

    @pytest.mark.parametrize(
        "finish,stop_reason",
        [
            ("stop", "end_turn"),
            ("length", "max_tokens"),
            ("tool_calls", "tool_use"),
            ("content_filter", "refusal"),
            (None, "end_turn"),
        ],
    )
    def test_stop_reason_mapping(self, finish, stop_reason):
        chat = self._chat(
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "x"},
                    "finish_reason": finish,
                }
            ]
        )
        out = chat_to_anthropic_message(chat, self.ECHO, id_factory=_ids())
        assert out["stop_reason"] == stop_reason

    def test_tool_calls_become_tool_use_blocks(self):
        chat = self._chat(
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "checking",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"SH"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        )
        out = chat_to_anthropic_message(chat, self.ECHO, id_factory=_ids())
        assert out["content"] == [
            {"type": "text", "text": "checking"},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "get_weather",
                "input": {"city": "SH"},
            },
        ]

    def test_malformed_tool_arguments_degrade_to_empty_object(self):
        chat = self._chat(
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c",
                                "function": {"name": "f", "arguments": "{oops"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        )
        out = chat_to_anthropic_message(chat, self.ECHO, id_factory=_ids())
        assert out["content"][0]["input"] == {}

    def test_reasoning_content_becomes_thinking_block(self):
        chat = self._chat(
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "answer",
                        "reasoning_content": "hmm",
                    },
                    "finish_reason": "stop",
                }
            ]
        )
        out = chat_to_anthropic_message(chat, self.ECHO, id_factory=_ids())
        assert out["content"][0] == {
            "type": "thinking",
            "thinking": "hmm",
            "signature": "",
        }

    def test_empty_choices_yields_empty_content(self):
        out = chat_to_anthropic_message(
            self._chat(choices=[]), self.ECHO, id_factory=_ids()
        )
        assert out["content"] == []


class TestAnthropicStreamTranslator:
    ECHO: ClassVar = {"model": "gpt-4o"}

    def _run(self, chunks: list[dict]) -> list[dict]:
        t = AnthropicStreamTranslator(self.ECHO, id_factory=_ids())
        frames = list(t.start())
        for chunk in chunks:
            frames += t.feed_line("data: " + json.dumps(chunk))
        frames += t.feed_line("data: [DONE]")
        events = []
        for frame in frames:
            head, _, payload = frame.decode().partition("\ndata: ")
            assert head.startswith("event: ")
            event = json.loads(payload.strip())
            assert head[len("event: ") :] == event["type"]
            events.append(event)
        return events

    def _delta(self, **delta):
        return {
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }

    def test_text_event_order(self):
        events = self._run(
            [
                self._delta(content="Hel"),
                self._delta(content="lo"),
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                },
            ]
        )
        assert [e["type"] for e in events] == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]

    def test_message_start_shape(self):
        events = self._run([self._delta(content="a")])
        message = events[0]["message"]
        assert message["type"] == "message"
        assert message["role"] == "assistant"
        assert message["content"] == []
        assert message["stop_reason"] is None
        assert message["usage"]["input_tokens"] == 0

    def test_text_deltas(self):
        events = self._run([self._delta(content="Hel"), self._delta(content="lo")])
        deltas = [
            e["delta"]["text"] for e in events if e["type"] == "content_block_delta"
        ]
        assert deltas == ["Hel", "lo"]
        assert events[1]["content_block"] == {"type": "text", "text": ""}

    def test_message_delta_carries_stop_reason_and_usage(self):
        events = self._run(
            [
                self._delta(content="a"),
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 9,
                        "total_tokens": 14,
                    },
                },
            ]
        )
        delta = next(e for e in events if e["type"] == "message_delta")
        assert delta["delta"] == {"stop_reason": "max_tokens", "stop_sequence": None}
        assert delta["usage"]["output_tokens"] == 9
        assert delta["usage"]["input_tokens"] == 5

    def test_tool_use_stream(self):
        events = self._run(
            [
                self._delta(content="checking "),
                self._delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": ""},
                        }
                    ]
                ),
                self._delta(
                    tool_calls=[{"index": 0, "function": {"arguments": '{"a":'}}]
                ),
                self._delta(tool_calls=[{"index": 0, "function": {"arguments": "1}"}}]),
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
        kinds = [e["type"] for e in events]
        assert kinds == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
        starts = [e for e in events if e["type"] == "content_block_start"]
        assert starts[0]["index"] == 0
        assert starts[1]["index"] == 1
        assert starts[1]["content_block"] == {
            "type": "tool_use",
            "id": "call_1",
            "name": "get_weather",
            "input": {},
        }
        partials = [
            e["delta"]["partial_json"]
            for e in events
            if e["type"] == "content_block_delta"
            and e["delta"]["type"] == "input_json_delta"
        ]
        assert "".join(partials) == '{"a":1}'

    def test_parallel_tool_calls_get_distinct_indexes(self):
        events = self._run(
            [
                self._delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {"name": "a", "arguments": "{}"},
                        },
                        {
                            "index": 1,
                            "id": "c2",
                            "function": {"name": "b", "arguments": "{}"},
                        },
                    ]
                ),
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
        starts = [e for e in events if e["type"] == "content_block_start"]
        assert [e["index"] for e in starts] == [0, 1]
        assert [e["content_block"]["id"] for e in starts] == ["c1", "c2"]
        stops = [e["index"] for e in events if e["type"] == "content_block_stop"]
        assert stops == [0, 1]

    def test_every_started_block_is_stopped(self):
        events = self._run(
            [
                self._delta(content="x"),
                self._delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "c",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ]
                ),
            ]
        )
        started = [e["index"] for e in events if e["type"] == "content_block_start"]
        stopped = [e["index"] for e in events if e["type"] == "content_block_stop"]
        assert sorted(started) == sorted(stopped)

    def test_upstream_error_emits_error_event(self):
        events = self._run([{"error": {"type": "overloaded_error", "message": "busy"}}])
        assert events[-1]["type"] == "error"
        assert events[-1]["error"] == {"type": "overloaded_error", "message": "busy"}

    def test_done_is_idempotent(self):
        t = AnthropicStreamTranslator(self.ECHO, id_factory=_ids())
        t.start()
        t.feed_line("data: [DONE]")
        assert t.finish() == []

    def test_garbage_lines_ignored(self):
        t = AnthropicStreamTranslator(self.ECHO, id_factory=_ids())
        t.start()
        assert t.feed_line(": ping") == []
        assert t.feed_line("data: nope") == []


class TestOpenAIModelsToAnthropic:
    def test_shape(self):
        out = openai_models_to_anthropic(
            {
                "object": "list",
                "data": [
                    {"id": "gpt-4o", "object": "model", "created": 1700000000},
                    {"id": "o3", "object": "model", "created": 1740000000},
                ],
            }
        )
        assert out["has_more"] is False
        assert out["first_id"] == "gpt-4o"
        assert out["last_id"] == "o3"
        assert out["data"][0] == {
            "type": "model",
            "id": "gpt-4o",
            "display_name": "gpt-4o",
            "created_at": "2023-11-14T22:13:20Z",
        }

    def test_empty_listing(self):
        out = openai_models_to_anthropic({"data": []})
        assert out["data"] == [] and out["first_id"] is None

    def test_garbage_entries_skipped(self):
        out = openai_models_to_anthropic({"data": ["x", {}, {"id": "ok"}]})
        assert [m["id"] for m in out["data"]] == ["ok"]

    def test_non_dict_payload(self):
        assert openai_models_to_anthropic(None)["data"] == []


# --- End-to-end: /v1/messages --------------------------------------------

ANTHROPIC_CHAT_OK = {
    "id": "chatcmpl-anth",
    "object": "chat.completion",
    "created": 1700000002,
    "model": "gpt-4o",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "pong"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13},
}


class TestAnthropicMessagesEndpoint:
    async def test_non_streaming_roundtrip(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _json_reply(
            ANTHROPIC_CHAT_OK,
            headers={
                "X-RateLimit-Remaining-Requests": "55",
                "X-RateLimit-Reset-Requests": "3",
            },
        )
        r = await client.post(
            "/v1/messages",
            json={
                "model": "gpt-4o",
                "max_tokens": 128,
                "system": "be terse",
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["content"] == [{"type": "text", "text": "pong"}]
        assert body["stop_reason"] == "end_turn"
        assert body["usage"]["input_tokens"] == 12
        assert r.headers["x-ratelimit-remaining-requests"] == "55"
        assert r.headers["x-ratelimit-reset-requests"] == "3"

        sent = json.loads(captured[-1]["body"])
        assert sent["messages"] == [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "ping"},
        ]
        assert sent["max_tokens"] == 128
        assert "max_completion_tokens" not in sent

    async def test_reasoning_model_uses_max_completion_tokens(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/o3/chat/completions"] = _json_reply(
            ANTHROPIC_CHAT_OK
        )
        r = await client.post(
            "/v1/messages",
            json={
                "model": "o3",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r.status_code == 200
        sent = json.loads(captured[-1]["body"])
        assert sent["max_completion_tokens"] == 32
        assert "max_tokens" not in sent

    async def test_token_field_retry(self, client, upstream):
        _, captured, responses_map = upstream
        state = {"calls": 0}
        ok = _json_reply(ANTHROPIC_CHAT_OK)
        err = json.dumps(
            {
                "error": {
                    "message": (
                        "Unsupported parameter: 'max_tokens' is not supported "
                        "with this model. Use 'max_completion_tokens' instead."
                    ),
                    "param": "max_tokens",
                }
            }
        ).encode()

        def handler(h, body):
            state["calls"] += 1
            if state["calls"] == 1:
                h.send_response(400)
                h.send_header("Content-Type", "application/json")
                h.send_header("Content-Length", str(len(err)))
                h.send_header("Connection", "close")
                h.end_headers()
                h.wfile.write(err)
            else:
                ok(h, body)

        responses_map["/openai/deployments/gpt-4o/chat/completions"] = handler
        r = await client.post(
            "/v1/messages",
            json={
                "model": "gpt-4o",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r.status_code == 200
        assert state["calls"] == 2
        assert json.loads(captured[-1]["body"])["max_completion_tokens"] == 8

    async def test_missing_max_tokens_400_anthropic_shape(self, client):
        r = await client.post(
            "/v1/messages",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 400
        body = r.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "invalid_request_error"
        assert "max_tokens" in body["error"]["message"]

    async def test_upstream_error_uses_anthropic_shape(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _error_reply(
            429,
            {"error": {"message": "slow down", "param": "prompt"}},
            headers={"Retry-After": "9", "X-RateLimit-Reset-Tokens": "9"},
        )
        r = await client.post(
            "/v1/messages",
            json={
                "model": "gpt-4o",
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r.status_code == 429
        assert r.json() == {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "slow down"},
        }
        assert r.headers["retry-after"] == "9"
        assert r.headers["x-ratelimit-reset-tokens"] == "9"

    async def test_invalid_json_400(self, client):
        r = await client.post(
            "/v1/messages",
            content=b"{nope",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400
        assert r.json()["type"] == "error"

    async def test_x_api_key_authenticates(self, upstream):
        upstream_url, _, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _json_reply(
            ANTHROPIC_CHAT_OK
        )
        app, _ = _build_for(upstream_url, api_key="sekret")
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            payload = {
                "model": "gpt-4o",
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "hi"}],
            }
            ok = await c.post(
                "/v1/messages", json=payload, headers={"x-api-key": "sekret"}
            )
            bad = await c.post(
                "/v1/messages", json=payload, headers={"x-api-key": "wrong"}
            )
        assert ok.status_code == 200
        assert bad.status_code == 401
        assert bad.json()["error"]["type"] == "authentication_error"

    async def test_anthropic_headers_are_not_forwarded(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _json_reply(
            ANTHROPIC_CHAT_OK
        )
        await client.post(
            "/v1/messages",
            json={
                "model": "gpt-4o",
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={
                "x-api-key": "client-key",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "tools-2024",
            },
        )
        hdrs = captured[-1]["headers"]
        assert hdrs["authorization"] == "Bearer TEST-TOKEN"
        assert "x-api-key" not in hdrs
        assert "anthropic-version" not in hdrs
        assert "anthropic-beta" not in hdrs

    async def test_bare_and_anthropic_prefixes_both_work(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _json_reply(
            ANTHROPIC_CHAT_OK
        )
        payload = {
            "model": "gpt-4o",
            "max_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}],
        }
        for path in ("/v1/messages", "/messages", "/anthropic/v1/messages"):
            r = await client.post(path, json=payload)
            assert r.status_code == 200, path

    async def test_default_deployment(self, upstream):
        upstream_url, captured, responses_map = upstream
        responses_map["/openai/deployments/dep-y/chat/completions"] = _json_reply(
            ANTHROPIC_CHAT_OK
        )
        app, _ = _build_for(upstream_url, default_dep="dep-y")
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            r = await c.post(
                "/v1/messages",
                json={
                    "max_tokens": 5,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert r.status_code == 200
        assert "/deployments/dep-y/" in captured[-1]["path"]

    async def test_passthrough_mode_forwards_verbatim(self, upstream):
        upstream_url, captured, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/messages"] = _json_reply({"ok": 2})
        app, _ = _build_for(upstream_url, anthropic_mode="passthrough")
        async with (
            LifespanManager(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
            ) as c,
        ):
            r = await c.post(
                "/v1/messages",
                json={
                    "model": "gpt-4o",
                    "max_tokens": 5,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert r.status_code == 200
        assert r.json() == {"ok": 2}
        assert "/deployments/gpt-4o/messages" in captured[-1]["path"]

    async def test_status_reports_anthropic_mode(self, client):
        assert (await client.get("/status")).json()["anthropic_mode"] == "translate"


class TestAnthropicCountTokens:
    async def test_counts_prompt_tokens(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _json_reply(
            ANTHROPIC_CHAT_OK
        )
        r = await client.post(
            "/v1/messages/count_tokens",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        assert r.json() == {"input_tokens": 12}
        sent = json.loads(captured[-1]["body"])
        assert sent["max_tokens"] == 1
        assert "stream" not in sent

    async def test_uses_reasoning_token_field(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/o3/chat/completions"] = _json_reply(
            ANTHROPIC_CHAT_OK
        )
        r = await client.post(
            "/v1/messages/count_tokens",
            json={"model": "o3", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        sent = json.loads(captured[-1]["body"])
        assert sent["max_completion_tokens"] == 1
        assert "max_tokens" not in sent

    async def test_client_max_tokens_is_overridden(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _json_reply(
            ANTHROPIC_CHAT_OK
        )
        await client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "gpt-4o",
                "max_tokens": 999,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert json.loads(captured[-1]["body"])["max_tokens"] == 1

    async def test_upstream_error_relayed(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _error_reply(
            403, {"error": {"message": "nope"}}
        )
        r = await client.post(
            "/v1/messages/count_tokens",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 403
        assert r.json()["error"]["type"] == "permission_error"


class TestAnthropicModels:
    MODELS: ClassVar = {
        "object": "list",
        "data": [
            {"id": "gpt-4o", "object": "model", "created": 1700000000},
            {"id": "o3", "object": "model", "created": 1740000000},
        ],
    }

    async def test_list(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/models"] = _json_reply(self.MODELS)
        r = await client.get("/anthropic/v1/models")
        assert r.status_code == 200
        body = r.json()
        assert [m["id"] for m in body["data"]] == ["gpt-4o", "o3"]
        assert body["has_more"] is False
        assert "/deployments" not in captured[-1]["path"]

    async def test_retrieve(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/models"] = _json_reply(self.MODELS)
        r = await client.get("/anthropic/v1/models/o3")
        assert r.status_code == 200
        assert r.json()["id"] == "o3"

    async def test_retrieve_unknown_404(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/models"] = _json_reply(self.MODELS)
        r = await client.get("/anthropic/v1/models/nope")
        assert r.status_code == 404
        assert r.json()["error"]["type"] == "not_found_error"

    async def test_openai_models_path_still_openai_shaped(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/models"] = _json_reply(self.MODELS)
        r = await client.get("/v1/models")
        assert r.json() == self.MODELS

    async def test_upstream_error(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/models"] = _error_reply(
            500, {"error": {"message": "down"}}
        )
        r = await client.get("/anthropic/v1/models")
        assert r.status_code == 500
        assert r.json()["error"]["type"] == "api_error"


class TestAnthropicStreamingEndpoint:
    def _chat_sse(self, chunks: list[str], sleep_s: float = 0.0):
        def handler(h, _body):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for payload in chunks:
                frame = f"data: {payload}\n\n".encode()
                h.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
                h.wfile.flush()
                time.sleep(sleep_s)
            h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()

        return handler

    def _parse(self, text: str) -> list[dict]:
        events = []
        for line in text.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        return events

    async def test_stream_translates_to_anthropic_events(self, client, upstream):
        _, captured, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = self._chat_sse(
            [
                json.dumps(
                    {
                        "id": "chatcmpl-s",
                        "model": "gpt-4o",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "He"},
                                "finish_reason": None,
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "id": "chatcmpl-s",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "y"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "id": "chatcmpl-s",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 4,
                            "completion_tokens": 2,
                            "total_tokens": 6,
                        },
                    }
                ),
                "[DONE]",
            ]
        )

        async with client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": "gpt-4o",
                "max_tokens": 50,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        ) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            text = "".join([piece async for piece in r.aiter_text()])

        events = self._parse(text)
        assert [e["type"] for e in events] == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
        assert "event: message_start" in text
        assert (
            "".join(
                e["delta"]["text"] for e in events if e["type"] == "content_block_delta"
            )
            == "Hey"
        )
        delta = next(e for e in events if e["type"] == "message_delta")
        assert delta["usage"]["output_tokens"] == 2
        sent = json.loads(captured[-1]["body"])
        assert sent["stream"] is True
        assert sent["stream_options"] == {"include_usage": True}

    async def test_error_before_stream_is_json(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = _error_reply(
            503, {"error": {"message": "overloaded"}}
        )
        r = await client.post(
            "/v1/messages",
            json={
                "model": "gpt-4o",
                "max_tokens": 5,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r.status_code == 503
        assert r.json()["error"]["type"] == "overloaded_error"

    async def test_chunks_arrive_incrementally(self, live_proxy):
        base, responses_map = live_proxy
        chunks = [
            json.dumps(
                {
                    "id": "c",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"t{i}"},
                            "finish_reason": None,
                        }
                    ],
                }
            )
            for i in range(3)
        ]
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = self._chat_sse(
            chunks, sleep_s=0.1
        )
        ts: list[float] = []
        async with (
            httpx.AsyncClient(timeout=10) as c,
            c.stream(
                "POST",
                f"{base}/v1/messages",
                json={
                    "model": "gpt-4o",
                    "max_tokens": 5,
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            ) as r,
        ):
            async for _ in r.aiter_raw():
                ts.append(time.time())
        assert ts[-1] - ts[0] >= 0.15, "anthropic stream appears buffered"

    async def test_many_concurrent_streams(self, client, upstream):
        _, _, responses_map = upstream
        chunks = [
            json.dumps(
                {
                    "id": "c",
                    "choices": [
                        {"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}
                    ],
                }
            ),
            "[DONE]",
        ]
        responses_map["/openai/deployments/gpt-4o/chat/completions"] = self._chat_sse(
            chunks, sleep_s=0.02
        )

        async def one(i):
            async with client.stream(
                "POST",
                "/v1/messages",
                json={
                    "model": "gpt-4o",
                    "max_tokens": 5,
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            ) as r:
                body = "".join([p async for p in r.aiter_text()])
                return r.status_code, body

        results = await asyncio.gather(*(one(i) for i in range(15)))
        assert all(code == 200 for code, _ in results)
        assert all("message_stop" in body for _, body in results)


# --- Edge cases & failure paths ------------------------------------------


class TestAnthropicContentEdgeCases:
    def _content(self, blocks):
        return anthropic_messages_to_chat([{"role": "user", "content": blocks}])[0][
            "content"
        ]

    def test_image_from_file_id(self):
        assert self._content(
            [{"type": "image", "source": {"type": "file", "file_id": "f1"}}]
        ) == [{"type": "file", "file": {"file_id": "f1"}}]

    def test_unusable_image_block_is_dropped(self):
        assert (
            self._content(
                [
                    {"type": "text", "text": "a"},
                    {"type": "image", "source": {"type": "?"}},
                ]
            )
            == "a"
        )

    def test_image_without_source_dropped(self):
        assert self._content([{"type": "text", "text": "a"}, {"type": "image"}]) == "a"

    def test_document_from_file_id(self):
        assert self._content(
            [{"type": "document", "source": {"type": "file", "file_id": "d1"}}]
        ) == [{"type": "file", "file": {"file_id": "d1"}}]

    def test_document_from_url(self):
        assert self._content(
            [{"type": "document", "source": {"type": "url", "url": "https://x/y.pdf"}}]
        ) == [{"type": "file", "file": {"file_data": "https://x/y.pdf"}}]

    def test_unusable_document_dropped(self):
        assert (
            self._content(
                [{"type": "text", "text": "a"}, {"type": "document", "source": {}}]
            )
            == "a"
        )

    def test_unknown_block_type_dropped(self):
        assert self._content([{"type": "text", "text": "a"}, {"type": "novel"}]) == "a"

    def test_bare_string_block(self):
        assert self._content(["hi"]) == "hi"

    def test_non_list_content_raises(self):
        with pytest.raises(AnthropicError):
            anthropic_messages_to_chat([{"role": "user", "content": 42}])

    def test_tool_result_with_structured_content_is_json(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": [{"type": "image", "source": {}}],
                        }
                    ],
                }
            ]
        )
        assert json.loads(msgs[0]["content"]) == [{"type": "image", "source": {}}]

    def test_tool_result_with_no_content(self):
        msgs = anthropic_messages_to_chat(
            [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]}]
        )
        assert msgs[0]["content"] == ""

    def test_tool_result_error_without_content(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t", "is_error": True}
                    ],
                }
            ]
        )
        assert msgs[0]["content"] == "Error"

    def test_assistant_string_content(self):
        msgs = anthropic_messages_to_chat([{"role": "assistant", "content": "ok"}])
        assert msgs == [{"role": "assistant", "content": "ok"}]

    def test_assistant_tool_use_only_has_null_content(self):
        msgs = anthropic_messages_to_chat(
            [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t", "name": "f"}],
                }
            ]
        )
        assert msgs[0]["content"] is None
        assert msgs[0]["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_assistant_empty_block_list_is_empty_string(self):
        msgs = anthropic_messages_to_chat([{"role": "assistant", "content": []}])
        assert msgs[0]["content"] == ""

    def test_non_dict_message_raises(self):
        with pytest.raises(AnthropicError):
            anthropic_messages_to_chat(["hi"])

    def test_models_without_created_have_null_timestamp(self):
        out = openai_models_to_anthropic({"data": [{"id": "m", "created": "nope"}]})
        assert out["data"][0]["created_at"] is None


class TestTranslatedEndpointFailureModes:
    """Both translating endpoints must degrade cleanly, not 500."""

    @pytest.fixture
    def broken_token(self, upstream):
        upstream_url, _, _ = upstream
        cfg = {
            "endpoint": upstream_url,
            "instance": "openai",
            "base": f"{upstream_url}/openai",
            "api_version": "2024-10-21",
            "default_dep": None,
            "api_key": None,
            "timeout": 10,
            "skip_warmup": True,
            "responses_mode": "translate",
            "anthropic_mode": "translate",
            "token_field": None,
        }

        async def boom():
            raise RuntimeError("no credentials")

        return build_app(cfg, token_provider=boom)

    def _client(self, app):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test", timeout=10
        )

    async def test_responses_token_failure_500(self, broken_token):
        async with LifespanManager(broken_token), self._client(broken_token) as c:
            r = await c.post("/v1/responses", json={"model": "m", "input": "hi"})
        assert r.status_code == 500
        assert r.json()["error"]["code"] == "auth_failed"

    async def test_messages_token_failure_500(self, broken_token):
        async with LifespanManager(broken_token), self._client(broken_token) as c:
            r = await c.post(
                "/v1/messages",
                json={
                    "model": "m",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert r.status_code == 500
        assert r.json()["error"]["type"] == "api_error"

    async def test_models_token_failure_500(self, broken_token):
        async with LifespanManager(broken_token), self._client(broken_token) as c:
            r = await c.get("/anthropic/v1/models")
        assert r.status_code == 500

    @pytest.fixture
    def dead_upstream(self):
        cfg = {
            "endpoint": "http://127.0.0.1:1",
            "instance": "openai",
            "base": "http://127.0.0.1:1/openai",
            "api_version": "v",
            "default_dep": None,
            "api_key": None,
            "timeout": 2,
            "skip_warmup": True,
            "responses_mode": "translate",
            "anthropic_mode": "translate",
            "token_field": None,
        }
        return build_app(cfg, token_provider=_fake_token)

    async def test_responses_upstream_unreachable_502(self, dead_upstream):
        async with LifespanManager(dead_upstream), self._client(dead_upstream) as c:
            r = await c.post("/v1/responses", json={"model": "m", "input": "hi"})
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "upstream_unreachable"

    async def test_messages_upstream_unreachable_502(self, dead_upstream):
        async with LifespanManager(dead_upstream), self._client(dead_upstream) as c:
            r = await c.post(
                "/v1/messages",
                json={
                    "model": "m",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert r.status_code == 502
        assert r.json()["error"]["type"] == "api_error"

    async def test_count_tokens_upstream_unreachable_502(self, dead_upstream):
        async with LifespanManager(dead_upstream), self._client(dead_upstream) as c:
            r = await c.post(
                "/v1/messages/count_tokens",
                json={"model": "m", "messages": [{"role": "user", "content": "x"}]},
            )
        assert r.status_code == 502

    async def test_models_upstream_unreachable_502(self, dead_upstream):
        async with LifespanManager(dead_upstream), self._client(dead_upstream) as c:
            r = await c.get("/anthropic/v1/models")
        assert r.status_code == 502

    def _garbage(self):
        def handler(h, _body):
            payload = b"<html>not json</html>"
            h.send_response(200)
            h.send_header("Content-Type", "text/html")
            h.send_header("Content-Length", str(len(payload)))
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(payload)

        return handler

    async def test_responses_non_json_upstream_502(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/deployments/m/chat/completions"] = self._garbage()
        r = await client.post("/v1/responses", json={"model": "m", "input": "hi"})
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "upstream_invalid_response"

    async def test_messages_non_json_upstream_502(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/deployments/m/chat/completions"] = self._garbage()
        r = await client.post(
            "/v1/messages",
            json={
                "model": "m",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r.status_code == 502

    async def test_count_tokens_non_json_upstream_502(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/deployments/m/chat/completions"] = self._garbage()
        r = await client.post(
            "/v1/messages/count_tokens",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 502

    async def test_models_non_json_upstream_502(self, client, upstream):
        _, _, responses_map = upstream
        responses_map["/openai/models"] = self._garbage()
        r = await client.get("/anthropic/v1/models")
        assert r.status_code == 502

    async def test_responses_survives_truncated_stream(self, client, upstream):
        """A stream that dies mid-flight still terminates with a failure event."""
        _, _, responses_map = upstream

        def handler(h, _body):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            payload = json.dumps(
                {"choices": [{"index": 0, "delta": {"content": "hi"}}]}
            )
            frame = f"data: {payload}\n\n".encode()
            h.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
            h.wfile.flush()
            h.close_connection = True  # no terminating 0-length chunk

        responses_map["/openai/deployments/m/chat/completions"] = handler
        async with client.stream(
            "POST", "/v1/responses", json={"model": "m", "input": "hi", "stream": True}
        ) as r:
            text = "".join([p async for p in r.aiter_text()])
        assert "response.failed" in text or "response.completed" in text


class TestCli:
    """The click layer must translate flags into the app config."""

    def _run(self, monkeypatch, args):
        import click.testing
        import openai_proxy

        captured = {}

        def fake_run(app, **kwargs):
            captured["app"] = app
            captured["kwargs"] = kwargs

        def fake_build_app(cfg, **kwargs):
            captured["cfg"] = cfg
            return "APP"

        monkeypatch.setattr(openai_proxy.uvicorn, "run", fake_run)
        monkeypatch.setattr(openai_proxy, "build_app", fake_build_app)
        result = click.testing.CliRunner().invoke(openai_proxy.cli, args)
        assert result.exit_code == 0, result.output
        return captured

    def test_defaults(self, monkeypatch):
        cfg = self._run(monkeypatch, [])["cfg"]
        assert cfg["responses_mode"] == "auto"
        assert cfg["anthropic_mode"] == "translate"
        assert cfg["token_field"] is None
        assert cfg["retry_429"] == 2
        assert cfg["retry_max_wait"] == 30
        assert cfg["base"].endswith("/gcr/shared/openai")

    def test_modes_and_token_field(self, monkeypatch):
        cfg = self._run(
            monkeypatch,
            [
                "--responses-mode",
                "passthrough",
                "--anthropic-mode",
                "passthrough",
                "--token-limit-field",
                "max_completion_tokens",
                "--deployment",
                "dep",
                "--api-key",
                "k",
                "--retry-429",
                "4",
                "--retry-max-wait",
                "12.5",
            ],
        )["cfg"]
        assert cfg["responses_mode"] == "passthrough"
        assert cfg["anthropic_mode"] == "passthrough"
        assert cfg["token_field"] == "max_completion_tokens"
        assert cfg["default_dep"] == "dep"
        assert cfg["api_key"] == "k"
        assert cfg["retry_429"] == 4
        assert cfg["retry_max_wait"] == 12.5

    def test_host_and_port_forwarded_to_uvicorn(self, monkeypatch):
        captured = self._run(monkeypatch, ["--host", "0.0.0.0", "--port", "9999"])
        assert captured["kwargs"]["host"] == "0.0.0.0"
        assert captured["kwargs"]["port"] == 9999

    def test_assistant_non_list_content_raises(self):
        with pytest.raises(AnthropicError):
            anthropic_messages_to_chat([{"role": "assistant", "content": 7}])

    def test_null_content_becomes_empty_string(self):
        msgs = anthropic_messages_to_chat([{"role": "user", "content": None}])
        assert msgs == [{"role": "user", "content": ""}]
