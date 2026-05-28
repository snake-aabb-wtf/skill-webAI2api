import json
import time
import asyncio
import websockets
from typing import AsyncGenerator, Optional
from tool_dsml import (
    build_dsml_tool_prompt,
    has_dsml_content,
    parse_dsml_invoke,
    strip_dsml_tags,
)
from tool_sieve import StreamSieve


class WebSocketChatAdapter:
    """Adapter that converts between OpenAI format and a target WebSocket chat API.

    Lifecycle:
      - __init__: stores config, does NOT connect
      - For each request: _connect() → send frame → receive frame(s) → close
      - If the HAR shows a persistent connection pattern (multiple exchanges
        on one WS), override _get_connection() to reuse.
    """

    def __init__(
        self,
        ws_url: str,
        headers: dict,
        dsml_enabled: bool = True,
    ):
        self.ws_url = ws_url
        self.headers = headers

        # ── Send side ───────────────────────────────────────────────
        # Template of the JSON frame to send (from HAR first send message).
        # AI will fill this from analysis.ws.send_template.
        self.send_template: dict = {}
        # Which field in send_template holds the user message.
        self.input_field: str = "content"
        # Extra fields carried over from HAR (session_id, type, etc.).
        self.extra_send_fields: dict = {}

        # ── Receive side ─────────────────────────────────────────────
        # Dot-path to content in received JSON frames.
        self.receive_field: str = "content"
        # True if multiple receive frames arrive per single send.
        self.receive_is_streaming: bool = False
        # If the API uses a type/event field to tag different messages.
        self.type_field: str = ""

        # ── Init messages ────────────────────────────────────────────
        # Messages to send right after connect (auth, init, etc.).
        self.init_messages: list[dict] = []

        # ── DSML ─────────────────────────────────────────────────────
        self.dsml_enabled = dsml_enabled
        self.dsml_ready = False

    # ── Connection ───────────────────────────────────────────────────

    async def _connect(self) -> websockets.WebSocketClientProtocol:
        """Open a new WebSocket connection.

        Targets requiring specific subprotocols or extra headers
        can override this method.
        """
        ws = await websockets.connect(
            self.ws_url,
            extra_headers=self.headers,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        )
        # Send init messages if any
        for msg in self.init_messages:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        return ws

    # ── DSML ─────────────────────────────────────────────────────────

    def _inject_dsml_prompt(self, messages: list, tools: list,
                            tool_choice: Optional[str] = None) -> list:
        if not self.dsml_enabled or not self.dsml_ready:
            return messages
        if tool_choice == "none":
            return messages
        dsml_prompt = build_dsml_tool_prompt(tools, tool_choice)
        if not dsml_prompt:
            return messages
        result = list(messages)
        for i, msg in enumerate(result):
            if msg.get("role") == "system":
                result[i] = {**msg, "content": msg["content"] + "\n\n" + dsml_prompt}
                return result
        result.insert(0, {"role": "system", "content": dsml_prompt})
        return result

    # ── Request conversion ──────────────────────────────────────────

    def convert_request(self, messages: list, stream: bool = False,
                        tools: Optional[list] = None,
                        tool_choice: Optional[str] = None, **kwargs) -> dict:
        """Build the JSON frame to send over WebSocket.

        Uses send_template as the base, replaces the input field
        with the last user message, and carries over extra fields.
        """
        if tools and self.dsml_enabled and self.dsml_ready:
            if tool_choice != "none":
                dsml_prompt = build_dsml_tool_prompt(tools, tool_choice)
                messages = self._inject_dsml_prompt(messages, dsml_prompt)

        payload = dict(self.extra_send_fields)

        last = messages[-1]["content"] if messages else ""
        if isinstance(last, list):
            last = " ".join(p.get("text", "") for p in last if p.get("type") == "text")

        if self.input_field == "messages":
            payload["messages"] = messages
        else:
            payload[self.input_field] = last

        if stream:
            payload["stream"] = True

        return payload

    # ── Non-streaming request ───────────────────────────────────────

    async def send_request(self, payload: dict) -> dict:
        """Send a frame over WebSocket, wait for the complete response,
        return OpenAI-format dict.

        For non-streaming: connect → send → collect receive frames
        until a "done" signal → parse → return.
        """
        ws = await self._connect()
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))

            full_text = ""
            done = False
            while not done:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                text = self._extract_content_from_frame(raw)
                if text is not None:
                    full_text += text

                # Check for done signal
                if self._is_done_frame(raw):
                    done = True

            return self.convert_response({"text": full_text})
        finally:
            await ws.close()

    # ── Streaming request ───────────────────────────────────────────

    async def stream_request(self, payload: dict) -> AsyncGenerator[bytes, None]:
        """Send a frame over WebSocket, yield each received frame
        as an OpenAI SSE chunk.

        Uses StreamSieve for DSML tool call separation if enabled.
        """
        ws = await self._connect()
        use_sieve = self.dsml_enabled and self.dsml_ready
        sieve = StreamSieve() if use_sieve else None

        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))

            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")

                text = self._extract_content_from_frame(raw)
                if text is None:
                    continue

                if sieve:
                    result = sieve.feed(text)
                    for t in result.text_parts:
                        if t:
                            yield self._build_content_chunk(t)
                    for tc in result.tool_calls:
                        yield from self._build_tool_calls_chunks(tc)
                    if result.pending:
                        continue
                else:
                    if text:
                        yield self._build_content_chunk(text)

                if self._is_done_frame(raw):
                    break

            if sieve:
                flush_result = sieve.flush()
                for t in flush_result.text_parts:
                    if t:
                        yield self._build_content_chunk(t)
                for tc in flush_result.tool_calls:
                    yield from self._build_tool_calls_chunks(tc)

            yield b"data: [DONE]\n\n"
        finally:
            await ws.close()

    # ── Frame analysis ──────────────────────────────────────────────

    def _extract_content_from_frame(self, raw: str) -> Optional[str]:
        """Extract the AI response text from a single WebSocket frame.

        Delegates to the same logic as HTTP SSE extraction but
        from a raw frame string.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw if raw.strip() else None

        if not isinstance(data, dict):
            return None

        # If there's a type/event field, skip non-content frames
        if self.type_field:
            msg_type = data.get(self.type_field, "")
            # Skip frames whose type clearly isn't content
            skip_types = {"error", "status", "ping", "pong", "ack", "connected",
                          "init", "auth", "join", "leave", "typing"}
            if msg_type in skip_types:
                return None

        # Navigate receive_field path
        path = self.receive_field
        if path:
            import functools
            try:
                parts = path.replace("[", ".").replace("]", "").split(".")
                val = functools.reduce(
                    lambda d, k: d[int(k) if k.isdigit() else k]
                    if isinstance(d, (dict, list)) else None,
                    parts, data,
                )
                if isinstance(val, str) and val.strip():
                    return val
            except (KeyError, IndexError, TypeError):
                pass

        # Fallback: common keys
        for key in ("content", "text", "answer", "v", "response",
                     "reply", "output", "message", "token", "delta"):
            if key in data:
                val = data[key]
                if isinstance(val, str) and val.strip():
                    return val
                if isinstance(val, dict):
                    sub = val.get("content") or val.get("text")
                    if sub:
                        return sub
        return None

    def _is_done_frame(self, raw: str) -> bool:
        """Check if a frame signals the end of a response."""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                # Finish reason present
                if data.get("finish_reason") in ("stop", "length", "tool_calls"):
                    return True
                # Explicit done flag
                if data.get("done") is True or data.get("is_finish") is True:
                    return True
                # Stop signal
                if data.get("type") in ("done", "finish", "complete", "stop"):
                    return True
                # Choices with finish_reason
                choices = data.get("choices", [])
                if isinstance(choices, list) and choices:
                    if choices[0].get("finish_reason") in ("stop", "length"):
                        return True
        except (json.JSONDecodeError, TypeError):
            pass
        return raw.strip() == "[DONE]"

    # ── Response formatting ─────────────────────────────────────────

    def _build_content_chunk(self, text: str) -> bytes:
        chunk = {"choices": [{"delta": {"content": text}, "index": 0}]}
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    def _build_tool_calls_chunks(self, tc: dict):
        chunk1 = {
            "choices": [{"delta": {"tool_calls": [tc]}, "index": 0}],
        }
        yield f"data: {json.dumps(chunk1, ensure_ascii=False)}\n\n".encode()
        chunk2 = {
            "choices": [{"delta": {}, "index": 0, "finish_reason": "tool_calls"}],
        }
        yield f"data: {json.dumps(chunk2, ensure_ascii=False)}\n\n".encode()

    def convert_response(self, response: dict) -> dict:
        if self.dsml_enabled and self.dsml_ready:
            text = json.dumps(response, ensure_ascii=False)
            if has_dsml_content(text):
                content = response.get("text") or response.get("content") or text
                if content and has_dsml_content(content):
                    return self._convert_with_dsml(content)

        content = response.get("text") or response.get("content") or json.dumps(response)
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

    def _convert_with_dsml(self, full_text: str) -> dict:
        if not has_dsml_content(full_text):
            return self.convert_response({"text": full_text})
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
