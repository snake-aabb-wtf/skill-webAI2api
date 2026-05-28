# web2api — 全自动逆向网页 AI 对话 → OpenAI 兼容 API

上传一个 **.har 文件**（浏览器 DevTools 导出的网络请求存档），自动解析、分析、适配，最终生成一个 **OpenAI 兼容的代理服务器**。任何支持 OpenAI API 的工具（Claude Code、Cursor、Continue、自定义脚本等）都可以通过此代理使用目标网站的聊天能力。

---

## 原理

```
用户上传 .har 文件（浏览器导出）
    │
    ▼
Step 0: HAR 文件自动解析（har_parser.py）
    │   ├─ 自动识别聊天 API 端点（评分算法）
    │   ├─ 提取请求头 / Cookie / Authorization
    │   ├─ 提取请求体格式模板
    │   ├─ 分析响应结构（JSON 字段 / SSE 格式）
    │   └─ 检测 PoW 挑战端点
    │
    ▼
Step 1: 利用 HAR 结果构建适配器（无需手动探测）
    │
    ▼
Step 1.5: 自动处理鉴权挑战（PoW / Token 刷新 / Captcha）
    │
    ▼
Step 1.6: 自动探测 DSML 兼容性（工具调用支持）
    │
    ▼
Step 2: 自动验证并微调（发真实请求确认）
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

## 与传统方式的区别

| 步骤 | 传统方式（旧） | HAR 方式（新） |
|------|---------------|---------------|
| 输入 | URL + Cookie + 手动 F12 分析 | 一个 .har 文件就够了 |
| API 端点 | 用户手动从 Network 面板复制 | 自动评分识别 |
| 请求头 | 用户手动提供 Cookie 和 Authorization | 自动从 HAR 提取 |
| 请求体格式 | 用户手动粘贴 Request Body | 自动提取为模板 |
| 响应分析 | AI 猜测内容字段 | 自动递归遍历评分 |
| SSE 检测 | AI 猜测格式 | 自动分析真实 SSE 数据 |
| PoW 检测 | 用户/AI 手动探测 | 自动扫描所有 HAR entry |
| 人工干预 | 每步都可能需要用户配合 | 只需上传 HAR 文件 |

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
| **任意自定义前端** | **自动识别，无需预置** |

## 文件结构

```
web2api/
├── prompt.md                     # 主规格文档（AI 执行的工作流指令）
├── templates/
│   ├── har_parser.py             # 【新增】HAR 文件解析器
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
3. **har_parser.py** — HAR 解析工具（保留以便后续更新）
4. **requirements.txt** — 依赖清单
5. **.env.example** — 配置模板
6. **启动命令** — 一行启动代理
7. **验证结果** — 流式 + 非流式测试确认
8. **集成指南** — 如何接入 Claude Code / Cursor / 任意 OpenAI SDK

## 如何获取 .har 文件

1. 在浏览器中打开目标 AI 聊天页面，按 `F12` 打开 DevTools
2. 切换到 **Network**（网络）面板
3. 勾选 **Preserve log**（保留日志）
4. 发送一条聊天消息，等待 AI 回复完成
5. 在 Network 面板中右键任意请求 → **Save all as HAR with content**（或 Export HAR）
6. 将保存的 `.har` 文件提供给 AI

## 限制

- 工具调用基于 DSML 提示词注入，不是原生 function calling，取决于目标模型能否理解 XML 指令
- 不支持多模态输入 — 仅文本对话
- `seed` / `response_format` / `json_mode` 等 OpenAI 扩展特性不可用
- `max_tokens` / `temperature` 取决于目标 API 是否支持
