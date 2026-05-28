# web2api — 让每个人都能用上更便宜的 Token

每个月花几百块买 API？那个网页 AI 明明免费又好用，却被锁在浏览器里。
**现在把它拽出来。**

上传一个 HAR 文件，web2api 自动把任何网页 AI 逆向成 OpenAI 兼容 API。
流式、多轮对话、工具调用，全都给你。一条命令接入 Claude Code、Cursor、Continue。

---

## Token 不应该那么贵

| 你在用什么 | 价格 |
|---|---|
| Claude Pro 订阅 | 每月 20$，还有用量限制 |
| GPT-4 API 按量 | 输入 30$/M token，输出 60$/M token |
| 国产网页 AI | **免费，或者便宜一个数量级** |

web2api 就是干这个的。把国产/开源网页 AI 背后的大模型能力解放出来，让你以零成本或几分之一的价格获得同等甚至更好的生成质量。**省下的钱是你的，token 的质量一点不少。**

## 怎么做到的

```
你：上传 .har 文件（F12 → Network → 发条消息 → 保存）
↓
AI：自动分析 HAR → 找到端点、鉴权、payload 格式
  → 生成适配器 → 启动代理服务器
↓
你：OPENAI_API_BASE=http://localhost:8000/v1
  该用哪个 SDK 用哪个 SDK，完全不用改代码
```

## 能干什么

- **搭私人 API**：把免费网页 AI 变成自己的 API 端点，想调多少次调多少次
- **工具调用**：网页不提供 function calling？DSML 注入给你安排上
- **随时续期**：Cookie 过期了？双击 config_tool.py 点几下完事，不用重新找 AI
- **能跑就行**：HTTP 也好、WebSocket 也好、有 PoW 也好，HAR 里有就能逆向

## 特性一览

| | |
|---|---|
| 零成本迁移 | 任何 OpenAI SDK 直接对接，一行不改 |
| 流式输出 | SSE → OpenAI chunk，延迟不增加 |
| 工具调用 | DSML 提示词注入，目标模型能吃 XML 就能用 |
| 协议自适应 | HTTP SSE / WebSocket 自动识别 |
| 参数透传 | 只传目标 API 实际支持的，不传的自动丢弃 |
| PoW 自动求解 | DeepSeek 等有验证挑战的也能打 |
| GUI 续期 | 改 Cookie 不用重新跑 AI，点几下就行 |

## 快速上手

```bash
# 1. 浏览器 F12 → Network → 发条消息 → 右键保存 HAR
# 2. 把 .har 扔给 AI，拿到交付的 server.py + adapter.py
# 3. 启动：
pip install fastapi uvicorn httpx python-dotenv websockets
python server.py

# 4. 爽：
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"写一首诗"}]}'
```

## 工具调用说明

基于 **DSML（DeepSeek Markup Language）** 提示词注入实现，非原生 function calling，取决于目标模型对 XML 指令的理解能力。StreamSieve 实时分离文本和 DSML 标签，流式状态下同样可用。

## 能干倒什么程度

- 同类模型 0 成本调用
- 国产大模型 API 免费白嫖（只需要有个网页账号）
- 私有部署的开源模型也不用再搭一套前端，直接 web2api 一把梭

## 限制

- 仅文本，不支持多模态
- `seed` / `response_format` / `json_mode` 不可用
- 参数支持取决于目标 API
- WebSocket 仅 text frame
