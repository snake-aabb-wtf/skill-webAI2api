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
        self.all_endpoints: list[str] = []
        self.chat_entry_index: int = -1


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
        "sse_format": "plain",
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
    for line in lines[:30]:
        line = line.strip()
        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: "):
            raw = line[6:]
            try:
                data = json.loads(raw)
                events.append((current_event, data))
            except json.JSONDecodeError:
                events.append((current_event, raw))
            current_event = ""

    result = {"sse_event_type": "", "sse_data_field": "content", "sse_format": "plain"}

    for event_type, data in events:
        if isinstance(data, dict):
            if "p" in data and "o" in data and "v" in data:
                # DeepSeek format: {"p": "...", "o": "APPEND", "v": "..."}
                result["sse_format"] = "path_op_value"
                result["sse_data_field"] = "v"
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

    seen = set()
    for entry in entries:
        url = entry.get("request", {}).get("url", "")
        path = urlparse(url).path
        if path and path not in seen:
            seen.add(path)
            analysis.all_endpoints.append(path)

    return analysis
