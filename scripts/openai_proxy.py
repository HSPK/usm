#!/usr/bin/env python3
"""Lightweight OpenAI-compatible proxy that forwards to Microsoft TRAPI.

Translates OpenAI-style HTTP into Azure OpenAI traffic against
``https://trapi.research.microsoft.com/{instance}``. Bearer tokens for the
``api://trapi/.default`` scope are obtained either locally via
``azure-identity`` (default) or remotely by running ``az`` over a persistent
SSH ControlMaster connection (``--ssh-host``). Bodies are streamed through
untouched. Depends only on stdlib + ``azure-identity`` (and only in local
mode).
"""

from __future__ import annotations

import argparse
import atexit
import http.server
import json
import os
import shlex
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_INSTANCE = "gcr/shared"
DEFAULT_API_VERSION = "2024-10-21"
DEFAULT_ENDPOINT = "https://trapi.research.microsoft.com"
DEFAULT_TIMEOUT = 600.0
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
    print(*args, file=sys.stderr, **kwargs)


# Token providers -----------------------------------------------------------

class RemoteAzTokenProvider:
    """Fetch Azure AD bearer tokens by running ``az`` on a remote SSH host.

    Reuses an OpenSSH ControlMaster socket so refreshes stay in the ms range.
    The master is opened once at startup and torn down via atexit.
    """

    def __init__(
        self,
        host: str,
        ssh_extra_args: list[str],
        az_cmd: str,
        scope: str,
        refresh_margin: float = 120.0,
        ssh_timeout: float = 60.0,
    ) -> None:
        self.host = host
        self.ssh_extra_args = list(ssh_extra_args)
        self.az_cmd = az_cmd
        self.scope = scope
        self.refresh_margin = refresh_margin
        self.ssh_timeout = ssh_timeout
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()
        self._control_path = str(
            Path(tempfile.gettempdir()) / f"usm-trapi-proxy-{os.getpid()}.sock"
        )
        self._master_started = False

    def start_control_master(self) -> None:
        if self._master_started:
            return
        try:
            os.remove(self._control_path)
        except OSError:
            pass

        cmd = [
            "ssh", "-fN",
            "-o", "ControlMaster=yes",
            "-o", f"ControlPath={self._control_path}",
            "-o", "ControlPersist=600",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            *self.ssh_extra_args,
            self.host,
        ]
        _eprint(f"Opening SSH control master to {self.host} ...")
        # Inherit stdio so interactive prompts (passphrase, host key) work.
        try:
            result = subprocess.run(cmd, timeout=120)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "'ssh' executable not found on PATH; needed for --ssh-host mode."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Timed out establishing SSH control master to {self.host!r}."
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to open SSH control master to {self.host!r} "
                f"(ssh exit {result.returncode}). Check connectivity, auth, "
                "and that the remote sshd allows ControlMaster sessions."
            )
        self._master_started = True
        atexit.register(self.stop_control_master)

    def stop_control_master(self) -> None:
        if not self._master_started:
            return
        try:
            subprocess.run(
                ["ssh", "-o", f"ControlPath={self._control_path}", "-O", "exit", self.host],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass
        try:
            os.remove(self._control_path)
        except OSError:
            pass
        self._master_started = False

    def _refresh(self) -> None:
        remote_cmd = (
            f"{self.az_cmd} account get-access-token "
            f"--scope {shlex.quote(self.scope)} --output json"
        )
        ssh_cmd = [
            "ssh", "-o", f"ControlPath={self._control_path}",
            *self.ssh_extra_args, self.host, remote_cmd,
        ]
        try:
            result = subprocess.run(
                ssh_cmd, capture_output=True, text=True, timeout=self.ssh_timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Timed out fetching token from {self.host!r} via SSH."
            ) from exc

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"Remote 'az account get-access-token' failed "
                f"(exit {result.returncode}): {err}"
            )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Failed to parse token response from remote 'az' ({exc}). "
                f"First bytes: {result.stdout[:200]!r}"
            ) from exc

        token = data.get("accessToken")
        if not token:
            raise RuntimeError(f"Remote 'az' response missing accessToken: {data!r}")
        self._token = token

        # Prefer unix-seconds 'expires_on' (Azure CLI 2.53+); fall back to ~50 min
        # since legacy 'expiresOn' is local-time and the remote TZ may differ.
        expires_on = data.get("expires_on")
        if isinstance(expires_on, (int, float)) or (
            isinstance(expires_on, str) and expires_on.isdigit()
        ):
            self._expires_at = float(expires_on)
        else:
            self._expires_at = time.time() + 50 * 60

    def __call__(self) -> str:
        with self._lock:
            if (
                not self._token
                or time.time() >= self._expires_at - self.refresh_margin
            ):
                self._refresh()
            assert self._token is not None
            return self._token


def make_token_provider(args: argparse.Namespace) -> TokenProvider:
    """Return a callable producing a bearer token for ``SCOPE``."""
    if args.ssh_host:
        ssh_args = [
            os.path.expanduser(tok)
            for raw in (args.ssh_option or [])
            for tok in shlex.split(raw)
        ]
        provider = RemoteAzTokenProvider(
            host=args.ssh_host,
            ssh_extra_args=ssh_args,
            az_cmd=args.remote_az_cmd,
            scope=SCOPE,
        )
        provider.start_control_master()
        return provider

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
            "Install it with:  pip install azure-identity\n"
            "Or use --ssh-host to obtain tokens from a remote host instead."
        )
        sys.exit(2)

    credential = ChainedTokenCredential(AzureCliCredential(), ManagedIdentityCredential())
    return get_bearer_token_provider(credential, SCOPE)


# Server state --------------------------------------------------------------

@dataclass
class ProxyConfig:
    endpoint: str
    instance: str
    api_version: str
    default_deployment: str | None
    timeout: float
    api_key: str | None
    token_provider: TokenProvider


class ProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: ProxyConfig) -> None:
        super().__init__(address, ProxyHandler)
        self.config = config


# Bad-request signal that aborts the proxy flow with a 400.
class _BadRequest(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# Request handler -----------------------------------------------------------

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

        req = urllib.request.Request(
            target, data=raw or None, method=self.command, headers=headers
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self.cfg.timeout)
        except urllib.error.HTTPError as exc:
            self._forward_http_error(exc)
            return
        except urllib.error.URLError as exc:
            self._send_json_error(502, "upstream_unreachable", str(exc.reason))
            return
        except Exception as exc:
            self._send_json_error(502, "bad_gateway", str(exc))
            return

        self._forward_response(resp)

    def _write_headers(self, status: int, headers: Any) -> None:
        self.send_response(status)
        if headers is not None:
            for key, value in headers.items():
                if key.lower() not in HOP_BY_HOP_RESPONSE:
                    self.send_header(key, value)

    def _forward_http_error(self, exc: urllib.error.HTTPError) -> None:
        try:
            body = exc.read() or b""
        except Exception:
            body = b""
        try:
            self._write_headers(exc.code or 502, exc.headers)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _forward_response(self, resp: Any) -> None:
        is_stream = "text/event-stream" in (resp.headers.get("Content-Type") or "").lower()
        try:
            self._write_headers(resp.status, resp.headers)
            if is_stream:
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                read = getattr(resp, "read1", resp.read)
                try:
                    while chunk := read(8192):
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
        finally:
            try:
                resp.close()
            except Exception:
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


# CLI -----------------------------------------------------------------------

def _env(name: str, fallback: Any = None) -> Any:
    value = os.environ.get(name)
    return value if value not in (None, "") else fallback


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="usm openai-proxy",
        description=(
            "Lightweight OpenAI-compatible proxy that forwards traffic to "
            "Microsoft TRAPI using Azure AD bearer tokens."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default=_env("TRAPI_PROXY_HOST", DEFAULT_HOST),
                   help="Bind address. Use 0.0.0.0 to expose on all interfaces.")
    p.add_argument("--port", type=int, default=int(_env("TRAPI_PROXY_PORT", DEFAULT_PORT)),
                   help="Listen port.")
    p.add_argument("--instance", default=_env("TRAPI_INSTANCE", DEFAULT_INSTANCE),
                   help="TRAPI instance, e.g. 'gcr/shared'. See https://aka.ms/trapi/models.")
    p.add_argument("--api-version", default=_env("TRAPI_API_VERSION", DEFAULT_API_VERSION),
                   help="Default Azure OpenAI api-version when the client omits it.")
    p.add_argument("--endpoint", default=_env("TRAPI_ENDPOINT", DEFAULT_ENDPOINT),
                   help="TRAPI base endpoint URL.")
    p.add_argument("--deployment", default=_env("TRAPI_DEFAULT_DEPLOYMENT"),
                   help="Default deployment when the request body omits 'model'. "
                        "Without it, clients must always send the deployment in 'model'.")
    p.add_argument("--timeout", type=float,
                   default=float(_env("TRAPI_PROXY_TIMEOUT", DEFAULT_TIMEOUT)),
                   help="Upstream socket timeout in seconds.")
    p.add_argument("--ssh-host", default=_env("TRAPI_PROXY_SSH_HOST"),
                   help="Optional SSH target (e.g. user@devbox). When set, tokens are "
                        "obtained by running 'az account get-access-token' on that host "
                        "over a persistent ControlMaster SSH connection.")
    p.add_argument("--ssh-option", action="append", default=[], metavar="OPT",
                   help="Extra args forwarded to ssh (repeatable, shell-tokenized). "
                        "Example: --ssh-option='-i ~/.ssh/id_ed25519' --ssh-option='-p 2222'.")
    p.add_argument("--remote-az-cmd", default=_env("TRAPI_PROXY_REMOTE_AZ_CMD", "az"),
                   help="Path to the 'az' binary on the SSH remote host.")
    p.add_argument("--api-key", default=_env("TRAPI_PROXY_API_KEY"),
                   help="Optional API key clients must present as 'Authorization: Bearer "
                        "<key>' or the 'api-key' header. Default: no auth.")
    p.add_argument("--skip-token-warmup", action="store_true",
                   help="Do not pre-fetch an Azure AD token before binding the server.")
    return p.parse_args(argv)


def _warmup(token_provider: TokenProvider, args: argparse.Namespace) -> None:
    try:
        token_provider()
    except Exception as exc:
        _eprint(f"Failed to acquire Azure AD token for {SCOPE}: {exc}")
        host_hint = f" on the remote host {args.ssh_host}" if args.ssh_host else ""
        _eprint(f"Try{host_hint}:  az login --scope api://trapi/.default")
        sys.exit(2)


def _print_banner(args: argparse.Namespace) -> None:
    upstream = f"{args.endpoint.rstrip('/')}/{args.instance.strip('/')}/openai"
    identity = (
        f"remote (ssh {args.ssh_host}, persistent ControlMaster)"
        if args.ssh_host
        else "local azure-identity (az CLI / managed identity)"
    )
    lines = [
        f"TRAPI proxy listening on http://{args.host}:{args.port}",
        f"  upstream:    {upstream}",
        f"  api-version: {args.api_version} (default; clients can override)",
    ]
    if args.deployment:
        lines.append(f"  default deployment: {args.deployment}")
    lines.append(f"  identity:    {identity}")
    lines.append(f"Point any OpenAI client at base_url=http://{args.host}:{args.port}/v1")
    lines.append(
        "  api_key required (set via --api-key / TRAPI_PROXY_API_KEY)."
        if args.api_key
        else "Use any non-empty api_key; the proxy injects the real bearer token."
    )
    _eprint("\n".join(lines))


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    token_provider = make_token_provider(args)
    if not args.skip_token_warmup:
        _warmup(token_provider, args)

    config = ProxyConfig(
        endpoint=args.endpoint,
        instance=args.instance,
        api_version=args.api_version,
        default_deployment=args.deployment,
        timeout=args.timeout,
        api_key=args.api_key or None,
        token_provider=token_provider,
    )
    try:
        server = ProxyServer((args.host, args.port), config)
    except OSError as exc:
        _eprint(f"Failed to bind {args.host}:{args.port}: {exc}")
        sys.exit(2)

    _print_banner(args)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _eprint("\nShutting down...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
