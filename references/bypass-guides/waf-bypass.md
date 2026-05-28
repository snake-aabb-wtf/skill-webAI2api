# WAF (Web Application Firewall) 绕过指南

## 适用场景

目标网站使用了 Web 应用防火墙（Cloudflare、Akamai、Imperva 等），自动请求被拦截。

## 检测方法

| 特征 | 可能原因 |
|---|---|
| 响应状态码 403 / 503 | WAF 拦截 |
| 响应体包含 `Just a moment...` | Cloudflare 质询页面 |
| 响应体包含 `cf-error`、`cf-browser-verification` | Cloudflare 人机验证 |
| 响应头包含 `server: cloudflare` + 状态码 403 | Cloudflare WAF 规则命中 |
| 请求被重定向到 `/_challenge` 等路径 | 自定义 WAF |

## 方案 A：完整复制浏览器环境（最有效）

从 HAR 中提取**所有**请求头，不要只取 Cookie：

```python
# 从 HAR 中复制完整 headers
headers = {}
for h in chat_request["headers"]:
    name = h.get("name", "")
    if name.lower() not in ("content-length",):
        headers[name] = h.get("value", "")
```

关键头列表：
- `User-Agent` — 必须完整（包括 Chrome 版本号）
- `Accept` — `text/html,application/json,...`
- `Accept-Language` — `zh-CN,zh;q=0.9`
- `Accept-Encoding` — `gzip, deflate, br`
- `Sec-Ch-Ua` — 浏览器标识（HAR 中自动包含）
- `Sec-Ch-Ua-Mobile`、`Sec-Ch-Ua-Platform`
- `Origin` — 必须匹配目标域名
- `Referer` — 必须匹配聊天页面 URL

## 方案 B：添加延迟和请求节流

WAF 通常检测请求频率。在适配器中加入随机延迟：

```python
import asyncio
import random

async def send_request(self, payload):
    # 随机延迟 0.5-2 秒，模拟人类操作
    await asyncio.sleep(random.uniform(0.5, 2.0))
    # ... 发送请求
```

## 方案 C：使用实际浏览器 Cookie

WAF 依赖 Cookie 中的会话标识判断是否为真人。从 HAR 中提取**所有** Cookie 值，包括：

- `__cf_bm`（Cloudflare 机器人管理）
- `cf_clearance`（Cloudflare 验证通过标识）
- `_cfuvid`（Cloudflare 用户视频 ID）
- `session_id`、`csrf_token` 等站点特有值

```python
# 合并 HAR 中所有 Set-Cookie 到请求 Cookie 中
all_cookies = []
for entry in entries:
    resp = entry.get("response", {})
    for h in resp.get("headers", []):
        if h.get("name", "").lower() == "set-cookie":
            all_cookies.append(h.get("value", ""))
```

## 方案 D：IP / 代理轮换

如果 WAF 根据 IP 限流：

1. 使用住宅代理轮换
2. 每次请求使用不同的出口 IP
3. 或告知用户：该网站对 API 反代不友好，建议换其他目标

## 降级策略

如果以上方案全部无效，向用户输出：
- 哪些请求头被 WAF 拦截了
- 建议的方案（重新用浏览器导出一份干净的 HAR）
- 该站点可能不适合做 API 反代
