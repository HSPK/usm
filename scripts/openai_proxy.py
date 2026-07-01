#!/usr/bin/env python3
"""OpenAI-compatible proxy that forwards to Microsoft TRAPI.

Built on Starlette + uvicorn + httpx.AsyncClient so a single process
handles hundreds of concurrent SSE streams in one event loop. Tokens
come from azure.identity.aio (az login / managed identity).

Endpoints
---------
* GET  /health          liveness probe (no auth)
* GET  /status          configured upstream + api-version + api-key state
* *    /v1/<...>        proxied to TRAPI (all OpenAI-compatible paths)
* OPT  /<...>           CORS preflight
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import urllib.parse
from copy import copy
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable

import click
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from uvicorn.logging import AccessFormatter

SCOPE = "api://trapi/.default"
DEFAULT_LOG_FILE = "openai-proxy.log"
DEFAULT_LOG_MAX_MB = 5
DEFAULT_LOG_MAX_BYTES = DEFAULT_LOG_MAX_MB * 1024 * 1024

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
# OpenAI paths that map directly under /openai/... (no deployment segment).
NO_DEPLOY = ("/models", "/files", "/fine_tuning", "/batches", "/threads", "/assistants")

AsyncTokenProvider = Callable[[], Awaitable[str]]


# Logging ------------------------------------------------------------------


class PrettyAccessFormatter(AccessFormatter):
    """Uvicorn access formatter with aligned IP, method, path, and status fields."""

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802
        recordcopy = copy(record)
        client_addr, method, full_path, _http_version, status_code = recordcopy.args
        status = self.get_status_code(int(status_code))
        path = str(full_path)
        if self.use_colors:
            method = click.style(str(method), bold=True)
            path = click.style(path, bold=True)

        recordcopy.__dict__.update(
            {
                "client_ip": _strip_client_port(str(client_addr)),
                "method": method,
                "path": path,
                "status_code": status,
            }
        )
        return logging.Formatter.formatMessage(self, recordcopy)


def _strip_client_port(client_addr: str) -> str:
    if client_addr.count(":") >= 1:
        host, port = client_addr.rsplit(":", 1)
        if port.isdigit():
            return host
    return client_addr or "-"


def build_uvicorn_log_config(
    log_level: str,
    log_file: str | Path | None,
    *,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
) -> dict[str, Any]:
    """Return uvicorn log config with access logs on stderr and a rotating file."""

    level = log_level.upper()
    access_handlers = ["access_console"]
    handlers: dict[str, dict[str, Any]] = {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access_console": {
            "formatter": "access_console",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
    }

    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers["access_file"] = {
            "formatter": "access_file",
            "class": "concurrent_log_handler.ConcurrentTimedRotatingFileHandler",
            "filename": str(log_path),
            "when": "midnight",
            "interval": 1,
            "backupCount": 0,
            "maxBytes": max_bytes,
            "encoding": "utf-8",
        }
        access_handlers.append("access_file")

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(asctime)s | %(levelprefix)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "use_colors": None,
            },
            "access_console": {
                "()": PrettyAccessFormatter,
                "fmt": (
                    "%(asctime)s | %(levelname)-7s | %(client_ip)-39s | "
                    "%(method)-6s | %(path)-72s | %(status_code)s"
                ),
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "use_colors": False,
            },
            "access_file": {
                "()": PrettyAccessFormatter,
                "fmt": (
                    "%(asctime)s | %(levelname)-7s | %(client_ip)-39s | "
                    "%(method)-6s | %(path)-72s | %(status_code)s"
                ),
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "use_colors": False,
            },
        },
        "handlers": handlers,
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": level, "propagate": False},
            "uvicorn.error": {"level": level},
            "uvicorn.access": {
                "handlers": access_handlers,
                "level": level,
                "propagate": False,
            },
        },
    }


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


def check_api_key(headers, expected: str | None) -> bool:
    """Return True iff the optional API-key gate passes (or is disabled)."""
    if not expected:
        return True
    auth = headers.get("Authorization") or ""
    bearer = (
        auth.split(None, 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    )
    api_key = (headers.get("api-key") or headers.get("Api-Key") or "").strip()
    return expected in (bearer, api_key)


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


# Route handlers -----------------------------------------------------------


def _json_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status
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


async def proxy(request: Request) -> Response:
    state = request.app.state
    cfg = state.cfg
    raw = await request.body()

    if not check_api_key(request.headers, cfg["api_key"]):
        return _json_error(401, "invalid_api_key", "Missing or invalid API key.")

    try:
        body_obj = json.loads(raw) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        body_obj = None

    path_qs = request.url.path + (
        ("?" + request.url.query) if request.url.query else ""
    )
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
    except Exception as exc:
        return _json_error(500, "auth_failed", f"Token error: {exc}")

    try:
        upstream_req = state.client.build_request(
            request.method,
            url,
            content=raw or None,
            headers=headers,
        )
        upstream_resp = await state.client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        return _json_error(502, "upstream_unreachable", str(exc))

    response_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_RES
    }
    if "text/event-stream" in (upstream_resp.headers.get("content-type") or "").lower():
        response_headers["Cache-Control"] = "no-cache"
        response_headers["X-Accel-Buffering"] = "no"

    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream_resp.aclose),
    )


# App factory --------------------------------------------------------------


def build_app(
    cfg: dict,
    *,
    token_provider: AsyncTokenProvider | None = None,
    client: httpx.AsyncClient | None = None,
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

        if not cfg.get("skip_warmup"):
            try:
                await app.state.token_provider()
            except Exception as exc:
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

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/status", status_endpoint, methods=["GET"]),
            Route("/{path:path}", options_preflight, methods=["OPTIONS"]),
            Route(
                "/{path:path}",
                proxy,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            ),
        ],
    )


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
    "--log-level",
    default="info",
    envvar="TRAPI_PROXY_LOG_LEVEL",
    type=click.Choice(["debug", "info", "warning", "error"]),
)
@click.option(
    "--log-file",
    default=DEFAULT_LOG_FILE,
    envvar="TRAPI_PROXY_LOG_FILE",
    help="Access log file. Rotates daily and when a file reaches --log-max-mb.",
)
@click.option(
    "--log-max-mb",
    default=DEFAULT_LOG_MAX_MB,
    envvar="TRAPI_PROXY_LOG_MAX_MB",
    type=click.IntRange(min=1),
    help="Maximum size of one access log file before rotation.",
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
    log_level,
    log_file,
    log_max_mb,
):
    base = f"{endpoint.rstrip('/')}/{instance.strip('/')}/openai"
    log_path = Path(log_file).expanduser() if log_file else None
    cfg = {
        "endpoint": endpoint,
        "instance": instance,
        "base": base,
        "api_version": api_version,
        "default_dep": deployment,
        "api_key": api_key or None,
        "timeout": timeout,
        "skip_warmup": skip_token_warmup,
    }
    click.echo(
        f"TRAPI proxy on http://{host}:{port}\n"
        f"  upstream:    {base}\n"
        f"  api-version: {api_version}\n"
        f"  access log:  {log_path or '-'}\n"
        f"  endpoints:   /health, /status, /v1/*",
        err=True,
    )
    uvicorn.run(
        build_app(cfg),
        host=host,
        port=port,
        log_level=log_level,
        log_config=build_uvicorn_log_config(
            log_level,
            log_path,
            max_bytes=log_max_mb * 1024 * 1024,
        ),
        access_log=True,
    )


if __name__ == "__main__":
    cli()
