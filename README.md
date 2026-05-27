# web2api — 全自动逆向网页 AI 对话 → OpenAI 兼容 API

输入任意 AI 聊天网页的 URL + Cookie，自动探测、分析、适配，最终生成一个 **OpenAI 兼容的代理服务器**。任何支持 OpenAI API 的工具（Claude Code、Cursor、Continue、自定义脚本等）都可以通过此代理使用目标网站的聊天能力。

---

## 原理

```
用户输入 (URL + Cookie)
    │
    ▼
Step 0: 浏览器 DevTools 分析（引导用户捕获真实请求）
    │
    ▼
Step 1: 自动探测 API 端点（扫描 20+ 常用路径 + 5 种 payload 格式）
    │
    ▼
Step 1.5: 自动处理鉴权挑战（PoW / Token 刷新 / Captcha）
    │
    ▼
Step 1.6: 自动探测 DSML 兼容性（工具调用支持）
    │
    ▼
Step 2: 自动分析响应格式（JSON 字段探测 / SSE 格式推断）
    │
    ▼
Step 3: 自动生成适配器 adapter.py + server.py
    │
    ▼
Step 4: 自动验证（非流式 + 流式，失败则重试 3 轮）
    │
    ▼
Step 5: 启动代理 + 端到端测试
    │
    ▼
Step 6: 输出集成指南
```

## 特性

| 特性 | 支持 |
|------|------|
| 纯文本对话 | ✅ |
| 流式输出 (SSE) | ✅ |
| 多轮对话 | ✅ |
| ContentPart 数组格式 | ✅ |
| Proof-of-Work 自动求解 | ✅（WASM） |
| 工具调用 (function calling) | ⚠️ 基于 DSML 提示词注入，非原生 |
| 多模态（图片/文件） | ❌ |
| `max_tokens` / `temperature` | ⚠️ 取决于目标 |

## 支持的网页类型

| 类型 | 典型端点 |
|------|---------|
| DeepSeek Chat | `/api/v0/chat/completion` |
| ChatGPT Next Web | `/api/chat` |
| ChatGPT 官方 | `/backend-api/conversation` |
| LobeChat | `/api/chat` |
| Open WebUI | `/chat/completions` |
| Gradio 聊天 | `/api/chat` |
| 自建 NextJS 前端 | `/api/chat` |

## 文件结构

```
web2api/
├── prompt.md                     # 主规格文档（AI 执行的工作流指令）
├── templates/
│   ├── adapter.py                # 适配器模板（含 DSML 工具调用支持）
│   ├── server.py                 # FastAPI 代理服务器模板
│   ├── tool_dsml.py              # DSML prompt 构建 + XML 解析
│   └── tool_sieve.py             # StreamSieve 流式分离引擎
└── README.md                     # 本文件
```

## 交付物

运行完成后，AI 将生成：

1. **adapter.py** — 完整填充的适配器（已验证通过）
2. **server.py** — OpenAI 兼容代理服务器
3. **requirements.txt** — 依赖清单
4. **.env.example** — 配置模板
5. **启动命令** — 一行启动代理
6. **验证结果** — 流式 + 非流式测试确认
7. **集成指南** — 如何接入 Claude Code / Cursor / 任意 OpenAI SDK

## 快速使用

```bash
# 1. 安装依赖
pip install fastapi uvicorn httpx python-dotenv

# 2. 配置环境变量
# TARGET_URL=https://chat.example.com
# COOKIES=__session=xxx; token=yyy
# MODEL_NAME=gpt-4o

# 3. 启动代理
python server.py

# 4. 测试
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}],"stream":false}'

# 5. 在任意 OpenAI 兼容工具中配置
# OPENAI_API_BASE=http://localhost:8000/v1
```

## 配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TARGET_URL` | `https://chat.example.com` | 目标 AI 聊天网站地址 |
| `COOKIES` | `""` | 登录后的 Cookie（关键鉴权） |
| `MODEL_NAME` | `gpt-4o` | 代理暴露的模型名 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `API_KEY` | `sk-web2api-placeholder` | 可选 API Key 鉴权 |
| `DSML_ENABLED` | `true` | 是否启用 DSML 工具调用 |

## 基于此 skill 构建的项目

- **[deepseek-web2api-free](https://github.com/snake-aabb-wtf/deepseek-web2api-free)** — 将 DeepSeek Chat 转换为 OpenAI / Anthropic 兼容 API，含 DSML 工具调用、PoW 求解、管理面板

## 限制

- 工具调用基于 DSML 提示词注入，不是原生 function calling，取决于目标模型能否理解 XML 指令
- 不支持多模态输入 — 仅文本对话
- `seed` / `response_format` / `json_mode` 等 OpenAI 扩展特性不可用
- `max_tokens` / `temperature` 取决于目标 API 是否支持
