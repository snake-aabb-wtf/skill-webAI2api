import json
import time
import base64
import httpx
from typing import AsyncGenerator, Optional
from tool_dsml import (
    build_dsml_tool_prompt,
    has_dsml_content,
    parse_dsml_invoke,
    strip_dsml_tags,
)
from tool_sieve import StreamSieve


class ChatAdapter:
    """Adapter that converts between OpenAI format and the target chat API format."""

    def __init__(self, cookies: str, base_url: str, dsml_enabled: bool = True):
        self.headers = {
            "Cookie": cookies,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
            # TODO: add any additional headers from the captured request
            # "Authorization": f"Bearer {token}",
            # "Origin": base_url,
            # "Referer": f"{base_url}/",
        }
        self.base_url = base_url.rstrip("/")

        # TODO: set the actual chat endpoint path
        self.chat_endpoint = "/api/chat"

        # ── Auth challenge handling (PoW, etc.) ──────────────────────
        self.auth_type = "none"  # "none", "pow", "token_refresh"
        self._solver = None      # WASM solver instance (lazy init)

        if self.auth_type == "pow":
            pass

        # ── DSML (DeepSeek Markup Language) for tool calling ──────────
        self.dsml_enabled = dsml_enabled
        self.dsml_ready = False  # Set to True after DSML compatibility probe

    # ── Auth ─────────────────────────────────────────────────────────

    def _ensure_auth_headers(self) -> dict:
        """Return additional headers needed for this request (challenge response, etc.)."""
        if self.auth_type == "pow":
            challenge = self._fetch_challenge()
            answer = self._solve_challenge(challenge)
            return {"X-DS-PoW-Response": self._encode_pow_answer(challenge, answer)}
        if self.auth_type == "token_refresh":
            pass
        return {}

    def _fetch_challenge(self) -> dict:
        raise NotImplementedError

    def _solve_challenge(self, challenge: dict) -> int:
        raise NotImplementedError

    @staticmethod
    def _encode_pow_answer(challenge: dict, answer: int) -> str:
        raw = json.dumps({
            "algorithm": challenge.get("algorithm", "DeepSeekHashV1"),
            "challenge": challenge["challenge"],
            "salt": challenge["salt"],
            "answer": answer,
            "signature": challenge["signature"],
            "target_path": challenge["target_path"],
        }, separators=(",", ":"))
        return base64.b64encode(raw.encode()).decode()

    # ── DSML: prompt injection ──────────────────────────────────────

    def _inject_dsml_prompt(self, messages: list, tools: list,
                            tool_choice: Optional[str] = None) -> list:
        """Inject DSML tool calling instructions into the messages array."""
        if not self.dsml_enabled or not self.dsml_ready:
            return messages

        if tool_choice == "none":
            return messages

        dsml_prompt = build_dsml_tool_prompt(tools, tool_choice)
        if not dsml_prompt:
            return messages

        # Inject DSML prompt into the system message or create one
        result = list(messages)
        for i, msg in enumerate(result):
            if msg.get("role") == "system":
                result[i] = {**msg, "content": msg["content"] + "\n\n" + dsml_prompt}
                return result

        result.insert(0, {"role": "system", "content": dsml_prompt})
        return result

    # ── Request / Response conversion ───────────────────────────────

    def convert_request(self, messages: list, stream: bool = False,
                        tools: Optional[list] = None,
                        tool_choice: Optional[str] = None, **kwargs) -> dict:
        """Convert OpenAI-format messages to target API request format."""
        # Inject DSML tool prompt if tools are provided
        if tools:
            messages = self._inject_dsml_prompt(messages, tools, tool_choice)

        # TODO: analyze the captured request body and implement conversion here
        last_msg = messages[-1]["content"] if messages else ""
        if isinstance(last_msg, list):
            last_msg = " ".join(p.get("text", "") for p in last_msg if p.get("type") == "text")
        return {
            "prompt": last_msg,
            "stream": stream,
        }

    # ── DSML: response handling ─────────────────────────────────────

    def convert_with_dsml(self, full_text: str) -> dict:
        """Convert a response containing DSML tags into an OpenAI tool_calls response.

        If DSML tags are present, extracts tool calls and strips DSML tags from text.
        If no DSML tags, returns a normal text response.
        """
        if not has_dsml_content(full_text):
            return {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "gpt-4o",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": full_text},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

        tool_calls = parse_dsml_invoke(full_text)
        cleaned_text = strip_dsml_tags(full_text)

        response = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "gpt-4o",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": cleaned_text},
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

        if tool_calls:
            response["choices"][0]["message"]["tool_calls"] = tool_calls

        return response

    # ── Non-streaming request ───────────────────────────────────────

    async def send_request(self, payload: dict) -> dict:
        """Non-streaming request to target API.

        If DSML is enabled and the response contains DSML tags,
        automatically parses them into OpenAI tool_calls format.
        """
        auth_headers = self._ensure_auth_headers()
        async with httpx.AsyncClient(headers={**self.headers, **auth_headers}, timeout=120) as client:
            resp = await client.post(f"{self.base_url}{self.chat_endpoint}", json=payload)
            resp.raise_for_status()

            # Try to get the full response text for DSML detection
            try:
                data = resp.json()
                # Convert to string and check for DSML
                text = json.dumps(data, ensure_ascii=False)
                if self.dsml_enabled and self.dsml_ready and has_dsml_content(text):
                    # Response contains DSML — extract content field and parse
                    content = self._extract_content_from_json(data)
                    if content:
                        return self.convert_with_dsml(content)
                return self.convert_response(data)
            except Exception:
                text = await resp.aread()
                text = text.decode("utf-8")
                if self.dsml_enabled and self.dsml_ready and has_dsml_content(text):
                    return self.convert_with_dsml(text)
                return self.convert_response({"text": text})

    # ── Streaming request ───────────────────────────────────────────

    async def stream_request(self, payload: dict) -> AsyncGenerator[bytes, None]:
        """Streaming request to target API, yielding OpenAI-format SSE chunks.

        Uses StreamSieve to separate normal text from DSML tool call tags
        in real time.
        """
        auth_headers = self._ensure_auth_headers()
        use_sieve = self.dsml_enabled and self.dsml_ready

        async with httpx.AsyncClient(headers={**self.headers, **auth_headers}, timeout=120) as client:
            async with client.stream(
                "POST", f"{self.base_url}{self.chat_endpoint}", json=payload
            ) as resp:
                sieve = StreamSieve() if use_sieve else None
                current_event = ""

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue

                    if line.startswith("event: "):
                        current_event = line[7:]
                        continue

                    content = None
                    if line.startswith("data: "):
                        raw = line[6:]
                        if raw.strip() == "[DONE]":
                            if sieve:
                                flush_result = sieve.flush()
                                for text in flush_result.text_parts:
                                    if text:
                                        yield self._build_content_chunk(text)
                                    for tc in flush_result.tool_calls:
                                        yield from self._build_tool_calls_chunks(tc)
                            yield b"data: [DONE]\n\n"
                            return
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                    elif line.startswith("{"):
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                    else:
                        continue

                    content = self._extract_content_from_data(data)
                    if content is None:
                        continue

                    if sieve:
                        result = sieve.feed(content)
                        for text in result.text_parts:
                            if text:
                                yield self._build_content_chunk(text)
                        for tc in result.tool_calls:
                            yield from self._build_tool_calls_chunks(tc)
                        if result.pending:
                            continue
                    else:
                        if content:
                            yield self._build_content_chunk(content)

                if sieve:
                    flush_result = sieve.flush()
                    for text in flush_result.text_parts:
                        if text:
                            yield self._build_content_chunk(text)
                    for tc in flush_result.tool_calls:
                        yield from self._build_tool_calls_chunks(tc)

                yield b"data: [DONE]\n\n"

    # ── Content extraction helpers ───────────────────────────────────

    def _extract_content_from_data(self, data: dict) -> Optional[str]:
        """Extract text content from a parsed SSE data dict."""
        if not isinstance(data, dict):
            return None
        if "v" in data and isinstance(data["v"], str):
            return data["v"]
        elif "v" in data and isinstance(data["v"], dict):
            return data["v"].get("response", {}).get("content", "")
        elif data.get("o") == "APPEND":
            return data.get("v", "")
        else:
            return data.get("content") or data.get("text") or data.get("answer") or None

    def _extract_content_from_json(self, data: dict) -> Optional[str]:
        """Extract text content from a non-streaming JSON response."""
        # Try common fields
        for key in ["answer", "text", "content", "reply", "response", "output", "completion"]:
            val = data.get(key)
            if isinstance(val, str) and len(val) > 0:
                return val
        # Walk nested objects
        if "choices" in data and isinstance(data["choices"], list):
            for choice in data["choices"]:
                msg = choice.get("message", {})
                val = msg.get("content")
                if val:
                    return val
        if "data" in data and isinstance(data["data"], dict):
            return self._extract_content_from_json(data["data"])
        return None

    def _build_content_chunk(self, text: str) -> bytes:
        """Build a text delta as an OpenAI SSE chunk."""
        chunk = {"choices": [{"delta": {"content": text}, "index": 0}]}
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    def _build_tool_calls_chunks(self, tc: dict):
        """Yield SSE chunks for a single tool call, ending with finish_reason."""
        chunk1 = {
            "choices": [{
                "delta": {"tool_calls": [tc]},
                "index": 0,
            }],
        }
        yield f"data: {json.dumps(chunk1, ensure_ascii=False)}\n\n".encode()
        chunk2 = {
            "choices": [{
                "delta": {},
                "index": 0,
                "finish_reason": "tool_calls",
            }],
        }
        yield f"data: {json.dumps(chunk2, ensure_ascii=False)}\n\n".encode()

    def convert_response(self, response: dict) -> dict:
        """Convert target API non-streaming response to OpenAI format."""
        # If DSML is enabled, try to detect DSML content
        if self.dsml_enabled and self.dsml_ready:
            text = json.dumps(response, ensure_ascii=False)
            if has_dsml_content(text):
                # Find the actual content field
                content = self._extract_content_from_json(response)
                if content and has_dsml_content(content):
                    return self.convert_with_dsml(content)

        content = response.get("answer") or response.get("text") or json.dumps(response)
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "gpt-4o",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
