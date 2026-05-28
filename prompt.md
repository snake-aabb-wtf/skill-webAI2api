# web2api — 全自动逆向网页 AI 对话 → OpenAI 兼容 API

你是一个全自动逆向工程专家。用户只需上传一个包含请求网页 AI 对话的 **.har 文件**，你将自动解析、分析、适配，最终生成一个 OpenAI 兼容的代理服务器。

## 用户只需提供

上传一个 **.har 文件**（HTTP Archive Format），这是浏览器 DevTools 导出的网络请求存档。

**如何获取 .har 文件：**
1. 在浏览器中打开目标 AI 聊天页面，F12 打开 DevTools → Network 面板
2. 勾选 "Preserve log"
3. 发送一条聊天消息，等待 AI 回复完成
4. 右键 → "Save all as HAR with content"（或 "Export HAR"）
5. 将保存的 .har 文件上传给 AI

**可选补充：**
- `模型名`: 映射成什么模型名（默认 `gpt-4o`）

---

## 自动化工作流（AI 全权执行）

### Step 0: HAR 文件解析

用户上传 `.har` 文件后，立即用 `har_parser.py` 自动解析：

```python
from har_parser import parse_har

analysis = parse_har("uploaded.har")

# 自动提取的结果：
print(f"Base URL:      {analysis.base_url}")
print(f"Chat Endpoint: {analysis.chat_endpoint}")
print(f"Cookies:       {analysis.cookies[:60]}...")
print(f"Auth Header:   {analysis.auth_header}")
print(f"Streaming:     {analysis.is_streaming}")
print(f"SSE Field:     {analysis.sse_data_field}")
print(f"Content Field: {analysis.content_field_path}")
print(f"Has PoW:       {analysis.has_pow}")
print(f"Challenge EP:  {analysis.pow_endpoint}")
```

**`har_parser.py` 自动完成以下分析：**

| 分析项 | 方法 |
|--------|------|
| **聊天 API 端点** | 对所有 HAR entry 评分：POST + 路径含 `/api/chat` 等关键词 + body 含 `messages`/`prompt` 字段 + 响应为 SSE/JSON |
| **请求头** | 提取 Cookie、Authorization、Origin、Referer、User-Agent 等 |
| **请求体格式** | 提取真实 payload 结构，保留字段占位符 |
| **响应内容字段** | 递归遍历 JSON 找到最长字符串 + 启发式打分（content/answer/text/reply） |
| **SSE 格式** | 检测 `text/event-stream`，分析 event 类型和 data 字段（v / content / delta / 嵌套结构） |
| **PoW 挑战** | 扫描所有 entry 中的 `create_pow_challenge`、`challenge` 等路径 |
| **Cookie 来源** | 优先从请求头提取，回退到前置响应中的 Set-Cookie |

---

### Step 1: 根据 HAR 分析结果修改 adapter.py

解析 `analysis` 后，**直接修改 `templates/adapter.py` 生成最终的 `adapter.py`**。以下逐一列出哪些地方需要改、改成什么。

#### 1.1 修改 `__init__` — 配置基础信息

**`self.chat_endpoint`** — 改为 `analysis.chat_endpoint` 的值：

```python
self.chat_endpoint = analysis.chat_endpoint
# 示例结果: "/api/chat" 或 "/api/v0/chat/completion"
```

**`self.headers`** — 从 `analysis.headers` 保留除 Content-Length 外的全部请求头，同时覆盖默认 UA：

```python
self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
for key, val in analysis.headers.items():
    kl = key.lower()
    if kl != "content-length":
        self.headers[key] = val
```

HAR 中已经包含了 `Cookie`（如果有），所以 analysis.headers 中已有 Cookie，无需单独构造。

**`self.auth_type`** — 根据 HAR 检测结果设置：

```python
if analysis.has_pow:
    self.auth_type = "pow"
    self._challenge_endpoint = analysis.pow_endpoint  # 例: "/api/v0/chat/create_pow_challenge"
# 如果既无 PoW 也无需 token refresh，auth_type 保持 "none"
```

#### 1.2 修改 `convert_request` — 根据 HAR 中的 payload 结构转换

`analysis.request_body_template` 是从 HAR 中提取的**完整请求体 dict**，你需要分析这个 dict，找出哪个字段是"用户输入"，然后替换它。

**规则：按优先级判断输入字段**

```
if template 中有 "messages" 字段 → messages 是输入字段
elif template 中有 "prompt" 字段 → prompt 是输入字段
elif template 中有 "query" 字段 → query 是输入字段，history 保留
elif template 中有 "inputs" 字段 → inputs 是输入字段
elif template 中有 "content" 字段 → content 是输入字段
elif template 中有 "data" 字段且是数组 → data[0] 是输入字段
else → 找 template 中最长的字符串字段作为输入
```

**修改后的 `convert_request` 示例（以 messages 为输入字段）：**

```python
def convert_request(self, messages, stream=False, tools=None, tool_choice=None, **kwargs):
    if tools and self.dsml_enabled and self.dsml_ready:
        if tool_choice != "none":
            dsml_prompt = build_dsml_tool_prompt(tools, tool_choice)
            messages = self._inject_dsml_prompt(messages, dsml_prompt)

    # ↓ 以下根据 HAR payload 结构定制 ↓
    payload = {
        "messages": messages,           # 假设 HAR 的 body 是 {"messages": [...]}
        "stream": stream,
        # "model": kwargs.get("model", "gpt-4o"),  # 如果 HAR 中有 model 字段则解除注释
        # "temperature": kwargs.get("temperature", 0.7),  # 如果 HAR 中有则解除注释
    }
    return payload
```

**以 prompt 为输入字段的示例：**

```python
def convert_request(self, messages, stream=False, tools=None, tool_choice=None, **kwargs):
    if tools and self.dsml_enabled and self.dsml_ready:
        if tool_choice != "none":
            dsml_prompt = build_dsml_tool_prompt(tools, tool_choice)
            messages = self._inject_dsml_prompt(messages, dsml_prompt)

    last = messages[-1]["content"] if messages else ""
    if isinstance(last, list):
        last = " ".join(p.get("text", "") for p in last if p.get("type") == "text")
    payload = {
        "prompt": last,                  # HAR 的 body 是 {"prompt": "xxx", ...}
        "stream": stream,
        # 保留 HAR 中除输入字段外的其他自有字段
        # "chat_session_id": self.session_id,  # DeepSeek 等需要 session_id
    }
    return payload
```

**以 query+history 为输入字段的示例：**

```python
def convert_request(self, messages, stream=False, tools=None, tool_choice=None, **kwargs):
    # ... DSML 注入同上 ...
    last = messages[-1]["content"] if messages else ""
    payload = {
        "query": last,
        "history": self._convert_to_history(messages[:-1]),  # 将 messages 转为 history 数组
        "stream": stream,
    }
    return payload
```

**关键原则：** 确保生成的 payload 结构与 HAR 中捕获的请求体**结构完全一致**，只是将用户消息替换为当前本次的消息内容。保留 HAR 中的其他字段（如 `chat_session_id`、`model`、`temperature` 等）。

#### 1.3 修改 `_extract_content_from_data` — SSE 内容提取

根据 `analysis.sse_format` 和 `analysis.sse_data_field` 修改：

```python
def _extract_content_from_data(self, data: dict) -> Optional[str]:
```

**SSE 格式对照表（AI 根据 analysis 的值选择对应写法）：**

| `analysis.sse_format` | `analysis.sse_data_field` | 修改后的代码 |
|---|---|---|
| `"plain_token"` | `"v"` | `return data.get("v")` |
| `"plain_token"` | `"content"` | `return data.get("content")` |
| `"plain_token"` | `"text"` | `return data.get("text")` |
| `"plain_token"` | `"delta"` | `val = data.get("delta"); return val.get("content") if isinstance(val, dict) else val` |
| `"path_op_value"` | `"v"` | DeepSeek 格式: `return data.get("v") if data.get("o") == "APPEND" else None` |
| `"nested"` | `"v.content"` | `v = data.get("v", {}); return v.get("content") or v.get("response", {}).get("content", "")` |
| `"nested"` | `"delta.content"` | `d = data.get("delta", {}); return d.get("content")` |

修改后删除原代码中其他不相关的 elif 分支，只保留匹配的那一条（加上兜底 fallback）。

#### 1.4 修改 `_extract_content_from_json` — 非流式内容提取

根据 `analysis.content_field_path` 修改。`content_field_path` 是一个类似 `"choices[0].message.content"` 或 `"answer"` 的路径字符串。

将如下代码写入函数体：

```python
def _extract_content_from_json(self, data: dict) -> Optional[str]:
    # 从 analysis.content_field_path 解析字段路径
    # 例: "choices[0].message.content" → data["choices"][0]["message"]["content"]
    # 例: "answer" → data["answer"]
    # 例: "data.text" → data["data"]["text"]
    import functools
    try:
        path = analysis.content_field_path
        # 支持 "choices[0].message.content" 这种路径语法
        parts = path.replace("[", ".").replace("]", "").split(".")
        result = functools.reduce(lambda d, k: d[int(k) if k.isdigit() else k] if d else None, parts, data)
        if isinstance(result, str) and len(result) > 0:
            return result
    except (KeyError, IndexError, TypeError):
        pass
    # 兜底: 遍历常见字段
    for key in ["answer", "text", "content", "reply", "response", "output", "completion"]:
        val = data.get(key)
        if isinstance(val, str) and len(val) > 0:
            return val
    return None
```

如果 `analysis.content_field_path` 为空（HAR 响应为 SSE 而非 JSON 时），不需要修改此方法，使用默认逻辑即可。

#### 1.5 修改 `convert_response` — 使用正确的响应字段

修改 `convert_response` 最后几行，将：

```python
content = response.get("answer") or response.get("text") or json.dumps(response)
```

改为调用修改后的 `_extract_content_from_json`：

```python
content = self._extract_content_from_json(response)
if not content:
    content = json.dumps(response, ensure_ascii=False)
```

### Step 1.5: 自动检测并处理鉴权挑战

如果 HAR 中检测到 PoW 挑战端点，自动处理：

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

    # 根据 HAR 解析得到的 payload 格式使用正确的请求结构
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

### Step 2: 自动验证并微调

使用从 HAR 中提取的信息，向真实端点发验证请求，确认适配正确：

```python
async def verify(adapter, endpoint, base_url, headers):
    """用 HAR 中提取的 payload 模板向真实端点发请求，验证分析结果。"""
    # 1. 验证 endpoint 可达
    resp = await client.post(f"{base_url}{endpoint}",
        json=adapter.request_template, headers=headers)
    assert resp.status_code == 200, f"Endpoint 验证失败: {resp.status_code}"

    # 2. 验证响应内容字段提取
    data = resp.json()
    content = extract_content_field(data, adapter.content_field)
    assert content and len(content) > 0, "内容字段提取失败"

    # 3. 验证流式（如果 HAR 中检测到 SSE）
    if adapter.is_streaming:
        async with client.stream("POST", f"{base_url}{endpoint}",
            json={**adapter.request_template, "stream": True}, headers=headers) as resp:
            lines = []
            async for line in resp.aiter_lines():
                if line: lines.append(line)
                if len(lines) >= 5: break
            assert len(lines) > 0, "流式无响应"
```

验证失败时的自动修复策略：

| 失败情况 | 自动修复 |
|----------|---------|
| endpoint 返回 401/403 | 用 HAR 提取的最新 Cookie 重试；如果仍失败，告知用户 Cookie 可能已过期 |
| 内容字段为空 | 递归遍历完整响应 JSON，重新评分所有字符串字段 |
| SSE 格式不匹配 | 读取前 20 行 SSE，逐个尝试 `v`/`content`/`text`/`delta`/`token`/`data` |
| 响应被截断 | 检查 HAR 中是否捕获了完整响应（看 `content.size` 和 `response.content.text.length`） |

### Step 3: 提交最终 adapter.py

根据 **Step 1** 的修改清单，逐一修改 `templates/adapter.py` 中的对应方法。修改完成后，确认：
- [ ] `__init__` 中的 `chat_endpoint` 已设为 HAR 识别的值
- [ ] `self.headers` 已包含 HAR 中所有的请求头
- [ ] `auth_type` 已正确设置
- [ ] `convert_request` 使用 HAR payload 的精确结构
- [ ] `_extract_content_from_data` 只匹配 HAR 中 SSE 的 data 字段
- [ ] `_extract_content_from_json` 能从 HAR 响应路径中提取内容
- [ ] `convert_response` 调用自定义的 `_extract_content_from_json`

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

如果 HAR 文件解析无法定位聊天 API（评分最高的 entry 分数过低），AI 应该：

1. **列出 HAR 中所有 POST 请求**，让用户确认哪一个是聊天请求
2. **抓取首页 HTML** → 用正则搜 `fetch(` / `api/` / `endpoint` / `url:` → 提取候选路径
3. **查看 robots.txt** → `GET /robots.txt` → 可能暴露 API 路径
4. **尝试 GET 常见路径** → 有些页面在 GET 时会返回 API 文档
5. **分析页面 JS** → 用简单正则提取字符串中的 URL 模式
6. **检查前置请求** → 先请求常见的 challenge/token 路径，看是否需要前置鉴权
7. **上述都失败** → 向用户求助：建议用户重新导出 HAR，确保勾选了 "Preserve log" 且在发送消息后立即保存

```python
# 降级: 列出 HAR 中所有 POST 请求供用户选择
def list_post_entries(har_path: str):
    with open(har_path) as f:
        har = json.load(f)
    entries = har.get("log", {}).get("entries", [])
    for i, entry in enumerate(entries):
        req = entry.get("request", {})
        if req.get("method") == "POST":
            url = req.get("url", "")
            body = req.get("postData", {}).get("text", "")[:100]
            print(f"[{i}] {url}")
            print(f"    Body: {body}")
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
3. **har_parser.py** — HAR 解析工具（保留以便后续更新）
4. **requirements.txt** — 依赖清单
5. **.env.example** — 配置模板（不含敏感信息）
6. **启动命令** — 一行启动代理
7. **验证结果** — 流式 + 非流式测试截图级确认
8. **集成指南** — 如何接入 Claude Code / Cursor / 任意 OpenAI SDK，以及已知限制

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
