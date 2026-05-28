# Captcha 处理指南

## 本质

Captcha（验证码）是人机识别系统，用于区分真实用户和自动化脚本。常见类型包括 Cloudflare Turnstile、Google reCAPTCHA、hCaptcha。**Captcha 无法通过纯代码自动求解**，必须借助用户交互或浏览器环境。

## 检测线索

### 从响应状态码 + 内容识别

```python
def detect_captcha(resp_text, resp_headers):
    keywords = {
        "turnstile": "Cloudflare Turnstile",
        "cf-turnstile": "Cloudflare Turnstile",
        "recaptcha": "Google reCAPTCHA",
        "g-recaptcha": "Google reCAPTCHA",
        "hcaptcha": "hCaptcha",
        "cf-browser-verification": "Cloudflare JS Challenge",
        "_cf_chl_opt": "Cloudflare Challenge",
    }
    for kw, name in keywords.items():
        if kw in resp_text:
            return name
    # 状态码 403 + 空白页也可能是拦截
    return None
```

### 从请求重定向路径识别

如果请求被 `302`/`307` 重定向到以下路径，基本可确认遇上了验证：

```
/cdn-cgi/bot/captcha
/cdn-cgi/challenge-platform
/_challenge
/captcha
```

### 从 Cookie 缺失识别

```python
if "cf_clearance" not in cookies and "Cloudflare" in server_header:
    # 缺少 cf_clearance，Cloudflare 大概率触发了验证
```

## 策略选择

| 检测结果 | 推荐策略 |
|---|---|
| Cloudflare Turnstile | 引导用户导出 HAR（清浏览器后登录一次） |
| reCAPTCHA / hCaptcha | 无法绕过，建议换目标 |
| 仅有 `cf_clearance` 缺失 | 从 HAR 的 Set-Cookie 中提取 |
| 首次访问即触发验证 | 用户需在浏览器中过验证后再导出 HAR |

## 从 HAR 中提取验证凭证

即使聊天请求本身被拦截，HAR 中之前成功的 Set-Cookie 响应可能已包含验证通过的令牌：

```python
def extract_cookies_from_har(entries):
    cookies = {}
    for entry in entries:
        for h in entry.get("response", {}).get("headers", []):
            if h.get("name", "").lower() != "set-cookie":
                continue
            # Set-Cookie: cf_clearance=xxx; path=/; domain=.example.com
            parts = h["value"].split(";")[0]  # 取分号前
            if "=" in parts:
                k, v = parts.split("=", 1)
                cookies[k.strip()] = v.strip()
    return cookies
```

## 核心策略：引导用户生成有效 HAR

Captcha 无法自动解，但用户已经在浏览器里过了一次验证。只需从 HAR 中拿到验证后的 Cookie：

```
该网站有验证码保护。请按以下步骤操作：
1. 打开隐身窗口，登录目标网站
2. 手动完成验证码（如果需要）
3. 发送一条聊天消息
4. F12 → Network → 右键 → Save all as HAR with content
5. 上传新的 HAR 文件
```

Set-Cookie 中特别关注：`cf_clearance`（Cloudflare 通行证，有效期通常 30m-24h）、`__cf_bm`（机器人管理令牌）、`_cfuvid`。

## 边界情况

- **站点头部多次验证**：每次 POST 前都弹验证码 → 此站点不适合反代
- **Cookie 极短过期**：`cf_clearance` 仅几分钟 → 告知用户频繁换 Cookie
- **部分请求通过、部分拦截**：可能是速率触发 → 加入请求间延迟

## 彻底失败

- 用户提供了 HAR 但仍然 403 → Cookie 已过期，让用户重新导出
- 目标使用了企业级 Bot Management → 无解，此站点不可反代
