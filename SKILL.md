---
name: webai2api
description: "Reverse-engineer any web AI chat interface into an OpenAI-compatible API. Upload a .har file (browser DevTools export), and automatically detect the chat endpoint, extract auth headers/cookies, analyze SSE streaming, identify PoW challenges, and generate a proxy server. Use when you need to use a web-only AI model through any OpenAI SDK (Claude Code, Cursor, Continue, etc.)."
---

# webai2api — HAR-driven Web AI to OpenAI API Proxy

## Overview

User uploads a `.har` file captured from browser DevTools while chatting with a web AI. You parse it, reverse-engineer the API, and generate an OpenAI-compatible proxy server.

## User Instructions

1. Open target AI chat page in browser, F12 → Network
2. Check "Preserve log", send a message, wait for reply
3. Right-click any request → "Save all as HAR with content"
4. Upload the `.har` file

Optionally they can specify a model name (default `gpt-4o`).

---

## Workflow

### Step 0: Parse HAR

```python
from har_parser import parse_har
analysis = parse_har("uploaded.har")
# Extracts: base_url, chat_endpoint, headers, cookies,
#           auth_header, auth_type, request_body_template,
#           content_field_path, is_streaming, sse_data_field,
#           sse_format, has_pow, pow_endpoint
```

### Step 1: Modify `adapter.py` per HAR analysis

Apply these modifications to `templates/adapter.py` → output `adapter.py`.

**1.1 `__init__` — endpoint & headers**

```python
self.chat_endpoint = analysis.chat_endpoint

self.headers = {"User-Agent": "Mozilla/5.0 ..."}
for key, val in analysis.headers.items():
    if key.lower() != "content-length":
        self.headers[key] = val

if analysis.has_pow:
    self.auth_type = "pow"
    self._challenge_endpoint = analysis.pow_endpoint
```

**1.2 `convert_request` — payload structure**

Examine `analysis.request_body_template`. Identify the "user input" field by priority:

```
1. "messages" → input field, keep array structure
2. "prompt" → extract last message content as string
3. "query" + "history" → query = last msg, convert history array
4. "inputs" → string
5. longest string field in template
```

Write the function body to produce the **exact same structure** as the HAR payload, replacing only the user message.

**1.3 `_extract_content_from_data` — SSE extraction**

| `sse_format` | `sse_data_field` | Code |
|---|---|---|
| `"plain_token"` | `"v"` | `return data.get("v")` |
| `"plain_token"` | `"content"` | `return data.get("content")` |
| `"plain_token"` | `"delta"` | `v = data.get("delta"); return v.get("content") if isinstance(v, dict) else v` |
| `"path_op_value"` | `"v"` | DeepSeek: `return data.get("v") if data.get("o") == "APPEND" else None` |
| `"nested"` | `"v.content"` | `v = data.get("v", {}); return v.get("content")` |

Remove unrelated branches, keep only the matching one + fallback.

**1.4 `_extract_content_from_json` — non-streaming extraction**

Use `analysis.content_field_path` (e.g. `"choices[0].message.content"`):

```python
import functools
try:
    parts = analysis.content_field_path.replace("[",".").replace("]","").split(".")
    result = functools.reduce(lambda d,k: d[int(k) if k.isdigit() else k] if d else None, parts, data)
    if isinstance(result, str) and result: return result
except: pass
# fallback
for key in ["answer","text","content","reply","response","output","completion"]:
    val = data.get(key)
    if isinstance(val, str) and val: return val
```

**1.5 `convert_response` — use custom extractor**

Replace the last lines:

```python
content = self._extract_content_from_json(response)
if not content:
    content = json.dumps(response, ensure_ascii=False)
```

### Step 2: Verify with real requests

Send a test request using the adapted payload to confirm:
- Endpoint returns 200
- Content field extracts correctly
- SSE streaming works (if `is_streaming`)

Retry max 3 rounds adjusting content field paths on failure.

### Step 3: Generate final files

- `adapter.py` — modified adapter
- `server.py` — copied from template (unchanged)
- `.env.example` — config template

### Step 4: Start proxy & E2E test

```bash
pip install fastapi uvicorn httpx python-dotenv
python server.py &
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

Test both streaming (`stream: true`) and non-streaming.

### Step 5: Output integration guide

Tell user to set `OPENAI_API_BASE=http://localhost:8000/v1` in their tooling. Note limitations (no multimodal, no `seed`/`response_format`, tool calling via DSML injection).

---

## HAR Parser Reference

**Fields extracted by `har_parser.py`:**

| Field | Source | Example |
|---|---|---|
| `base_url` | Scheme + netloc of matched entry | `https://chat.example.com` |
| `chat_endpoint` | URL path of chat POST | `/api/v0/chat/completion` |
| `headers` | All request headers | `{Cookie: ..., Origin: ...}` |
| `cookies` | Cookie header value | `__session=abc; token=xyz` |
| `auth_header` | Authorization header | `Bearer eyJ...` |
| `auth_type` | `"none"` / `"pow"` / `"oauth"` | — |
| `request_body_template` | Parsed POST body dict | `{messages: [...], stream: true}` |
| `content_field_path` | Deepest non-streaming JSON content field | `choices[0].message.content` |
| `is_streaming` | Whether response was SSE | `true` / `false` |
| `sse_event_type` | SSE event name | `"append"` or `""` |
| `sse_data_field` | Key within SSE data JSON | `"v"` / `"content"` / `"delta"` |
| `sse_format` | `"plain_token"` / `"path_op_value"` / `"nested"` | — |
| `has_pow` | Challenge endpoint detected | `true` / `false` |
| `pow_endpoint` | PoW challenge URL path | `/api/v0/chat/create_pow_challenge` |
| `all_endpoints` | All URL paths in HAR | `[...]` |

## Common Patterns

| Type | Endpoint | Payload | Content Field | SSE |
|---|---|---|---|---|
| DeepSeek Chat | `/api/v0/chat/completion` | `{chat_session_id, prompt, stream}` | APPEND `v` | event + data |
| ChatGPT Next Web | `/api/chat` | `{messages, model}` | `choices[0].message.content` | data |
| ChatGPT Official | `/backend-api/conversation` | `{action, messages}` | `message.content.parts[0]` | data |
| LobeChat | `/api/chat` | `{messages, model}` | `choices[0].delta.content` | data |
| Open WebUI | `/chat/completions` | `{messages, model}` | OpenAI standard | data |
| Gradio Chat | `/api/chat` | `{data: [...]}` | `data[0]` | data |

## DSML Tool Calling

DSML injects XML-style tool call instructions into the system prompt for models that don't natively support function calling.

Tags: `<|DSML|tool_calls>`, `<|DSML|invoke name="fn">`, `<|DSML|parameter name="k"><![CDATA[v]]></|DSML|parameter>`

StreamSieve character-state machine separates DSML tags from normal text in real-time SSE output.

## PoW Auto-Solve

If `has_pow` is true, include a WASM solver. Use `wasmtime` to load the solver wasm from the target site, extract the solve function from exports, and generate `X-DS-PoW-Response` header.

## Deliverables

1. `adapter.py` — modified adapter (verified)
2. `server.py` — proxy server (from template)
3. `.env.example` — config template
4. `requirements.txt`
5. Start command + E2E test results
6. Integration guide with known limitations

## Limitations

- No multimodal (text only)
- Tool calling via DSML prompt injection, not native function calling
- `seed` / `response_format` / `json_mode` not supported
- `max_tokens` / `temperature` depends on target API
- PoW support is site-specific (DeepSeek pattern)
