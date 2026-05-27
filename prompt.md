# web2api — 全自动逆向网页 AI 对话 → OpenAI 兼容 API

你是一个全自动逆向工程专家。输入一个 AI 对话网页的 URL + Cookie，你将自动探测、分析、适配，最终生成一个 OpenAI 兼容的代理服务器。

## 用户只需提供

```
URL:     https://chat.example.com           # 对话页面地址
Cookie:  __session=xxx; token=yyy           # 登录后的 Cookie（关键鉴权信息）
```

**可选补充：**
- `测试问题`: "你好，请介绍一下自己"（默认用 "Hello"）
- `模型名`: 映射成什么模型名（默认 `gpt-4o`）

---

## 自动化工作流（AI 全权执行）

### Step 0: 浏览器 DevTools 分析

先引导用户在浏览器里发一条消息，从 Network 面板捕获真正的 API 请求：

```python
print("请用户在浏览器 F12 → Network 中:")
print("  1. 发一条消息")
print("  2. 找到聊天请求（XHR 或 Fetch 类型）")
print("  3. 提供: 请求 URL、请求体、响应头 Content-Type、完整 Cookie 和 Authorization")
```

**从捕获的请求中提取关键信息：**

| 信息 | 来源 |
|---|---|
| 聊天 API 端点 | Request URL |
| 请求格式（payload） | Request Body |
| 响应格式 / SSE 结构 | Response 或 Response Stream |
| 鉴权方式 | Request Headers（Cookie、Authorization、X-*） |
| **额外鉴权 / 挑战** | 检查聊天请求之前是否有前置请求（如 PoW challenge、token 刷新） |

### Step 1: 自动探测 API 端点

用 `httpx` 对目标网站进行主动探测，找到真正的聊天 API 接口。

```python
import httpx

headers = {
    "Cookie": COOKIES,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
}

async def probe_endpoints(base_url, headers):
    """扫描常见的聊天 API 路径，找出哪个返回了预期响应。"""
    candidates = [
        "/api/chat", "/api/chat/completions", "/api/conversation",
        "/api/generate", "/api/completion", "/v1/chat/completions",
        "/api/send", "/api/message", "/api/ask", "/api/stream",
        "/chat", "/conversation", "/api/v1/chat", "/api/chat/send",
        # v0 API paths (DeepSeek, others)
        "/api/v0/chat/completion", "/api/v0/chat_session/create",
        "/api/v0/chat/create_pow_challenge",
    ]
    # 第1轮: 用简单 payload 发 POST，看哪个不返回 404
    for path in candidates:
        try:
            url = f"{base_url}{path}"
            resp = await client.post(url, json={"prompt": "Hello"}, timeout=10)
            if resp.status_code not in (404, 405):
                print(f"[HIT] {url} -> {resp.status_code}")
                # 保存命中结果
        except Exception:
            continue
```

**探测策略（按优先级）：**

1. **DevTools 直接捕获**（最快最准）— 用户 F12 发一条消息，直接拿到 endpoint
2. **静态路径扫描** — 对 20+ 个常见聊天 API 路径发 POST，过滤 404/405
3. **页面源码分析** — `GET` 目标 URL，从 HTML 中搜索 `fetch(`、`axios.post(`、`api/`、`/chat` 等关键字，提取潜在 endpoint
4. **Payload 格式探针** — 对命中的 endpoint，用 5 种不同 payload 格式测试，找出哪个返回有效内容：
5. **前置请求探测** — 检查 `/api/*/create_pow_challenge`、`/api/auth/*`、`/api/refresh` 等前置鉴权路径

```python
payload_templates = [
    {"prompt": "Hello"},
    {"messages": [{"role": "user", "content": "Hello"}]},
    {"query": "Hello", "history": []},
    {"content": "Hello"},
    {"text": "Hello"},
]
```

6. **流式检测** — 对成功的 endpoint 加 `stream: true`，检查 `Content-Type` 是否为 `text/event-stream`

### Step 1.5: 自动检测并处理鉴权挑战

许多 AI 聊天服务（如 DeepSeek）在发消息前需要先通过一个挑战。自动检测并按类型处理：

```python
async def detect_and_handle_challenge(client, base_url, headers) -> dict:
    """检测目标是否有前置鉴权挑战（如 PoW），自动求解并返回增强后的 headers。"""
    challenge_endpoints = [
        "/api/v0/chat/create_pow_challenge",   # DeepSeek
        "/api/challenge",                       # 通用
        "/api/v1/challenge",
    ]
    for path in challenge_endpoints:
        url = f"{base_url}{path}"
        try:
            resp = await client.post(url, json={"target_path": "/api/chat"}, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                # 尝试识别挑战类型
                if "challenge" in str(data):
                    # DeepSeek 类型: challenge + salt + signature + difficulty
                    challenge_data = data.get("data", {}).get("biz_data", {}).get("challenge", data)
                    if "difficulty" in challenge_data:
                        print(f"[POW] 检测到 PoW 挑战（难度 {challenge_data['difficulty']}）")
                        return {"type": "pow", "data": challenge_data,
                                "solver": "wasm", "header_name": "X-DS-PoW-Response",
                                "endpoint": path}
                    print(f"[CHALLENGE] 检测到其他挑战类型: {list(challenge_data.keys())}")
                    return {"type": "unknown", "data": challenge_data}
        except Exception:
            continue
    return {"type": "none"}
```

**挑战类型处理策略：**

| 类型 | 检测特征 | 处理方式 |
|---|---|---|
| **PoW（DeepSeek 类型）** | `create_pow_challenge` 返回 `challenge` + `salt` + `difficulty` + `signature` | 需要 WASM 求解（见下文） |
| **Token 刷新** | 请求返回 401 + `token_expired` | 检查是否有 refresh_token 机制 |
| **Captcha** | 返回包含 `captcha` / `turnstile` 字段 | 无法自动处理，需用户手动获取 Cookie |

**WASM PoW 求解模板**（自动检测并使用）：

```python
class WASMSolver:
    """WASM-based PoW solver — 从目标网站提取 wasm 二进制后使用。"""

    def __init__(self, wasm_path: str):
        from wasmtime import Store, Module, Instance
        self.store = Store()
        with open(wasm_path, "rb") as f:
            module = Module(self.store.engine, f.read())
        instance = Instance(self.store, module, [])
        exports = instance.exports(self.store)
        self.memory = exports["memory"]
        # 以下函数名因 WASM 编译目标而异，需从 DevTools 的 worker chunk 中确认
        self.wasm_solve = exports.get("wasm_solve") or exports.get("solve")
        self.malloc = exports["__wbindgen_export_0"]

    def solve(self, challenge: str, salt: str, expire_at: int, difficulty: int) -> int:
        # 典型调用模式（DeepSeek）:
        # prefix = f"{salt}_{expire_at}_"
        # 在 WASM 中执行哈希碰撞，返回 nonce
        ...
        return nonce
```

### Step 1.6: 自动探测 DSML 兼容性（工具调用支持）

对目标端点发送一条包含工具定义的测试请求，检查模型是否能理解 DSML（DeepSeek Markup Language）格式指令：

```python
DSML_PROBE_TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": "获取指定城市的当前时间",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名称"}
            },
            "required": ["city"]
        }
    }
}]

async def probe_dsml_compatibility(client, base_url, headers, endpoint) -> bool:
    """探测目标模型是否支持 DSML 格式的工具调用。"""
    dsml_prompt = build_dsml_tool_prompt(DSML_PROBE_TOOLS, tool_choice="required")
    test_messages = [
        {"role": "system", "content": dsml_prompt},
        {"role": "user", "content": "告诉我北京的当前时间"}
    ]

    # 简单 payload 格式
    for payload in [
        {"messages": test_messages, "stream": False},
        {"prompt": "告诉我北京的当前时间", "messages": test_messages},
    ]:
        try:
            resp = await client.post(
                f"{base_url}{endpoint}",
                json=payload,
                headers=headers,
                timeout=30,
            )
            if resp.status_code == 200:
                text = json.dumps(resp.json(), ensure_ascii=False)
                if "<|DSML|" in text:
                    print(f"[DSML] ✅ 目标模型支持 DSML 工具调用")
                    return True
        except Exception:
            continue

    print(f"[DSML] ❌ 目标模型不支持 DSML（响应中未检测到 DSML 标签）")
    return False
```

探测结果写入 `adapter.py`：
- `dsml_ready = True` — 之后所有请求遇到 `tools` 参数时，自动注入 DSML 提示词
- `dsml_ready = False` — tools 参数被忽略，退化为纯文本对话

### Step 2: 自动分析响应格式

对探测成功的请求，自动分析响应结构并提取内容字段。

```python
async def analyze_response(response_json: dict) -> dict:
    """自动遍历 JSON，找到最可能的内容字段。"""
    def find_content_field(obj, path=""):
        results = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                new_path = f"{path}.{k}" if path else k
                if isinstance(v, str) and len(v) > 20:
                    results.append((new_path, v[:100], k))
                if isinstance(v, (dict, list)):
                    results.extend(find_content_field(v, new_path))
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                results.extend(find_content_field(item, f"{path}[{i}]"))
        return results

    fields = find_content_field(response_json)
    # 按字段名启发式打分
    KEYWORDS = ["content", "text", "answer", "reply", "response", "message", "output",
                "completion", "result", "data.text", "choices[0].message.content"]
    scored = []
    for path, preview, name in fields:
        score = sum(3 if kw in path.lower() else 0 for kw in ["content", "answer", "reply", "text"])
        # content 字段的路径越深通常越准确
        depth = path.count(".") + path.count("[")
        scored.append((score + depth * 0.5, path, preview))
    scored.sort(reverse=True)
    return {"content_field": scored[0][1] if scored else None, "all_fields": fields}
```

**自动识别 SSE：**
```python
async def analyze_sse_stream(response):
    """读取 SSE 流的前几行，自动推断字段格式。"""
    lines = []
    async for line in response.aiter_lines():
        if line:
            lines.append(line)
        if len(lines) >= 10:
            break

    # 分析 line 模式
    events = []
    current_event = ""
    for line in lines:
        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: "):
            data_str = line[6:]
            try:
                data = json.loads(data_str)
                events.append((current_event, data))
            except json.JSONDecodeError:
                events.append((current_event, data_str))
            current_event = ""

    if events:
        # 自动推断内容字段
        for event_type, data in events:
            if isinstance(data, dict):
                # 常见字段名
                for key in ["v", "content", "text", "answer", "delta"]:
                    if key in data:
                        val = data[key]
                        if isinstance(val, str):
                            return {"sse_type": f"data.{key}", "events": events, "format": "plain_token"}
                        if isinstance(val, dict):
                            sub = val.get("content") or val.get("text") or ""
                            return {"sse_type": f"data.{key}.content", "events": events, "format": "nested"}
                # DeepSeek 格式: {"p": "response/content", "o": "APPEND", "v": "..."}
                if "p" in data and "o" in data and "v" in data:
                    return {"sse_type": "deepseek_append", "events": events, "format": "path_op_value"}
    return {"sse_type": "unknown", "events": events}
```

### Step 3: 自动生成适配器

根据探测结果，自动填充 adapter.py（已内建 DSML 支持模板）：

```python
# ═══════════════════════════════════════════════════════════
#  adapter.py  —  由 AI 自动生成，切勿手动修改
#  探测目标: {target_url}
#  聊天端点: {detected_endpoint}
#  请求格式: {detected_request_format}
#  响应字段: {detected_content_field}
#  流式格式: {detected_sse_type}
#  鉴权类型: {auth_type}
#  DSML 工具调用: {dsml_ready}
# ═══════════════════════════════════════════════════════════

class ChatAdapter:
    def __init__(self, cookies: str, base_url: str, dsml_enabled: bool = True):
        ...
        self.chat_endpoint = "{detected_endpoint}"
        self.content_field = "{detected_content_field}"
        self.auth_type = "{auth_type}"  # "none", "pow", "token_refresh"

        if self.auth_type == "pow":
            self._pow_wasm = load_wasm_solver("pow_solver.wasm")
            self._challenge_endpoint = "/api/v0/chat/create_pow_challenge"

        # DSML (DeepSeek Markup Language) — 基于提示词注入的工具调用
        self.dsml_enabled = dsml_enabled
        self.dsml_ready = {dsml_ready}  # 探测阶段设置的

    def _ensure_auth(self) -> dict:
        """确保请求头包含有效鉴权。处理 PoW 挑战等"""
        if self.auth_type == "pow":
            challenge = self._fetch_challenge()
            answer = self._pow_wasm.solve(challenge)
            return {"X-DS-PoW-Response": encode_pow_answer(challenge, answer)}
        return {}

    def convert_request(self, messages: list, stream: bool = False,
                        tools: list = None, tool_choice: str = None) -> dict:
        """OUTPUT: 已验证可被目标 API 接受的请求格式"""
        # 如果传入了 tools 且 DSML 可用，注入 DSML 提示词
        if tools and self.dsml_enabled and self.dsml_ready:
            if tool_choice != "none":
                dsml_prompt = build_dsml_tool_prompt(tools, tool_choice)
                messages = self._inject_dsml_prompt(messages, dsml_prompt)
        return {detected_request_format}

    async def send_request(self, payload: dict) -> dict:
        auth_headers = self._ensure_auth()
        async with httpx.AsyncClient(headers={**self.headers, **auth_headers}, timeout=120) as client:
            resp = await client.post(f"{self.base_url}{self.chat_endpoint}", json=payload)
            resp.raise_for_status()
            data = resp.json()
            # 检查响应是否含 DSML 标签
            if self.dsml_enabled and self.dsml_ready:
                text = json.dumps(data, ensure_ascii=False)
                if has_dsml_content(text):
                    content = extract_content_field(data)
                    if content and has_dsml_content(content):
                        return self.convert_with_dsml(content)
            return self.convert_response(data)

    async def stream_request(self, payload: dict) -> AsyncGenerator[bytes, None]:
        auth_headers = self._ensure_auth()
        use_sieve = self.dsml_enabled and self.dsml_ready
        async with httpx.AsyncClient(headers={**self.headers, **auth_headers}, timeout=120) as client:
            async with client.stream("POST", f"{self.base_url}{self.chat_endpoint}", json=payload) as resp:
                sieve = StreamSieve() if use_sieve else None
                async for line in resp.aiter_lines():
                    if not line: continue
                    content = self._extract_content(line)
                    if content is None: continue

                    if sieve:
                        result = sieve.feed(content)
                        for text in result.text_parts:
                            if text:
                                yield format_sse_chunk(text)
                        for tc in result.tool_calls:
                            yield format_tool_call_sse(tc)
                        if result.pending:
                            continue
                    else:
                        yield format_sse_chunk(content)

                if sieve:
                    flush_result = sieve.flush()
                    for text in flush_result.text_parts:
                        if text: yield format_sse_chunk(text)
                    for tc in flush_result.tool_calls:
                        yield format_tool_call_sse(tc)

                yield b"data: [DONE]\n\n"

    def _extract_content(self, line: str) -> Optional[str]:
        """自动适配 {detected_sse_type} 格式"""
        ...

    def _inject_dsml_prompt(self, messages: list, dsml_prompt: str) -> list:
        """将 DSML 提示词注入 system message"""
        ...
```

### Step 4: 自动验证

生成后立即自动验证：

```python
async def verify():
    # 1. 发一条消息（非流式）
    resp = await adapter.send_request(adapter.convert_request([{"role": "user", "content": "Hello"}]))
    result = adapter.convert_response(resp)
    assert "choices" in result, "非流式响应格式错误"

    # 2. 发一条消息（流式）
    chunks = []
    async for chunk in adapter.stream_request(adapter.convert_request([{"role": "user", "content": "Hello"}], stream=True)):
        chunks.append(chunk)
    assert len(chunks) > 1, "流式响应没有产生数据块"
    assert chunks[-1] == b"data: [DONE]\n\n", "流式缺少 [DONE] 标记"

    print("✅ 非流式 验证通过")
    print("✅ 流式    验证通过")
```

4. **（DSML 验证）** 如果 `dsml_ready` 为 True，额外发一条含 tools 的请求，验证响应是否正确解析出 `tool_calls`：
   ```python
   if dsml_ready:
       resp = await adapter.send_request(payload_with_tools)
       tool_calls = resp.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
       assert len(tool_calls) > 0, "DSML 工具调用未正确解析"
       print("✅ DSML 工具调用验证通过")
   ```

如果验证失败，自动回到 Step 2 调整分析逻辑，最多重试 3 轮。

### Step 5: 启动代理

生成完整的 `server.py` + `adapter.py`，启动代理并进行端到端测试：

```bash
pip install fastapi uvicorn httpx python-dotenv 2>/dev/null
python server.py &
sleep 2

# 端到端测试
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello"}],"stream":false}' \
  | python -c "import sys,json; d=json.load(sys.stdin); print('OK:', d['choices'][0]['message']['content'][:50])"

curl -s -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello"}],"stream":true}' \
  | head -5
```

### Step 6: 输出集成指南

告诉用户如何使用，包括：
- 设置环境变量 `OPENAI_API_BASE=http://localhost:8000/v1`
- 或直接在 Claude Code / Cursor / Continue 中配置
- **工具调用**：web2api 通过 DSML（DeepSeek Markup Language）提示词注入支持工具调用，但这**不是原生 function calling**，取决于目标模型能否理解 XML 格式指令（详见下方 DSML 参考）
- **关键限制**：不支持多模态、seed、response_format 等 OpenAI 扩展特性

---

## 常见探测模式速查表

| 页面类型 | 典型 endpoint | 典型 payload | 响应字段 | SSE |
|----------|---------------|-------------|----------|-----|
| DeepSeek Chat | `/api/v0/chat/completion` | `{chat_session_id, prompt, stream}` | APPEND 事件 `v` 字段 | 事件 + `data: {...}` |
| ChatGPT Next Web (Vercel) | `/api/chat` | `{messages: [], model: ""}` | `choices[0].message.content` | SSE + `data: {...}` |
| ChatGPT 官方 | `/backend-api/conversation` | `{action: "next", messages: []}` | `message.content.parts[0]` | SSE |
| LobeChat | `/api/chat` | `{messages: [], model: ""}` | `choices[0].delta.content` | SSE |
| Open WebUI | `/chat/completions` | `{messages: [], model: ""}` | OpenAI 标准 | SSE |
| 自建 NextJS 前端 | `/api/chat` | `{prompt: "", history: []}` | `text` / `answer` | SSE / JSON |
| Gradio 聊天 | `/api/chat` | `{data: ["Hello"]}` | `data[0]` | SSE |

---

## 自动探测失败时的降级策略

如果自动扫描没有命中任何 endpoint，AI 应该：

1. **抓取首页 HTML** → 用正则搜 `fetch(` / `api/` / `endpoint` / `url:` → 提取候选路径
2. **查看 robots.txt** → `GET /robots.txt` → 可能暴露 API 路径
3. **尝试 GET 常见路径** → 有些页面在 GET 时会返回 API 文档
4. **分析页面 JS** → 用简单正则提取字符串中的 URL 模式
5. **检查前置请求** → 先请求常见的 challenge/token 路径，看是否需要前置鉴权
6. **上述都失败** → 向用户求助：建议用户在 F12 Network 中发一条消息，截取请求 URL 和方法

```python
# 降级: 从 HTML 中提取 API 路径
async def extract_endpoints_from_html(base_url: str, headers: dict) -> list:
    async with httpx.AsyncClient() as client:
        resp = await client.get(base_url, headers=headers)
        html = resp.text
        # 搜索常见模式
        patterns = [
            r'fetch\s*\(\s*["\']([^"\']+)["\']',
            r'axios\.\w+\s*\(\s*["\']([^"\']+)["\']',
            r'url:\s*["\']([^"\']+)["\']',
            r'endpoint:\s*["\']([^"\']+)["\']',
            r'"api/([^"\']+)"',
            r"'api/([^\"']+)'",
            r'/api/\w+',
        ]
        urls = set()
        for p in patterns:
            for m in re.finditer(p, html):
                url = m.group(1) if m.lastindex else m.group(0)
                if url.startswith("/"):
                    urls.add(url)
                elif url.startswith("http"):
                    urls.add(url)
        return sorted(urls)
```

---

## 特性支持清单

不是所有 OpenAI 特性都能被反向代理支持。生成前告知用户此项目的能力边界：

| 特性 | 支持情况 | 说明 |
|---|---|---|
| 纯文本对话 | ✅ | 核心功能 |
| 流式输出 (stream) | ✅ | SSE → OpenAI chunk 格式 |
| 多轮对话 | ✅ | 客户端管理 messages 数组 |
| `ContentPart` 数组格式 | ✅ | `content: [{type: "text", text: "..."}]` |
| 多模态 (图片/文件) | ❌ | 底层 API 通常不支持 |
| 工具调用 (function calling) | ⚠️ | 基于 DSML 提示词注入实现，非原生，取决于目标模型能否理解 XML 格式指令 |
| `max_tokens` / `temperature` | ⚠️ | 取决于底层 API 是否支持 |
| `seed` / `response_format` / `json_mode` | ❌ | 网页 API 不支持 |

---

## 输出交付物

1. **adapter.py** — 完整填充的适配器（已验证通过）
2. **server.py** — OpenAI 兼容代理服务器
3. **requirements.txt** — 依赖清单
4. **.env.example** — 配置模板（不含敏感信息）
5. **启动命令** — 一行启动代理
6. **验证结果** — 流式 + 非流式测试截图级确认
7. **集成指南** — 如何接入 Claude Code / Cursor / 任意 OpenAI SDK，以及已知限制

---

## DSML 格式参考

DSML（DeepSeek Markup Language）是一套基于 XML 标签的提示词注入协议，用于在**不支持原生 function calling 的网页 API 上实现工具调用**。

### 注入方式

当用户请求携带 `tools` 参数且目标模型通过 DSML 兼容性探测时，adapter 会在发送给目标的 messages 数组头部注入一段 DSML 格式的 system prompt，指导模型按约定格式输出工具调用。

### DSML 标签规范

| 标签 | 用途 |
|------|------|
| `<\|DSML\|tool_calls>` | 工具调用集合的根标签 |
| `<\|DSML\|invoke name="xxx">` | 单个工具调用的起始标签，name 为函数名 |
| `<\|DSML\|parameter name="yyy"><![CDATA[zzz]]></\|DSML\|parameter>` | 参数键值对，value 用 CDATA 包裹 |
| `</\|DSML\|invoke>` | 工具调用结束标签 |
| `</\|DSML\|tool_calls>` | 根标签闭合 |

### 典型流程

```
用户请求:
  POST /v1/chat/completions
  {"tools": [{"type": "function", "function": {"name": "get_weather", ...}}],
   "tool_choice": "auto"}

↓↓↓ adapter 内部 ↓↓↓

发给目标的 payload:
  {"messages": [
    {"role": "system", "content": "...当需要调用工具时，使用 DSML 格式：
     <|DSML|tool_calls>
       <|DSML|invoke name=\"get_weather\">
         <|DSML|parameter name=\"city\"><![CDATA[北京]]></|DSML|parameter>
       </|DSML|invoke>
     </|DSML|tool_calls>"},
    {"role": "user", "content": "北京的天气怎么样？"}
  ]}

↓↓↓ 目标模型响应（含 DSML 标签） ↓↓↓

  "北京的天气是... <|DSML|tool_calls>..."

↓↓↓ StreamSieve 逐字符检测并分离 ↓↓↓

  普通文本: "北京的天气是..."
  → yield delta.content

  DSML 标签: <|DSML|tool_calls>...
  → 捕获整块 → parse_dsml_invoke() → tool_calls
  → yield delta.tool_calls
```

### 流式筛分（StreamSieve）

`StreamSieve` 是一个逐字符状态机：

```
状态: NORMAL → 逐字符累积到 _text_buffer
      遇到 < 字符 → 切到 CAPTURING
状态: CAPTURING → 逐字符累积到 _buffer
      跟踪标签深度 _tag_depth
      当 _tag_depth 归零 → 尝试解析 DSML
        解析成功 → 吐出 tool_calls
        解析失败 → 作为普通文本回退
      回到 NORMAL
```

边界情况处理：
- **分片边界**：`<|DSML|` 可能被拆在两个 TCP chunk 中，按字符处理天然免疫
- **CDATA 内容**：`<![CDATA[...]]>` 内的内容不会被误判为标签
- **异常中断**：流中断时 `flush()` 将未闭合的 buffer 作为普通文本吐出
- **文本+工具混合**：模型可能在回答正文后再输出 DSML，两者都能被正确分离

### tool_choice 映射

| client 传入 | DSML 行为 |
|---|---|
| `"auto"` | 提示词写"仅在需要时调用工具" |
| `"none"` | 跳过 DSML 注入，不走工具调用流程 |
| `"required"` | 提示词强制要求必须调用一个工具 |
| `{"type": "function", "function": {"name": "xxx"}}` | 暂不支持，退化到 "required" |
