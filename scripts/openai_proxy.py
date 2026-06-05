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
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_INSTANCE = "gcr/shared"
DEFAULT_API_VERSION = "2024-10-21"
DEFAULT_ENDPOINT = "https://trapi.research.microsoft.com"
DEFAULT_TIMEOUT = 600.0
SCOPE = "api://trapi/.default"

# Stripped from every outbound request (transport-level or replaced by us).
HOP_BY_HOP_REQUEST = {
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

# Stripped from every relayed response; we rewrite Content-Length ourselves.
HOP_BY_HOP_RESPONSE = {
    "transfer-encoding",
    "content-encoding",
    "content-length",
    "connection",
    "keep-alive",
    "trailer",
    "upgrade",
}

# OpenAI paths that map directly under ``/openai/...`` (no deployment segment).
NO_DEPLOYMENT_PATH_PREFIXES = (
    "/models",
    "/files",
    "/fine_tuning",
    "/batches",
    "/threads",
    "/assistants",
)


def _eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def _default_token_provider() -> str:
    return ""


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
        self._control_path: str | None = None
        self._master_started = False

    def _default_control_path(self) -> str:
        # Per-pid socket so multiple proxies don't collide.
        sock = Path(tempfile.gettempdir()) / f"usm-trapi-proxy-{os.getpid()}.sock"
        return str(sock)

    def start_control_master(self) -> None:
        if self._master_started:
            return
        self._control_path = self._default_control_path()
        try:
            if os.path.exists(self._control_path):
                os.remove(self._control_path)
        except OSError:
            pass

        cmd = [
            "ssh",
            "-fN",
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
                f"(ssh exit {result.returncode}). Check connectivity, auth, and "
                "that the remote sshd allows ControlMaster sessions."
            )
        self._master_started = True
        atexit.register(self.stop_control_master)

    def stop_control_master(self) -> None:
        if not self._master_started or not self._control_path:
            return
        try:
            subprocess.run(
                [
                    "ssh",
                    "-o", f"ControlPath={self._control_path}",
                    "-O", "exit",
                    self.host,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            pass
        try:
            if os.path.exists(self._control_path):
                os.remove(self._control_path)
        except OSError:
            pass
        self._master_started = False

    def _ssh_cmd(self, remote_cmd: str) -> list[str]:
        args: list[str] = ["ssh"]
        if self._control_path:
            args += ["-o", f"ControlPath={self._control_path}"]
        args += list(self.ssh_extra_args)
        args += [self.host, remote_cmd]
        return args

    def _refresh(self) -> None:
        remote_cmd = (
            f"{self.az_cmd} account get-access-token "
            f"--scope {shlex.quote(self.scope)} --output json"
        )
        try:
            result = subprocess.run(
                self._ssh_cmd(remote_cmd),
                capture_output=True,
                text=True,
                timeout=self.ssh_timeout,
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
                "Failed to parse token response from remote 'az' "
                f"({exc}). First bytes: {result.stdout[:200]!r}"
            ) from exc

        token = data.get("accessToken")
        if not token:
            raise RuntimeError(f"Remote 'az' response missing accessToken: {data!r}")
        self._token = token

        # Prefer the unix-seconds field (Azure CLI 2.53+); fall back to ~50 min
        # since the legacy "expiresOn" is local-time and the remote TZ may differ.
        expires_on = data.get("expires_on")
        if isinstance(expires_on, (int, float)):
            self._expires_at = float(expires_on)
        elif isinstance(expires_on, str) and expires_on.isdigit():
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


def make_local_token_provider():
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

    credential = ChainedTokenCredential(
        AzureCliCredential(),
        ManagedIdentityCredential(),
    )
    return get_bearer_token_provider(credential, SCOPE)


def make_token_provider(args: argparse.Namespace):
    """Return a callable producing a bearer token for ``SCOPE``."""
    if getattr(args, "ssh_host", None):
        ssh_args: list[str] = []
        for raw in args.ssh_option or []:
            for tok in shlex.split(raw):
                ssh_args.append(os.path.expanduser(tok))
        provider = RemoteAzTokenProvider(
            host=args.ssh_host,
            ssh_extra_args=ssh_args,
            az_cmd=args.remote_az_cmd,
            scope=SCOPE,
        )
        provider.start_control_master()
        return provider
    return make_local_token_provider()


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    config: dict[str, Any] = {}
    token_provider = staticmethod(_default_token_provider)

    server_version = "TrapiProxy/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), fmt % args)
        )

    def _read_request_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_json_error(self, status: int, code: str, message: str) -> None:
        payload = json.dumps({"error": {"code": code, "message": message}}).encode(
            "utf-8"
        )
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
        expected = self.config.get("api_key")
        if not expected:
            return True
        candidates: list[str] = []
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            candidates.append(auth.split(None, 1)[1].strip())
        api_key_header = self.headers.get("api-key") or self.headers.get("Api-Key")
        if api_key_header:
            candidates.append(api_key_header.strip())
        if any(c == expected for c in candidates):
            return True
        self._send_json_error(
            401,
            "invalid_api_key",
            "Missing or invalid API key. Send 'Authorization: Bearer <key>'.",
        )
        return False

    def _resolve_target(
        self, body_obj: Any
    ) -> tuple[str | None, str | None, str | None]:
        path_qs = self.path or "/"
        # Strip the optional "/v1" prefix used by OpenAI clients.
        if path_qs.startswith("/v1/"):
            path_qs = path_qs[3:]
        elif path_qs == "/v1":
            path_qs = "/"

        url_parts = urllib.parse.urlsplit(path_qs)
        path = url_parts.path or "/"
        query_pairs = urllib.parse.parse_qsl(url_parts.query, keep_blank_values=True)
        query = {k: v for k, v in query_pairs}

        deployment = None
        if isinstance(body_obj, dict):
            value = body_obj.get("model") or body_obj.get("deployment")
            if isinstance(value, str):
                deployment = value
        if not deployment:
            deployment = self.config.get("default_deployment")

        endpoint = self.config["endpoint"].rstrip("/")
        instance = self.config["instance"].strip("/")
        base = f"{endpoint}/{instance}/openai"

        no_deployment = any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in NO_DEPLOYMENT_PATH_PREFIXES
        )

        if no_deployment:
            target_path = f"{base}{path}"
        elif deployment:
            target_path = (
                f"{base}/deployments/"
                f"{urllib.parse.quote(deployment, safe='')}{path}"
            )
        else:
            return (
                None,
                "missing_model",
                "Request body must include 'model' (the Azure deployment name) "
                "or start the proxy with --deployment to set a default.",
            )

        query.setdefault("api-version", self.config["api_version"])
        target = target_path + "?" + urllib.parse.urlencode(query)
        return target, None, None

    def _proxy(self) -> None:
        try:
            raw = self._read_request_body()
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
                body_obj = None

        target, err_code, err_msg = self._resolve_target(body_obj)
        if target is None:
            self._send_json_error(400, err_code or "bad_request", err_msg or "")
            return

        out_headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP_REQUEST:
                continue
            out_headers[key] = value

        try:
            token = self.token_provider()
        except Exception as exc:
            self._send_json_error(
                500,
                "auth_failed",
                f"Failed to acquire Azure AD token for {SCOPE}: {exc}",
            )
            return
        out_headers["Authorization"] = "Bearer " + token

        req = urllib.request.Request(
            target,
            data=raw if raw else None,
            method=self.command,
            headers=out_headers,
        )

        try:
            resp = urllib.request.urlopen(req, timeout=self.config["timeout"])
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

    def _forward_http_error(self, exc: urllib.error.HTTPError) -> None:
        try:
            body = exc.read() or b""
        except Exception:
            body = b""
        status = exc.code or 502
        try:
            self.send_response(status)
            if exc.headers is not None:
                for key, value in exc.headers.items():
                    if key.lower() in HOP_BY_HOP_RESPONSE:
                        continue
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _forward_response(self, resp: Any) -> None:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        is_stream = "text/event-stream" in content_type
        try:
            self.send_response(resp.status)
            for key, value in resp.headers.items():
                if key.lower() in HOP_BY_HOP_RESPONSE:
                    continue
                self.send_header(key, value)

            if is_stream:
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    while True:
                        chunk = (
                            resp.read1(8192)
                            if hasattr(resp, "read1")
                            else resp.read(8192)
                        )
                        if not chunk:
                            break
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

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

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


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _env_default(name: str, fallback: Any) -> Any:
    value = os.environ.get(name)
    return value if value not in (None, "") else fallback


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="usm openai-proxy",
        description=(
            "Lightweight OpenAI-compatible proxy that forwards traffic to "
            "Microsoft TRAPI using Azure AD bearer tokens."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=_env_default("TRAPI_PROXY_HOST", DEFAULT_HOST),
        help="Bind address. Use 0.0.0.0 to expose on all interfaces.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(_env_default("TRAPI_PROXY_PORT", DEFAULT_PORT)),
        help="Listen port.",
    )
    parser.add_argument(
        "--instance",
        default=_env_default("TRAPI_INSTANCE", DEFAULT_INSTANCE),
        help="TRAPI instance, e.g. 'gcr/shared'. See https://aka.ms/trapi/models.",
    )
    parser.add_argument(
        "--api-version",
        default=_env_default("TRAPI_API_VERSION", DEFAULT_API_VERSION),
        help="Default Azure OpenAI api-version when the client omits it.",
    )
    parser.add_argument(
        "--endpoint",
        default=_env_default("TRAPI_ENDPOINT", DEFAULT_ENDPOINT),
        help="TRAPI base endpoint URL.",
    )
    parser.add_argument(
        "--deployment",
        default=_env_default("TRAPI_DEFAULT_DEPLOYMENT", None),
        help=(
            "Optional default deployment to use when the request body omits "
            "'model'. Without it, clients must always send the deployment "
            "name in the 'model' field."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(_env_default("TRAPI_PROXY_TIMEOUT", DEFAULT_TIMEOUT)),
        help="Upstream socket timeout in seconds.",
    )
    parser.add_argument(
        "--ssh-host",
        default=_env_default("TRAPI_PROXY_SSH_HOST", None),
        help=(
            "Optional SSH target (e.g. user@devbox). When set, tokens are "
            "obtained by running 'az account get-access-token' on that host "
            "over a persistent ControlMaster SSH connection instead of using "
            "local azure-identity credentials."
        ),
    )
    parser.add_argument(
        "--ssh-option",
        action="append",
        default=[],
        metavar="OPT",
        help=(
            "Extra arguments forwarded to ssh (repeatable, shell-tokenized). "
            "Example: --ssh-option='-i ~/.ssh/id_ed25519' "
            "--ssh-option='-p 2222' --ssh-option='-o ProxyJump=jump'."
        ),
    )
    parser.add_argument(
        "--remote-az-cmd",
        default=_env_default("TRAPI_PROXY_REMOTE_AZ_CMD", "az"),
        help="Path to the 'az' binary on the SSH remote host.",
    )
    parser.add_argument(
        "--api-key",
        default=_env_default("TRAPI_PROXY_API_KEY", None),
        help=(
            "Optional API key clients must present as 'Authorization: Bearer "
            "<key>' or the 'api-key' header. Default is no auth — anyone who "
            "can reach the listen address can use the proxy."
        ),
    )
    parser.add_argument(
        "--skip-token-warmup",
        action="store_true",
        help="Do not pre-fetch an Azure AD token before binding the server.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    token_provider = make_token_provider(args)

    if not args.skip_token_warmup:
        try:
            token_provider()
        except Exception as exc:
            _eprint(f"Failed to acquire Azure AD token for {SCOPE}: {exc}")
            if args.ssh_host:
                _eprint(
                    f"Try on the remote host {args.ssh_host}: "
                    f"  az login --scope api://trapi/.default"
                )
            else:
                _eprint("Try:  az login --scope api://trapi/.default")
            sys.exit(2)

    ProxyHandler.config = {
        "endpoint": args.endpoint,
        "instance": args.instance,
        "api_version": args.api_version,
        "default_deployment": args.deployment,
        "timeout": args.timeout,
        "api_key": args.api_key or None,
    }
    ProxyHandler.token_provider = staticmethod(token_provider)

    try:
        server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    except OSError as exc:
        _eprint(f"Failed to bind {args.host}:{args.port}: {exc}")
        sys.exit(2)

    base_url = f"http://{args.host}:{args.port}/v1"
    _eprint(f"TRAPI proxy listening on http://{args.host}:{args.port}")
    _eprint(f"  upstream:    {args.endpoint.rstrip('/')}/{args.instance.strip('/')}/openai")
    _eprint(f"  api-version: {args.api_version} (default; clients can override)")
    if args.deployment:
        _eprint(f"  default deployment: {args.deployment}")
    if args.ssh_host:
        _eprint(
            f"  identity:    remote (ssh {args.ssh_host}, persistent ControlMaster)"
        )
    else:
        _eprint("  identity:    local azure-identity (az CLI / managed identity)")
    _eprint(f"Point any OpenAI client at base_url={base_url}")
    if args.api_key:
        _eprint("  api_key required (set via --api-key / TRAPI_PROXY_API_KEY).")
    else:
        _eprint("Use any non-empty api_key; the proxy injects the real bearer token.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _eprint("\nShutting down...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
