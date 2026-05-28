# Captcha 挑战绕过指南

## 适用场景

目标网站弹出验证码（CAPTCHA）拦截自动请求，常见于 Cloudflare Turnstile、Google reCAPTCHA、hCaptcha。

## 检测方法

HAR 文件中出现以下特征：

```
响应包含 "captcha"、"turnstile"、"recaptcha"、"hcaptcha" 字段
请求被重定向到 /challenge、/captcha、/under-attack 等路径
响应状态码 403 且包含验证页面 HTML
```

## 方案 A：引导用户手动获取 Cookie（推荐）

Captcha 无法自动求解。最佳方案是让用户在浏览器中完成验证后导出 HAR：

```
1. 在浏览器中打开目标网站
2. 完成验证码（手动点击）
3. 正常登录并发送一条聊天消息
4. F12 → Network → Save all as HAR with content
5. 重新上传 HAR 文件
```

HAR 中已经包含了验证后的 Cookie，直接使用即可。

## 方案 B：Cloudflare Turnstile 特殊处理

如果检测到 Cloudflare Turnstile，尝试以下步骤：

1. 请求目标页面时带上 `User-Agent` 和 `Accept-Language` 等完整浏览器头
2. 先用 `GET` 请求首页，收集页面中的 `turnstile` token
3. 部分网站允许在 Cookie 中直接携带 `cf_clearance` 绕过

```python
# 从 HAR 的响应 Cookie 中提取 cf_clearance
def extract_cf_clearance(entries):
    for entry in entries:
        resp = entry.get("response", {})
        for cookie in resp.get("cookies", []):
            if cookie.get("name") == "cf_clearance":
                return cookie.get("value")
    return None
```

## 方案 C：降低请求频率

部分 WAF 将高频请求误判为机器人。如果请求被拦截：

1. 在每次请求之间加入 1-3 秒延迟
2. 确保请求头与浏览器完全一致（从 HAR 中复制完整 headers）
3. 在请求头中添加 `Accept-Language: zh-CN,zh;q=0.9`

## 无法绕过的情况

- hCaptcha / reCAPTCHA v3 等需要视觉识别的验证码
- 目标网站使用了人机验证且没有提供 API 免验证通道
- 这种情况下只能告知用户：该网站不支持自动化 API 反代
