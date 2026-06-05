#!/usr/bin/env python3
"""Lightweight OpenAI-compatible proxy that forwards to Microsoft TRAPI.

Translates OpenAI-style HTTP into Azure OpenAI traffic against
``https://trapi.research.microsoft.com/{instance}``. Bearer tokens for the
``api://trapi/.default`` scope are obtained via ``azure-identity`` (az login
first, then a managed identity if available) and injected into every
upstream request. Bodies stream through untouched.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

import click
import httpx

DEFAULT_INSTANCE = "gcr/shared"
DEFAULT_API_VERSION = "2024-10-21"
DEFAULT_ENDPOINT = "https://trapi.research.microsoft.com"
SCOPE = "api://trapi/.default"

# Stripped from every outbound request (transport-level or replaced by us).
HOP_BY_HOP_REQUEST = {
    "host", "content-length", "connection", "transfer-encoding", "te",
    "keep-alive", "upgrade", "proxy-connection", "trailer", "authorization",
    "api-key", "expect",
}

# Stripped from every relayed response; we rewrite Content-Length ourselves.
HOP_BY_HOP_RESPONSE = {
    "transfer-encoding", "content-encoding", "content-length", "connection",
    "keep-alive", "trailer", "upgrade",
}

# OpenAI paths that map directly under ``/openai/...`` (no deployment segment).
NO_DEPLOYMENT_PATH_PREFIXES = (
    "/models", "/files", "/fine_tuning", "/batches", "/threads", "/assistants",
)

TokenProvider = Callable[[], str]


def _eprint(*args: Any, **kwargs: Any) -> None:
    click.echo(*args, err=True, **kwargs)


def make_token_provider() -> TokenProvider:
    """Return a callable that yields a fresh Azure AD bearer token."""
    try:
        from azure.identity import (
            AzureCliCredential,
            ChainedTokenCredential,
            ManagedIdentityCredential,
            get_bearer_token_provider,
        )
    except ImportError:
        _eprint(
            "Missing dependency 'azure-identity'.\n"
            "Install it with:  pip install azure-identity"
        )
        sys.exit(2)
    credential = ChainedTokenCredential(
        AzureCliCredential(), ManagedIdentityCredential()
    )
    return get_bearer_token_provider(credential, SCOPE)


@dataclass(frozen=True)
class ProxyConfig:
    endpoint: str
    instance: str
    api_version: str
    default_deployment: str | None
    api_key: str | None
    token_provider: TokenProvider
    client: httpx.Client


class ProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: ProxyConfig) -> None:
        super().__init__(address, ProxyHandler)
        self.config = config


class _BadRequest(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    server_version = "TrapiProxy/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def cfg(self) -> ProxyConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), fmt % args)
        )

    def _send_json_error(self, status: int, code: str, message: str) -> None:
        payload = json.dumps({"error": {"code": code, "message": message}}).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _check_api_key(self) -> bool:
        """Return True iff the optional API-key gate passes (or is disabled)."""
        expected = self.cfg.api_key
        if not expected:
            return True
        candidates: list[str] = []
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            candidates.append(auth.split(None, 1)[1].strip())
        api_key_header = self.headers.get("api-key") or self.headers.get("Api-Key")
        if api_key_header:
            candidates.append(api_key_header.strip())
        if expected in candidates:
            return True
        self._send_json_error(
            401, "invalid_api_key",
            "Missing or invalid API key. Send 'Authorization: Bearer <key>'.",
        )
        return False

    def _resolve_target(self, body_obj: Any) -> str:
        """Translate an inbound path/body into the upstream TRAPI URL."""
        # Strip the optional "/v1" prefix used by OpenAI clients.
        path_qs = self.path or "/"
        if path_qs.startswith("/v1/"):
            path_qs = path_qs[3:]
        elif path_qs == "/v1":
            path_qs = "/"

        parts = urllib.parse.urlsplit(path_qs)
        path = parts.path or "/"
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))

        deployment = None
        if isinstance(body_obj, dict):
            value = body_obj.get("model") or body_obj.get("deployment")
            if isinstance(value, str):
                deployment = value
        deployment = deployment or self.cfg.default_deployment

        base = f"{self.cfg.endpoint.rstrip('/')}/{self.cfg.instance.strip('/')}/openai"
        no_deployment = any(
            path == p or path.startswith(p + "/") for p in NO_DEPLOYMENT_PATH_PREFIXES
        )
        if no_deployment:
            target_path = f"{base}{path}"
        elif deployment:
            target_path = (
                f"{base}/deployments/{urllib.parse.quote(deployment, safe='')}{path}"
            )
        else:
            raise _BadRequest(
                "missing_model",
                "Request body must include 'model' (the Azure deployment name) "
                "or start the proxy with --deployment to set a default.",
            )

        query.setdefault("api-version", self.cfg.api_version)
        return target_path + "?" + urllib.parse.urlencode(query)

    def _proxy(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            raw = self.rfile.read(length) if length > 0 else b""
        except Exception as exc:
            self._send_json_error(400, "bad_request", f"Failed to read body: {exc}")
            return

        if not self._check_api_key():
            return

        body_obj: Any = None
        if raw:
            try:
                body_obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass

        try:
            target = self._resolve_target(body_obj)
        except _BadRequest as exc:
            self._send_json_error(400, exc.code, exc.message)
            return

        headers = {
            k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP_REQUEST
        }
        try:
            headers["Authorization"] = "Bearer " + self.cfg.token_provider()
        except Exception as exc:
            self._send_json_error(
                500, "auth_failed",
                f"Failed to acquire Azure AD token for {SCOPE}: {exc}",
            )
            return

        try:
            with self.cfg.client.stream(
                self.command, target, content=raw or None, headers=headers
            ) as resp:
                self._forward(resp)
        except httpx.HTTPError as exc:
            self._send_json_error(502, "upstream_unreachable", str(exc))

    def _forward(self, resp: httpx.Response) -> None:
        is_stream = "text/event-stream" in (
            resp.headers.get("content-type") or ""
        ).lower()
        try:
            self.send_response(resp.status_code)
            for key, value in resp.headers.items():
                if key.lower() not in HOP_BY_HOP_RESPONSE:
                    self.send_header(key, value)

            if is_stream:
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for chunk in resp.iter_raw():
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # http.server dispatches via getattr(self, 'do_<VERB>'); alias the
    # verbs we proxy onto a single handler.
    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _proxy  # noqa: N815

    def do_OPTIONS(self) -> None:  # noqa: N802
        # CORS preflight passthrough for browser clients.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            self.headers.get("Access-Control-Request-Headers", "*"),
        )
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()


@click.command(
    context_settings={"show_default": True, "help_option_names": ["-h", "--help"]},
    help="Lightweight OpenAI-compatible proxy that forwards traffic to "
         "Microsoft TRAPI using Azure AD bearer tokens.",
)
@click.option("--host", default="127.0.0.1", envvar="TRAPI_PROXY_HOST",
              help="Bind address. Use 0.0.0.0 to expose on all interfaces.")
@click.option("--port", type=int, default=8080, envvar="TRAPI_PROXY_PORT",
              help="Listen port.")
@click.option("--instance", default=DEFAULT_INSTANCE, envvar="TRAPI_INSTANCE",
              help="TRAPI instance, e.g. 'gcr/shared'. See https://aka.ms/trapi/models.")
@click.option("--api-version", default=DEFAULT_API_VERSION, envvar="TRAPI_API_VERSION",
              help="Default Azure OpenAI api-version when the client omits it.")
@click.option("--endpoint", default=DEFAULT_ENDPOINT, envvar="TRAPI_ENDPOINT",
              help="TRAPI base endpoint URL.")
@click.option("--deployment", default=None, envvar="TRAPI_DEFAULT_DEPLOYMENT",
              help="Default deployment when the request body omits 'model'.")
@click.option("--timeout", type=float, default=600.0, envvar="TRAPI_PROXY_TIMEOUT",
              help="Upstream socket timeout in seconds.")
@click.option("--api-key", default=None, envvar="TRAPI_PROXY_API_KEY",
              help="Optional API key clients must present as 'Authorization: "
                   "Bearer <key>' or the 'api-key' header. Default: no auth.")
@click.option("--skip-token-warmup", is_flag=True,
              help="Do not pre-fetch an Azure AD token before binding.")
def cli(
    host: str, port: int, instance: str, api_version: str, endpoint: str,
    deployment: str | None, timeout: float, api_key: str | None,
    skip_token_warmup: bool,
) -> None:
    token_provider = make_token_provider()
    if not skip_token_warmup:
        try:
            token_provider()
        except Exception as exc:
            _eprint(f"Failed to acquire Azure AD token for {SCOPE}: {exc}")
            _eprint("Try:  az login --scope api://trapi/.default")
            sys.exit(2)

    config = ProxyConfig(
        endpoint=endpoint, instance=instance, api_version=api_version,
        default_deployment=deployment, api_key=api_key or None,
        token_provider=token_provider,
        client=httpx.Client(timeout=timeout, follow_redirects=True),
    )
    try:
        server = ProxyServer((host, port), config)
    except OSError as exc:
        _eprint(f"Failed to bind {host}:{port}: {exc}")
        sys.exit(2)

    upstream = f"{endpoint.rstrip('/')}/{instance.strip('/')}/openai"
    banner = [
        f"TRAPI proxy listening on http://{host}:{port}",
        f"  upstream:    {upstream}",
        f"  api-version: {api_version} (default; clients can override)",
    ]
    if deployment:
        banner.append(f"  default deployment: {deployment}")
    banner.append(f"Point any OpenAI client at base_url=http://{host}:{port}/v1")
    banner.append(
        "  api_key required (set via --api-key / TRAPI_PROXY_API_KEY)."
        if api_key
        else "Use any non-empty api_key; the proxy injects the real bearer token."
    )
    _eprint("\n".join(banner))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _eprint("\nShutting down...")
    finally:
        server.server_close()
        config.client.close()


if __name__ == "__main__":
    cli()
