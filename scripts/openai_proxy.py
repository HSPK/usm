#!/usr/bin/env python3
"""Lightweight OpenAI-compatible proxy that forwards to Microsoft TRAPI.

Tokens come from azure-identity (az login / managed identity); paths and
bodies stream through untouched. Stdlib http.server + httpx, no framework.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import sys
import urllib.parse
from typing import Any

import click
import httpx

SCOPE = "api://trapi/.default"
# Headers we never relay: transport-level, or replaced by the proxy itself.
HOP_REQ = {
    "host", "content-length", "connection", "transfer-encoding", "te",
    "keep-alive", "upgrade", "proxy-connection", "trailer", "authorization",
    "api-key", "expect",
}
HOP_RES = {
    "transfer-encoding", "content-encoding", "content-length", "connection",
    "keep-alive", "trailer", "upgrade",
}
# OpenAI paths that map directly under /openai/... (no deployment segment).
NO_DEPLOY = ("/models", "/files", "/fine_tuning", "/batches", "/threads", "/assistants")


def make_token_provider():
    """Return a callable that yields a fresh Azure AD bearer token."""
    try:
        from azure.identity import (
            AzureCliCredential, ChainedTokenCredential,
            ManagedIdentityCredential, get_bearer_token_provider,
        )
    except ImportError:
        click.echo("Missing 'azure-identity'. Install with: pip install azure-identity", err=True)
        sys.exit(2)
    return get_bearer_token_provider(
        ChainedTokenCredential(AzureCliCredential(), ManagedIdentityCredential()),
        SCOPE,
    )


def resolve_url(
    path_qs: str, body_obj: Any, base: str, api_version: str, default_dep: str | None
) -> str | None:
    """Translate inbound path+body → upstream URL, or None if 'model' missing."""
    if path_qs.startswith("/v1/"):
        path_qs = path_qs[3:]
    elif path_qs == "/v1":
        path_qs = "/"
    parts = urllib.parse.urlsplit(path_qs)
    path = parts.path or "/"
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
    bearer = auth.split(None, 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    api_key = (headers.get("api-key") or headers.get("Api-Key") or "").strip()
    return expected in (bearer, api_key)


def make_handler(cfg: dict):
    """Build a BaseHTTPRequestHandler closed over the proxy *cfg*."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "TrapiProxy/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(
                f"{self.address_string()} - [{self.log_date_time_string()}] {fmt % args}\n"
            )

        def _err(self, status: int, code: str, message: str) -> None:
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

        def _proxy(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""

            if not check_api_key(self.headers, cfg["api_key"]):
                return self._err(401, "invalid_api_key", "Missing or invalid API key.")

            try:
                body_obj = json.loads(raw) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                body_obj = None

            url = resolve_url(
                self.path or "/", body_obj,
                cfg["base"], cfg["api_version"], cfg["default_dep"],
            )
            if url is None:
                return self._err(
                    400, "missing_model",
                    "Request must include 'model' or start the proxy with --deployment.",
                )

            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_REQ}
            try:
                headers["Authorization"] = "Bearer " + cfg["token_provider"]()
            except Exception as exc:
                return self._err(500, "auth_failed", f"Token error: {exc}")

            try:
                with cfg["client"].stream(
                    self.command, url, content=raw or None, headers=headers
                ) as r:
                    self._relay(r)
            except httpx.HTTPError as exc:
                self._err(502, "upstream_unreachable", str(exc))

        def _relay(self, r: httpx.Response) -> None:
            is_sse = "text/event-stream" in (r.headers.get("content-type") or "").lower()
            try:
                self.send_response(r.status_code)
                for k, v in r.headers.items():
                    if k.lower() not in HOP_RES:
                        self.send_header(k, v)
                if is_sse:
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("X-Accel-Buffering", "no")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        for chunk in r.iter_raw():
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
                    data = r.read()
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # http.server dispatches via getattr(self, 'do_<VERB>').
        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _proxy  # noqa: N815

        def do_OPTIONS(self) -> None:  # noqa: N802 - CORS preflight
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

    return Handler


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@click.command(
    context_settings={"show_default": True, "help_option_names": ["-h", "--help"]},
    help="Lightweight OpenAI-compatible proxy forwarding to Microsoft TRAPI.",
)
@click.option("--host", default="127.0.0.1", envvar="TRAPI_PROXY_HOST",
              help="Bind address. 0.0.0.0 exposes on all interfaces.")
@click.option("--port", type=int, default=8080, envvar="TRAPI_PROXY_PORT")
@click.option("--instance", default="gcr/shared", envvar="TRAPI_INSTANCE",
              help="TRAPI instance. See https://aka.ms/trapi/models.")
@click.option("--api-version", default="2024-10-21", envvar="TRAPI_API_VERSION")
@click.option("--endpoint", default="https://trapi.research.microsoft.com",
              envvar="TRAPI_ENDPOINT")
@click.option("--deployment", default=None, envvar="TRAPI_DEFAULT_DEPLOYMENT",
              help="Default deployment when the request omits 'model'.")
@click.option("--timeout", type=float, default=600.0, envvar="TRAPI_PROXY_TIMEOUT")
@click.option("--api-key", default=None, envvar="TRAPI_PROXY_API_KEY",
              help="If set, clients must present this via 'Authorization: Bearer <key>' "
                   "or 'api-key' header.")
@click.option("--skip-token-warmup", is_flag=True,
              help="Skip the upfront token fetch.")
def cli(host, port, instance, api_version, endpoint, deployment, timeout, api_key,
        skip_token_warmup):
    tp = make_token_provider()
    if not skip_token_warmup:
        try:
            tp()
        except Exception as exc:
            click.echo(f"Failed to acquire token: {exc}\n"
                       f"Try: az login --scope {SCOPE}", err=True)
            sys.exit(2)

    base = f"{endpoint.rstrip('/')}/{instance.strip('/')}/openai"
    cfg = {
        "token_provider": tp,
        "api_key": api_key or None,
        "client": httpx.Client(timeout=timeout, follow_redirects=True),
        "base": base,
        "api_version": api_version,
        "default_dep": deployment,
    }
    server = _Server((host, port), make_handler(cfg))
    click.echo(
        f"TRAPI proxy on http://{host}:{port}\n  upstream:    {base}\n"
        f"  api-version: {api_version}",
        err=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nShutting down...", err=True)
    finally:
        server.server_close()
        cfg["client"].close()


if __name__ == "__main__":
    cli()
