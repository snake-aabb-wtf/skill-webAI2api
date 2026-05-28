import json
import re
from urllib.parse import urlparse, parse_qs
from typing import Optional


class HarAnalysis:
    """Result of analyzing a HAR file."""

    def __init__(self):
        self.base_url: str = ""
        self.chat_endpoint: str = "/api/chat"
        self.headers: dict[str, str] = {}
        self.cookies: str = ""
        self.auth_header: Optional[str] = None
        self.auth_type: str = "none"
        self.request_body_template: dict = {}
        self.content_field_path: str = "choices[0].message.content"
        self.is_streaming: bool = False
        self.sse_event_type: str = ""
        self.sse_data_field: str = "v"
        self.sse_format: str = "plain_token"
        self.has_pow: bool = False
        self.pow_endpoint: str = ""
        self.supported_params: list[str] = []
        self.has_websocket: bool = False
        self.ws: Optional["WsAnalysis"] = None
        self.all_endpoints: list[str] = []
        self.chat_entry_index: int = -1


class WsAnalysis:
    """Result of analyzing WebSocket messages in a HAR file."""

    def __init__(self):
        self.ws_url: str = ""
        self.entry_index: int = -1
        self.send_template: dict = {}
        self.input_field: str = "content"
        self.receive_field: str = "content"
        self.receive_is_streaming: bool = False
        self.type_field: str = ""
        self.extra_send_fields: dict = {}
        self.init_messages: list[dict] = []


CHAT_ENDPOINT_KEYWORDS = [
    "/api/chat", "/chat/completions", "/v1/chat/completions",
    "/api/conversation", "/api/generate", "/api/completion",
    "/api/send", "/api/message", "/api/ask", "/api/stream",
    "/chat", "/conversation", "/api/v1/chat",
    "/api/v0/chat/completion", "/api/v0/chat_session",
    "/api/rp/chat", "/api/rp/conversation",
]

CHALLENGE_KEYWORDS = [
    "challenge", "pow", "captcha", "turnstile", "token/refresh",
    "create_pow", "auth/challenge",
]

EXCLUDE_KEYWORDS = [
    "analytics", "collect", "log", "tracking", "telemetry",
    "favicon", "static", ".js", ".css", ".png", ".jpg", ".gif",
    ".svg", ".woff", ".woff2", ".ico", "hot-update",
]


def _score_entry(entry: dict) -> int:
    """Score a HAR entry to determine how likely it is the chat API call."""
    req = entry.get("request", {})
    method = req.get("method", "")
    url = req.get("url", "")
    path = urlparse(url).path.lower()

    if method != "POST":
        return -100

    for exclude in EXCLUDE_KEYWORDS:
        if exclude in path:
            return -50

    score = 0

    for kw in CHAT_ENDPOINT_KEYWORDS:
        if kw in path:
            score += 50

    post_data = req.get("postData", {})
    text = post_data.get("text", "") or post_data.get("params", "")
    try:
        body = json.loads(text) if isinstance(text, str) else {}
    except (json.JSONDecodeError, TypeError):
        body = {}

    if isinstance(body, dict):
        if "messages" in body:
            score += 40
            msgs = body.get("messages", [])
            if isinstance(msgs, list) and len(msgs) > 0:
                score += 10
                last = msgs[-1]
                if isinstance(last, dict) and last.get("content"):
                    score += 10
        if "prompt" in body:
            score += 30
        if "stream" in body:
            score += 10
        if "model" in body:
            score += 10
        if "max_tokens" in body or "temperature" in body:
            score += 5

    resp = entry.get("response", {})
    content_type = ""
    resp_headers = resp.get("headers", [])
    for h in resp_headers:
        if h.get("name", "").lower() == "content-type":
            content_type = h.get("value", "")

    if "text/event-stream" in content_type:
        score += 30
    if "application/json" in content_type:
        score += 15

    resp_text = resp.get("content", {}).get("text", "")
    if isinstance(resp_text, str) and len(resp_text) > 50:
        score += 5

    return score


def _find_chat_entry(entries: list) -> Optional[int]:
    """Find the entry most likely to be the chat API call."""
    best_score = -1000
    best_idx = -1
    for i, entry in enumerate(entries):
        score = _score_entry(entry)
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx if best_idx >= 0 else None


def _extract_cookies(req_headers: list[dict]) -> str:
    """Extract Cookie string from request headers."""
    for h in req_headers:
        if h.get("name", "").lower() == "cookie":
            return h.get("value", "")
    return ""


def _extract_header_value(req_headers: list[dict], name: str) -> str:
    """Extract a specific header value by name (case-insensitive)."""
    name_lower = name.lower()
    for h in req_headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _extract_all_headers(req_headers: list[dict]) -> dict:
    """Extract all request headers as a dict, filtering out pseudo-headers."""
    result = {}
    skip = {":method", ":path", ":authority", ":scheme"}
    for h in req_headers:
        name = h.get("name", "")
        if name.lower() not in skip:
            result[name] = h.get("value", "")
    return result


def _analyze_response_structure(entry: dict) -> dict:
    """Analyze the response to extract content field path and SSE info."""
    resp = entry.get("response", {})
    content_type = ""
    resp_headers = resp.get("headers", [])
    for h in resp_headers:
        if h.get("name", "").lower() == "content-type":
            content_type = h.get("value", "")

    result = {
        "is_streaming": "text/event-stream" in content_type,
        "sse_event_type": "",
        "sse_data_field": "content",
        "sse_format": "plain_token",
        "content_field_path": "",
    }

    resp_text = resp.get("content", {}).get("text", "")

    if result["is_streaming"]:
        sse_info = _analyze_sse_text(resp_text)
        result.update(sse_info)
    elif resp_text:
        try:
            data = json.loads(resp_text)
            field = _find_content_field(data)
            result["content_field_path"] = field
        except json.JSONDecodeError:
            result["content_field_path"] = ""

    return result


def _analyze_sse_text(text: str) -> dict:
    """Analyze SSE text to determine event type and data field."""
    lines = text.split("\n")
    events = []
    current_event = ""
    has_data = False
    for line in lines[:30]:
        line = line.strip()
        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: "):
            has_data = True
            raw = line[6:]
            try:
                data = json.loads(raw)
                events.append((current_event, data))
            except json.JSONDecodeError:
                events.append((current_event, raw))
            current_event = ""

    result = {"sse_event_type": "", "sse_data_field": "content", "sse_format": "plain_token"}

    if not has_data:
        return result

    non_json_count = sum(1 for _, d in events if isinstance(d, str) and d.strip())
    json_count = len(events) - non_json_count

    # If all data lines are non-JSON plain text → raw_text format
    if json_count == 0 and non_json_count > 0:
        result["sse_format"] = "raw_text"
        result["sse_data_field"] = ""
        return result

    for event_type, data in events:
        if isinstance(data, dict):
            if "p" in data and "o" in data and "v" in data:
                result["sse_format"] = "path_op_value"
                result["sse_data_field"] = "v"
                result["sse_event_type"] = event_type
                return result

            # choices[0].delta.content nested pattern (ChatGPT Next Web style)
            choices = data.get("choices")
            if isinstance(choices, list) and len(choices) > 0:
                delta = choices[0].get("delta", {})
                if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                    result["sse_data_field"] = "choices[0].delta.content"
                    result["sse_format"] = "nested"
                    result["sse_event_type"] = event_type
                    return result

            for key in ["v", "content", "text", "answer", "delta", "token"]:
                val = data.get(key)
                if isinstance(val, str) and len(val) > 0:
                    result["sse_data_field"] = key
                    result["sse_format"] = "plain_token"
                    result["sse_event_type"] = event_type
                    return result
                if isinstance(val, dict):
                    sub = val.get("content", "") or val.get("text", "")
                    if sub:
                        result["sse_data_field"] = f"{key}.content"
                        result["sse_format"] = "nested"
                        result["sse_event_type"] = event_type
                        return result

    return result


def _find_content_field(data, path="") -> str:
    """Find the most likely content field in a JSON response."""
    best_score = 0
    best_path = ""

    def walk(obj, current_path):
        nonlocal best_score, best_path
        if isinstance(obj, dict):
            for k, v in obj.items():
                new_path = f"{current_path}.{k}" if current_path else k
                if isinstance(v, str) and len(v) > 20:
                    score = 0
                    for kw in ["content", "answer", "reply", "text",
                               "response", "output", "completion", "message"]:
                        if kw in new_path.lower():
                            score += 3
                    depth = new_path.count(".") + new_path.count("[")
                    score += depth * 0.5
                    if score > best_score:
                        best_score = score
                        best_path = new_path
                if isinstance(v, (dict, list)):
                    walk(v, new_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                walk(item, f"{current_path}[{i}]")

    walk(data, path)
    return best_path


def _find_challenge_entries(entries: list) -> list:
    """Find entries related to auth challenges (PoW, etc.)."""
    challenges = []
    for entry in entries:
        req = entry.get("request", {})
        url = req.get("url", "")
        path = urlparse(url).path.lower()
        for kw in CHALLENGE_KEYWORDS:
            if kw in path:
                challenges.append({
                    "url": url,
                    "method": req.get("method", ""),
                    "type": "pow" if "pow" in path or "challenge" in path else "unknown",
                })
                break
    return challenges


# ── WebSocket detection ─────────────────────────────────────────────

WS_CHAT_KEYWORDS = [
    "message", "content", "prompt", "text", "query", "ask",
    "input", "conversation", "chat", "send", "talk",
]

def _find_websocket_entry(entries: list) -> Optional[dict]:
    """Find a HAR entry that upgraded to WebSocket (HTTP 101)
    and carries chat-like messages in _webSocketMessages.

    Returns a dict with keys: index, ws_url, messages, init_messages
    or None if no suitable WS entry found.
    """
    for i, entry in enumerate(entries):
        resp = entry.get("response", {})
        if resp.get("status") != 101:
            continue

        ws_messages = entry.get("_webSocketMessages", [])
        if not isinstance(ws_messages, list) or len(ws_messages) < 2:
            continue

        sends = [m for m in ws_messages if m.get("type") == "send"]
        receives = [m for m in ws_messages if m.get("type") == "receive"]
        if not sends or not receives:
            continue

        # Check opcode — we only support text frames (opcode 1)
        for m in ws_messages:
            if m.get("opcode") not in (None, 1):
                continue

        # Score: does this look like a chat conversation?
        chat_score = 0
        for m in sends:
            raw = m.get("data", "")
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    keys = " ".join(d.keys()).lower()
                    if any(kw in keys for kw in WS_CHAT_KEYWORDS):
                        chat_score += 2
                    if "stream" in d:
                        chat_score += 1
                    # Long string value → user message
                    for v in d.values():
                        if isinstance(v, str) and len(v) > 10:
                            chat_score += 2
            except (json.JSONDecodeError, TypeError):
                continue

        for m in receives:
            raw = m.get("data", "")
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    for f in WS_CHAT_KEYWORDS:
                        val = d.get(f)
                        if isinstance(val, str) and len(val) > 10:
                            chat_score += 2
                            break
            except (json.JSONDecodeError, TypeError):
                continue

        if chat_score < 3:
            continue

        # Passed all checks — this is a chat WebSocket
        req = entry.get("request", {})
        req_url = req.get("url", "")

        # Build template from first send message
        first_send = sends[0]
        try:
            send_template = json.loads(first_send.get("data", "{}"))
        except (json.JSONDecodeError, TypeError):
            send_template = {}

        # Find init messages (sends before the first meaningful chat message)
        init_msgs = []
        for m in ws_messages:
            if m.get("type") != "send":
                continue
            raw = m.get("data", "")
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    # If body has no chat-like key, it's probably an init/connect message
                    keys = " ".join(d.keys()).lower()
                    if not any(kw in keys for kw in WS_CHAT_KEYWORDS):
                        init_msgs.append(d)
            except (json.JSONDecodeError, TypeError):
                continue

        req_parsed = urlparse(req_url)
        ws_scheme = "wss" if req_parsed.scheme == "https" else "ws"
        ws_url = f"{ws_scheme}://{req_parsed.netloc}{req_parsed.path}"
        if req_parsed.query:
            ws_url += "?" + req_parsed.query

        return {
            "index": i,
            "ws_url": ws_url,
            "messages": ws_messages,
            "send_template": send_template,
            "sends": sends,
            "receives": receives,
            "init_messages": init_msgs,
        }

    return None


def _analyze_websocket(ws_info: dict) -> WsAnalysis:
    """Analyze WebSocket messages to extract send/receive patterns."""
    ws = WsAnalysis()
    ws.ws_url = ws_info["ws_url"]
    ws.entry_index = ws_info["index"]
    ws.send_template = ws_info.get("send_template", {})
    ws.init_messages = ws_info.get("init_messages", [])

    # Find input field in send template
    send_template = ws.send_template
    if "messages" in send_template:
        ws.input_field = "messages"
    elif "prompt" in send_template:
        ws.input_field = "prompt"
    elif "content" in send_template:
        ws.input_field = "content"
    elif "query" in send_template:
        ws.input_field = "query"
    elif "message" in send_template:
        ws.input_field = "message"
    elif "text" in send_template:
        ws.input_field = "text"
    elif "input" in send_template:
        ws.input_field = "input"
    else:
        # Find the longest string field
        max_len = 0
        for k, v in send_template.items():
            if isinstance(v, str) and len(v) > max_len:
                max_len = len(v)
                ws.input_field = k
        if not ws.input_field:
            ws.input_field = "content"

    # Find content field in receive messages
    receives = ws_info.get("receives", [])
    receive_texts = []
    for m in receives:
        raw = m.get("data", "")
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                receive_texts.append(d)
        except (json.JSONDecodeError, TypeError):
            receive_texts.append({"__raw_text__": raw})

    # Detect streaming: multiple receives for one send
    ws.receive_is_streaming = len(receives) >= 3

    # Find content field in first receive
    if receive_texts:
        first = receive_texts[0]
        for key in ["content", "answer", "reply", "text", "response",
                     "output", "completion", "message", "data"]:
            if key in first:
                val = first[key]
                if isinstance(val, str) and len(val) > 0:
                    ws.receive_field = key
                    break
                if isinstance(val, dict):
                    sub = val.get("content", "") or val.get("text", "")
                    if sub:
                        ws.receive_field = f"{key}.content"
                        break
        else:
            # Deep search
            path = _find_content_field(first)
            if path:
                ws.receive_field = path

    # Detect type/event field that distinguishes message types
    seen_types = set()
    for d in receive_texts:
        if isinstance(d, dict):
            for tkey in ("type", "event", "action", "method"):
                val = d.get(tkey)
                if isinstance(val, str):
                    seen_types.add(val)
                    ws.type_field = tkey
    if len(seen_types) <= 1:
        ws.type_field = ""

    # Detect extra fields that should be carried over in every send
    if send_template:
        ws.extra_send_fields = {
            k: v for k, v in send_template.items()
            if k != ws.input_field and k != "stream"
        }

    return ws


# Parameters known from OpenAI spec that a target API might support.
# Key = the name used in the target API's request body.
# Value = the OpenAI canonical name (used in SKILL.md and user-facing docs).
# Multiple possible key names map to the same OpenAI concept.
PARAM_ALIASES = {
    "temperature": "temperature",
    "max_tokens": "max_tokens",
    "max_length": "max_tokens",
    "max_new_tokens": "max_tokens",
    "top_p": "top_p",
    "top_k": "top_k",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
    "repetition_penalty": "repetition_penalty",
    "stop": "stop",
    "stop_sequences": "stop",
    "n": "n",
    "seed": "seed",
    "user": "user",
    "system": "system",
    "system_prompt": "system",
}


def _infer_supported_params(entries: list, chat_idx: int) -> list[str]:
    """Scan all chat-like POST requests in the HAR to find which
    OpenAI-compatible parameters the target API actually supports.

    Strategy:
      1. Collect all POST entries whose URL path matches
         the primary chat entry's endpoint.
      2. For each entry, extract the JSON request body keys.
      3. Intersect with PARAM_ALIASES — if a key appears in any
         request with a non-null value, mark the canonical param
         as supported.

    This is zero-cost: it reads what the browser already sent,
    without sending any extra probe requests.
    """
    chat_entry = entries[chat_idx]
    chat_req = chat_entry.get("request", {})
    endpoint_path = urlparse(chat_req.get("url", "")).path

    seen_values = {}  # canonical param name → set of distinct values seen

    for entry in entries:
        req = entry.get("request", {})
        if req.get("method") != "POST":
            continue
        path = urlparse(req.get("url", "")).path
        if path != endpoint_path:
            continue

        post_data = req.get("postData", {})
        text = post_data.get("text", "")
        try:
            body = json.loads(text) if text else {}
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(body, dict):
            continue

        for body_key, body_val in body.items():
            canonical = PARAM_ALIASES.get(body_key)
            if canonical is None:
                continue
            if canonical not in seen_values:
                seen_values[canonical] = set()
            # Treat None and 0 as "not really set"
            if body_val is not None and body_val != 0:
                val_str = str(body_val)
                seen_values[canonical].add(val_str)

    result = []
    for canonical, values in seen_values.items():
        if len(values) >= 1:
            result.append(canonical)

    return result


def parse_har(har_path: str) -> HarAnalysis:
    """Parse a HAR file and extract all information needed for the adapter.

    Args:
        har_path: Path to the .har file

    Returns:
        HarAnalysis object with all extracted information
    """
    with open(har_path, "r", encoding="utf-8") as f:
        har = json.load(f)

    entries = har.get("log", {}).get("entries", [])

    if not entries:
        raise ValueError("HAR file has no entries")

    analysis = HarAnalysis()

    chat_idx = _find_chat_entry(entries)
    if chat_idx is None:
        raise ValueError("Could not identify the chat API request in the HAR file")

    analysis.chat_entry_index = chat_idx
    chat_entry = entries[chat_idx]
    req = chat_entry.get("request", {})
    url = req.get("url", "")
    parsed = urlparse(url)

    analysis.base_url = f"{parsed.scheme}://{parsed.netloc}"
    analysis.chat_endpoint = parsed.path
    if parsed.query:
        analysis.chat_endpoint += "?" + parsed.query

    req_headers = req.get("headers", [])
    analysis.headers = _extract_all_headers(req_headers)
    analysis.cookies = _extract_cookies(req_headers)

    auth_val = _extract_header_value(req_headers, "authorization")
    if auth_val:
        analysis.auth_header = auth_val
        if auth_val.startswith("Bearer "):
            analysis.auth_type = "oauth"

    post_data = req.get("postData", {})
    text = post_data.get("text", "")
    try:
        body = json.loads(text) if text else {}
    except json.JSONDecodeError:
        body = {}

    if isinstance(body, dict):
        analysis.request_body_template = body

    analysis.supported_params = _infer_supported_params(entries, chat_idx)

    resp_struct = _analyze_response_structure(chat_entry)
    analysis.is_streaming = resp_struct["is_streaming"]
    analysis.sse_event_type = resp_struct["sse_event_type"]
    analysis.sse_data_field = resp_struct["sse_data_field"]
    analysis.sse_format = resp_struct["sse_format"]
    analysis.content_field_path = resp_struct["content_field_path"]

    challenges = _find_challenge_entries(entries)
    for ch in challenges:
        if ch["type"] == "pow":
            analysis.has_pow = True
            analysis.pow_endpoint = urlparse(ch["url"]).path

    if not analysis.cookies and not analysis.auth_header:
        for entry in entries[:chat_idx]:
            resp = entry.get("response", {})
            resp_cookies = resp.get("cookies", [])
            if resp_cookies:
                parts = []
                for c in resp_cookies:
                    name = c.get("name", "")
                    value = c.get("value", "")
                    if name and value:
                        parts.append(f"{name}={value}")
                if parts:
                    analysis.cookies = "; ".join(parts)

    # ── WebSocket detection ──────────────────────────────────────
    ws_info = _find_websocket_entry(entries)
    if ws_info is not None:
        analysis.has_websocket = True
        analysis.ws = _analyze_websocket(ws_info)

    seen = set()
    for entry in entries:
        url = entry.get("request", {}).get("url", "")
        path = urlparse(url).path
        if path and path not in seen:
            seen.add(path)
            analysis.all_endpoints.append(path)

    return analysis
