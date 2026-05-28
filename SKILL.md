---
name: webai2api
description: "Reverse-engineer any web AI chat interface into an OpenAI-compatible API. Upload a .har file (browser DevTools export), and automatically detect the chat endpoint, extract auth headers/cookies, analyze SSE streaming, identify PoW challenges, and generate a proxy server. Use when you need to use a web-only AI model through any OpenAI SDK (Claude Code, Cursor, Continue, etc.)."
---

# webai2api — HAR-driven Web AI to OpenAI API Proxy

## Overview

User uploads a `.har` file. You parse it, extract every detail of the chat API, then generate an OpenAI-compatible proxy. Supports both **HTTP (SSE/JSON)** and **WebSocket** transports — auto-detected from the HAR file.

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

The entry with the highest score >= 0 is the chat API.

### 0.3 WebSocket detection (fallback from HTTP)

If no HTTP POST entry scored >= 0, or if a `response.status == 101` (Switching Protocols) entry is found, `parse_har` tries WebSocket detection:

1. Find entries with `status == 101` that have a `_webSocketMessages` array
2. Require at least 1 "send" + 1 "receive" message in the array
3. Score the messages: JSON body contains `message`/`content`/`prompt` keywords, long string values, `stream` flag
4. If score >= 3, mark `analysis.has_websocket = True` and populate `analysis.ws.*`

**WS scoring criteria:**

| Criterion | Points |
|-----------|--------|
| Entry has `_webSocketMessages` with ≥1 send + ≥1 receive | required |
| Send frame JSON has chat-like keys (message, content, prompt, text, query) | +2 each key |
| Send frame JSON has `stream` field | +1 |
| Send frame JSON has a string value longer than 10 characters | +2 |
| Receive frame JSON has a chat-like key with a long string value | +2 per frame |
| **Minimum score to qualify** | **3** |

The WS entry also provides:
- **Send template**: JSON structure of the first send message (used as payload blueprint)
- **Init messages**: any send messages that lack chat content keys (likely auth/init frames to replay on connect)
- **Streaming detection**: if ≥3 receive frames for a single send → `receive_is_streaming = True`

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
analysis.supported_params      # ["temperature", "top_p", "max_tokens", ...]
                               # inferred from HAR bodies (zero-cost probe)
analysis.has_websocket         # True / False — WebSocket detected
analysis.ws.ws_url             # "wss://chat.example.com/ws" (WS URL)
analysis.ws.send_template      # JSON dict of the first send frame
analysis.ws.input_field        # which key in send_template holds user msg
analysis.ws.receive_field      # which key in receive frames holds AI text
analysis.ws.receive_is_streaming  # True if multiple receives per send
analysis.ws.type_field         # if frames use a type/event discriminator
analysis.ws.extra_send_fields  # non-input keys to carry in every send
analysis.ws.init_messages      # auth/init frames to replay after connect
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

### Rule 6: WebSocket — `analysis.has_websocket == True`

The target API uses WebSocket, not HTTP. **Do NOT modify `adapter.py`**. Instead, generate **`ws_adapter.py`** from `templates/ws_adapter.py`.

**Send frame format** — derived from `analysis.ws.send_template`:

```python
# HAR show first send frame was: {"type": "chat", "content": "Hello", "stream": true}
# analysis.ws.input_field = "content"
# analysis.ws.extra_send_fields = {"type": "chat"}
payload = dict(analysis.ws.extra_send_fields)  # {"type": "chat"}
last = messages[-1]["content"] if messages else ""
if isinstance(last, list):
    last = " ".join(p.get("text","") for p in last if p.get("type")=="text")
payload[analysis.ws.input_field] = last       # {"type": "chat", "content": "Hello"}
if stream:
    payload["stream"] = True
```

If `input_field` is `"messages"`, pass the full messages array instead:

```python
# HAR: {"messages": [...], "model": "gpt-4o"}
payload = dict(analysis.ws.extra_send_fields)
payload["messages"] = messages
if stream:
    payload["stream"] = True
```

**Receive extraction** — based on `analysis.ws.receive_field`. Same dot-path logic as Step 2.2.

**Init messages** — if `analysis.ws.init_messages` is non-empty, send them in order right after WebSocket connect before the first user message:

```python
await ws.send(json.dumps(init_msg))
# ... for each init_msg in analysis.ws.init_messages
```

**Type field filtering** — if `analysis.ws.type_field` is set, skip receive frames whose type is `error`, `ping`, `pong`, `ack`, `status`, `typing`, etc.

### 1.1 Dynamic parameter passthrough (auto-inferred from HAR)

`analysis.supported_params` contains every OpenAI-compatible parameter that the target API was observed using in the HAR. Use this list to dynamically decide which kwargs to pass through:

```python
# In __init__, store the supported list:
self.supported_params = analysis.supported_params
# e.g. ["temperature", "top_p", "max_tokens", "stop", "presence_penalty"]

# In convert_request, after building the payload:
for param in self.supported_params:
    # User's request may have passed this param via kwargs
    val = kwargs.get(param)
    if val is not None:
        payload[param] = val
```

**How it works at runtime:**

| User sends via OpenAI SDK | `analysis.supported_params` contains | adapter puts in payload |
|---|---|---|
| `temperature=0.7` | `"temperature"` | `temperature: 0.7` |
| `temperature=0.7` | *not in list* | **omitted** — target doesn't support it |
| `max_tokens=4096, temperature=0.5` | `["temperature"]` | only `temperature: 0.5` passed, `max_tokens` dropped |
| `stop=["\n\n"], frequency_penalty=0.3` | `["stop"]` | only `stop: ["\n\n"]` passed |

**How `har_parser.py` infers the list:**

```
1. Collect all HAR POST entries whose URL path matches the chat endpoint.
2. For each entry, extract JSON body keys.
3. Match keys against a known alias table:
     "temperature"        → "temperature"
     "max_tokens"         → "max_tokens"
     "max_length"         → "max_tokens"     (alias)
     "max_new_tokens"     → "max_tokens"     (alias)
     "top_p"              → "top_p"
     "top_k"              → "top_k"
     "presence_penalty"   → "presence_penalty"
     "frequency_penalty"  → "frequency_penalty"
     "repetition_penalty" → "repetition_penalty"
     "stop"               → "stop"
     "stop_sequences"     → "stop"           (alias)
     "n"                  → "n"
     "seed"               → "seed"
     "user"               → "user"
4. If a key appears in any request with a non-null, non-zero value,
   its canonical name is added to supported_params.
```

This is **zero-cost**: it reads what the browser already sent, without sending any extra probe requests.

**Verification** (optional, added to Step 6):

```python
# If temperature is supported, send two requests with extreme values
# and confirm the responses are meaningfully different
if "temperature" in adapter.supported_params:
    cold = await _send_with_temp(adapter, 0.0)
    hot  = await _send_with_temp(adapter, 2.0)
    if len(cold) == len(hot) and cold == hot:
        print("[WARN] temperature set but responses identical — target may ignore it")
```

### 1.2 Inject DSML for tool calling (if applicable)

```python
if tools and self.dsml_enabled and self.dsml_ready:
    if tool_choice != "none":
        dsml_prompt = build_dsml_tool_prompt(tools, tool_choice)
        messages = self._inject_dsml_prompt(messages, dsml_prompt)
```

Place this **before** the payload construction so `messages` is already modified.

### 1.3 Building `ws_adapter.py` (only when `has_websocket`)

If `analysis.has_websocket` is True, generate **`ws_adapter.py`** from `templates/ws_adapter.py`. Do NOT modify `adapter.py`.

**Modification table — map each `analysis.ws.*` field to the corresponding `WebSocketChatAdapter` attribute:**

| `ws_adapter.py` attribute/method | Set from `analysis.ws.*` |
|---|---|
| `self.ws_url` | `analysis.ws.ws_url` |
| `self.send_template` | `analysis.ws.send_template` |
| `self.input_field` | `analysis.ws.input_field` |
| `self.receive_field` | `analysis.ws.receive_field` |
| `self.receive_is_streaming` | `analysis.ws.receive_is_streaming` |
| `self.type_field` | `analysis.ws.type_field` |
| `self.extra_send_fields` | `analysis.ws.extra_send_fields` |
| `self.init_messages` | `analysis.ws.init_messages` |
| `_connect()` | Override only if target requires custom subprotocols |
| `_extract_content_from_frame()` | Uses `receive_field` + `type_field` — no change needed |
| `_is_done_frame()` | Override only if done signal differs from defaults |

**The template already uses `self.input_field` and `self.extra_send_fields` in `convert_request()`** — just fill them in `__init__` and it works.

**Key behavioral differences from HTTP adapter to note:**

1. **Connection lifecycle**: Each request opens a fresh WS connection (connect → send → receive → close). If the HAR shows multiple send/receive pairs on one WS (persistent session), change to reuse the connection.
2. **Init messages**: If `init_messages` is non-empty, `_connect()` automatically sends them after connecting. No additional code needed.
3. **Frame done detection**: `_is_done_frame()` checks multiple patterns (finish_reason, done flag, type field, `[DONE]`). If the target uses a unique signal, override this method.
4. **Non-streaming mode**: When `receive_is_streaming` is False, the adapter reads exactly one receive frame and returns it. When True, it reads frames until `_is_done_frame()` returns True.
5. **Binary frames**: WebSocketChatAdapter only handles text frames (opcode 1). Binary frames (opcode 2) are not supported — report this limitation.

---

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

**Check `analysis.request_body_template` or `analysis.ws.send_template`** for keys like:
- `chat_session_id`, `session_id`, `conversation_id`, `conversationId`

### For HTTP adapter

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

### For WebSocket adapter (persistent connection)

If the HAR shows multiple send/receive pairs on the **same** WebSocket (check if `_webSocketMessages` has alternating send/receive across the whole entry), the target expects a persistent connection. Modify `WebSocketChatAdapter` to keep the WS open:

```python
def __init__(self, ...):
    ...
    self._ws = None  # persistent connection
    self._session_id = analysis.ws.extra_send_fields.get("chat_session_id")
    self._auto_update = "chat_session_id" in analysis.ws.extra_send_fields

async def _get_connection(self):
    """Reuse or create a persistent WebSocket."""
    if self._ws is None or self._ws.closed:
        self._ws = await self._connect()
    return self._ws
```

Then replace `_connect()` calls in `send_request`/`stream_request` with `_get_connection()`. This avoids reconnecting and re-sending init messages for every request.

---

## Step 6 — Generate config_tool.py

Copy `templates/config_tool.py` → output `config_tool.py`. This is a standalone Tkinter GUI that lets the user re-configure .env from any HAR file without re-running the AI.

### 6.1 Fill `FIELD_LABELS`

Map every key in the `info` dict (from `parse_har_file()`) to a Chinese label:

```python
FIELD_LABELS = {
    "base_url": "目标地址",
    "chat_endpoint": "聊天端点",
    "cookies": "Cookie",
    "auth_header": "Authorization",
    "auth_type": "认证类型",
    "is_streaming": "流式支持",
    "has_websocket": "WebSocket",
    "has_pow": "PoW 挑战",
    "content_field_path": "内容字段路径",
    "supported_params": "支持的参数",
    # HAR特有的字段，从 analysis 中取:
    # "chat_session_id": "会话 ID",
}
```

Only include keys that exist in `analysis`. If the target has unique fields (DeepSeek's `chat_session_id`, Gemini's `bl`/`f.sid`/`at`/`sn`), add them here.

### 6.2 Fill `ENV_MAPPING`

Map (`env_key`, `info_key`, `comment`) for every field that should appear in .env:

```python
ENV_MAPPING = [
    ("HAR_PATH",       "har_path",           "HAR 文件路径（用于重新解析）"),
    ("TARGET_URL",     "base_url",           "目标网站地址"),
    ("CHAT_ENDPOINT",  "chat_endpoint",      "聊天 API 端点路径"),
    ("COOKIES",        "cookies",            "登录 Cookie"),
    ("AUTH_HEADER",    "auth_header",        "Authorization 令牌"),
    ("AUTH_TYPE",      "auth_type",          "认证类型"),
    ("STREAMING",      "is_streaming",       "是否支持流式输出"),
    ("WEBSOCKET",      "has_websocket",      "是否使用 WebSocket"),
    # 目标特有:
    # ("CHAT_SESSION_ID", "chat_session_id", "会话 ID"),
]
```

### 6.3 Fill `DISPLAY_FIELDS`

Control which fields appear in the TreeView and in what order:

```python
DISPLAY_FIELDS = [
    ("目标地址", "base_url"),
    ("聊天端点", "chat_endpoint"),
    ("认证类型", "auth_type"),
    ("Cookie", "cookies"),
    ("Authorization", "auth_header"),
    # ...
]
```

### 6.4 Customize `parse_har_file()` if needed

The default implementation calls `har_parser.parse_har()` and extracts common fields. If the target has unique parameters not covered by `har_parser.py`, add extraction logic in `parse_har_file()` or use the `_extract_raw_har()` helper which can regex-search the raw HAR text for specific keys.

Example for DeepSeek (adds chat_session_id):

```python
def parse_har_file(har_path: str) -> dict:
    info = _default_parse(har_path)  # calls har_parser.parse_har
    # Extract target-specific fields
    chat_session_id = _extract_raw_har(har_path, "chat_session_id")
    if chat_session_id:
        info["chat_session_id"] = chat_session_id
        FIELD_LABELS["chat_session_id"] = "会话 ID"
        ENV_MAPPING.append(("CHAT_SESSION_ID", "chat_session_id", "DeepSeek 会话 ID"))
        DISPLAY_FIELDS.append(("会话 ID", "chat_session_id"))
    return info
```

### 6.5 Fill `MUTABLE_KEYS` — the split design

This is the key innovation. `.env` is split into two sections. **AI decides which keys go where** based on the nature of each field:

| 放入 `MUTABLE_KEYS`（异变） | 不放入（不易变） |
|---|---|
| Cookie、Authorization 等会过期的鉴权凭证 | PORT、HOST 等服务器监听配置 |
| Token、session_id 等每次登录变化的参数 | MODEL_NAME、API_KEY 等固定标识 |
| TARGET_URL、CHAT_ENDPOINT（站点结构可能变） | DSML_ENABLED 等功能开关 |
| STREAMING、WEBSOCKET 等传输方式标记 | 用户自定义的运行时参数 |
| 从 HAR 中解析出的任何易变字段 | 不会在 Cookie 过期时一起变化的值 |

**规则：凡是会随登录过期/变化的 key → 放入 `MUTABLE_KEYS`；凡是服务器本身的配置 → 不放入。**

运行时 `merge_env_with_auth()` 的行为：
1. 读取现有 `.env`
2. **只覆写** `MUTABLE_KEYS` 中的 key
3. 其他 key（PORT、MODEL_NAME 等）保留原值
4. 如果 `.env` 不存在，不易变部分用 `DEFAULT_IMMUTABLE` 填充

示例输出 `.env`：
```
# ============================================
# 不易变部分 — 服务器配置（配置工具不会修改）
# ============================================
MODEL_NAME=gpt-4o
HOST=0.0.0.0
PORT=8000
...

# ============================================
# 异变部分 — 账号鉴权凭证（配置工具只更新此段）
# ============================================
COOKIES=...
AUTH_HEADER=...
```

### 6.6 Deliver

Add to the output deliverables. The user can double-click `config_tool.py` to:
1. Select a HAR file from disk
2. Click "解析" to extract all config fields
3. View the generated .env in a syntax-highlighted preview
4. Click "保存到 .env" to persist
5. Click "启动代理服务器" to run `server.py`

When Cookie expires, the user just re-opens `config_tool.py`, selects the same HAR (or a fresh one), clicks "解析" → "保存" → "启动", all without touching the AI.

---

## Step 7 — Verify with Real Requests

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

### 6.3 WebSocket verification (only when `has_websocket`)

```python
import websockets
async def verify_ws_non_streaming():
    payload = adapter.convert_request(
        [{"role": "user", "content": "Hello"}],
        stream=False,
    )
    ws = await adapter._connect()
    try:
        await ws.send(json.dumps(payload, ensure_ascii=False))
        frames = []
        if adapter.receive_is_streaming:
            async for raw in ws:
                text = adapter._extract_content_from_frame(raw)
                if text:
                    frames.append(text)
                if adapter._is_done_frame(raw):
                    break
        else:
            raw = await ws.recv()
            text = adapter._extract_content_from_frame(raw)
            if text:
                frames.append(text)
        assert len(frames) > 0, "WS non-streaming: no content frames"
        print(f"[OK] WS non-streaming: got {len(frames)} frames")
        return True
    finally:
        await ws.close()

async def verify_ws_streaming():
    if not adapter.receive_is_streaming:
        print("[SKIP] WS streaming: target is non-streaming")
        return True
    payload = adapter.convert_request(
        [{"role": "user", "content": "Hello"}],
        stream=True,
    )
    ws = await adapter._connect()
    try:
        await ws.send(json.dumps(payload, ensure_ascii=False))
        chunks = []
        async for raw in ws:
            text = adapter._extract_content_from_frame(raw)
            if text:
                chunks.append(text)
            if adapter._is_done_frame(raw):
                break
        assert len(chunks) > 1, f"WS streaming: only {len(chunks)} chunks (need >1)"
        print(f"[OK] WS streaming: got {len(chunks)} chunks")
        return True
    finally:
        await ws.close()
```

### 6.4 Failure recovery

| Failure | Root cause | Fix |
|---------|-----------|-----|
| Status 401/403 | Cookie expired or auth missing | Tell user Cookie expired, ask for fresh HAR |
| Status 404 | Wrong endpoint | Check `analysis.chat_endpoint`; try common alternatives |
| Status 400 | Wrong payload shape | Compare generated payload against `analysis.request_body_template` byte-for-byte |
| Empty content in JSON | Wrong content field path | Re-scan JSON keys; pick the longest string value path |
| No SSE chunks | Wrong streaming mode or field | Read raw HAR SSE text; manually identify which key holds text |
| Connection refused | Target blocked by CORS/geo | Add `Origin` and `Referer` headers from HAR |
| WS connection refused | Wrong URL scheme/port | Check `analysis.ws.ws_url`; try both `wss://` and `ws://` |
| WS closed immediately | Auth/init messages missing | Check `analysis.ws.init_messages` and verify they replay correctly |
| WS receive empty | Wrong `receive_field` | Re-scan HAR receive frame JSON keys; pick the longest string value |
| WS stream stuck | Wrong done signal | Read HAR receive frames manually; find what signals completion |
| WS binary frame | Target sends opcode 2 | Cannot support — tell user this AI uses binary WS frames |

**Max 3 retry rounds.** After each round, adjust and re-run verification. If still failing after 3, output what was tried and ask user for a fresh HAR file.

---

## Step 8 — Start Proxy & Final Test

```bash
pip install fastapi uvicorn httpx python-dotenv websockets
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

## Step 9 — Deliverables



1. `adapter.py` — modified HTTP adapter (if HTTP detected)
2. `ws_adapter.py` — WebSocket adapter (if `has_websocket` is True)
3. `config_tool.py` — GUI configurator (HAR → .env, with Cookie update support)
4. `server.py` — from template (no changes needed)
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
4. `requirements.txt`: `fastapi`, `uvicorn`, `httpx`, `python-dotenv`, `pydantic`, `websockets`
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
| `supported_params` | `list[str]` | OpenAI params seen in HAR request bodies | `["temperature", "top_p"]` |
| `chat_entry_index` | `int` | Index in HAR entries list | `42` |
| `has_websocket` | `bool` | WebSocket entry detected in HAR | `True` or `False` |
| `ws.ws_url` | `str` | WebSocket URL (wss:// or ws://) | `wss://chat.example.com/ws` |
| `ws.send_template` | `dict` | JSON of first WS send frame | `{type: "chat", content: ""}` |
| `ws.input_field` | `str` | Key in send_template for user msg | `"content"` / `"messages"` |
| `ws.receive_field` | `str` | Key in receive frames for AI text | `"content"` / `"answer"` |
| `ws.receive_is_streaming` | `bool` | Multiple receives per single send | `True` / `False` |
| `ws.type_field` | `str` | Frame type discriminator key | `"type"` / `"event"` / `""` |
| `ws.extra_send_fields` | `dict` | Non-input keys to carry in every send | `{type: "chat", id: 1}` |
| `ws.init_messages` | `list[dict]` | Auth/init frames to replay on connect | `[{type: "auth", ...}]` |

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
| **WebSocket Generic** | `wss://.../ws` | send: `{content: "..."}` / `{messages: [...]}` | receive `content` | `receive_is_streaming` auto-detected |

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
- Parameters (`temperature`, `max_tokens`, ...) automatically detected from HAR — only those the target API actually uses will be passed through
- PoW solver is site-specific (DeepSeek pattern)
- Cookie-based auth expires; user must refresh HAR when needed
- WebSocket only supports text frames (opcode 1) — binary frames not supported
- WebSocket adapter requires `websockets` library (added to requirements)
- WS init messages are replayed on every new connection — ensure they are idempotent
