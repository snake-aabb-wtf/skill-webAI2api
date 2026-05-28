# Captcha 处理指南

## 本质

Captcha（验证码）是人机识别系统，用于区分真实用户和自动化脚本。常见类型包括 Cloudflare Turnstile、Google reCAPTCHA、hCaptcha。**Captcha 无法通过纯代码自动求解**，必须借助用户交互或浏览器环境。

## 检测线索

### 从响应状态码识别

- 状态码 **403** + 响应 HTML 包含验证脚本 → 被 Captcha 拦截
- 状态码 **503** + 响应含 `Just a moment...` → Cloudflare 质询中

### 从响应内容关键词识别

搜索响应文本中的关键字：

| 关键词 | 可能来源 |
|---|---|
| `turnstile` | Cloudflare Turnstile |
| `cf-turnstile` | Cloudflare Turnstile |
| `recaptcha` / `g-recaptcha` | Google reCAPTCHA |
| `hcaptcha` | hCaptcha |
| `cf-browser-verification` | Cloudflare JS 质询 |
| `_cf_chl_opt` | Cloudflare 挑战参数 |
| `challenge-platform` | Cloudflare 挑战平台 |

### 从请求重定向路径识别

如果请求被 `302` 或 `307` 重定向到以下路径，基本可确认遇上了验证：

```
/cdn-cgi/bot/captcha
/cdn-cgi/challenge-platform
/_challenge
/captcha
/verify
```

### 从 Cookie 缺失识别

请求缺少 `cf_clearance` 或 `__cf_bm` Cookie 时，Cloudflare 会触发验证。

## 策略选择

| 检测结果 | 推荐策略 |
|---|---|
| Cloudflare Turnstile | 引导用户导出 HAR（清浏览器后登录一次） |
| reCAPTCHA / hCaptcha | 无法绕过，建议换目标 |
| 仅有 `cf_clearance` 缺失 | 从 HAR 中找 Set-Cookie 提取 |
| 首次访问即触发验证 | 用户需在浏览器中手动过验证后再导出 HAR |

## 核心策略：引导用户生成有效 HAR

Captcha 无法自动解，但**用户已经在浏览器里过了一次验证**。你只是需要从 HAR 中拿到验证后的 Cookie。

引导话术模板：

```
该网站有验证码保护。请按以下步骤操作：
1. 打开隐身窗口，登录目标网站
2. 手动完成验证码（如果需要）
3. 发送一条聊天消息
4. F12 → Network → 右键 → Save all as HAR with content
5. 上传新的 HAR 文件
```

## 从 HAR 中提取验证凭证

即使聊天请求本身被拦截，HAR 中之前成功的 Set-Cookie 响应可能已经包含了验证通过的令牌：

| Cookie 名 | 来源 | 有效期 |
|---|---|---|
| `cf_clearance` | Cloudflare 验证通过 | 通常 30 分钟到 24 小时 |
| `__cf_bm` | Cloudflare 机器人管理 | 通常 30 分钟 |
| `_cfuvid` | Cloudflare 用户视频 ID | 会话级 |
| `session` | 站点自身登录会话 | 取决于站点 |

遍历 HAR 中所有响应的 Set-Cookie 头，合并到请求 Cookie 中。

## 边界情况

- **验证码类型无法识别**：响应不包含任何已知关键词 → 按通用验证码处理，要求用户重新导出 HAR
- **站点头部多次验证**：每次 POST 前都弹验证码 → 此站点不适合反代
- **Cookie 极短过期**：`cf_clearance` 仅几分钟 → 需告知用户频繁换 Cookie
- **部分请求通过、部分拦截**：可能是速率触发 → 加入请求间延迟

## 彻底失败

- 用户提供了 HAR 但仍然 403 → Cookie 已过期，让用户重新导出一份
- 目标使用了企业级 Bot Management（非 Turnstile）→ 无解，此站点不可反代
- 目标使用了自研验证码 → 逻辑未知，不可绕过
