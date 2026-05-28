# WAF 绕过指南

## 本质

WAF（Web Application Firewall）通过分析请求特征识别并拦截非浏览器流量。判断依据包括：请求头缺失、HTTP 指纹不一致、请求频率异常、Cookie 不完整等。绕过 WAF 的关键是**让请求看起来和浏览器发出的完全一致**。

## 检测线索

### 从响应特征识别

| 特征 | 含义 |
|---|---|
| 状态码 403 + 空响应体 | WAF 直接拒绝 |
| 状态码 503 + `server: cloudflare` | Cloudflare 速率限制 |
| 响应头含 `cf-ray` | 经过了 Cloudflare |
| 响应 HTML 含 `window._cf_chl_opt` | Cloudflare JS 质询 |
| 状态码 406 | WAF 内容过滤 |
| 响应含 `blocked` / `denied` / `forbidden` | 通用拦截 |

### 从行为模式识别

- GET 正常、POST 被拦 → WAF 规则针对 POST 请求体
- 短时间多发被拦、慢速正常 → 速率触发
- 更换 IP 后问题消失 → 基于 IP 信誉

## 策略优先级（按成功率排列）

### 1. 完整复制浏览器请求头

WAF 最基础的检测是 HTTP 头完备性。从 HAR 的聊天请求中复制**全部**请求头：

```python
headers = {}
for h in chat_request.get("headers", []):
    name = h.get("name", "")
    if name.lower() != "content-length":
        headers[name] = h.get("value", "")
```

关键头列表：`User-Agent`（必须完整含 WebKit 版本）、`Sec-Ch-Ua` / `Sec-Ch-Ua-Platform` / `Sec-Ch-Ua-Mobile`（Client Hints 一致性校验）、`Accept` / `Accept-Language`、`Origin`（必须等于目标域名）、`Referer`（必须指向聊天页面）、`Sec-Fetch-Site` / `Sec-Fetch-Mode` / `Sec-Fetch-Dest`（Fetch 元数据头，缺失会触发 WAF）。

### 2. 从 Set-Cookie 补全 Cookie

WAF（尤其是 Cloudflare）依赖 Cookie 判断请求来源。用户提供的 Cookie 可能不完整——某些令牌是在多个 Set-Cookie 响应中累积的：

```python
def merge_cookies(user_cookie, har_entries):
    all_cookies = {}
    # 先从用户提供的 Cookie 解析
    for pair in user_cookie.split(";"):
        if "=" in pair:
            k, v = pair.strip().split("=", 1)
            all_cookies[k] = v
    # 再用 HAR 中的 Set-Cookie 补充
    for entry in har_entries:
        for h in entry.get("response", {}).get("headers", []):
            if h.get("name", "").lower() == "set-cookie":
                parts = h["value"].split(";")[0]
                if "=" in parts:
                    k, v = parts.split("=", 1)
                    all_cookies[k.strip()] = v.strip()
    return "; ".join(f"{k}={v}" for k, v in all_cookies.items())
```

特别关注：`cf_clearance`（Cloudflare 通行证）、`__cf_bm`、`_cfuvid`。

### 3. 请求节流

如果前两步后仍然被间歇性拦截，大概率是速率触发。加入随机延迟：

```python
import asyncio, random
await asyncio.sleep(random.uniform(1.0, 3.0))  # 每次请求前
```

不要用固定间隔——固定间隔反而更容易被识别为脚本。

### 4. 使用 HTTP/2

HAR 中每个 entry 都有 `httpVersion` 字段。如果浏览器用的是 HTTP/2 而你用 HTTP/1.1，WAF 可能检测到指纹差异。使用支持 HTTP/2 的客户端（`httpx` 默认支持 h2；`requests` 不支持，应避免使用）。

## 边界情况

- **请求体字段触发 WAF**：某些字段名可能被误认为注入（如 `"role":"system"`）。尝试简化
- **WebSocket 被 WAF 拦截**：保留完整的 Origin 和 Cookie，部分 WAF 不拦截 WS 升级（101）
- **Cloudflare 多层叠加**：Bot Management + WAF + Rate Limit 三层，每层都需要通过
- **TLS 指纹（JA3）检测**：极少数企业 WAF 会用到，无代码层解法

## 彻底失败

- 完整复制头 + 补全 Cookie + 延迟后仍然 403 → 可能涉及 TLS 指纹或 IP 信誉，此站点不适合反代
- 目标使用了自定义 WAF + JS 质询 → 需要运行完整浏览器引擎，超出本工具范围
