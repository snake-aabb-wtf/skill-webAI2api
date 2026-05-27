"""
web2api — OpenAI-compatible proxy for any web chat tool.
"""
import os
import json
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Union, Any

from adapter import ChatAdapter

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────
TARGET_URL = os.getenv("TARGET_URL", "https://chat.example.com")
COOKIES = os.getenv("COOKIES", "")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
API_KEY = os.getenv("API_KEY", "sk-web2api-placeholder")
DSML_ENABLED = os.getenv("DSML_ENABLED", "true").lower() in ("true", "1", "yes")

adapter = ChatAdapter(cookies=COOKIES, base_url=TARGET_URL, dsml_enabled=DSML_ENABLED)

# ── FastAPI App ─────────────────────────────────────────────────────
app = FastAPI(title="web2api", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic Models ─────────────────────────────────────────────────
class ContentPart(BaseModel):
    type: str
    text: Optional[str] = None


class ChatMessage(BaseModel):
    role: str
    content: Union[str, list[ContentPart]]


class ToolFunction(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[dict] = None

class Tool(BaseModel):
    type: str = "function"
    function: ToolFunction

class ChatCompletionRequest(BaseModel):
    model: str = MODEL_NAME
    messages: list[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: Optional[list[Tool]] = None
    tool_choice: Optional[Union[str, dict]] = None


# ── Auth middleware (optional) ──────────────────────────────────────
@app.middleware("http")
async def auth_middleware(request, call_next):
    # Simple API key check — disabled by default
    # if request.url.path.startswith("/v1/"):
    #     auth = request.headers.get("Authorization", "")
    #     if auth != f"Bearer {API_KEY}":
    #         return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await call_next(request)


# ── Endpoints ───────────────────────────────────────────────────────
@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "web2api",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    messages = [{"role": m.role, "content": m.content} for m in request.messages]
    stream = request.stream

    # Convert tools to dict if present
    tools_dict = None
    if request.tools:
        tools_dict = [t.model_dump() for t in request.tools]

    payload = adapter.convert_request(
        messages, stream=stream,
        tools=tools_dict,
        tool_choice=request.tool_choice,
    )

    if stream:
        return StreamingResponse(
            adapter.stream_request(payload),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        try:
            response = await adapter.send_request(payload)
            return adapter.convert_response(response)
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": f"Upstream error: {str(e)}",
                        "type": "upstream_error",
                        "code": 502,
                    }
                },
            )


@app.get("/health")
async def health():
    return {"status": "ok", "target": TARGET_URL, "model": MODEL_NAME}


# ── Main ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"web2api proxy running on http://{HOST}:{PORT}")
    print(f"  Target: {TARGET_URL}")
    print(f"  Model:  {MODEL_NAME}")
    print(f"  Stream: supported")
    print(f"  DSML:   {'enabled' if DSML_ENABLED else 'disabled'} (tool calling via prompt injection)")
    print(f"\n  Test with:")
    print(f'    curl http://localhost:{PORT}/v1/chat/completions \\')
    print(f'      -H "Content-Type: application/json" \\')
    print(f'      -d \'{{"model":"{MODEL_NAME}","messages":[{{"role":"user","content":"Hello"}}]}}\'')
    uvicorn.run(app, host=HOST, port=PORT)
