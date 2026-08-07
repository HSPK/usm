# `usm openai-proxy`

Run a local HTTP server that speaks the OpenAI REST API — chat
completions, the Responses API and the Anthropic Messages API — and
forwards every call to Microsoft's TRAPI endpoint, using your Azure AD
identity for auth. Lets any OpenAI- or Anthropic-SDK-compatible client
(LangChain, LiteLLM, the official `openai` and `anthropic` Python libs,
curl, IDE plugins, …) talk to TRAPI without writing TRAPI-specific code.

```bash
usm openai-proxy [--host 127.0.0.1] [--port 8000] [--upstream URL] [--api-version YYYY-MM-DD]
```

## What it speaks

Endpoints (under `/v1/...` and `/openai/...`):

- `GET  /health` — liveness probe (no auth)
- `GET  /status` — current upstream + api-version + token state
- `POST /v1/responses` — OpenAI **Responses API**, emulated over chat
  completions
- `POST /v1/messages` — Anthropic **Messages API**, emulated over chat
  completions (plus `/v1/messages/count_tokens` and `/anthropic/v1/models`)
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
- **Auth headers**: the optional `--api-key` gate accepts the OpenAI
  (`Authorization: Bearer`), Azure (`api-key`) and Anthropic (`x-api-key`)
  conventions. Client credentials are never forwarded upstream.

## Three dialects, one upstream

TRAPI deployments only speak chat completions, so the other two APIs are
translated: request in, chat completion out, native-shaped response back.

The code is a **shared kernel plus two sibling adapters**. The kernel
(deployment capability probes, the token-limit decision, upstream parameter
negotiation) is stdlib-only with no Starlette/httpx imports; each adapter
depends on the kernel and never on the other. Both expose the same three
seams — build the chat request, convert the reply, translate the stream — so
the transport is one shared code path.

| | Endpoint | Adapter seams |
| --- | --- | --- |
| OpenAI Responses | `POST /v1/responses` | `build_chat_request` · `chat_to_responses` · `ResponsesStreamTranslator` |
| Anthropic Messages | `POST /v1/messages` | `build_chat_request_from_messages` · `chat_to_anthropic_message` · `AnthropicStreamTranslator` |

### `max_tokens` vs `max_completion_tokens`

Both dialects have an output-token cap (`max_output_tokens` in Responses,
`max_tokens` in Messages), and both route through the same kernel decision,
because the two chat-completions fields are **not** interchangeable:

- `max_tokens` is deprecated and **rejected outright by reasoning
  deployments** (o-series, gpt-5, codex) — they must budget invisible
  reasoning tokens too, and answer *"Use `max_completion_tokens` instead"*.
- `max_completion_tokens` is its replacement, but **older api-versions and
  older deployments** answer *"Unrecognized request argument"*.

So the cap is sent as `max_completion_tokens` for reasoning model names and
`max_tokens` otherwise — exactly one of the two, never both. If the upstream
still disagrees, the proxy reads the 400, swaps the field name and retries
(the same recovery drops other optional parameters a deployment rejects,
e.g. `temperature` on o-series). Force a field with `--token-limit-field`.

### Responses API

| Responses | Chat completions |
| --- | --- |
| `instructions` | leading `system` message (`developer` for reasoning models) |
| `input` (string / items / multimodal parts) | `messages` |
| `function_call` + `function_call_output` items | `assistant.tool_calls` + `tool` messages |
| `tools` (flat) | `tools` (nested under `function`) |
| `text.format.json_schema` | `response_format.json_schema` |
| `reasoning.effort` | `reasoning_effort` |
| `max_output_tokens` | `max_tokens` / `max_completion_tokens` |
| `stream: true` | `stream` + `stream_options.include_usage` |

Stateless by design: `store` is always `false`, so `previous_response_id`,
`conversation`, `background`, stored prompts and `GET /v1/responses/{id}`
are rejected — resend the full `input` each turn.

### Anthropic Messages API

| Messages | Chat completions |
| --- | --- |
| `system` (string or blocks) | leading `system` / `developer` message |
| `messages[].content` blocks | `messages[].content` parts |
| `image` / `document` blocks | `image_url` (data URL) / `file` parts |
| `tool_use` blocks | `assistant.tool_calls` |
| `tool_result` blocks (in a *user* turn) | separate `tool` messages |
| `tools[].input_schema` | `tools[].function.parameters` |
| `tool_choice` `auto`/`any`/`tool`/`none` | `auto`/`required`/named/`none` |
| `thinking.budget_tokens` | `reasoning_effort` (low/medium/high) |
| `stop_sequences` | `stop` |
| `metadata.user_id` | `user` |
| `max_tokens` | `max_tokens` / `max_completion_tokens` |

Responses come back as real Anthropic objects — `stop_reason`
(`end_turn`/`max_tokens`/`tool_use`/`refusal`), `content` blocks, Anthropic
`usage`, and the full streaming event sequence (`message_start` →
`content_block_start`/`_delta`/`_stop` → `message_delta` → `message_stop`).
Errors use Anthropic's `{"type": "error", "error": {...}}` envelope.

Also served:

- `POST /v1/messages/count_tokens` — chat completions has no token-counting
  endpoint, so the prompt is priced by running it with a 1-token generation
  budget and reading back `usage.prompt_tokens`. Exact, at the cost of one
  tiny upstream call.
- `GET /anthropic/v1/models` (and `/{id}`) — the upstream catalogue reshaped
  into Anthropic's model list. It lives under `/anthropic` because the bare
  `/v1/models` must stay OpenAI-shaped.

Point an Anthropic SDK at either the root or the `/anthropic` prefix:

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8000", api_key="dummy")

msg = client.messages.create(
    model="gpt-4o",              # the TRAPI deployment name
    max_tokens=1024,
    system="be terse",
    messages=[{"role": "user", "content": "hi"}],
)
print(msg.content[0].text)
```

### Limits

Hosted/server-side tools have no chat-completions equivalent — Responses'
`web_search`/`file_search`/MCP and Anthropic's `web_search_*`, `computer_*`,
`bash_*`, `text_editor_*` are dropped rather than failing an otherwise
answerable request. Anthropic `top_k` is not forwarded, prompt caching is
reported as zero, and `stop_sequence` is always `null` (chat completions
never says which stop string matched). Thinking blocks are not replayable,
so they're skipped on input. Use `--responses-mode passthrough` /
`--anthropic-mode passthrough` to forward those paths untouched if your
upstream ever supports them natively.

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

Or with the Responses API:

```python
with client.responses.stream(model="gpt-4o", input="hi") as stream:
    for event in stream:
        if event.type == "response.output_text.delta":
            print(event.delta, end="")
    final = stream.get_final_response()
```

Or with the Anthropic SDK:

```python
with anthropic_client.messages.stream(
    model="gpt-4o", max_tokens=1024, messages=[{"role": "user", "content": "hi"}]
) as stream:
    for text in stream.text_stream:
        print(text, end="")
    final = stream.get_final_message()
```

## Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. Use `0.0.0.0` to expose on the network. |
| `--port` | `8000` | Listen port. |
| `--upstream` | TRAPI prod URL | Override the upstream base. |
| `--api-version` | TRAPI default | The `api-version` query parameter. |
| `--responses-mode` | `translate` | `translate` emulates `/v1/responses` over chat completions; `passthrough` forwards it unchanged. |
| `--anthropic-mode` | `translate` | `translate` emulates `/v1/messages` (+ `count_tokens`, `/anthropic/v1/models`) over chat completions; `passthrough` forwards them unchanged. |
| `--token-limit-field` | `auto` | Pin the output-token cap to `max_tokens` or `max_completion_tokens` instead of auto-detecting. |

`--help` for the full list (timeouts, default deployment, etc.).

## Why it exists

Lots of internal tooling targets the OpenAI or Anthropic REST APIs. TRAPI is
OpenAI-chat-shaped but uses different routing + Azure AD auth, and speaks
neither the Responses nor the Messages dialect. This proxy makes any OpenAI
*or* Anthropic client work against TRAPI without code changes.

## Source

[`scripts/openai_proxy.py`](https://github.com/HSPK/usm/blob/main/scripts/openai_proxy.py).
Built on Starlette + uvicorn + httpx.

Test suite at
[`tests/test_openai_proxy.py`](https://github.com/HSPK/usm/blob/main/tests/test_openai_proxy.py)
(283 unit + integration tests at 93% line coverage, covering both
translation layers, SSE streaming, concurrency and failure paths).
