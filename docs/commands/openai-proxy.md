# `usm openai-proxy`

Run a local HTTP server that speaks the OpenAI REST API and forwards
every call to Microsoft's TRAPI endpoint, using your Azure AD identity
for auth. Lets any OpenAI-SDK-compatible client (LangChain, LiteLLM,
the official `openai` Python lib, curl, IDE plugins, …) talk to TRAPI
without writing TRAPI-specific code.

```bash
usm openai-proxy [--host 127.0.0.1] [--port 8000] [--upstream URL] [--api-version YYYY-MM-DD]
```

## What it speaks

Endpoints (under `/v1/...` and `/openai/...`):

- `GET  /health` — liveness probe (no auth)
- `GET  /status` — current upstream + api-version + token state
- `*    /v1/<...>` — proxied to TRAPI; all OpenAI-compatible paths
  (`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`, …) work
- `OPT  /<...>` — CORS preflight

It handles:

- **Path → deployment routing**: most OpenAI paths map to
  `/openai/deployments/<model>/...`; `model` comes from the request body.
- **No-deployment paths**: `/models`, `/files`, `/fine_tuning`, `/batches`,
  `/threads`, `/assistants` go directly under `/openai/...`.
- **SSE streaming**: chat completions with `stream=true` are streamed
  through `httpx.AsyncClient` + Starlette `StreamingResponse` —
  hundreds of concurrent streams in one event loop.
- **Token refresh**: Azure AD bearer tokens are minted via
  `azure.identity.aio` (so `az login` or managed identity both work)
  and renewed transparently.

## Using it

In one terminal:

```bash
usm openai-proxy --port 8000
```

In your client:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="dummy",  # the proxy uses your Azure identity; this is just to satisfy the SDK
)

resp = client.chat.completions.create(
    model="gpt-4o",  # the TRAPI deployment name
    messages=[{"role": "user", "content": "hi"}],
)
```

For streaming:

```python
stream = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

## Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. Use `0.0.0.0` to expose on the network. |
| `--port` | `8000` | Listen port. |
| `--upstream` | TRAPI prod URL | Override the upstream base. |
| `--api-version` | TRAPI default | The `api-version` query parameter. |

`--help` for the full list (timeouts, default deployment, etc.).

## Why it exists

Lots of internal tooling targets the OpenAI REST API. TRAPI is API-shaped
but uses different routing + Azure AD auth. This proxy makes any OpenAI
client work against TRAPI without code changes.

## Source

[`scripts/openai_proxy.py`](https://github.com/HSPK/usm/blob/main/scripts/openai_proxy.py).
Built on Starlette + uvicorn + httpx; ~250 lines.

Test suite at
[`tests/test_openai_proxy.py`](https://github.com/HSPK/usm/blob/main/tests/test_openai_proxy.py)
(38 unit + integration tests, including SSE streaming).
