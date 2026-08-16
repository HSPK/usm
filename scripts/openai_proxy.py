#!/usr/bin/env python3
"""OpenAI-compatible proxy that forwards to Microsoft TRAPI.

Built on Starlette + uvicorn + httpx.AsyncClient so a single process
handles hundreds of concurrent SSE streams in one event loop. Tokens
come from azure.identity.aio (az login / managed identity).

Endpoints
---------
* GET  /health          liveness probe (no auth)
* GET  /status          configured upstream + api-version + api-key state
* POST /v1/responses    OpenAI Responses API, auto-routed native/translated
* POST /v1/messages     Anthropic Messages API, emulated over chat completions
* WS   /v1/realtime     OpenAI Realtime API, bidirectionally proxied
* *    /v1/<...>        OpenAI-compatible HTTP paths proxied to TRAPI
* OPT  /<...>           CORS preflight
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
import time
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from typing import Any

import click
import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket
from websockets.exceptions import InvalidStatus

SCOPE = "api://trapi/.default"

# Headers we never relay (transport-level, or replaced by the proxy itself).
HOP_REQ = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "te",
    "keep-alive",
    "upgrade",
    "proxy-connection",
    "trailer",
    "authorization",
    "api-key",
    "x-api-key",
    "expect",
}
HOP_RES = {
    "transfer-encoding",
    "content-encoding",
    "content-length",
    "connection",
    "keep-alive",
    "trailer",
    "upgrade",
}
HOP_WS_REQ = HOP_REQ | {
    "origin",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
    "user-agent",
}
# OpenAI paths that map directly under /openai/... (no deployment segment).
NO_DEPLOY = ("/models", "/files", "/fine_tuning", "/batches", "/threads", "/assistants")
# TRAPI routes these APIs from the model in the body/query rather than from
# an Azure-style deployment path.
NATIVE_V1 = ("/audio/speech", "/images/edits", "/videos")
MAX_MULTIPART_HEADER = 64 * 1024
MAX_MODEL_LENGTH = 1024
MAX_VIDEO_MODELS = 1024
DEFAULT_429_RETRIES = 2
DEFAULT_429_MAX_WAIT = 30.0
MODEL_CAPABILITY_TTL = 300.0

#: Access-log timestamps default to China Standard Time rather than UTC,
#: because that is where these logs get read. Override with --log-tz.
DEFAULT_LOG_UTC_OFFSET = 8.0
#: Only this much of a request body is buffered to find "model". A chat
#: request puts it in the first few dozen bytes; an image upload must not be
#: held in memory just to be logged.
MAX_LOG_BODY_SNIFF = 64 * 1024

AsyncTokenProvider = Callable[[], Awaitable[str]]
AsyncSleeper = Callable[[float], Awaitable[None]]


# Pure helpers (unit-tested) -----------------------------------------------


def resolve_url(
    path_qs: str, body_obj: Any, base: str, api_version: str, default_dep: str | None
) -> str | None:
    """Translate inbound path+body → upstream URL, or None if 'model' missing
    or the path attempts directory traversal."""
    if path_qs.startswith("/v1/"):
        path_qs = path_qs[3:]
    elif path_qs == "/v1":
        path_qs = "/"
    parts = urllib.parse.urlsplit(path_qs)
    path = parts.path or "/"
    # Refuse any `..` segment. httpx.Request RFC-3986-normalizes the URL when
    # built, collapsing `…/deployments/X/../../foo` into a sibling of the
    # deployment scope on the upstream host — which would leak the proxy's
    # AAD bearer token to arbitrary endpoints.
    if ".." in urllib.parse.unquote(path).split("/"):
        return None
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))

    dep = None
    if isinstance(body_obj, dict):
        dep = body_obj.get("model") or body_obj.get("deployment")
    dep = dep if isinstance(dep, str) else default_dep

    if any(path == p or path.startswith(p + "/") for p in NO_DEPLOY):
        url = f"{base}{path}"
    elif dep:
        url = f"{base}/deployments/{urllib.parse.quote(dep, safe='')}{path}"
    else:
        return None
    query.setdefault("api-version", api_version)
    return url + "?" + urllib.parse.urlencode(query)


def resolve_native_responses_url(path_qs: str, base: str) -> str | None:
    """Map a proxied Responses path to TRAPI's native OpenAI-v1 endpoint.

    TRAPI's native Responses API is model-routed by the JSON body:

        <instance>/openai/v1/responses

    It is not an Azure deployment-scoped endpoint.  Sending it through
    :func:`resolve_url` would incorrectly produce
    ``/deployments/<model>/responses?api-version=...`` and return 404.
    Both root-style and ``/v1`` client base URLs are accepted.
    """
    parts = urllib.parse.urlsplit(path_qs)
    path = parts.path or "/"
    decoded_parts = urllib.parse.unquote(path).split("/")
    if ".." in decoded_parts:
        return None
    if path == "/responses" or path.startswith("/responses/"):
        path = "/v1" + path
    elif path != "/v1/responses" and not path.startswith("/v1/responses/"):
        return None
    query = f"?{parts.query}" if parts.query else ""
    return f"{base}{path}{query}"


def resolve_native_api_url(path_qs: str, base: str) -> str | None:
    """Map TRAPI's body-routed media APIs to their native OpenAI-v1 URL."""
    parts = urllib.parse.urlsplit(path_qs)
    path = parts.path or "/"
    if ".." in urllib.parse.unquote(path).split("/"):
        return None
    if path.startswith("/v1/"):
        path = path[3:]
    if not any(path == prefix or path.startswith(prefix + "/") for prefix in NATIVE_V1):
        return None
    query = f"?{parts.query}" if parts.query else ""
    return f"{base}/v1{path}{query}"


def resolve_realtime_url(
    path_qs: str, base: str, default_dep: str | None
) -> str | None:
    """Build the native TRAPI WebSocket URL for an OpenAI realtime client."""
    parts = urllib.parse.urlsplit(path_qs)
    path = parts.path or "/"
    if ".." in urllib.parse.unquote(path).split("/"):
        return None
    if path == "/realtime":
        path = "/v1/realtime"
    elif path != "/v1/realtime":
        return None
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    model = query.get("model") or query.get("deployment") or default_dep
    if not model:
        return None
    query.setdefault("model", model)
    target = urllib.parse.urlsplit(base)
    scheme = "wss" if target.scheme == "https" else "ws"
    return urllib.parse.urlunsplit(
        (
            scheme,
            target.netloc,
            target.path.rstrip("/") + path,
            urllib.parse.urlencode(query),
            "",
        )
    )


def resolve_chat_url(
    payload: Any, base: str, api_version: str, default_dep: str | None
) -> str | None:
    """Resolve a translated chat request to the usable TRAPI route.

    Deployment-scoped URLs cannot represent catalogue ids containing ``/``
    (for example ``Qwen/Qwen3.5-27B``): percent-encoding the slash still
    reaches TRAPI as a path separator and returns 404.  Those models use the
    native body-routed OpenAI-v1 endpoint instead.  Existing deployment ids
    keep the established Azure-style route.
    """
    model = payload.get("model") if isinstance(payload, dict) else None
    model = model if isinstance(model, str) else default_dep
    if isinstance(model, str) and "/" in model:
        return f"{base}/v1/chat/completions"
    return resolve_url("/v1/chat/completions", payload, base, api_version, default_dep)


def _multipart_model(raw: bytes, content_type: str) -> str | None:
    """Read only the small ``model`` field from a multipart request body."""
    mime = Message()
    mime["content-type"] = content_type
    boundary = mime.get_boundary()
    if not boundary or len(boundary) > 200:
        return None
    try:
        marker = b"--" + boundary.encode("ascii")
    except UnicodeEncodeError:
        return None

    cursor = 0
    while True:
        part = raw.find(marker, cursor)
        if part < 0:
            return None
        part += len(marker)
        if raw[part : part + 2] == b"--":
            return None
        if raw[part : part + 2] == b"\r\n":
            part += 2
        header_end = raw.find(b"\r\n\r\n", part)
        if header_end < 0:
            return None
        if header_end - part > MAX_MULTIPART_HEADER:
            cursor = header_end + 4
            continue

        headers = BytesHeaderParser(policy=policy.default).parsebytes(
            raw[part:header_end] + b"\r\n\r\n"
        )
        value_start = header_end + 4
        next_part = raw.find(b"\r\n" + marker, value_start)
        if next_part < 0:
            return None
        if (
            headers.get_content_disposition() == "form-data"
            and headers.get_param("name", header="content-disposition") == "model"
        ):
            value = raw[value_start:next_part]
            if not value or len(value) > MAX_MODEL_LENGTH:
                return None
            try:
                model = value.decode("utf-8").strip()
            except UnicodeDecodeError:
                return None
            return model or None
        cursor = next_part + 2


def parse_body_metadata(raw: bytes, content_type: str) -> Any:
    """Parse routing fields without decoding uploaded audio/image files."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    media_type = content_type.partition(";")[0].strip().lower()
    model = None
    if media_type == "application/x-www-form-urlencoded":
        try:
            fields = urllib.parse.parse_qs(
                raw.decode("utf-8"), keep_blank_values=True, strict_parsing=False
            )
        except UnicodeDecodeError:
            return None
        values = fields.get("model")
        model = values[0] if values else None
    elif media_type == "multipart/form-data":
        model = _multipart_model(raw, content_type)
    return {"model": model} if model else None


def retry_after_seconds(
    headers: Any,
    body: bytes | str = b"",
    *,
    retry_number: int = 1,
    now: float | None = None,
) -> float:
    """Return the upstream-requested delay, or bounded exponential fallback."""
    values = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    current_time = time.time() if now is None else now

    retry_after = values.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                return max(
                    0.0, parsedate_to_datetime(retry_after).timestamp() - current_time
                )
            except (TypeError, ValueError, OverflowError):
                pass

    retry_ms = values.get("x-ms-retry-after-ms") or values.get("retry-after-ms")
    if retry_ms:
        try:
            return max(0.0, float(retry_ms) / 1000.0)
        except ValueError:
            pass

    resets = []
    for name in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        value = values.get(name)
        if value:
            delay = _duration_seconds(value, current_time)
            if delay is not None:
                resets.append(delay)
    if resets:
        return max(resets)

    text = body.decode(errors="replace") if isinstance(body, bytes) else body
    match = re.search(
        r"retry\s+after\s+(\d+(?:\.\d+)?)\s*(milliseconds?|ms|seconds?|s)?",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        delay = float(match.group(1))
        if (match.group(2) or "").lower().startswith(("milli", "ms")):
            delay /= 1000.0
        return max(0.0, delay)

    return min(2.0 ** max(0, retry_number - 1), 8.0)


def _duration_seconds(value: str, now: float) -> float | None:
    value = value.strip().lower()
    try:
        number = float(value)
    except ValueError:
        number = None
    if number is not None:
        return max(0.0, number - now) if number > 1_000_000_000 else max(0.0, number)

    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    matches = list(re.finditer(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value))
    if not matches or "".join(match.group(0) for match in matches) != value:
        return None
    return sum(float(match.group(1)) * units[match.group(2)] for match in matches)


def check_api_key(headers, expected: str | None) -> bool:
    """Return True iff the optional API-key gate passes (or is disabled).

    Accepts the OpenAI (``Authorization: Bearer``), Azure (``api-key``) and
    Anthropic (``x-api-key``) conventions so any SDK can authenticate.
    """
    if not expected:
        return True
    auth = headers.get("Authorization") or ""
    bearer = (
        auth.split(None, 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    )
    api_key = (headers.get("api-key") or headers.get("Api-Key") or "").strip()
    anthropic_key = (headers.get("x-api-key") or headers.get("X-Api-Key") or "").strip()
    return expected in (bearer, api_key, anthropic_key)


def make_token_provider() -> AsyncTokenProvider:
    """Return an async callable producing a fresh bearer token.

    Uses the sync ``azure-identity`` library (caches internally) and offloads
    the call to a thread so it doesn't block the event loop. Avoids pulling
    in ``aiohttp`` (the default transport for ``azure-identity.aio``).
    """
    try:
        from azure.identity import (
            AzureCliCredential,
            ChainedTokenCredential,
            ManagedIdentityCredential,
            get_bearer_token_provider,
        )
    except ImportError:
        click.echo(
            "Missing 'azure-identity'. Install: pip install azure-identity", err=True
        )
        sys.exit(2)
    sync_provider = get_bearer_token_provider(
        ChainedTokenCredential(AzureCliCredential(), ManagedIdentityCredential()),
        SCOPE,
    )

    async def async_provider() -> str:
        return await asyncio.to_thread(sync_provider)

    return async_provider


# ==========================================================================
# Chat-completions kernel — shared by the API adapters below
#
# Stdlib only: no Starlette / httpx / TRAPI imports and no shared mutable
# state with the proxy. Holds what every adapter needs regardless of the
# dialect it speaks: deployment capability probes, the token-limit field
# decision, and upstream parameter negotiation.
# ==========================================================================


class TranslationError(Exception):
    """A request an API adapter cannot translate (surfaced as HTTP 400)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_request_error",
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.param = param


class ResponsesError(TranslationError):
    """Untranslatable OpenAI Responses request."""


class AnthropicError(TranslationError):
    """Untranslatable Anthropic Messages request."""


TOKEN_LIMIT_FIELDS = frozenset({"max_tokens", "max_completion_tokens"})

# Reasoning deployments (o-series, gpt-5.x, codex-*) reject `max_tokens`,
# `temperature` and `system` messages. TRAPI deployment names carry a date
# suffix (e.g. `o3_2025-04-16`), so match on the leading family token.
_REASONING_MODEL_RE = re.compile(r"^(?:o[1-9]|gpt-5|gpt-6|codex)")

# Optional tuning knobs that some deployments/api-versions reject outright.
# When the upstream complains about one of these we drop it and retry once.
_DROPPABLE_PARAMS = frozenset(
    {
        "frequency_penalty",
        "logprobs",
        "metadata",
        "parallel_tool_calls",
        "presence_penalty",
        "prompt_cache_key",
        "reasoning_effort",
        "response_format",
        "safety_identifier",
        "seed",
        "service_tier",
        "stop",
        "stream_options",
        "temperature",
        "top_logprobs",
        "top_p",
        "user",
        "verbosity",
    }
)

MAX_PARAM_RETRIES = 4

IdFactory = Callable[[str], str]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def sanitize_id(value: Any, prefix: str, id_factory: IdFactory) -> str:
    """Reuse an upstream id under *prefix*, or mint a fresh one."""
    if isinstance(value, str) and value:
        return f"{prefix}_" + re.sub(r"[^A-Za-z0-9]", "", value)
    return id_factory(prefix)


def _join_text(value: Any) -> str:
    """Flatten a string or a list of ``{"text": ...}`` parts into one string."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    out = []
    for part in value:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
    return "".join(out)


@dataclass
class ChatRequestPlan:
    """Everything the transport needs to run one translated request."""

    payload: dict
    echo: dict
    stream: bool
    token_field: str
    dropped_tools: list[str] = field(default_factory=list)


def sse_event(name: str, payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {name}\ndata: {body}\n\n".encode()


# -- model capability probes ----------------------------------------------


def is_reasoning_model(model: str | None) -> bool:
    """True for deployments of the o-series / gpt-5 / codex reasoning families."""
    name = (model or "").strip().lower().rsplit("/", 1)[-1]
    return bool(_REASONING_MODEL_RE.match(name))


def token_limit_field(model: str | None, override: str | None = None) -> str:
    """Pick the chat-completions field that caps *generated* tokens.

    ``max_tokens`` counts only completion tokens but is deprecated, and
    reasoning models reject it outright ("Use 'max_completion_tokens'
    instead") because their invisible reasoning tokens must be budgeted too.
    ``max_completion_tokens`` replaces it, but older api-versions and older
    deployments answer "Unrecognized request argument". So: pick the
    model-appropriate default here and let :func:`plan_retry` swap fields if
    the upstream disagrees.

    Both the Responses API's ``max_output_tokens`` and Anthropic's
    ``max_tokens`` mean "cap the generated tokens", so both route through here.
    """
    if override in TOKEN_LIMIT_FIELDS:
        return override  # type: ignore[return-value]
    return "max_completion_tokens" if is_reasoning_model(model) else "max_tokens"


# -- upstream parameter negotiation ----------------------------------------

_USE_INSTEAD_RE = re.compile(
    r"use\s+['\"]?([a-z_][a-z0-9_]*)['\"]?\s+instead", re.IGNORECASE
)
_BAD_PARAM_RE = re.compile(
    r"(?:unsupported|unrecognized|unknown|invalid|not\s+supported)"
    r"[^:]{0,60}[:\s]\s*['\"]?([a-z_][a-z0-9_.]*)['\"]?",
    re.IGNORECASE,
)


def _error_param(body_text: str) -> tuple[str | None, str]:
    """Extract ``(error.param, error.message)`` from an upstream error body."""
    try:
        data = json.loads(body_text)
    except (TypeError, ValueError):
        return None, body_text
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None, body_text
    param = error.get("param")
    message = error.get("message")
    return (
        param if isinstance(param, str) else None,
        message if isinstance(message, str) else body_text,
    )


def _other_token_field(name: str) -> str:
    return "max_tokens" if name == "max_completion_tokens" else "max_completion_tokens"


def plan_retry(status: int, body_text: str, payload: dict) -> tuple[dict, str] | None:
    """Recover from a 400 caused by a parameter the deployment doesn't accept.

    Returns ``(new_payload, description)`` or ``None`` when the error isn't a
    fixable parameter problem. The two token-limit fields are *swapped* for
    each other (``max_tokens`` ⇄ ``max_completion_tokens``); other optional
    tuning parameters are simply dropped.
    """
    if status != 400 or not isinstance(payload, dict):
        return None
    param, message = _error_param(body_text)

    hint = _USE_INSTEAD_RE.search(message)
    if hint and hint.group(1) in TOKEN_LIMIT_FIELDS:
        wanted = hint.group(1)
        other = _other_token_field(wanted)
        if other in payload and wanted not in payload:
            new = dict(payload)
            new[wanted] = new.pop(other)
            return new, f"{other} → {wanted}"

    candidate = param if param in payload else None
    if candidate is None:
        match = _BAD_PARAM_RE.search(f"{param or ''} {message}")
        if match and match.group(1) in payload:
            candidate = match.group(1)
    if candidate is None:
        return None

    if candidate in TOKEN_LIMIT_FIELDS:
        other = _other_token_field(candidate)
        new = dict(payload)
        new[other] = new.pop(candidate)
        return new, f"{candidate} → {other}"
    if candidate in _DROPPABLE_PARAMS:
        new = dict(payload)
        new.pop(candidate)
        return new, f"dropped {candidate}"
    return None


def chat_usage(chat: Any) -> dict:
    """Normalised token counts from a chat completion (0 when absent)."""
    usage = chat.get("usage") if isinstance(chat, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    return {
        "prompt": prompt,
        "completion": completion,
        "total": usage.get("total_tokens", prompt + completion),
        "cached": prompt_details.get("cached_tokens", 0) or 0,
        "reasoning": completion_details.get("reasoning_tokens", 0) or 0,
    }


# ==========================================================================
# OpenAI Responses API ⇄ Chat Completions adapter
#
# One of two sibling adapters over the kernel above; it knows nothing about
# the Anthropic adapter and vice versa. The proxy talks to it through three
# seams only: ``build_chat_request`` (in), ``chat_to_responses`` /
# ``ResponsesStreamTranslator`` (out), plus the shared ``plan_retry``.
# ==========================================================================

# Responses items that have no chat-completions equivalent. They are echoes of
# server-side state we never produced, so replaying them is a no-op.
_IGNORED_INPUT_ITEMS = frozenset(
    {
        "reasoning",
        "item_reference",
        "web_search_call",
        "file_search_call",
        "computer_call",
        "computer_call_output",
        "image_generation_call",
        "code_interpreter_call",
        "local_shell_call",
        "local_shell_call_output",
        "mcp_call",
        "mcp_list_tools",
        "mcp_approval_request",
        "mcp_approval_response",
    }
)

# Stateful / server-side features a stateless translating proxy cannot honour.
_UNSUPPORTED_RESPONSES_FIELDS = {
    "previous_response_id": "This proxy is stateless; resend the full 'input'.",
    "conversation": "This proxy is stateless; resend the full 'input'.",
    "background": "Background responses require server-side storage.",
    "prompt": "Stored prompt templates are not available upstream.",
}

_PASSTHROUGH_PARAMS = (
    "temperature",
    "top_p",
    "top_logprobs",
    "seed",
    "user",
    "metadata",
    "service_tier",
    "safety_identifier",
    "prompt_cache_key",
)

_FINISH_REASON_STATUS = {
    "stop": ("completed", None),
    "tool_calls": ("completed", None),
    "function_call": ("completed", None),
    "length": ("incomplete", "max_output_tokens"),
    "content_filter": ("incomplete", "content_filter"),
}


# -- request: Responses → chat completions ---------------------------------


def _to_chat_content_part(part: Any) -> dict | None:
    """Map one Responses content part to a chat-completions content part."""
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return None
    ptype = part.get("type")
    if ptype in ("input_text", "output_text", "text", "summary_text"):
        return {"type": "text", "text": part.get("text") or ""}
    if ptype == "refusal":
        return {"type": "refusal", "refusal": part.get("refusal") or ""}
    if ptype in ("input_image", "image_url"):
        url = part.get("image_url")
        if isinstance(url, dict):
            image = dict(url)
        elif isinstance(url, str) and url:
            image = {"url": url}
        elif part.get("file_id"):
            return {"type": "file", "file": {"file_id": part["file_id"]}}
        else:
            return None
        if part.get("detail") and "detail" not in image:
            image["detail"] = part["detail"]
        return {"type": "image_url", "image_url": image}
    if ptype in ("input_file", "file"):
        spec = part.get("file") if isinstance(part.get("file"), dict) else None
        if spec is None:
            spec = {
                k: part[k]
                for k in ("file_id", "filename", "file_data", "file_url")
                if part.get(k)
            }
        return {"type": "file", "file": spec} if spec else None
    if ptype in ("input_audio", "audio"):
        audio = part.get("input_audio") or part.get("audio")
        return {"type": "input_audio", "input_audio": audio} if audio else None
    return None


def _to_chat_content(content: Any) -> Any:
    """Convert Responses content to chat content, collapsing pure text."""
    if content is None or isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ResponsesError(
            "Message 'content' must be a string or an array of content parts.",
            param="input",
        )
    parts = [p for p in (_to_chat_content_part(c) for c in content) if p]
    if all(p["type"] == "text" for p in parts):
        return "".join(p["text"] for p in parts)
    return parts


def _append_input_item(messages: list[dict], item: Any) -> None:
    if isinstance(item, str):
        messages.append({"role": "user", "content": item})
        return
    if not isinstance(item, dict):
        raise ResponsesError(
            "Each 'input' item must be a string or an object.", param="input"
        )

    itype = item.get("type")
    if itype in (None, "message") and item.get("role"):
        content = _to_chat_content(item.get("content"))
        messages.append({"role": item["role"], "content": content or ""})
        return

    if itype == "function_call":
        call = {
            "id": item.get("call_id") or item.get("id") or "",
            "type": "function",
            "function": {
                "name": item.get("name") or "",
                "arguments": item.get("arguments") or "",
            },
        }
        last = messages[-1] if messages else None
        # Parallel tool calls arrive as sibling items; chat completions expects
        # them merged into a single assistant message.
        if last and last.get("role") == "assistant" and last.get("tool_calls"):
            last["tool_calls"].append(call)
        else:
            messages.append(
                {"role": "assistant", "content": None, "tool_calls": [call]}
            )
        return

    if itype == "function_call_output":
        output = item.get("output")
        if isinstance(output, list):
            output = _join_text(output)
        elif not isinstance(output, str):
            output = "" if output is None else json.dumps(output, ensure_ascii=False)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "",
                "content": output,
            }
        )
        return

    if itype in _IGNORED_INPUT_ITEMS:
        return
    raise ResponsesError(f"Unsupported input item type: {itype!r}.", param="input")


def responses_input_to_messages(
    input_value: Any,
    instructions: Any = None,
    *,
    instructions_role: str = "system",
) -> list[dict]:
    """Flatten Responses ``instructions`` + ``input`` into chat messages."""
    messages: list[dict] = []
    if isinstance(instructions, str) and instructions:
        messages.append({"role": instructions_role, "content": instructions})
    if input_value is None:
        return messages
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
        return messages
    if not isinstance(input_value, list):
        raise ResponsesError(
            "'input' must be a string or an array of items.", param="input"
        )
    for item in input_value:
        _append_input_item(messages, item)
    return messages


def responses_tools_to_chat(tools: Any) -> tuple[list[dict] | None, list[str]]:
    """Return ``(chat_tools, dropped_types)``.

    Hosted tools (``web_search``, ``file_search``, ``computer_use``, MCP, …)
    are server-side features chat completions cannot provide; they are dropped
    rather than failing an otherwise-answerable request.
    """
    if tools is None:
        return None, []
    if not isinstance(tools, list):
        raise ResponsesError("'tools' must be an array.", param="tools")
    out: list[dict] = []
    dropped: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if isinstance(tool.get("function"), dict):  # already chat-shaped
            out.append({"type": "function", "function": tool["function"]})
            continue
        if tool.get("type") == "function" and tool.get("name"):
            fn: dict[str, Any] = {"name": tool["name"]}
            if tool.get("description"):
                fn["description"] = tool["description"]
            params = tool.get("parameters")
            fn["parameters"] = (
                params
                if isinstance(params, dict)
                else {"type": "object", "properties": {}}
            )
            if tool.get("strict") is not None:
                fn["strict"] = tool["strict"]
            out.append({"type": "function", "function": fn})
            continue
        dropped.append(str(tool.get("type") or "unknown"))
    return (out or None), dropped


def responses_tool_choice_to_chat(tool_choice: Any) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice if tool_choice in ("auto", "none", "required") else "auto"
    if isinstance(tool_choice, dict):
        if isinstance(tool_choice.get("function"), dict):  # already chat-shaped
            return tool_choice
        if tool_choice.get("type") == "function" and tool_choice.get("name"):
            return {"type": "function", "function": {"name": tool_choice["name"]}}
        if tool_choice.get("type") == "allowed_tools":
            return "required" if tool_choice.get("mode") == "required" else "auto"
        return "auto"
    return None


def responses_text_to_chat(text: Any) -> tuple[dict | None, str | None]:
    """Map ``text`` → ``(response_format, verbosity)``."""
    if not isinstance(text, dict):
        return None, None
    verbosity = text.get("verbosity")
    fmt = text.get("format")
    if not isinstance(fmt, dict):
        return None, verbosity
    ftype = fmt.get("type")
    if ftype in (None, "text"):
        return {"type": "text"}, verbosity
    if ftype == "json_object":
        return {"type": "json_object"}, verbosity
    if ftype == "json_schema":
        schema: dict[str, Any] = {
            "name": fmt.get("name") or "response",
            "schema": fmt.get("schema") or {},
        }
        if fmt.get("description"):
            schema["description"] = fmt["description"]
        if fmt.get("strict") is not None:
            schema["strict"] = fmt["strict"]
        return {"type": "json_schema", "json_schema": schema}, verbosity
    raise ResponsesError(
        f"Unsupported text.format.type: {ftype!r}.", param="text.format.type"
    )


def _echo_fields(body: dict, model: str, reasoning: dict) -> dict:
    """Request fields the Responses object must mirror back to the client."""
    return {
        "instructions": body.get("instructions"),
        "max_output_tokens": body.get("max_output_tokens"),
        "metadata": body.get("metadata") or {},
        "model": model,
        "parallel_tool_calls": bool(body.get("parallel_tool_calls", True)),
        "previous_response_id": None,
        "reasoning": {
            "effort": reasoning.get("effort"),
            "summary": reasoning.get("summary"),
        },
        "store": False,  # stateless proxy: nothing is ever persisted
        "temperature": body.get("temperature"),
        "text": body.get("text") or {"format": {"type": "text"}},
        "tool_choice": body.get("tool_choice", "auto"),
        "tools": body.get("tools") or [],
        "top_p": body.get("top_p"),
        "truncation": body.get("truncation") or "disabled",
        "user": body.get("user"),
    }


def build_chat_request(
    body: Any,
    *,
    default_model: str | None = None,
    token_field_override: str | None = None,
) -> ChatRequestPlan:
    """Translate a Responses API request body into a chat-completions body."""
    if not isinstance(body, dict):
        raise ResponsesError("Request body must be a JSON object.")

    for name, why in _UNSUPPORTED_RESPONSES_FIELDS.items():
        if body.get(name) not in (None, False):
            raise ResponsesError(
                f"'{name}' is not supported by this proxy. {why}",
                code="unsupported_parameter",
                param=name,
            )

    model = body.get("model") or default_model
    if not isinstance(model, str) or not model:
        raise ResponsesError(
            "Request must include 'model' or start the proxy with --deployment.",
            param="model",
        )

    reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), dict) else {}
    messages = responses_input_to_messages(
        body.get("input"),
        body.get("instructions"),
        # o-series/gpt-5 deployments take 'developer' where others take 'system'.
        instructions_role="developer" if is_reasoning_model(model) else "system",
    )
    if not messages:
        raise ResponsesError(
            "Request must include 'input' or 'instructions'.", param="input"
        )

    payload: dict[str, Any] = {"model": model, "messages": messages}
    stream = bool(body.get("stream"))
    if stream:
        payload["stream"] = True
        # Chat completions only reports usage on the final chunk when asked.
        payload["stream_options"] = {"include_usage": True}

    field_name = token_limit_field(model, token_field_override)
    max_output = body.get("max_output_tokens")
    if isinstance(max_output, int) and not isinstance(max_output, bool):
        payload[field_name] = max_output

    tools, dropped_tools = responses_tools_to_chat(body.get("tools"))
    if tools:
        payload["tools"] = tools
        choice = responses_tool_choice_to_chat(body.get("tool_choice"))
        if choice is not None:
            payload["tool_choice"] = choice
        if isinstance(body.get("parallel_tool_calls"), bool):
            payload["parallel_tool_calls"] = body["parallel_tool_calls"]

    response_format, verbosity = responses_text_to_chat(body.get("text"))
    if response_format:
        payload["response_format"] = response_format
    if verbosity:
        payload["verbosity"] = verbosity
    if reasoning.get("effort"):
        payload["reasoning_effort"] = reasoning["effort"]

    for name in _PASSTHROUGH_PARAMS:
        if body.get(name) is not None:
            payload[name] = body[name]
    if payload.get("top_logprobs") is not None:
        payload["logprobs"] = True

    return ChatRequestPlan(
        payload=payload,
        echo=_echo_fields(body, model, reasoning),
        stream=stream,
        token_field=field_name,
        dropped_tools=dropped_tools,
    )


# -- response: chat completions → Responses --------------------------------


def chat_usage_to_responses(usage: Any) -> dict | None:
    if not isinstance(usage, dict):
        return None
    counts = chat_usage({"usage": usage})
    return {
        "input_tokens": counts["prompt"],
        "input_tokens_details": {"cached_tokens": counts["cached"]},
        "output_tokens": counts["completion"],
        "output_tokens_details": {"reasoning_tokens": counts["reasoning"]},
        "total_tokens": counts["total"],
    }


def _message_output_items(message: dict, id_factory: IdFactory) -> list[dict]:
    items: list[dict] = []
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        items.append(
            {
                "id": id_factory("rs"),
                "type": "reasoning",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": reasoning}],
            }
        )
    parts: list[dict] = []
    text = _join_text(message.get("content"))
    if text:
        parts.append({"type": "output_text", "text": text, "annotations": []})
    if message.get("refusal"):
        parts.append({"type": "refusal", "refusal": message["refusal"]})
    if parts:
        items.append(
            {
                "id": id_factory("msg"),
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": parts,
            }
        )
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        items.append(
            {
                "id": id_factory("fc"),
                "type": "function_call",
                "status": "completed",
                "call_id": call.get("id") or id_factory("call"),
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "",
            }
        )
    return items


def chat_to_responses(chat: Any, echo: dict, *, id_factory: IdFactory = new_id) -> dict:
    """Convert a non-streaming chat completion into a Responses object."""
    if not isinstance(chat, dict):
        raise ResponsesError("Upstream returned a non-object chat completion.")
    choices = chat.get("choices") or []
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") or {}
    status, incomplete = _FINISH_REASON_STATUS.get(
        choice.get("finish_reason"), ("completed", None)
    )
    response = dict(echo)
    response.update(
        {
            "id": sanitize_id(chat.get("id"), "resp", id_factory),
            "object": "response",
            "created_at": chat.get("created") or int(time.time()),
            "status": status,
            "error": None,
            "incomplete_details": {"reason": incomplete} if incomplete else None,
            "model": chat.get("model") or echo.get("model"),
            "output": _message_output_items(message, id_factory),
            "usage": chat_usage_to_responses(chat.get("usage")),
        }
    )
    return response


class ResponsesStreamTranslator:
    """Turn a chat-completions SSE stream into Responses API SSE events.

    Synchronous and I/O-free: feed it raw SSE lines with :meth:`feed_line`
    (or already-parsed chunks with :meth:`feed`) and it returns the encoded
    frames to forward. Ordering follows the real API: ``response.created`` →
    ``response.in_progress`` → per-item add/delta/done → ``response.completed``
    (or ``.incomplete`` / ``.failed``).
    """

    def __init__(self, echo: dict, *, id_factory: IdFactory = new_id) -> None:
        self._echo = echo
        self._id_factory = id_factory
        self._seq = 0
        self._response_id = id_factory("resp")
        self._created_at = int(time.time())
        self._model = echo.get("model")
        self._output: list[dict] = []
        self._message: dict | None = None
        self._message_index = 0
        self._message_text: list[str] = []
        self._calls: dict[Any, dict] = {}
        self._usage: dict | None = None
        self._status = "completed"
        self._incomplete: str | None = None
        self._done = False

    # -- emit helpers ------------------------------------------------------

    def _emit(self, name: str, payload: dict) -> bytes:
        frame = sse_event(name, {"type": name, "sequence_number": self._seq, **payload})
        self._seq += 1
        return frame

    def _snapshot(self, status: str) -> dict:
        snapshot = dict(self._echo)
        snapshot.update(
            {
                "id": self._response_id,
                "object": "response",
                "created_at": self._created_at,
                "status": status,
                "error": None,
                "incomplete_details": (
                    {"reason": self._incomplete} if self._incomplete else None
                ),
                "model": self._model,
                "output": [dict(item) for item in self._output],
                "usage": self._usage,
            }
        )
        return snapshot

    def _open(self, item: dict) -> int:
        self._output.append(item)
        return len(self._output) - 1

    # -- stream lifecycle --------------------------------------------------

    def start(self) -> list[bytes]:
        snapshot = self._snapshot("in_progress")
        return [
            self._emit("response.created", {"response": snapshot}),
            self._emit("response.in_progress", {"response": snapshot}),
        ]

    def feed_line(self, line: str) -> list[bytes]:
        line = line.strip()
        if not line.startswith("data:"):
            return []
        data = line[5:].strip()
        if data == "[DONE]":
            return self.finish()
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return []
        return self.feed(chunk)

    def feed(self, chunk: Any) -> list[bytes]:
        if self._done or not isinstance(chunk, dict):
            return []
        if isinstance(chunk.get("error"), dict):
            error = chunk["error"]
            return self.fail(
                str(error.get("message") or "Upstream error."),
                code=str(error.get("code") or "server_error"),
            )
        if chunk.get("model"):
            self._model = chunk["model"]
        if isinstance(chunk.get("usage"), dict):
            self._usage = chat_usage_to_responses(chunk["usage"])

        frames: list[bytes] = []
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            text = _join_text(delta.get("content"))
            if text:
                frames += self._append_text(text)
            for call in delta.get("tool_calls") or []:
                frames += self._append_tool_call(call)
            finish = choice.get("finish_reason")
            if finish:
                self._status, self._incomplete = _FINISH_REASON_STATUS.get(
                    finish, ("completed", None)
                )
        return frames

    def finish(self) -> list[bytes]:
        if self._done:
            return []
        frames = self._close_message() + self._close_calls()
        self._done = True
        event = {
            "completed": "response.completed",
            "incomplete": "response.incomplete",
            "failed": "response.failed",
        }[self._status]
        frames.append(self._emit(event, {"response": self._snapshot(self._status)}))
        return frames

    def fail(self, message: str, *, code: str = "server_error") -> list[bytes]:
        if self._done:
            return []
        self._done = True
        snapshot = self._snapshot("failed")
        snapshot["error"] = {"code": code, "message": message}
        return [
            self._emit("error", {"code": code, "message": message, "param": None}),
            self._emit("response.failed", {"response": snapshot}),
        ]

    # -- item state machines ----------------------------------------------

    def _append_text(self, text: str) -> list[bytes]:
        frames: list[bytes] = []
        if self._message is None:
            self._message = {
                "id": self._id_factory("msg"),
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            self._message_index = self._open(self._message)
            frames.append(
                self._emit(
                    "response.output_item.added",
                    {"output_index": self._message_index, "item": dict(self._message)},
                )
            )
            frames.append(
                self._emit(
                    "response.content_part.added",
                    {
                        "item_id": self._message["id"],
                        "output_index": self._message_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                )
            )
        self._message_text.append(text)
        frames.append(
            self._emit(
                "response.output_text.delta",
                {
                    "item_id": self._message["id"],
                    "output_index": self._message_index,
                    "content_index": 0,
                    "delta": text,
                    "logprobs": [],
                },
            )
        )
        return frames

    def _close_message(self) -> list[bytes]:
        if self._message is None:
            return []
        item_id = self._message["id"]
        index = self._message_index
        text = "".join(self._message_text)
        part = {"type": "output_text", "text": text, "annotations": []}
        self._message["status"] = "completed"
        self._message["content"] = [part]
        item = dict(self._message)
        self._message = None
        self._message_text = []
        return [
            self._emit(
                "response.output_text.done",
                {
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "text": text,
                    "logprobs": [],
                },
            ),
            self._emit(
                "response.content_part.done",
                {
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "part": part,
                },
            ),
            self._emit(
                "response.output_item.done", {"output_index": index, "item": item}
            ),
        ]

    def _append_tool_call(self, call: Any) -> list[bytes]:
        if not isinstance(call, dict):
            return []
        # A tool call ends any open text message (mirrors the real API).
        frames = self._close_message()
        key = call.get("index", 0)
        fn = call.get("function") or {}
        state = self._calls.get(key)
        if state is None:
            item = {
                "id": self._id_factory("fc"),
                "type": "function_call",
                "status": "in_progress",
                "call_id": call.get("id") or self._id_factory("call"),
                "name": fn.get("name") or "",
                "arguments": "",
            }
            state = {"item": item, "index": self._open(item), "args": []}
            self._calls[key] = state
            frames.append(
                self._emit(
                    "response.output_item.added",
                    {"output_index": state["index"], "item": dict(item)},
                )
            )
        else:
            item = state["item"]
            if call.get("id"):
                item["call_id"] = call["id"]
            if fn.get("name") and not item["name"]:
                item["name"] = fn["name"]

        arguments = fn.get("arguments")
        if arguments:
            state["args"].append(arguments)
            item["arguments"] = "".join(state["args"])
            frames.append(
                self._emit(
                    "response.function_call_arguments.delta",
                    {
                        "item_id": item["id"],
                        "output_index": state["index"],
                        "delta": arguments,
                    },
                )
            )
        return frames

    def _close_calls(self) -> list[bytes]:
        frames: list[bytes] = []
        for key in sorted(self._calls, key=lambda k: self._calls[k]["index"]):
            state = self._calls[key]
            item = state["item"]
            item["status"] = "completed"
            item["arguments"] = "".join(state["args"])
            frames.append(
                self._emit(
                    "response.function_call_arguments.done",
                    {
                        "item_id": item["id"],
                        "output_index": state["index"],
                        "arguments": item["arguments"],
                    },
                )
            )
            frames.append(
                self._emit(
                    "response.output_item.done",
                    {"output_index": state["index"], "item": dict(item)},
                )
            )
        self._calls = {}
        return frames


# ==========================================================================
# Anthropic Messages API ⇄ Chat Completions adapter
#
# Sibling of the Responses adapter: same kernel, no dependency in either
# direction. Seams: ``build_chat_request_from_messages`` (in),
# ``chat_to_anthropic_message`` / ``AnthropicStreamTranslator`` (out).
# ==========================================================================

# Anthropic server-side tools (web_search_*, computer_*, bash_*, text_editor_*)
# execute inside Anthropic's infrastructure and have no chat-completions
# equivalent, so they are dropped rather than failing the whole request.
_ANTHROPIC_CLIENT_TOOL_KEYS = ("input_schema", "parameters")

_ANTHROPIC_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

# HTTP status → Anthropic error type, for relaying upstream failures.
_ANTHROPIC_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    408: "timeout_error",
    413: "request_too_large",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
    529: "overloaded_error",
}

# Anthropic exposes a token budget; chat completions exposes coarse effort.
_THINKING_EFFORT_BUDGETS = ((2048, "low"), (8192, "medium"))


def anthropic_error_type(status: int) -> str:
    return _ANTHROPIC_ERROR_TYPES.get(status, "api_error")


def thinking_to_reasoning_effort(thinking: Any) -> str | None:
    """Map ``thinking.budget_tokens`` onto a chat-completions effort level."""
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        return None
    budget = thinking.get("budget_tokens")
    if not isinstance(budget, int) or isinstance(budget, bool):
        return "medium"
    for ceiling, effort in _THINKING_EFFORT_BUDGETS:
        if budget <= ceiling:
            return effort
    return "high"


def _anthropic_image_part(source: Any) -> dict | None:
    if not isinstance(source, dict):
        return None
    stype = source.get("type")
    if stype == "base64" and source.get("data"):
        media = source.get("media_type") or "image/png"
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media};base64,{source['data']}"},
        }
    if stype == "url" and source.get("url"):
        return {"type": "image_url", "image_url": {"url": source["url"]}}
    if stype == "file" and source.get("file_id"):
        return {"type": "file", "file": {"file_id": source["file_id"]}}
    return None


def _anthropic_document_part(block: dict) -> dict | None:
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    spec: dict[str, Any] = {}
    if block.get("title"):
        spec["filename"] = block["title"]
    stype = source.get("type")
    if stype == "base64" and source.get("data"):
        media = source.get("media_type") or "application/pdf"
        spec["file_data"] = f"data:{media};base64,{source['data']}"
    elif stype == "file" and source.get("file_id"):
        spec["file_id"] = source["file_id"]
    elif stype == "text" and source.get("data"):
        return {"type": "text", "text": source["data"]}
    elif stype == "url" and source.get("url"):
        spec["file_data"] = source["url"]
    else:
        return None
    return {"type": "file", "file": spec}


def _anthropic_content_part(block: Any) -> dict | None:
    """Map one Anthropic content block to a chat-completions content part."""
    if isinstance(block, str):
        return {"type": "text", "text": block}
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    if btype == "text":
        return {"type": "text", "text": block.get("text") or ""}
    if btype == "image":
        return _anthropic_image_part(block.get("source"))
    if btype == "document":
        return _anthropic_document_part(block)
    # thinking / redacted_thinking are opaque server state: not replayable.
    return None


def _anthropic_blocks_to_content(blocks: Any) -> Any:
    if blocks is None or isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        raise AnthropicError(
            "Message 'content' must be a string or an array of content blocks.",
            param="messages",
        )
    parts = [p for p in (_anthropic_content_part(b) for b in blocks) if p]
    if all(p["type"] == "text" for p in parts):
        return "".join(p["text"] for p in parts)
    return parts


def _tool_result_content(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = _join_text(
            [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
        )
        if not text:
            text = json.dumps(content, ensure_ascii=False)
    elif content is None:
        text = ""
    else:
        text = json.dumps(content, ensure_ascii=False)
    if block.get("is_error"):
        text = f"Error: {text}" if text else "Error"
    return text


def _validate_content(content: Any) -> Any:
    """Anthropic content is a string or a block array; reject anything else.

    Failing here gives the caller a clear 400 instead of forwarding a body the
    upstream will reject with a confusing chat-completions error.
    """
    if content is None:
        return ""
    if isinstance(content, (str, list)):
        return content
    raise AnthropicError(
        "Message 'content' must be a string or an array of content blocks.",
        param="messages",
    )


def _append_user_message(messages: list[dict], content: Any) -> None:
    """Split an Anthropic user turn into tool results + a plain user message.

    Anthropic carries ``tool_result`` blocks inside the *user* message;
    chat completions needs a separate ``tool`` message per result, emitted
    before whatever the user actually typed.
    """
    content = _validate_content(content)
    if not isinstance(content, list):
        messages.append({"role": "user", "content": content})
        return
    results = [
        b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    rest = [
        b
        for b in content
        if not (isinstance(b, dict) and b.get("type") == "tool_result")
    ]
    for block in results:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": block.get("tool_use_id") or "",
                "content": _tool_result_content(block),
            }
        )
    body = _anthropic_blocks_to_content(rest)
    if body:
        messages.append({"role": "user", "content": body})


def _append_assistant_message(messages: list[dict], content: Any) -> None:
    content = _validate_content(content)
    if not isinstance(content, list):
        messages.append({"role": "assistant", "content": content})
        return
    tool_calls = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(
                            block.get("input")
                            if block.get("input") is not None
                            else {},
                            ensure_ascii=False,
                        ),
                    },
                }
            )
    body = _anthropic_blocks_to_content(
        [
            b
            for b in content
            if not (isinstance(b, dict) and b.get("type") == "tool_use")
        ]
    )
    message: dict[str, Any] = {"role": "assistant", "content": body or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    elif not body:
        message["content"] = ""
    messages.append(message)


def anthropic_messages_to_chat(
    messages: Any, system: Any = None, *, system_role: str = "system"
) -> list[dict]:
    """Flatten Anthropic ``system`` + ``messages`` into chat messages."""
    out: list[dict] = []
    system_text = system if isinstance(system, str) else _join_text(system)
    if system_text:
        out.append({"role": system_role, "content": system_text})
    if messages is None:
        return out
    if not isinstance(messages, list):
        raise AnthropicError("'messages' must be an array.", param="messages")
    for message in messages:
        if not isinstance(message, dict):
            raise AnthropicError("Each message must be an object.", param="messages")
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            _append_assistant_message(out, content)
        elif role == "user":
            _append_user_message(out, content)
        else:
            raise AnthropicError(
                f"Unsupported message role: {role!r}.", param="messages"
            )
    return out


def anthropic_tools_to_chat(tools: Any) -> tuple[list[dict] | None, list[str]]:
    """Return ``(chat_tools, dropped_types)``; server-side tools are dropped."""
    if tools is None:
        return None, []
    if not isinstance(tools, list):
        raise AnthropicError("'tools' must be an array.", param="tools")
    out: list[dict] = []
    dropped: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        schema = next(
            (
                tool[k]
                for k in _ANTHROPIC_CLIENT_TOOL_KEYS
                if isinstance(tool.get(k), dict)
            ),
            None,
        )
        if tool.get("name") and schema is not None:
            fn: dict[str, Any] = {"name": tool["name"], "parameters": schema}
            if tool.get("description"):
                fn["description"] = tool["description"]
            out.append({"type": "function", "function": fn})
            continue
        dropped.append(str(tool.get("type") or tool.get("name") or "unknown"))
    return (out or None), dropped


def anthropic_tool_choice_to_chat(tool_choice: Any) -> tuple[Any, bool | None]:
    """Map ``tool_choice`` → ``(chat_tool_choice, parallel_tool_calls)``."""
    if not isinstance(tool_choice, dict):
        return None, None
    parallel = False if tool_choice.get("disable_parallel_tool_use") is True else None
    ctype = tool_choice.get("type")
    if ctype == "auto":
        return "auto", parallel
    if ctype == "any":
        return "required", parallel
    if ctype == "none":
        return "none", parallel
    if ctype == "tool" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}, parallel
    return None, parallel


def build_chat_request_from_messages(
    body: Any,
    *,
    default_model: str | None = None,
    token_field_override: str | None = None,
    require_max_tokens: bool = True,
) -> ChatRequestPlan:
    """Translate an Anthropic Messages request into a chat-completions body."""
    if not isinstance(body, dict):
        raise AnthropicError("Request body must be a JSON object.")

    model = body.get("model") or default_model
    if not isinstance(model, str) or not model:
        raise AnthropicError(
            "Request must include 'model' or start the proxy with --deployment.",
            param="model",
        )

    messages = anthropic_messages_to_chat(
        body.get("messages"),
        body.get("system"),
        # o-series/gpt-5 deployments take 'developer' where others take 'system'.
        system_role="developer" if is_reasoning_model(model) else "system",
    )
    if not messages:
        raise AnthropicError("Request must include 'messages'.", param="messages")

    payload: dict[str, Any] = {"model": model, "messages": messages}
    stream = bool(body.get("stream"))
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}

    field_name = token_limit_field(model, token_field_override)
    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
        # Anthropic's max_tokens caps *output* tokens, exactly like the
        # Responses API's max_output_tokens — same field decision applies.
        payload[field_name] = max_tokens
    elif require_max_tokens:
        raise AnthropicError(
            "'max_tokens' is required by the Messages API.", param="max_tokens"
        )

    tools, dropped_tools = anthropic_tools_to_chat(body.get("tools"))
    if tools:
        payload["tools"] = tools
        choice, parallel = anthropic_tool_choice_to_chat(body.get("tool_choice"))
        if choice is not None:
            payload["tool_choice"] = choice
        if parallel is not None:
            payload["parallel_tool_calls"] = parallel

    effort = thinking_to_reasoning_effort(body.get("thinking"))
    if effort:
        payload["reasoning_effort"] = effort

    stop = body.get("stop_sequences")
    if isinstance(stop, list) and stop:
        payload["stop"] = stop
    for name in ("temperature", "top_p", "service_tier"):
        if body.get(name) is not None:
            payload[name] = body[name]
    metadata = body.get("metadata")
    if isinstance(metadata, dict) and metadata.get("user_id"):
        payload["user"] = metadata["user_id"]

    return ChatRequestPlan(
        payload=payload,
        echo={"model": model, "stream": stream},
        stream=stream,
        token_field=field_name,
        dropped_tools=dropped_tools,
    )


# -- response: chat completions → Anthropic --------------------------------


def _tool_use_input(arguments: Any) -> dict:
    """``tool_use.input`` must be an object; degrade gracefully if it isn't."""
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def chat_usage_to_anthropic(chat: Any) -> dict:
    counts = chat_usage(chat)
    return {
        "input_tokens": counts["prompt"] - counts["cached"],
        "output_tokens": counts["completion"],
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": counts["cached"],
    }


def chat_to_anthropic_message(
    chat: Any, echo: dict, *, id_factory: IdFactory = new_id
) -> dict:
    """Convert a non-streaming chat completion into an Anthropic message."""
    if not isinstance(chat, dict):
        raise AnthropicError("Upstream returned a non-object chat completion.")
    choices = chat.get("choices") or []
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") or {}

    content: list[dict] = []
    thinking = message.get("reasoning_content")
    if isinstance(thinking, str) and thinking:
        content.append({"type": "thinking", "thinking": thinking, "signature": ""})
    text = _join_text(message.get("content"))
    if text:
        content.append({"type": "text", "text": text})
    if message.get("refusal"):
        content.append({"type": "text", "text": message["refusal"]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") or id_factory("toolu"),
                "name": fn.get("name") or "",
                "input": _tool_use_input(fn.get("arguments")),
            }
        )

    return {
        "id": sanitize_id(chat.get("id"), "msg", id_factory),
        "type": "message",
        "role": "assistant",
        "model": chat.get("model") or echo.get("model"),
        "content": content,
        "stop_reason": _ANTHROPIC_STOP_REASON.get(
            choice.get("finish_reason"), "end_turn"
        ),
        # Chat completions never reports which stop string matched.
        "stop_sequence": None,
        "usage": chat_usage_to_anthropic(chat),
    }


class AnthropicStreamTranslator:
    """Turn a chat-completions SSE stream into Anthropic Messages SSE events.

    Synchronous and I/O-free, mirroring :class:`ResponsesStreamTranslator`.
    Emits ``message_start`` → ``content_block_start`` / ``_delta`` / ``_stop``
    per block → ``message_delta`` → ``message_stop``.
    """

    def __init__(self, echo: dict, *, id_factory: IdFactory = new_id) -> None:
        self._echo = echo
        self._id_factory = id_factory
        self._message_id = id_factory("msg")
        self._model = echo.get("model")
        self._next_index = 0
        self._text_index: int | None = None
        self._calls: dict[Any, dict] = {}
        self._usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        self._stop_reason = "end_turn"
        self._done = False

    def _emit(self, name: str, payload: dict) -> bytes:
        return sse_event(name, {"type": name, **payload})

    def start(self) -> list[bytes]:
        return [
            self._emit(
                "message_start",
                {
                    "message": {
                        "id": self._message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self._model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": dict(self._usage),
                    }
                },
            )
        ]

    def feed_line(self, line: str) -> list[bytes]:
        line = line.strip()
        if not line.startswith("data:"):
            return []
        data = line[5:].strip()
        if data == "[DONE]":
            return self.finish()
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return []
        return self.feed(chunk)

    def feed(self, chunk: Any) -> list[bytes]:
        if self._done or not isinstance(chunk, dict):
            return []
        if isinstance(chunk.get("error"), dict):
            error = chunk["error"]
            return self.fail(
                str(error.get("message") or "Upstream error."),
                error_type=str(error.get("type") or "api_error"),
            )
        if chunk.get("model"):
            self._model = chunk["model"]
        if isinstance(chunk.get("usage"), dict):
            self._usage = chat_usage_to_anthropic(chunk)

        frames: list[bytes] = []
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            text = _join_text(delta.get("content"))
            if text:
                frames += self._append_text(text)
            for call in delta.get("tool_calls") or []:
                frames += self._append_tool_call(call)
            finish = choice.get("finish_reason")
            if finish:
                self._stop_reason = _ANTHROPIC_STOP_REASON.get(finish, "end_turn")
        return frames

    def finish(self) -> list[bytes]:
        if self._done:
            return []
        frames = self._close_blocks()
        self._done = True
        frames.append(
            self._emit(
                "message_delta",
                {
                    "delta": {
                        "stop_reason": self._stop_reason,
                        "stop_sequence": None,
                    },
                    "usage": dict(self._usage),
                },
            )
        )
        frames.append(self._emit("message_stop", {}))
        return frames

    def fail(self, message: str, *, error_type: str = "api_error") -> list[bytes]:
        if self._done:
            return []
        self._done = True
        return [
            self._emit("error", {"error": {"type": error_type, "message": message}})
        ]

    # -- block state machines ---------------------------------------------

    def _append_text(self, text: str) -> list[bytes]:
        frames: list[bytes] = []
        if self._text_index is None:
            self._text_index = self._next_index
            self._next_index += 1
            frames.append(
                self._emit(
                    "content_block_start",
                    {
                        "index": self._text_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
        frames.append(
            self._emit(
                "content_block_delta",
                {
                    "index": self._text_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        return frames

    def _close_text(self) -> list[bytes]:
        if self._text_index is None:
            return []
        index = self._text_index
        self._text_index = None
        return [self._emit("content_block_stop", {"index": index})]

    def _append_tool_call(self, call: Any) -> list[bytes]:
        if not isinstance(call, dict):
            return []
        # A tool call ends any open text block (mirrors the real API).
        frames = self._close_text()
        key = call.get("index", 0)
        fn = call.get("function") or {}
        state = self._calls.get(key)
        if state is None:
            state = {
                "index": self._next_index,
                "id": call.get("id") or self._id_factory("toolu"),
                "name": fn.get("name") or "",
                "args": [],
            }
            self._next_index += 1
            self._calls[key] = state
            frames.append(
                self._emit(
                    "content_block_start",
                    {
                        "index": state["index"],
                        "content_block": {
                            "type": "tool_use",
                            "id": state["id"],
                            "name": state["name"],
                            "input": {},
                        },
                    },
                )
            )
        else:
            if call.get("id"):
                state["id"] = call["id"]
            if fn.get("name") and not state["name"]:
                state["name"] = fn["name"]

        arguments = fn.get("arguments")
        if arguments:
            state["args"].append(arguments)
            frames.append(
                self._emit(
                    "content_block_delta",
                    {
                        "index": state["index"],
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": arguments,
                        },
                    },
                )
            )
        return frames

    def _close_blocks(self) -> list[bytes]:
        frames = self._close_text()
        for key in sorted(self._calls, key=lambda k: self._calls[k]["index"]):
            frames.append(
                self._emit("content_block_stop", {"index": self._calls[key]["index"]})
            )
        self._calls = {}
        return frames


def openai_models_to_anthropic(payload: Any) -> dict:
    """Reshape an OpenAI ``/models`` listing into Anthropic's model list."""
    entries = payload.get("data") if isinstance(payload, dict) else None
    models = []
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        created = entry.get("created")
        models.append(
            {
                "type": "model",
                "id": entry["id"],
                "display_name": entry.get("display_name") or entry["id"],
                "created_at": (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created))
                    if isinstance(created, int)
                    else None
                ),
            }
        )
    return {
        "data": models,
        "has_more": False,
        "first_id": models[0]["id"] if models else None,
        "last_id": models[-1]["id"] if models else None,
    }


# Route handlers -----------------------------------------------------------


def _json_error(
    status: int,
    code: str,
    message: str,
    *,
    param: str | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": code,
                "type": code,
                "message": message,
                "param": param,
            }
        },
        status_code=status,
        headers=headers,
    )


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def status_endpoint(request: Request) -> JSONResponse:
    cfg = request.app.state.cfg
    return JSONResponse(
        {
            "endpoint": cfg["endpoint"],
            "instance": cfg["instance"],
            "api_version": cfg["api_version"],
            "default_deployment": cfg["default_dep"],
            "api_key_required": cfg["api_key"] is not None,
            "responses_mode": cfg.get("responses_mode", "auto"),
            "anthropic_mode": cfg.get("anthropic_mode", "translate"),
            "token_limit_field": cfg.get("token_field") or "auto",
            "retry_429": cfg.get("retry_429", DEFAULT_429_RETRIES),
            "retry_max_wait": cfg.get("retry_max_wait", DEFAULT_429_MAX_WAIT),
        }
    )


async def options_preflight(request: Request) -> Response:
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
            "Access-Control-Allow-Headers": request.headers.get(
                "Access-Control-Request-Headers", "*"
            ),
        },
    )


def _video_resource_id(path: str) -> str | None:
    if path.startswith("/v1/"):
        path = path[3:]
    parts = path.strip("/").split("/")
    return parts[1] if len(parts) > 1 and parts[0] == "videos" else None


def _with_query_model(path_qs: str, model: str) -> str:
    parts = urllib.parse.urlsplit(path_qs)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("model", model)
    return urllib.parse.urlunsplit(
        ("", "", parts.path, urllib.parse.urlencode(query), "")
    )


def _remember_video_model(state: Any, video_id: str, model: str) -> None:
    models = state.video_models
    models.pop(video_id, None)
    models[video_id] = model
    if len(models) > MAX_VIDEO_MODELS:
        models.pop(next(iter(models)))


async def proxy(request: Request) -> Response:
    state = request.app.state
    cfg = state.cfg
    raw = await request.body()

    if not check_api_key(request.headers, cfg["api_key"]):
        return _json_error(401, "invalid_api_key", "Missing or invalid API key.")

    body_obj = parse_body_metadata(raw, request.headers.get("content-type", ""))

    path_qs = request.url.path + (
        ("?" + request.url.query) if request.url.query else ""
    )
    video_id = _video_resource_id(request.url.path)
    query = dict(urllib.parse.parse_qsl(request.url.query, keep_blank_values=True))
    video_model = query.get("model") or query.get("deployment")
    if video_id and not video_model:
        video_model = state.video_models.get(video_id) or cfg["default_dep"]
        if not video_model:
            return _json_error(
                400,
                "missing_model",
                "Video retrieval requires the creating proxy process, a "
                "'model' query parameter, or --deployment.",
                param="model",
            )
        path_qs = _with_query_model(path_qs, video_model)
    if video_id and video_model:
        _remember_video_model(state, video_id, video_model)

    url = None
    if cfg.get("responses_mode", "auto") in ("auto", "passthrough"):
        url = resolve_native_responses_url(path_qs, cfg["base"])
        if (
            url is not None
            and request.method == "POST"
            and request.url.path.rstrip("/") in ("/responses", "/v1/responses")
            and isinstance(body_obj, dict)
            and not body_obj.get("model")
            and cfg["default_dep"]
        ):
            body_obj = {**body_obj, "model": cfg["default_dep"]}
            raw = json.dumps(body_obj, ensure_ascii=False).encode()
    if url is None:
        url = resolve_native_api_url(path_qs, cfg["base"])
    if url is None:
        url = resolve_url(
            path_qs, body_obj, cfg["base"], cfg["api_version"], cfg["default_dep"]
        )
    if url is None:
        return _json_error(
            400,
            "missing_model",
            "Request must include 'model' or start the proxy with --deployment.",
        )

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_REQ}
    try:
        headers["Authorization"] = "Bearer " + await state.token_provider()
    except Exception as exc:  # noqa: BLE001
        return _json_error(500, "auth_failed", f"Token error: {exc}")

    try:
        upstream_resp = await _send_http_with_retry(
            state,
            request.method,
            url,
            content=raw or None,
            headers=headers,
        )
    except httpx.HTTPError as exc:
        return _json_error(502, "upstream_unreachable", str(exc))

    response_headers = _response_headers(upstream_resp)
    if "text/event-stream" in (upstream_resp.headers.get("content-type") or "").lower():
        response_headers["Cache-Control"] = "no-cache"
        response_headers["X-Accel-Buffering"] = "no"

    if request.method == "POST" and request.url.path.rstrip("/") in (
        "/videos",
        "/v1/videos",
    ):
        body = await _drain(upstream_resp)
        if upstream_resp.status_code < 300:
            try:
                video = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                video = None
            model = video.get("model") if isinstance(video, dict) else None
            video_id = video.get("id") if isinstance(video, dict) else None
            if not isinstance(model, str) and isinstance(body_obj, dict):
                model = body_obj.get("model")
            if isinstance(video_id, str) and isinstance(model, str):
                _remember_video_model(state, video_id, model)
        return Response(
            content=body,
            status_code=upstream_resp.status_code,
            headers=response_headers,
        )

    if request.method == "DELETE" and video_id and upstream_resp.status_code < 300:
        state.video_models.pop(video_id, None)

    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream_resp.aclose),
    )


# Shared transport for the translating endpoints ---------------------------


def _response_headers(resp: httpx.Response) -> dict[str, str]:
    """Headers safe to relay from an upstream response."""
    return {k: v for k, v in resp.headers.items() if k.lower() not in HOP_RES}


async def _drain(resp: httpx.Response) -> bytes:
    try:
        return await resp.aread()
    finally:
        await resp.aclose()


class _ReplayStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self):
        yield self.body


def _buffered_response(resp: httpx.Response, body: bytes) -> httpx.Response:
    return httpx.Response(
        status_code=resp.status_code,
        headers=resp.headers,
        stream=_ReplayStream(body),
        request=resp.request,
        extensions=resp.extensions,
    )


async def _wait_for_rate_limit(
    state: Any,
    headers: Any,
    body: bytes,
    *,
    retry_number: int,
    waited: float,
) -> float | None:
    cfg = state.cfg
    delay = retry_after_seconds(
        headers,
        body,
        retry_number=retry_number,
        now=state.retry_now(),
    )
    delay += min(1.0, delay * 0.1) * state.retry_random()
    max_wait = max(0.0, float(cfg.get("retry_max_wait", DEFAULT_429_MAX_WAIT)))
    if waited + delay > max_wait:
        return None
    click.echo(
        f"upstream returned 429; retrying in {delay:.3g}s "
        f"({retry_number}/{cfg.get('retry_429', DEFAULT_429_RETRIES)})",
        err=True,
    )
    await state.retry_sleep(delay)
    return waited + delay


async def _send_http_with_retry(
    state: Any,
    method: str,
    url: str,
    *,
    content: bytes | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    """Send a replayable HTTP request and retry explicit 429 responses."""
    retries = max(0, int(state.cfg.get("retry_429", DEFAULT_429_RETRIES)))
    waited = 0.0
    for attempt in range(retries + 1):
        request = state.client.build_request(
            method,
            url,
            content=content,
            headers=headers,
        )
        response = await state.client.send(request, stream=True)
        if response.status_code != 429 or attempt >= retries:
            return response

        body = await _drain(response)
        new_waited = await _wait_for_rate_limit(
            state,
            response.headers,
            body,
            retry_number=attempt + 1,
            waited=waited,
        )
        if new_waited is None:
            return _buffered_response(response, body)
        waited = new_waited

    raise RuntimeError("unreachable")  # pragma: no cover


def _catalogue_responses_capabilities(payload: Any) -> dict[str, bool]:
    entries = payload.get("data") if isinstance(payload, dict) else None
    capabilities = {}
    for entry in entries or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        model_capabilities = entry.get("capabilities")
        value = (
            model_capabilities.get("responses")
            if isinstance(model_capabilities, dict)
            else False
        )
        capabilities[entry["id"]] = value is True or (
            isinstance(value, str) and value.lower() == "true"
        )
    return capabilities


async def _model_supports_responses(state: Any, model: str) -> bool:
    """Read a model's native Responses capability from a short-lived cache."""
    now = state.models_now()
    if (
        state.responses_capabilities is not None
        and now < state.responses_capabilities_expires
    ):
        return state.responses_capabilities.get(model, False)

    async with state.responses_capabilities_lock:
        now = state.models_now()
        if (
            state.responses_capabilities is not None
            and now < state.responses_capabilities_expires
        ):
            return state.responses_capabilities.get(model, False)

        cfg = state.cfg
        try:
            token = await state.token_provider()
            url = resolve_url(
                "/v1/models",
                None,
                cfg["base"],
                cfg["api_version"],
                cfg["default_dep"],
            )
            upstream = await _send_http_with_retry(
                state,
                "GET",
                url,
                headers={"Authorization": "Bearer " + token},
            )
            body = await _drain(upstream)
        except (httpx.HTTPError, OSError) as exc:
            click.echo(
                f"model capability refresh failed; using translate: {exc}", err=True
            )
            return False
        except Exception as exc:  # noqa: BLE001
            click.echo(
                f"model capability refresh failed; using translate: {exc}", err=True
            )
            return False

        if upstream.status_code >= 400:
            click.echo(
                f"model capability refresh returned HTTP {upstream.status_code}; "
                "using translate",
                err=True,
            )
            return False
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            click.echo(
                "model capability refresh returned invalid JSON; using translate",
                err=True,
            )
            return False

        state.responses_capabilities = _catalogue_responses_capabilities(payload)
        state.responses_capabilities_expires = now + MODEL_CAPABILITY_TTL
        return state.responses_capabilities.get(model, False)


async def _send_chat(state: Any, payload: dict, headers: dict) -> httpx.Response:
    """POST a translated chat-completions payload upstream (streaming read)."""
    cfg = state.cfg
    url = resolve_chat_url(
        payload,
        cfg["base"],
        cfg["api_version"],
        cfg["default_dep"],
    )
    return await _send_http_with_retry(
        state,
        "POST",
        url,
        content=json.dumps(payload, ensure_ascii=False).encode(),
        headers=headers,
    )


def _upstream_headers(
    request: Request, token: str, *, drop: frozenset = frozenset()
) -> dict:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_REQ and k.lower() not in drop
    }
    headers["content-type"] = "application/json"
    headers["Authorization"] = "Bearer " + token
    return headers


@dataclass
class UpstreamFailure:
    """A non-2xx upstream reply, already fully read."""

    status: int
    body: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)


async def _call_chat_with_retry(
    state: Any, payload: dict, headers: dict
) -> tuple[httpx.Response | None, UpstreamFailure | None]:
    """Send *payload*, renegotiating parameters the deployment rejects.

    Returns ``(open_streaming_response, None)`` on success, or
    ``(None, UpstreamFailure)`` when the upstream refused for a reason
    :func:`plan_retry` cannot repair.
    """
    for attempt in range(MAX_PARAM_RETRIES):
        try:
            upstream = await _send_chat(state, payload, headers)
        except httpx.HTTPError as exc:
            return None, UpstreamFailure(502, str(exc).encode(), "text/plain")
        if upstream.status_code == 400:
            failure_headers = _response_headers(upstream)
            error_body = await _drain(upstream)
            retry = plan_retry(400, error_body.decode(errors="replace"), payload)
            if retry is None or attempt + 1 >= MAX_PARAM_RETRIES:
                return None, UpstreamFailure(
                    400, error_body, "application/json", failure_headers
                )
            payload, note = retry
            click.echo(f"retrying chat completion with {note}", err=True)
            continue
        if upstream.status_code >= 400:
            failure_headers = _response_headers(upstream)
            error_body = await _drain(upstream)
            return None, UpstreamFailure(
                upstream.status_code,
                error_body,
                upstream.headers.get("content-type", "application/json"),
                failure_headers,
            )
        return upstream, None
    # pragma: no cover - the loop always returns
    return None, UpstreamFailure(502, b"parameter negotiation failed", "text/plain")


async def _relay_translated_stream(upstream: httpx.Response, translator: Any):
    """Pump upstream SSE lines through a translator's ``feed_line``."""
    try:
        for frame in translator.start():
            yield frame
        async for line in upstream.aiter_lines():
            for frame in translator.feed_line(line):
                yield frame
        for frame in translator.finish():
            yield frame
    except httpx.HTTPError as exc:
        for frame in translator.fail(f"Upstream stream error: {exc}"):
            yield frame
    finally:
        await upstream.aclose()


def _sse_response(
    body: Any, *, upstream_headers: dict[str, str] | None = None
) -> StreamingResponse:
    headers = dict(upstream_headers or {})
    headers["Cache-Control"] = "no-cache"
    headers["X-Accel-Buffering"] = "no"
    return StreamingResponse(
        body,
        status_code=200,
        media_type="text/event-stream",
        headers=headers,
    )


# Responses API endpoint ---------------------------------------------------


async def responses_auto(request: Request) -> Response:
    """Use native Responses when the model catalogue advertises support."""
    raw = await request.body()
    cfg = request.app.state.cfg
    if not check_api_key(request.headers, cfg["api_key"]):
        return _json_error(401, "invalid_api_key", "Missing or invalid API key.")
    try:
        body = json.loads(raw) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return await responses(request)
    model = body.get("model") if isinstance(body, dict) else None
    model = model if isinstance(model, str) and model else cfg["default_dep"]
    if isinstance(model, str) and await _model_supports_responses(
        request.app.state, model
    ):
        return await proxy(request)
    return await responses(request)


async def responses(request: Request) -> Response:
    """Emulate ``POST /v1/responses`` on top of ``/v1/chat/completions``."""
    state = request.app.state
    cfg = state.cfg
    raw = await request.body()

    if not check_api_key(request.headers, cfg["api_key"]):
        return _json_error(401, "invalid_api_key", "Missing or invalid API key.")

    try:
        body = json.loads(raw) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_error(
            400, "invalid_request_error", "Request body must be valid JSON."
        )

    try:
        plan = build_chat_request(
            body,
            default_model=cfg["default_dep"],
            token_field_override=cfg.get("token_field"),
        )
    except TranslationError as exc:
        return _json_error(400, exc.code, exc.message, param=exc.param)

    try:
        headers = _upstream_headers(request, await state.token_provider())
    except Exception as exc:  # noqa: BLE001
        return _json_error(500, "auth_failed", f"Token error: {exc}")

    upstream, failure = await _call_chat_with_retry(state, plan.payload, headers)
    if failure is not None:
        if failure.status == 502:
            return _json_error(
                502,
                "upstream_unreachable",
                failure.body.decode(errors="replace"),
                headers=failure.headers,
            )
        return Response(
            content=failure.body,
            status_code=failure.status,
            media_type=failure.content_type,
            headers=failure.headers,
        )

    response_headers = _response_headers(upstream)
    if plan.stream:
        return _sse_response(
            _relay_translated_stream(upstream, ResponsesStreamTranslator(plan.echo)),
            upstream_headers=response_headers,
        )

    completion = await _drain(upstream)
    try:
        chat_obj = json.loads(completion)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_error(
            502, "upstream_invalid_response", "Upstream returned non-JSON content."
        )
    try:
        return JSONResponse(
            chat_to_responses(chat_obj, plan.echo), headers=response_headers
        )
    except TranslationError as exc:
        return _json_error(502, "upstream_invalid_response", exc.message)


async def responses_unsupported(_request: Request) -> Response:
    return _json_error(
        404,
        "not_found",
        "This proxy translates /v1/responses into chat completions and never "
        "stores responses ('store' is always false), so stored-response "
        "operations are unavailable.",
    )


# Anthropic Messages endpoints ---------------------------------------------

# Anthropic clients send their own protocol headers; none apply upstream.
ANTHROPIC_DROP_HEADERS = frozenset(
    {
        "anthropic-version",
        "anthropic-beta",
        "anthropic-dangerous-direct-browser-access",
    }
)


def _anthropic_error(
    status: int,
    message: str,
    *,
    error_type: str | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        {
            "type": "error",
            "error": {
                "type": error_type or anthropic_error_type(status),
                "message": message,
            },
        },
        status_code=status,
        headers=headers,
    )


def _anthropic_upstream_error(failure: UpstreamFailure) -> Response:
    """Relay an upstream chat-completions failure in Anthropic's error shape."""
    _, message = _error_param(failure.body.decode(errors="replace"))
    return _anthropic_error(
        failure.status,
        message or "Upstream request failed.",
        headers=failure.headers,
    )


async def _anthropic_prepare(
    request: Request, *, require_max_tokens: bool = True
) -> tuple[ChatRequestPlan | None, dict | None, Response | None]:
    """Auth, parse and translate; returns ``(plan, headers, error_response)``."""
    state = request.app.state
    cfg = state.cfg
    raw = await request.body()

    if not check_api_key(request.headers, cfg["api_key"]):
        return None, None, _anthropic_error(401, "Missing or invalid API key.")
    try:
        body = json.loads(raw) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None, _anthropic_error(400, "Request body must be valid JSON.")
    try:
        plan = build_chat_request_from_messages(
            body,
            default_model=cfg["default_dep"],
            token_field_override=cfg.get("token_field"),
            require_max_tokens=require_max_tokens,
        )
    except TranslationError as exc:
        return None, None, _anthropic_error(400, exc.message)
    try:
        headers = _upstream_headers(
            request, await state.token_provider(), drop=ANTHROPIC_DROP_HEADERS
        )
    except Exception as exc:  # noqa: BLE001
        return None, None, _anthropic_error(500, f"Token error: {exc}")
    return plan, headers, None


async def anthropic_messages(request: Request) -> Response:
    """Emulate ``POST /v1/messages`` on top of ``/v1/chat/completions``."""
    plan, headers, error = await _anthropic_prepare(request)
    if error is not None:
        return error

    upstream, failure = await _call_chat_with_retry(
        request.app.state, plan.payload, headers
    )
    if failure is not None:
        return _anthropic_upstream_error(failure)

    response_headers = _response_headers(upstream)
    if plan.stream:
        return _sse_response(
            _relay_translated_stream(upstream, AnthropicStreamTranslator(plan.echo)),
            upstream_headers=response_headers,
        )

    completion = await _drain(upstream)
    try:
        chat_obj = json.loads(completion)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _anthropic_error(502, "Upstream returned non-JSON content.")
    try:
        return JSONResponse(
            chat_to_anthropic_message(chat_obj, plan.echo), headers=response_headers
        )
    except TranslationError as exc:
        return _anthropic_error(502, exc.message)


async def anthropic_count_tokens(request: Request) -> Response:
    """Emulate ``POST /v1/messages/count_tokens``.

    Chat completions has no token-counting endpoint, so the prompt is priced
    by running it with a 1-token generation budget and reading back
    ``usage.prompt_tokens`` — exact rather than a client-side estimate, at
    the cost of one (tiny) upstream call.
    """
    plan, headers, error = await _anthropic_prepare(request, require_max_tokens=False)
    if error is not None:
        return error

    payload = dict(plan.payload)
    for key in ("stream", "stream_options", *TOKEN_LIMIT_FIELDS):
        payload.pop(key, None)
    payload[plan.token_field] = 1

    upstream, failure = await _call_chat_with_retry(request.app.state, payload, headers)
    if failure is not None:
        return _anthropic_upstream_error(failure)

    response_headers = _response_headers(upstream)
    completion = await _drain(upstream)
    try:
        chat_obj = json.loads(completion)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _anthropic_error(502, "Upstream returned non-JSON content.")
    return JSONResponse(
        {"input_tokens": chat_usage(chat_obj)["prompt"]}, headers=response_headers
    )


async def anthropic_models(request: Request) -> Response:
    """Anthropic-shaped model listing, translated from the upstream catalogue."""
    state = request.app.state
    cfg = state.cfg
    if not check_api_key(request.headers, cfg["api_key"]):
        return _anthropic_error(401, "Missing or invalid API key.")
    try:
        headers = _upstream_headers(
            request, await state.token_provider(), drop=ANTHROPIC_DROP_HEADERS
        )
    except Exception as exc:  # noqa: BLE001
        return _anthropic_error(500, f"Token error: {exc}")

    url = resolve_url(
        "/v1/models", None, cfg["base"], cfg["api_version"], cfg["default_dep"]
    )
    try:
        upstream = await _send_http_with_retry(state, "GET", url, headers=headers)
    except httpx.HTTPError as exc:
        return _anthropic_error(502, str(exc))
    response_headers = _response_headers(upstream)
    body = await _drain(upstream)
    if upstream.status_code >= 400:
        _, message = _error_param(body.decode(errors="replace"))
        return _anthropic_error(
            upstream.status_code,
            message or "Upstream error.",
            headers=response_headers,
        )
    try:
        catalogue = openai_models_to_anthropic(json.loads(body))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _anthropic_error(502, "Upstream returned non-JSON content.")

    model_id = request.path_params.get("model_id")
    if model_id:
        for model in catalogue["data"]:
            if model["id"] == model_id:
                return JSONResponse(model, headers=response_headers)
        return _anthropic_error(404, f"Model {model_id!r} not found.")
    return JSONResponse(catalogue, headers=response_headers)


async def _close_websocket_error(
    websocket: WebSocket, code: str, message: str, *, close_code: int = 1011
) -> None:
    await websocket.accept()
    await websocket.send_json(
        {
            "type": "error",
            "error": {
                "type": "server_error",
                "code": code,
                "message": message,
            },
        }
    )
    await websocket.close(code=close_code)


async def realtime_proxy(websocket: WebSocket) -> None:
    """Bridge an OpenAI realtime WebSocket to TRAPI with AAD authentication."""
    state = websocket.app.state
    cfg = state.cfg
    if not check_api_key(websocket.headers, cfg["api_key"]):
        await websocket.close(code=1008, reason="Missing or invalid API key.")
        return

    path_qs = websocket.url.path + (
        ("?" + websocket.url.query) if websocket.url.query else ""
    )
    url = resolve_realtime_url(path_qs, cfg["base"], cfg["default_dep"])
    if url is None:
        await _close_websocket_error(
            websocket,
            "missing_model",
            "Realtime connections require a 'model' query parameter or --deployment.",
            close_code=1008,
        )
        return

    try:
        token = await state.token_provider()
    except Exception as exc:  # noqa: BLE001
        await _close_websocket_error(websocket, "auth_failed", f"Token error: {exc}")
        return

    headers = {
        key: value
        for key, value in websocket.headers.items()
        if key.lower() not in HOP_WS_REQ
    }
    headers["Authorization"] = "Bearer " + token
    protocols = [
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    ]

    retries = max(0, int(cfg.get("retry_429", DEFAULT_429_RETRIES)))
    waited = 0.0
    upstream = None
    for attempt in range(retries + 1):
        try:
            upstream = await websockets.connect(
                url,
                additional_headers=headers,
                subprotocols=protocols or None,
                open_timeout=min(float(cfg.get("timeout", 600.0)), 30.0),
                close_timeout=5,
                max_size=None,
            )
            break
        except InvalidStatus as exc:
            status = exc.response.status_code
            body = exc.response.body or b""
            if isinstance(body, str):
                body = body.encode()
            if status == 429 and attempt < retries:
                new_waited = await _wait_for_rate_limit(
                    state,
                    exc.response.headers,
                    body,
                    retry_number=attempt + 1,
                    waited=waited,
                )
                if new_waited is not None:
                    waited = new_waited
                    continue

            retry_after = exc.response.headers.get("Retry-After")
            message = None
            try:
                error = json.loads(body).get("error")
            except (UnicodeDecodeError, json.JSONDecodeError):
                error = None
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
            if not message:
                message = f"Upstream WebSocket handshake failed with HTTP {status}."
            if retry_after:
                message += f" Retry after {retry_after} seconds."
            await _close_websocket_error(
                websocket,
                "upstream_rejected",
                message,
                close_code=1013 if status == 429 else 1011,
            )
            return
        except (OSError, TimeoutError, websockets.WebSocketException) as exc:
            await _close_websocket_error(websocket, "upstream_unreachable", str(exc))
            return

    if upstream is None:  # pragma: no cover - every loop path breaks or returns
        return

    await websocket.accept(subprotocol=upstream.subprotocol)

    async def client_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                await upstream.close(code=message.get("code", 1000))
                return
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])

    async def upstream_to_client() -> None:
        async for message in upstream:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)

    pumps = {
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    }
    try:
        _, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
    finally:
        await upstream.close()
        try:
            await websocket.close(code=upstream.close_code or 1000)
        except RuntimeError:
            pass


# Access log ---------------------------------------------------------------


def log_timezone(offset_hours: float) -> timezone:
    """A fixed-offset zone, so log timestamps do not depend on the host's TZ.

    A proxy usually runs on a box configured in UTC while the person reading
    the log is not, which makes every timestamp a subtraction.
    """
    return timezone(timedelta(hours=offset_hours))


def log_client(scope: dict) -> tuple[str, str]:
    """The peer address as (ip, port), with "-" for anything unknown.

    Deliberately the socket peer rather than X-Forwarded-For: that header is
    attacker-controlled, and a log that can be forged is worse than one that
    only reports what the kernel saw.
    """
    client = scope.get("client")
    if not client:
        return "-", "-"
    host = client[0] or "-"
    port = client[1] if len(client) > 1 else None
    return str(host), ("-" if port in (None, "") else str(port))


def log_model(body: bytes, query_string: bytes, default: str | None = None) -> str:
    """Best-effort model name for one request.

    The body is authoritative and is where every chat/responses/messages call
    puts it; a few endpoints pass it in the query instead. Anything
    unparseable simply has no model, which is not worth a log warning.
    """
    if body:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            for key in ("model", "deployment"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:MAX_MODEL_LENGTH]
    if query_string:
        try:
            query = urllib.parse.parse_qs(query_string.decode("utf-8", "replace"))
        except ValueError:  # pragma: no cover - parse_qs is very tolerant
            query = {}
        for key in ("model", "deployment"):
            values = query.get(key)
            if values and isinstance(values[0], str) and values[0].strip():
                return values[0].strip()[:MAX_MODEL_LENGTH]
    return default or "-"


def format_access_line(
    *,
    when: datetime,
    ip: str,
    port: str,
    method: str,
    path: str,
    model: str,
    status: int | str,
    duration: float,
) -> str:
    """One greppable line per request, fields in a stable order."""
    stamp = when.strftime("%Y-%m-%d %H:%M:%S %z")
    return (
        f"{stamp} {ip}:{port} {method} {path} model={model} "
        f"status={status} {duration * 1000:.0f}ms"
    )


class AccessLog:
    """ASGI middleware logging time, peer, model and outcome per request.

    Written as raw ASGI rather than BaseHTTPMiddleware so that streaming
    responses are untouched: the body is *observed* on its way past, never
    consumed, and only the first MAX_LOG_BODY_SNIFF bytes are held.
    """

    def __init__(
        self,
        app,
        *,
        tz: timezone,
        stream=None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[timezone], datetime] | None = None,
        default_model: str | None = None,
    ) -> None:
        self.app = app
        self.tz = tz
        self.stream = stream
        self.clock = clock
        self.now = now or (lambda tz: datetime.now(tz))
        self.default_model = default_model

    def _write(self, line: str) -> None:
        stream = self.stream if self.stream is not None else sys.stderr
        try:
            print(line, file=stream, flush=True)
        except (OSError, ValueError):  # pragma: no cover - closed stream
            pass

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        ip, port = log_client(scope)
        path = scope.get("path", "-")
        started = self.clock()

        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            self._write(
                format_access_line(
                    when=self.now(self.tz),
                    ip=ip,
                    port=port,
                    method="WS",
                    path=path,
                    model="-",
                    status="closed",
                    duration=self.clock() - started,
                )
            )
            return

        sniffed = bytearray()

        async def receive_logging():
            message = await receive()
            if message.get("type") == "http.request":
                room = MAX_LOG_BODY_SNIFF - len(sniffed)
                if room > 0:
                    sniffed.extend(message.get("body", b"")[:room])
            return message

        status: int | str = "-"

        async def send_logging(message):
            nonlocal status
            if message.get("type") == "http.response.start":
                status = message.get("status", "-")
            await send(message)

        try:
            await self.app(scope, receive_logging, send_logging)
        except Exception:
            # Still record the request; the exception belongs to the caller.
            self._write(
                format_access_line(
                    when=self.now(self.tz),
                    ip=ip,
                    port=port,
                    method=scope.get("method", "-"),
                    path=path,
                    model=log_model(
                        bytes(sniffed),
                        scope.get("query_string", b""),
                        self.default_model,
                    ),
                    status="error",
                    duration=self.clock() - started,
                )
            )
            raise

        self._write(
            format_access_line(
                when=self.now(self.tz),
                ip=ip,
                port=port,
                method=scope.get("method", "-"),
                path=path,
                model=log_model(
                    bytes(sniffed), scope.get("query_string", b""), self.default_model
                ),
                status=status,
                duration=self.clock() - started,
            )
        )


# App factory --------------------------------------------------------------


def build_app(
    cfg: dict,
    *,
    token_provider: AsyncTokenProvider | None = None,
    client: httpx.AsyncClient | None = None,
    retry_sleep: AsyncSleeper | None = None,
    retry_random: Callable[[], float] | None = None,
    retry_now: Callable[[], float] | None = None,
    log_stream=None,
) -> Starlette:
    """Build the ASGI app.

    Test seams: pass *token_provider* and/or *client* to skip the real
    azure-identity wiring and outbound httpx.AsyncClient instantiation.
    """

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.cfg = cfg
        owned_client = client is None
        app.state.client = client or httpx.AsyncClient(
            timeout=cfg.get("timeout", 600.0),
            follow_redirects=True,
        )
        app.state.token_provider = token_provider or make_token_provider()
        app.state.video_models = {}
        app.state.retry_sleep = retry_sleep or asyncio.sleep
        app.state.retry_random = retry_random or random.random
        app.state.retry_now = retry_now or time.time
        app.state.models_now = time.monotonic
        app.state.responses_capabilities = None
        app.state.responses_capabilities_expires = 0.0
        app.state.responses_capabilities_lock = asyncio.Lock()

        if not cfg.get("skip_warmup"):
            try:
                await app.state.token_provider()
            except Exception as exc:  # noqa: BLE001
                click.echo(
                    f"Failed to acquire Azure AD token: {exc}\n"
                    f"Try: az login --scope {SCOPE}",
                    err=True,
                )
                raise SystemExit(2)

        try:
            yield
        finally:
            if owned_client:
                await app.state.client.aclose()

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/status", status_endpoint, methods=["GET"]),
        WebSocketRoute("/v1/realtime", realtime_proxy),
        WebSocketRoute("/realtime", realtime_proxy),
    ]
    # Registered ahead of the catch-all so translation/auto selection wins;
    # passthrough leaves all Responses paths to the generic native proxy.
    responses_mode = cfg.get("responses_mode", "auto")
    if responses_mode in ("auto", "translate"):
        for prefix in ("/v1", ""):
            handler = responses_auto if responses_mode == "auto" else responses
            routes.append(Route(f"{prefix}/responses", handler, methods=["POST"]))
            if responses_mode == "translate":
                routes.append(
                    Route(
                        f"{prefix}/responses/{{rest:path}}",
                        responses_unsupported,
                        methods=["GET", "POST", "DELETE"],
                    )
                )
    if cfg.get("anthropic_mode", "translate") == "translate":
        # Mounted at the root and under /anthropic so a client can point an
        # Anthropic SDK at either base URL; the /anthropic prefix additionally
        # serves an Anthropic-shaped /v1/models that would otherwise collide
        # with the OpenAI listing.
        for prefix in ("/v1", "", "/anthropic/v1"):
            routes.append(
                Route(
                    f"{prefix}/messages/count_tokens",
                    anthropic_count_tokens,
                    methods=["POST"],
                )
            )
            routes.append(
                Route(f"{prefix}/messages", anthropic_messages, methods=["POST"])
            )
        routes.append(Route("/anthropic/v1/models", anthropic_models, methods=["GET"]))
        routes.append(
            Route("/anthropic/v1/models/{model_id}", anthropic_models, methods=["GET"])
        )
    routes += [
        Route("/{path:path}", options_preflight, methods=["OPTIONS"]),
        Route(
            "/{path:path}",
            proxy,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        ),
    ]

    app = Starlette(lifespan=lifespan, routes=routes)
    if cfg.get("access_log", True):
        # Wrapped rather than added via add_middleware so the log sees the
        # raw ASGI events, including for the WebSocket routes.
        app = AccessLog(
            app,
            tz=log_timezone(cfg.get("log_utc_offset", DEFAULT_LOG_UTC_OFFSET)),
            stream=log_stream,
            default_model=cfg.get("default_dep"),
        )
    return app


# CLI ----------------------------------------------------------------------


@click.command(
    context_settings={"show_default": True, "help_option_names": ["-h", "--help"]},
    help="OpenAI-compatible async proxy forwarding to Microsoft TRAPI.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    envvar="TRAPI_PROXY_HOST",
    help="Bind address. 0.0.0.0 exposes on all interfaces.",
)
@click.option("--port", type=int, default=8080, envvar="TRAPI_PROXY_PORT")
@click.option(
    "--instance",
    default="gcr/shared",
    envvar="TRAPI_INSTANCE",
    help="TRAPI instance. See https://aka.ms/trapi/models.",
)
@click.option("--api-version", default="2024-10-21", envvar="TRAPI_API_VERSION")
@click.option(
    "--endpoint",
    default="https://trapi.research.microsoft.com",
    envvar="TRAPI_ENDPOINT",
)
@click.option(
    "--deployment",
    default=None,
    envvar="TRAPI_DEFAULT_DEPLOYMENT",
    help="Default deployment when the request omits 'model'.",
)
@click.option("--timeout", type=float, default=600.0, envvar="TRAPI_PROXY_TIMEOUT")
@click.option(
    "--api-key",
    default=None,
    envvar="TRAPI_PROXY_API_KEY",
    help="If set, clients must present this via 'Authorization: Bearer "
    "<key>' or 'api-key' header.",
)
@click.option("--skip-token-warmup", is_flag=True, help="Skip the upfront token fetch.")
@click.option(
    "--responses-mode",
    type=click.Choice(["auto", "translate", "passthrough"]),
    default="auto",
    envvar="TRAPI_PROXY_RESPONSES_MODE",
    help="'auto' translates ordinary creates and uses native Responses for "
    "server-side features; 'translate' always emulates over chat completions; "
    "'passthrough' always forwards unchanged.",
)
@click.option(
    "--anthropic-mode",
    type=click.Choice(["translate", "passthrough"]),
    default="translate",
    envvar="TRAPI_PROXY_ANTHROPIC_MODE",
    help="'translate' emulates the Anthropic Messages API (/v1/messages, "
    "/v1/messages/count_tokens, /anthropic/v1/models) over chat completions; "
    "'passthrough' forwards those paths unchanged.",
)
@click.option(
    "--token-limit-field",
    type=click.Choice(["auto", "max_tokens", "max_completion_tokens"]),
    default="auto",
    envvar="TRAPI_PROXY_TOKEN_LIMIT_FIELD",
    help="Chat-completions field carrying the Responses API's "
    "'max_output_tokens'. 'auto' sends max_completion_tokens to reasoning "
    "deployments (o-series/gpt-5/codex) and max_tokens elsewhere, then "
    "retries with the other name if the upstream rejects it.",
)
@click.option(
    "--retry-429",
    type=click.IntRange(min=0),
    default=DEFAULT_429_RETRIES,
    envvar="TRAPI_PROXY_RETRY_429",
    help="Additional attempts after an upstream HTTP 429. Set 0 to disable.",
)
@click.option(
    "--retry-max-wait",
    type=click.FloatRange(min=0.0),
    default=DEFAULT_429_MAX_WAIT,
    envvar="TRAPI_PROXY_RETRY_MAX_WAIT",
    help="Maximum cumulative seconds spent waiting on 429 retries.",
)
@click.option(
    "--log-level",
    default="info",
    envvar="TRAPI_PROXY_LOG_LEVEL",
    type=click.Choice(["debug", "info", "warning", "error"]),
)
@click.option(
    "--log-tz",
    type=float,
    default=DEFAULT_LOG_UTC_OFFSET,
    envvar="TRAPI_PROXY_LOG_TZ",
    help="UTC offset in hours for access-log timestamps.",
)
@click.option(
    "--access-log/--no-access-log",
    default=True,
    envvar="TRAPI_PROXY_ACCESS_LOG",
    help="Log one line per request: time, peer, model, status.",
)
def cli(
    host,
    port,
    instance,
    api_version,
    endpoint,
    deployment,
    timeout,
    api_key,
    skip_token_warmup,
    responses_mode,
    anthropic_mode,
    token_limit_field,
    retry_429,
    retry_max_wait,
    log_level,
    log_tz,
    access_log,
):
    base = f"{endpoint.rstrip('/')}/{instance.strip('/')}/openai"
    cfg = {
        "endpoint": endpoint,
        "instance": instance,
        "base": base,
        "api_version": api_version,
        "default_dep": deployment,
        "api_key": api_key or None,
        "timeout": timeout,
        "skip_warmup": skip_token_warmup,
        "responses_mode": responses_mode,
        "anthropic_mode": anthropic_mode,
        "token_field": None if token_limit_field == "auto" else token_limit_field,
        "retry_429": retry_429,
        "retry_max_wait": retry_max_wait,
        "access_log": access_log,
        "log_utc_offset": log_tz,
    }
    apis = ["/v1/chat/completions", f"/v1/responses ({responses_mode})"]
    if anthropic_mode == "translate":
        apis.append("/v1/messages")
    click.echo(
        f"TRAPI proxy on http://{host}:{port}\n"
        f"  upstream:    {base}\n"
        f"  api-version: {api_version}\n"
        f"  endpoints:   /health, /status, /v1/*\n"
        f"  apis:        {', '.join(apis)}\n"
        f"  429 retries: {retry_429} (max wait {retry_max_wait:g}s)\n"
        f"  access log:  {'on' if access_log else 'off'} (UTC{log_tz:+g})",
        err=True,
    )
    uvicorn.run(
        build_app(cfg),
        host=host,
        port=port,
        log_level=log_level,
        # Our own line carries the model and the peer port; uvicorn's would
        # only duplicate the rest of it.
        access_log=False,
    )


if __name__ == "__main__":
    cli()
