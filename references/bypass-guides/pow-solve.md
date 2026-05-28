# PoW (Proof-of-Work) 挑战绕过指南

## 本质

目标在接收聊天请求前强制客户端做一次哈希碰撞，证明请求来自真实浏览器而非脚本。碰撞难度由 `difficulty` 控制（例如 4 表示 SHA256 结果前缀必须有 4 个零字节），`expire_at` 设定了挑战的有效期窗口。

## 检测线索

### 从 HAR 路径识别

搜索所有 entry 的 URL 路径，包含以下关键字之一即可能为 PoW 挑战端点：

- `create_pow_challenge` — DeepSeek 标准路径
- `challenge` — 通用路径
- `pow` — 通用缩写

### 从响应结构识别

PoW 挑战的响应 JSON 通常包含这些字段。解析时注意嵌套路径，常见于 `data.biz_data.challenge` 下：

```python
def extract_challenge(resp_json):
    # DeepSeek 嵌套格式: data.biz_data.challenge
    biz = resp_json.get("data", {}).get("biz_data", {}).get("challenge", resp_json)
    return {
        "algorithm": biz.get("algorithm", "SHA256"),
        "challenge": biz["challenge"],
        "salt": biz["salt"],
        "expire_at": biz["expire_at"],
        "difficulty": biz["difficulty"],
        "signature": biz["signature"],
        "target_path": biz.get("target_path", "/api/chat"),
    }
```

### 从请求顺序推断

如果 HAR 中聊天 API 请求之前总是跟随着一个对未知端点的 POST 请求，且后者返回 JSON 中包含数值型 `difficulty` 字段，基本可以确认是 PoW。

## 策略选择

| 条件 | 推荐方案 |
|---|---|
| HAR 中可提取到 `.wasm` 文件 | 使用 WASM 求解，性能最优 |
| HAR 中无 wasm，但 PoW 结构清晰 | 纯 Python SHA256 碰撞 |
| difficulty > 5 | 告知用户难度过高，可能无法实时求解 |
| 响应结构不标准 | 先用通用解析提取字段，再逐个尝试哈希算法 |

## 两种求解方案

### 纯 Python 哈希（无额外依赖）

```python
import hashlib, base64, json

def solve(salt, expire_at, difficulty, target_path):
    nonce = 0
    while True:
        raw = f"{salt}{expire_at}{nonce}{target_path}"
        h = hashlib.sha256(raw.encode()).hexdigest()
        if h.startswith("0" * difficulty):
            return nonce
        nonce += 1

def encode_answer(challenge, salt, answer, signature, target_path):
    payload = {
        "algorithm": "DeepSeekHashV1",
        "challenge": challenge, "salt": salt,
        "answer": answer, "signature": signature,
        "target_path": target_path,
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
```

### WASM 求解（高性能，需提取 wasm 文件）

从 HAR 或 DevTools Sources 中定位 `.wasm` 文件后：

```python
from wasmtime import Store, Module, Instance
store = Store()
module = Module(store.engine, wasm_bytes)
instance = Instance(store, module, [])
exports = instance.exports(store)
solve_fn = exports.get("wasm_solve") or exports.get("solve")
answer = solve_fn(store, challenge, salt, expire_at, difficulty)
```

## 集成位置

PoW 是**每次请求前的前置步骤**，放在 `_ensure_auth_headers()` 中：

```python
def _ensure_auth_headers(self):
    if self.auth_type != "pow":
        return {}
    challenge = self._fetch_challenge()
    answer = solve(challenge["salt"], challenge["expire_at"],
                   challenge["difficulty"], challenge["target_path"])
    return {"X-DS-PoW-Response": encode_answer(
        challenge["challenge"], challenge["salt"],
        answer, challenge["signature"], challenge["target_path"])}
```

## 边界情况

- **挑战复用**：部分站点允许同一挑战在 expire 前多次使用，可缓存减少开销
- **难度波动**：同一站点在不同负载下的 difficulty 可能不同，求解时间不固定
- **算法变种**：除了 SHA256，还可能遇到 SHA512、BLAKE2、或自定义哈希
- **签名验证**：应答中的 signature 必须与挑战时返回的一致，否则服务端拒绝

## 彻底失败

- 求解时间超过 30 秒 → 提示用户 difficulty 过高，建议换目标
- 应答发送后仍返回 401 → 挑战已过期，需要重新获取
- 响应结构完全无法解析 → 此站点使用非标准 PoW，不可绕过
