# PoW (Proof-of-Work) 挑战绕过指南

## 适用场景

目标聊天 API 在发送消息前需要先求解一个哈希碰撞挑战。常见于 DeepSeek 等国产 AI 网站。

## 检测方法

HAR 文件中出现以下请求路径之一：

```
/api/v0/chat/create_pow_challenge
/api/challenge
/api/v1/challenge
pow
```

响应 JSON 包含字段：`algorithm`、`challenge`、`salt`、`expire_at`、`difficulty`、`signature`。

## 求解方案

### 方案 A：纯 Python 哈希求解（推荐，无额外依赖）

```python
import hashlib, json, base64, httpx

def solve_pow(base_url, headers):
    # 1. 获取挑战
    resp = httpx.post(
        f"{base_url}/api/v0/chat/create_pow_challenge",
        json={"target_path": "/api/v0/chat/completion"},
        headers=headers, timeout=15,
    )
    biz = resp.json().get("data", {}).get("biz_data", {}).get("challenge", {})
    salt = biz["salt"]
    expire_at = biz["expire_at"]
    difficulty = biz["difficulty"]
    signature = biz["signature"]
    challenge = biz["challenge"]
    target_path = biz.get("target_path", "/api/v0/chat/completion")

    # 2. 暴力碰撞
    nonce = 0
    while True:
        raw = f"{salt}{expire_at}{nonce}{target_path}"
        h = hashlib.sha256(raw.encode()).hexdigest()
        if h.startswith("0" * difficulty):
            break
        nonce += 1

    # 3. 编码应答
    answer = json.dumps({
        "algorithm": "DeepSeekHashV1",
        "challenge": challenge,
        "salt": salt,
        "answer": nonce,
        "signature": signature,
        "target_path": target_path,
    }, separators=(",", ":"))
    return {"X-DS-PoW-Response": base64.b64encode(answer.encode()).decode()}
```

### 方案 B：WASM 求解（性能高，但需要从目标站点提取 wasm）

```python
from wasmtime import Store, Module, Instance

class WASMSolver:
    def __init__(self, wasm_bytes):
        self.store = Store()
        module = Module(self.store.engine, wasm_bytes)
        self.instance = Instance(self.store, module, [])
        self.exports = self.instance.exports(self.store)
        self.solve_fn = self.exports.get("wasm_solve") or self.exports.get("solve")
        self.malloc = self.exports["__wbindgen_export_0"]
        self.memory = self.exports["memory"]

    def solve(self, challenge, salt, expire_at, difficulty):
        # 调用 wasm 导出的 solve 函数
        return self.solve_fn(self.store, challenge, salt, expire_at, difficulty)
```

### 获取 wasm 的方法

1. 在 HAR 的 JS chunk 请求中找到 `.wasm` 文件
2. 或者从浏览器 DevTools → Sources → Page 中搜索 `.wasm`
3. 右键 → Save as 保存到本地

## 集成到 adapter

```python
class ChatAdapter:
    def __init__(self, ...):
        self.auth_type = "pow"
        self._solver = None  # 延迟初始化

    def _ensure_auth_headers(self):
        if self.auth_type == "pow":
            return solve_pow(self.base_url, self.headers)
        return {}
```

## 注意

- 挑战有 `expire_at` 过期时间，必须在过期前求解并发送聊天请求
- 每次聊天请求都需要重新求解（不能复用）
- 如果用户 Cookie 已过期，PoW 请求也会返回 401
