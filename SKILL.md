---
name: webai2api
description: "Reverse-engineer any web AI chat interface into an OpenAI-compatible API. Upload a .har file (browser DevTools export), and automatically detect the chat endpoint, extract auth headers/cookies, analyze SSE streaming, identify PoW challenges, and generate a proxy server. Use when you need to use a web-only AI model through any OpenAI SDK (Claude Code, Cursor, Continue, etc.)."
---

# webai2api — HAR-driven Web AI to OpenAI API Proxy

## Overview

User uploads a `.har` file. You parse it, extract every detail of the chat API, then modify `templates/adapter.py` into a working OpenAI-compatible proxy.

---

## User Instructions

1. Open target AI chat page, F12 → Network, check "Preserve log"
2. Send a chat message, wait for full reply
3. Right-click any request → "Save all as HAR with content"
4. Upload the `.har` file

Optionally: specify model name (default `gpt-4o`).

---

## Step 0 — Parse HAR

```python
from har_parser import parse_har
analysis = parse_har("uploaded.har")
```

### 0.1 How `parse_har` identifies the chat entry

It scores every HAR entry by these rules (higher = more likely):

| Criterion | Points |
|-----------|--------|
| Method is POST | +0 (required, else -100) |
| URL path contains `/api/chat`, `/v1/chat`, `/conversation`, `/completion` etc. | +50 |
| Request body has `"messages"` array | +40 |
| `"messages"` has ≥1 item with non-empty `"content"` | +20 |
| Body has `"prompt"` field | +30 |
| Body has `"stream"` field | +10 |
| Body has `"model"` field | +10 |
| Body has `"temperature"`, `"max_tokens"`, `"top_p"` | +5 each |
| Response Content-Type is `text/event-stream` | +30 |
| Response Content-Type is `application/json` | +15 |
| Response body is JSON with a long string field (>50 chars) | +5 |
| URL path contains `.js`, `.css`, `.png`, `analytics`, `favicon` | -50 each |

The entry with the highest score >= 0 is the chat API. If max score < 0, fallback to manual selection (list all POST entries for user to pick).

### 0.2 What `analysis` contains

```python
analysis.base_url              # "https://chat.example.com"
analysis.chat_endpoint         # "/api/v0/chat/completion"
analysis.headers               # dict of ALL request headers from the matched entry
analysis.cookies               # raw Cookie header string
analysis.auth_header           # Authorization header or None
analysis.auth_type             # "none" | "pow" | "oauth"
analysis.request_body_template # parsed JSON body (a dict)
analysis.content_field_path    # dot-path to content in non-streaming JSON response
analysis.is_streaming          # True if response Content-Type was text/event-stream
analysis.sse_event_type        # SSE event name from "event: ..." lines
analysis.sse_data_field        # key name inside SSE data JSON that holds text
analysis.sse_format            # "plain_token" | "path_op_value" | "nested" | "raw_text"
analysis.has_pow               # True if any entry hit a path with "challenge"/"pow"
analysis.pow_endpoint          # "/api/v0/chat/create_pow_challenge"
analysis.all_endpoints         # list of all unique URL paths in HAR
analysis.chat_entry_index      # index of the matched entry
```

---

## Step 1 — Determine the Payload Pattern

Open `analysis.request_body_template` and inspect it. Below are the exhaustive set of patterns, ordered by detection priority. Pick the **first match**.

### Rule 1: `"messages"` array exists

```python
# HAR body: {"messages": [{"role":"user","content":"Hello"}], "stream":true, "model":"deepseek"}
# Strategy: pass messages through directly, keep all other keys
payload = {
    "messages": messages,  # OpenAI messages → pass straight through
    "stream": stream,
}
# Also carry over any extra HAR keys not related to messages content:
for k, v in analysis.request_body_template.items():
    if k not in ("messages", "stream"):
        payload[k] = v  # e.g. model, temperature, session_id
```

### Rule 2: `"prompt"` is a string field

```python
# HAR body: {"prompt":"Hello", "stream":true}
# Strategy: extract last user message as plain string
last = messages[-1]["content"] if messages else ""
if isinstance(last, list):
    # ContentPart array: extract text parts joined
    last = " ".join(p.get("text","") for p in last if p.get("type")=="text")
payload = {"prompt": last, "stream": stream}
for k, v in analysis.request_body_template.items():
    if k not in ("prompt", "stream"):
        payload[k] = v
```

### Rule 3: `"query"` is a string field

```python
# HAR body: {"query":"Hello", "history":[{"role":"user","content":"Hi"}], "stream":true}
# Strategy: query = last message, convert OpenAI messages to history format
last = messages[-1]["content"] if messages else ""
payload = {"query": last, "stream": stream}
if "history" in analysis.request_body_template:
    payload["history"] = messages[:-1]  # all except last
for k, v in analysis.request_body_template.items():
    if k not in ("query", "history", "stream"):
        payload[k] = v
```

### Rule 4: `"inputs"` is a string field (common in Hugging Face / Gradio)

```python
payload = {"inputs": last, "stream": stream}
for k, v in analysis.request_body_template.items():
    if k not in ("inputs", "stream"):
        payload[k] = v
```

### Rule 5: Neither messages/prompt/query/inputs — generic fallback

```python
# Find the field with the longest string value in the template
# That's the "user input" field. Also track if there's a history array.
input_field = None
history_field = None
max_len = 0
for k, v in analysis.request_body_template.items():
    if isinstance(v, str) and len(v) > max_len:
        max_len = len(v)
        input_field = k
    if isinstance(v, list) and v and isinstance(v[0], dict) and "role" in v[0]:
        history_field = k
# Reconstruct
payload = dict(analysis.request_body_template)
last = messages[-1]["content"] if messages else ""
payload[input_field] = last
if history_field:
    payload[history_field] = messages[:-1]
payload["stream"] = stream
```

### 1.1 Handle OpenAI parameters that map to HAR fields

Check if the HAR payload contains any of these keys. If yes, pass through the corresponding kwargs value. If not, omit them entirely (the target API doesn't support them).

```python
PARAM_MAP = {
    "temperature": "temperature",
    "max_tokens": "max_tokens",
    "top_p": "top_p",
    "top_k": "top_k",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
}
for har_key, param_name in PARAM_MAP.items():
    if har_key in analysis.request_body_template:
        if kwargs.get(param_name) is not None:
            payload[har_key] = kwargs[param_name]
```

### 1.2 Inject DSML for tool calling (if applicable)

```python
if tools and self.dsml_enabled and self.dsml_ready:
    if tool_choice != "none":
        dsml_prompt = build_dsml_tool_prompt(tools, tool_choice)
        messages = self._inject_dsml_prompt(messages, dsml_prompt)
```

Place this **before** the payload construction so `messages` is already modified.

---

## Step 2 — Determine the Response Field

### 2.1 Non-streaming response

`analysis.is_streaming` is `False`. The HAR response is JSON.

Extract `analysis.content_field_path` which is a dot-path like:
- `"answer"` → `data["answer"]`
- `"choices[0].message.content"` → `data["choices"][0]["message"]["content"]`
- `"data.text"` → `data["data"]["text"]`

Modify `_extract_content_from_json`:

```python
def _extract_content_from_json(self, data: dict) -> Optional[str]:
    """Navigate the content_field_path to extract the AI response text."""
    # Build the field path from analysis.content_field_path
    path = analysis.content_field_path
    if path:
        import functools
        try:
            parts = path.replace("[", ".").replace("]", "").split(".")
            val = functools.reduce(lambda d, k: d[int(k) if k.isdigit() else k] if isinstance(d, (dict, list)) else None, parts, data)
            if isinstance(val, str) and val.strip():
                return val
        except (KeyError, IndexError, TypeError):
            pass
    # Fallback: scan common top-level keys
    for key in ["answer", "text", "content", "reply", "response", "output", "completion", "result"]:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val
    # Deep fallback: recursively find longest string in full JSON
    def deepest(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                r = deepest(v)
                if r: return r
        elif isinstance(obj, list):
            for v in obj:
                r = deepest(v)
                if r: return r
        elif isinstance(obj, str) and len(obj) > 20:
            return obj
        return None
    return deepest(data)
```

### 2.2 Streaming response (SSE)

`analysis.is_streaming` is `True`. The HAR response has `Content-Type: text/event-stream`.

Modify `_extract_content_from_data` according to the table below. **Only keep the matching branch + one generic fallback. Delete all other branches.**

| `sse_format` | `sse_data_field` | Code to write in `_extract_content_from_data` |
|---|---|---|
| `"plain_token"` | `"v"` | `return data.get("v") if isinstance(data.get("v"), str) else None` |
| `"plain_token"` | `"content"` | `return data.get("content") if isinstance(data.get("content"), str) else None` |
| `"plain_token"` | `"text"` | `return data.get("text") if isinstance(data.get("text"), str) else None` |
| `"plain_token"` | `"delta"` | `d = data.get("delta"); return d.get("content") if isinstance(d, dict) else (d if isinstance(d, str) else None)` |
| `"plain_token"` | `"token"` | `return data.get("token") if isinstance(data.get("token"), str) else None` |
| `"plain_token"` | `"response"` | `return data.get("response") if isinstance(data.get("response"), str) else None` |
| `"path_op_value"` | `"v"` | DeepSeek: `return data.get("v") if data.get("o") == "APPEND" else None` |
| `"nested"` | `"v.content"` | `v = data.get("v", {}); return v.get("content") or v.get("response", {}).get("content")` |
| `"nested"` | `"delta.content"` | `d = data.get("delta", {}); return d.get("content")` |
| `"nested"` | `"choices[0].delta.content"` | `c = data.get("choices", []); return c[0].get("delta", {}).get("content") if c else None` |
| `"raw_text"` | — | SSE data line is raw text, not JSON: `return data if isinstance(data, str) else None` |

**Fallback** that always stays at the end:

```python
# Final fallback: any string field in the data dict
if isinstance(data, dict):
    for key in ("content", "text", "answer", "v", "response", "token", "delta"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict):
            sub = val.get("content") or val.get("text")
            if sub:
                return sub
return None
```

### 2.3 Modify `convert_response`

Replace the hardcoded field lookup:

```python
# BEFORE (in template):
content = response.get("answer") or response.get("text") or json.dumps(response)

# AFTER:
content = self._extract_content_from_json(response)
if not content:
    content = json.dumps(response, ensure_ascii=False)
```

---

## Step 3 — Configure Auth

### 3.1 None (most common)

```python
# auth_type is "none"
# Nothing extra needed, Cookie in headers is sufficient
```

### 3.2 Authorization header

```python
if analysis.auth_header:
    self.headers["Authorization"] = analysis.auth_header
```

### 3.3 PoW (DeepSeek pattern)

```python
self.auth_type = "pow"
self._challenge_endpoint = analysis.pow_endpoint  # "/api/v0/chat/create_pow_challenge"

# In _ensure_auth_headers: fetch challenge, solve, return header
def _ensure_auth_headers(self) -> dict:
    if self.auth_type != "pow":
        return {}
    challenge = self._fetch_challenge()
    answer = self._solve_challenge(challenge)
    return {"X-DS-PoW-Response": self._encode_pow_answer(challenge, answer)}

def _fetch_challenge(self) -> dict:
    resp = httpx.post(
        f"{self.base_url}{self._challenge_endpoint}",
        json={"target_path": "/api/v0/chat/completion"},
        headers=self.headers,
        timeout=15,
    )
    return resp.json()

def _solve_challenge(self, challenge_data: dict) -> int:
    # Extract from nested DeepSeek response format
    biz = challenge_data.get("data", {}).get("biz_data", {}).get("challenge", challenge_data)
    # Use wasm solver if wasm binary available, else implement hash loop
    # Typical: hash = sha256(salt + expire_at + nonce), find nonce where hash[:difficulty_hex] == target
    import hashlib
    salt = biz["salt"]
    expire_at = biz["expire_at"]
    difficulty = biz["difficulty"]
    target = biz.get("target_path", "")
    nonce = 0
    while True:
        h = hashlib.sha256(f"{salt}{expire_at}{nonce}{target}".encode()).hexdigest()
        if h.startswith("0" * difficulty):
            return nonce
        nonce += 1
```

### 3.4 Token refresh

```python
self.auth_type = "token_refresh"
# In _ensure_auth_headers: if 401, call refresh endpoint, update Cookie in self.headers
```

---

## Step 4 — Construct Headers

The `__init__` must produce headers identical to what the browser sent:

```python
def __init__(self, cookies: str, base_url: str, dsml_enabled: bool = True):
    self.headers = {}
    # 1. Take ALL headers from HAR except content-length
    for key, val in analysis.headers.items():
        if key.lower() != "content-length":
            self.headers[key] = val
    # 2. Ensure User-Agent exists (HAR always has it, but be safe)
    if "User-Agent" not in self.headers:
        self.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    # 3. Ensure Content-Type is set
    if "Content-Type" not in self.headers:
        self.headers["Content-Type"] = "application/json"
    # 4. Override with explicit auth if detected
    if analysis.auth_header:
        self.headers["Authorization"] = analysis.auth_header

    self.base_url = base_url.rstrip("/")
    self.chat_endpoint = analysis.chat_endpoint
    self.auth_type = analysis.auth_type
    self.dsml_enabled = dsml_enabled
    self.dsml_ready = False
```

---

## Step 5 — Session State (if needed)

Some APIs require state across requests (session ID, chat session ID).

**Check `analysis.request_body_template`** for keys like:
- `chat_session_id`, `session_id`, `conversation_id`, `conversationId`

If found, add state management to the adapter:

```python
def __init__(self, ...):
    ...
    # Session state from HAR
    self.chat_session_id = analysis.request_body_template.get("chat_session_id")
    self.conversation_id = analysis.request_body_template.get("conversation_id")
    # If the API returns a new session ID per response, update it:
    self._auto_update_session = "chat_session_id" in analysis.request_body_template

def convert_request(self, ...):
    ...
    payload["chat_session_id"] = self.chat_session_id
    # After receiving response, extract new session ID:
    # response.get("chat_session_id") → update self.chat_session_id
```

---

## Step 6 — Verify with Real Requests

Run this exact verification protocol, in order:

### 6.1 Non-streaming test

```python
import httpx
async def verify_non_streaming():
    payload = adapter.convert_request(
        [{"role": "user", "content": "Hello"}],
        stream=False,
    )
    async with httpx.AsyncClient(headers=adapter.headers, timeout=30) as client:
        resp = await client.post(
            f"{adapter.base_url}{adapter.chat_endpoint}",
            json=payload,
        )
    assert resp.status_code == 200, f"Non-streaming: status {resp.status_code}"
    data = resp.json()
    content = adapter._extract_content_from_json(data)
    assert content and len(content) > 0, f"Non-streaming: empty content from {list(data.keys())}"
    print(f"[OK] Non-streaming: got {len(content)} chars")
    return True
```

### 6.2 Streaming test

```python
async def verify_streaming():
    payload = adapter.convert_request(
        [{"role": "user", "content": "Hello"}],
        stream=True,
    )
    chunks = []
    async with httpx.AsyncClient(headers=adapter.headers, timeout=30) as client:
        async with client.stream("POST", f"{adapter.base_url}{adapter.chat_endpoint}", json=payload) as resp:
            assert resp.status_code == 200, f"Streaming: status {resp.status_code}"
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line: continue
                if line.startswith("data: "):
                    raw = line[6:]
                    if raw.strip() == "[DONE]": break
                    chunks.append(raw)
    assert len(chunks) > 0, "Streaming: no data chunks received"
    print(f"[OK] Streaming: got {len(chunks)} chunks")
    return True
```

### 6.3 Failure recovery

| Failure | Root cause | Fix |
|---------|-----------|-----|
| Status 401/403 | Cookie expired or auth missing | Tell user Cookie expired, ask for fresh HAR |
| Status 404 | Wrong endpoint | Check `analysis.chat_endpoint`; try common alternatives |
| Status 400 | Wrong payload shape | Compare generated payload against `analysis.request_body_template` byte-for-byte |
| Empty content in JSON | Wrong content field path | Re-scan JSON keys; pick the longest string value path |
| No SSE chunks | Wrong streaming mode or field | Read raw HAR SSE text; manually identify which key holds text |
| Connection refused | Target blocked by CORS/geo | Add `Origin` and `Referer` headers from HAR |

**Max 3 retry rounds.** After each round, adjust and re-run verification. If still failing after 3, output what was tried and ask user for a fresh HAR file.

---

## Step 7 — Start Proxy & Final Test

```bash
pip install fastapi uvicorn httpx python-dotenv
python server.py &
sleep 2

# Non-streaming
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello"}],"stream":false}' \
  | python -c "import sys,json; d=json.load(sys.stdin); print('OK:', d['choices'][0]['message']['content'][:80])"

# Streaming
curl -s -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello"}],"stream":true}' \
  | head -5
```

---

## Step 8 — Deliverables



1. `adapter.py` — modified adapter
2. `server.py` — from template (no changes needed)
3. `.env.example`:
   ```
   TARGET_URL=https://chat.example.com
   COOKIES=...
   MODEL_NAME=gpt-4o
   HOST=0.0.0.0
   PORT=8000
   API_KEY=sk-web2api-placeholder
   DSML_ENABLED=true
   ```
4. `requirements.txt`: `fastapi`, `uvicorn`, `httpx`, `python-dotenv`, `pydantic`
5. Start command and E2E verification output
6. Integration guide: `OPENAI_API_BASE=http://localhost:8000/v1`

---

## HAR Parser Field Reference

| Field | Type | Source | Example |
|---|---|---|---|
| `base_url` | `str` | Scheme + netloc of matched entry | `https://chat.example.com` |
| `chat_endpoint` | `str` | URL path + query | `/api/v0/chat/completion` |
| `headers` | `dict` | All request headers | `{Cookie: ..., Origin: ...}` |
| `cookies` | `str` | Cookie header value | `__session=abc; token=xyz` |
| `auth_header` | `Optional[str]` | Authorization header | `Bearer eyJ...` |
| `auth_type` | `str` | `"none"` / `"pow"` / `"oauth"` | — |
| `request_body_template` | `dict` | Parsed POST body | `{messages: [...], stream: true}` |
| `content_field_path` | `str` | Deepest non-streaming content field | `choices[0].message.content` |
| `is_streaming` | `bool` | `text/event-stream` in Content-Type | — |
| `sse_event_type` | `str` | SSE `event:` lines | `"append"` or `""` |
| `sse_data_field` | `str` | Key inside SSE data JSON | `"v"` / `"content"` / `"delta"` |
| `sse_format` | `str` | Structure of SSE data payload | `"plain_token"` / `"path_op_value"` / `"nested"` / `"raw_text"` |
| `has_pow` | `bool` | Challenge endpoint in HAR | — |
| `pow_endpoint` | `str` | PoW challenge URL path | `/api/v0/chat/create_pow_challenge` |
| `all_endpoints` | `list[str]` | All unique URL paths in HAR | — |
| `chat_entry_index` | `int` | Index in HAR entries list | `42` |

---

## SSE Format Detection Guide

`har_parser.py` classifies SSE data into these formats. Understand them to write correct extraction code:

### `"plain_token"`
Each `data:` line contains a JSON object. One key holds the incremental text directly.

```json
// data: {"v": "Hello"}
// data: {"content": " world"}
// data: {"token": "!"}
```
→ The value at `sse_data_field` is a plain string.

### `"path_op_value"` (DeepSeek)
Each `data:` line has `p` (path), `o` (operation), `v` (value). Text arrives as `APPEND` operations.

```json
// event: append
// data: {"p": "response/content", "o": "APPEND", "v": "Hello"}
```
→ Only extract when `o == "APPEND"`, take `v`.

### `"nested"`
The value at `sse_data_field` is a dict, and content is nested deeper.

```json
// data: {"delta": {"content": "Hello"}}
// data: {"choices": [{"delta": {"content": " world"}}]}
```
→ Navigate one level deeper: `data[sse_field]["content"]`.

### `"raw_text"`
The `data:` line content is not JSON at all — it's a plain string.

```
// data: Hello
// data:  world
```
→ The content IS the raw string itself.

---

## Common Pattern Reference

| Type | Endpoint | Payload | Content Field | SSE |
|---|---|---|---|---|
| DeepSeek Chat | `/api/v0/chat/completion` | `{chat_session_id, prompt, stream}` | APPEND `v` | `path_op_value` |
| ChatGPT Next Web | `/api/chat` | `{messages, model}` | `choices[0].message.content` | `nested` / `choices[0].delta.content` |
| ChatGPT Official | `/backend-api/conversation` | `{action, messages}` | `message.content.parts[0]` | `nested` |
| LobeChat | `/api/chat` | `{messages, model}` | `choices[0].delta.content` | `nested` |
| Open WebUI | `/chat/completions` | `{messages, model}` | OpenAI standard | `nested` |
| Gradio Chat | `/api/chat` | `{data: [...]}` | `data[0]` | `raw_text` or `plain_token` |
| Custom NextJS | `/api/chat` | `{prompt, history}` | `text` / `answer` | varies |

---

## DSML Tool Calling Reference

DSML injects XML-style instructions into the system prompt to support tool calling on models without native function calling.

### Tags

| Tag | Purpose |
|---|---|
| `<\|DSML\|tool_calls>` | Root container for one or more invocations |
| `<\|DSML\|invoke name="fn_name">` | Single tool call start |
| `<\|DSML\|parameter name="key"><![CDATA[value]]></\|DSML\|parameter>` | Key-value parameter |
| `</\|DSML\|invoke>` | Tool call end |
| `</\|DSML\|tool_calls>` | Root end |

### How it works

```python
# User sends tools → adapter injects DSML prompt into system message
messages_with_dsml = adapter._inject_dsml_prompt(messages, tools, tool_choice)
# Target model responds with DSML tags embedded in text
# adapter._extract_content_from_data captures the text
# StreamSieve character-by-character state machine separates:
#   - Normal text → OpenAI delta.content chunks
#   - DSML blocks → parsed into OpenAI delta.tool_calls chunks
```

StreamSieve handles: split TCP chunks, embedded CDATA, mixed text+tools, abrupt stream end.

`tool_choice` mapping: `"auto"` → optional, `"none"` → skip, `"required"` → force.

---

## PoW Solving Reference

When `has_pow` is True (DeepSeek pattern):

1. PRE-request: `POST /api/v0/chat/create_pow_challenge` → returns `{data: {biz_data: {challenge: {algorithm, challenge, salt, expire_at, difficulty, signature, target_path}}}}`
2. Solve: find nonce such that `sha256(salt + expire_at + nonce + target_path)` starts with `difficulty` zero hex chars
3. Encode: `base64({algorithm, challenge, salt, answer, signature, target_path})`
4. Send as `X-DS-PoW-Response` header with the main chat request

If WASM solver binary is available from the target site, use `wasmtime` instead of Python hashlib for performance.

---

## Limitations

- Text-only: no multimodal (images/files)
- Tool calling via DSML injection, not native function calling — depends on model's XML comprehension
- `seed` / `response_format` / `json_mode` not supported
- `max_tokens` / `temperature` only if target API exposes them
- PoW solver is site-specific (DeepSeek pattern)
- Cookie-based auth expires; user must refresh HAR when needed
