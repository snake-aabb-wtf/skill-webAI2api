# WAF 绕过指南

## 本质

WAF（Web Application Firewall）通过分析请求特征识别并拦截非浏览器流量。判断依据包括：请求头缺失、HTTP 指纹不一致、请求频率异常、Cookie 不完整等。绕过 WAF 的关键是**让请求看起来和浏览器发出的完全一致**。

## 检测线索

### 从响应状态码和头识别

| 特征 | 含义 |
|---|---|
| 状态码 403 + 响应体空或极短 | WAF 直接拒绝 |
| 状态码 503 + `server: cloudflare` | Cloudflare 速率限制 |
| 响应头包含 `cf-ray` | 请求经过了 Cloudflare |
| 响应 HTML 含 `window._cf_chl_opt` | Cloudflare JS 质询 |
| 状态码 406 | WAF 内容过滤 |
| 响应含 `blocked`、`denied`、`forbidden` | 通用拦截页面 |

### 从行为模式识别

- **部分端点可用、部分不可用**：WAF 只保护了聊天 API 路径
- **GET 正常、POST 被拦**：WAF 规则针对 POST 请求体做了检查
- **短时间多发被拦、慢速正常**：速率触发的 WAF 规则
- **更换 IP 后问题消失**：基于 IP 信誉的 WAF

## 策略优先级

按成功率从高到低排列：

### 1. 完整复制浏览器请求头（最有效）

WAF 最基础的检测是 HTTP 头完备性。从 HAR 的聊天请求中复制**全部**请求头，不做任何删减。特别关键的字段：

| 请求头 | 作用 |
|---|---|
| `User-Agent` | 包含 Chrome/Safari 版本号、WebKit 版本、OS 标识的完整字符串 |
| `Sec-Ch-Ua` / `Sec-Ch-Ua-Platform` / `Sec-Ch-Ua-Mobile` | 客户端提示（Client Hints），WAF 会校验一致性 |
| `Accept` / `Accept-Language` / `Accept-Encoding` | 必须与浏览器一致，不能省略 |
| `Origin` | 必须等于目标域名 |
| `Referer` | 必须指向聊天页面 URL |
| `Sec-Fetch-Site` / `Sec-Fetch-Mode` / `Sec-Fetch-Dest` | Fetch 元数据头，缺失会触发 WAF |

这些头在 HAR 的 `request.headers` 中全有，直接复制即可。

### 2. 从 Set-Cookie 中补全 Cookie

WAF（尤其是 Cloudflare）依赖 Cookie 判断请求来源。如果直接使用用户提供的 Cookie 字符串可能不够，因为某些令牌是在登录过程中通过多个 Set-Cookie 累积的。

遍历 HAR 中所有响应（不仅是聊天请求）的 Set-Cookie 头，收集以下值：
- `cf_clearance` — Cloudflare 验证通行证
- `__cf_bm` — 机器人管理令牌
- `_cfuvid` — 用户视频指纹
- 站点自己的会话令牌

将这些值 merge 到最终的 Cookie 字符串中。

### 3. 请求节流

如果前两步后仍然被间歇性拦截，大概率是速率触发。在每次请求间加入 1-3 秒随机延迟。注意不要用固定间隔——固定间隔反而更容易被识别为脚本。

### 4. 使用与 HAR 相同的 HTTP 版本

HAR 中注明了每次请求的 HTTP 版本（`httpVersion` 字段）。如果浏览器用的是 HTTP/2 而你用 HTTP/1.1 发请求，WAF 可能检测到指纹差异。使用支持 HTTP/2 的客户端（`httpx` 默认支持 h2）。

## 边界情况

- **请求体格式被 WAF 检查**：JSON 中的某些字段名可能触发规则（如 `"role":"system"` 被误认为注入）。尝试简化请求体或调整字段名
- **WebSocket 连接被 WAF 拦截**：部分 WAF 不拦截 WebSocket 升级（101），但会检测 WS 帧内容。保留完整的 Origin 和 Cookie
- **WAF 同时检查 TLS 指纹**：JA3 指纹检测。需要使用真实浏览器的 TLS 库（无解，极少数企业 WAF 才会用）
- **Cloudflare 同时在多个层拦截**：Bot Management + WAF + Rate Limit 三层叠加。每一层都需要通过

## 彻底失败

- 完整复制请求头、补全 Cookie、加入延迟后仍然 403 → 可能涉及 TLS 指纹或 IP 信誉，此站点不适合反代
- 换用住宅代理后仍然被拦 → WAF 策略不依赖 IP，主动告知用户
- 目标使用了自定义 WAF + JS 质询 → 需要运行完整浏览器引擎，超出本工具范围
