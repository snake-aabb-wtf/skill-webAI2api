# web2api — 把任意网页 AI 变成 OpenAI 兼容 API

只需上传一个浏览器导出的 **.har 文件**，自动逆向出完整的 API 代理，支持流式输出、多轮对话、工具调用。一行命令接入 Claude Code、Cursor、Continue 或任何 OpenAI SDK。

---

## 有什么用

- 想用某个网页 AI 的能力但不想开浏览器？搭个代理用 API 调
- 需要工具调用（function calling）但目标只提供了网页版？DSML 注入实现
- Cookie 过期了不想重新配置？GUI 工具点几下续期

## 一句话原理

```
你：上传 .har 文件
↓
AI：解析 HAR → 识别端点/鉴权/payload 格式 → 生成适配器 → 启动代理
↓
你：OPENAI_API_BASE=http://localhost:8000/v1 直接调用
```

## 特性

| 特性 | |
|---|---|
| 协议自动识别 | HTTP SSE / WebSocket，从 HAR 自动判断 |
| 流式输出 | SSE → OpenAI chunk 格式 |
| 多轮对话 | 客户端管理 messages 数组 |
| 工具调用 | 通过 DSML（DeepSeek Markup Language）提示词注入实现 |
| 参数自动推断 | 只透传目标 API 实际支持的参数 |
| Proof-of-Work | 自动求解（DeepSeek 等） |
| Cookie 续期 | GUI 工具点几下完成，无需重新跑 AI |
| 多模态 | ❌ 仅文本 |

## 快速上手

```bash
# 1. 去浏览器 F12 → Network → 发条消息 → 右键保存 HAR
# 2. 把 .har 文件扔给 AI
# 3. 拿到交付的项目后：
pip install fastapi uvicorn httpx python-dotenv websockets
python server.py

# 4. 调用：
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}'
```

## Cookie 过期了？

```
不需要重新找 AI。
1. 双击 config_tool.py
2. 选择 .har 文件 → 解析
3. 粘贴新 Cookie → 保存
4. 启动服务器
```

## 工具调用说明

工具调用基于 **DSML（DeepSeek Markup Language）** 的提示词注入机制实现，并非原生 function calling。取决于目标模型对 XML 格式指令的理解能力。StreamSieve 实时分离文本和 DSML 标签，支持流式混合输出。

## 限制

- 仅文本对话，不支持多模态
- `seed` / `response_format` / `json_mode` 不可用
- 参数支持取决于目标 API 是否暴露
- WebSocket 仅 text frame（opcode=1）

## 文件结构

```
web2api/
├── SKILL.md                # 技能定义（AI 工作流指令）
├── templates/
│   ├── har_parser.py       # HAR 解析器
│   ├── adapter.py          # HTTP 适配器模板
│   ├── ws_adapter.py       # WebSocket 适配器模板
│   ├── config_tool.py      # GUI 配置工具模板
│   ├── server.py           # FastAPI 代理服务器模板
│   ├── tool_dsml.py        # DSML 工具调用实现
│   └── tool_sieve.py       # 流式分离引擎
├── LICENSE
└── README.md
```
