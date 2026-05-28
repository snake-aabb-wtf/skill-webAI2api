"""
web2api config_tool — HAR → .env GUI configurator.
AI fills FIELD_LABELS, ENV_MAPPING, DISPLAY_FIELDS, MUTABLE_KEYS.
"""
import json
import os
import re
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SELF_DIR, ".env")

# ═══════════════════════════════════════════════════════════
# AI fills these four tables from the HAR analysis.
# ═══════════════════════════════════════════════════════════

FIELD_LABELS = {
    "base_url": "目标地址",
    "chat_endpoint": "聊天端点",
    "cookies": "Cookie",
    "auth_header": "Authorization",
    "auth_type": "认证类型",
    "is_streaming": "流式支持",
    "has_websocket": "WebSocket",
    "has_pow": "PoW 挑战",
    "content_field_path": "内容字段路径",
    "supported_params": "支持的参数",
}

ENV_MAPPING = [
    ("HAR_PATH",       "har_path",           "HAR 文件路径（用于重新解析）"),
    ("TARGET_URL",     "base_url",           "目标网站地址"),
    ("CHAT_ENDPOINT",  "chat_endpoint",      "聊天 API 端点路径"),
    ("COOKIES",        "cookies",            "登录 Cookie"),
    ("AUTH_HEADER",    "auth_header",        "Authorization 令牌"),
    ("AUTH_TYPE",      "auth_type",          "认证类型"),
    ("STREAMING",      "is_streaming",       "是否支持流式输出"),
    ("WEBSOCKET",      "has_websocket",      "是否使用 WebSocket"),
]

DISPLAY_FIELDS = [
    ("目标地址", "base_url"),
    ("聊天端点", "chat_endpoint"),
    ("认证类型", "auth_type"),
    ("流式支持", "is_streaming"),
    ("WebSocket", "has_websocket"),
    ("PoW 挑战", "has_pow"),
    ("Cookie", "cookies"),
    ("Authorization", "auth_header"),
    ("内容字段路径", "content_field_path"),
    ("支持的参数", "supported_params"),
]

MUTABLE_KEYS = {
    # AI 在此列出所有"异变"的 env key（鉴权凭证、token 等会过期的值）
    # 配置工具更新 .env 时只覆写这些 key，不动不易变部分
    "HAR_PATH", "TARGET_URL", "CHAT_ENDPOINT",
    "COOKIES", "AUTH_HEADER", "AUTH_TYPE",
    "STREAMING", "WEBSOCKET",
}

# ═══════════════════════════════════════════════════════════

IMMUTABLE_HEADER = """# ============================================
# 不易变部分 — 服务器配置（配置工具不会修改）
# ============================================
"""
MUTABLE_HEADER = """
# ============================================
# 异变部分 — 账号鉴权凭证（配置工具只更新此段）
# ============================================
"""

DEFAULT_IMMUTABLE = """MODEL_NAME=gpt-4o
HOST=0.0.0.0
PORT=8000
API_KEY=sk-web2api-placeholder
DSML_ENABLED=false"""


def parse_har_file(har_path: str) -> dict:
    from har_parser import parse_har
    a = parse_har(har_path)

    info = {
        "har_path": har_path,
        "base_url": a.base_url,
        "chat_endpoint": a.chat_endpoint,
        "cookies": a.cookies,
        "auth_header": a.auth_header or "",
        "auth_type": a.auth_type,
        "is_streaming": str(a.is_streaming),
        "has_websocket": str(a.has_websocket),
        "has_pow": str(a.has_pow),
        "content_field_path": a.content_field_path,
        "supported_params": ", ".join(a.supported_params) if a.supported_params else "",
        "header_count": str(len(a.headers)),
    }

    if a.has_websocket and a.ws:
        info["ws_url"] = a.ws.ws_url
        info["ws_input_field"] = a.ws.input_field
        info["ws_receive_field"] = a.ws.receive_field
        info["ws_type_field"] = a.ws.type_field or ""

    return info


def _extract_raw_har(har_path: str, field_name: str) -> str:
    """Extract a single field by regex from raw HAR text."""
    with open(har_path, "r", encoding="utf-8") as f:
        text = f.read()
    patterns = {
        "chat_session_id": r'"chat_session_id"\s*:\s*"([^"]+)"',
        "bl": r'[?&]bl=([^&\s"]+)',
        "f_sid": r'[?&]f\.sid=([^&\s"]+)',
        "at": r'[?&]at=([^&\s"]+)',
    }
    pat = patterns.get(field_name)
    if pat:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


def read_existing_env() -> dict:
    """Read current .env, return dict of all key=value pairs."""
    result = {}
    if not os.path.exists(ENV_PATH):
        return result
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
    return result


def merge_env_with_auth(auth_info: dict) -> str:
    """Merge newly parsed auth info with existing immutable config.

    - MUTABLE_KEYS: overwrite with values from auth_info
    - Non-MUTABLE_KEYS: keep existing .env values (or use defaults)
    """
    existing = read_existing_env()

    # Collect mutable lines from current parse result
    mutable_lines = []
    for env_key, info_key, _comment in ENV_MAPPING:
        if env_key not in MUTABLE_KEYS:
            continue
        val = auth_info.get(info_key)
        if val and str(val).strip():
            mutable_lines.append(f"{env_key}={val}")

    # Collect immutable lines from existing .env
    immutable_lines = []
    for k, v in existing.items():
        if k not in MUTABLE_KEYS:
            immutable_lines.append(f"{k}={v}")

    if not immutable_lines:
        immutable_lines = DEFAULT_IMMUTABLE.split("\n")

    result = IMMUTABLE_HEADER + "\n".join(immutable_lines) + "\n"
    result += MUTABLE_HEADER + "\n".join(mutable_lines) + "\n"
    return result


class ConfigToolGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("web2api — 代理配置工具")
        self.root.geometry("780x680")
        self.root.minsize(620, 520)

        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")

        main_frame = ttk.Frame(self.root, padding=16)
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main_frame, text="web2api 代理配置",
                  font=("Segoe UI", 16, "bold")).pack(anchor=tk.W, pady=(0, 4))
        ttk.Label(main_frame, text="选择 HAR 文件 → 自动解析 → 保存 .env → 启动代理",
                  foreground="#666").pack(anchor=tk.W, pady=(0, 16))

        file_frame = ttk.Frame(main_frame)
        file_frame.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(file_frame, text="HAR 文件:",
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=(0, 8))

        self.har_path_var = tk.StringVar()
        self.har_entry = ttk.Entry(file_frame, textvariable=self.har_path_var)
        self.har_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Button(file_frame, text="浏览...",
                   command=self._browse_har).pack(side=tk.LEFT)
        ttk.Button(file_frame, text="解析",
                   command=self._parse).pack(side=tk.LEFT, padx=(4, 0))

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Tab 1: extracted info
        self.info_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.info_frame, text="  解析信息  ")

        self.info_tree = ttk.Treeview(
            self.info_frame, columns=("key", "value"),
            show="tree headings", height=18)
        self.info_tree.heading("#0", text="字段")
        self.info_tree.heading("key", text="键")
        self.info_tree.heading("value", text="值")
        self.info_tree.column("#0", width=180, minwidth=120)
        self.info_tree.column("key", width=200, minwidth=120)
        self.info_tree.column("value", width=320, minwidth=200)

        vsb = ttk.Scrollbar(self.info_frame, orient=tk.VERTICAL,
                            command=self.info_tree.yview)
        self.info_tree.configure(yscrollcommand=vsb.set)
        self.info_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Tab 2: .env preview
        self.env_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.env_frame, text="  .env 预览  ")

        text_frame = ttk.Frame(self.env_frame)
        text_frame.pack(fill=tk.BOTH, expand=True)

        self.env_text = tk.Text(
            text_frame, wrap=tk.NONE, font=("Consolas", 10),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        env_vsb = ttk.Scrollbar(text_frame, orient=tk.VERTICAL,
                                command=self.env_text.yview)
        env_hsb = ttk.Scrollbar(text_frame, orient=tk.HORIZONTAL,
                                command=self.env_text.xview)
        self.env_text.configure(yscrollcommand=env_vsb.set,
                                xscrollcommand=env_hsb.set)
        self.env_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        env_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        env_hsb.pack(side=tk.BOTTOM, fill=tk.X)

        # Bottom buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(12, 0))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(btn_frame, textvariable=self.status_var,
                  font=("Segoe UI", 9),
                  foreground="#555").pack(side=tk.LEFT)

        ttk.Button(btn_frame, text="保存到 .env",
                   command=self._save_env).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btn_frame, text="启动代理服务器",
                   command=self._launch_server).pack(side=tk.RIGHT, padx=(8, 0))

        self._parsed_info = None
        self._env_content = ""

        self._try_load_existing()
        self.root.mainloop()

    def _try_load_existing(self):
        if os.path.exists(ENV_PATH):
            har_path = None
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("HAR_PATH="):
                        har_path = line.split("=", 1)[1].strip()
                        break
            if har_path and os.path.exists(har_path):
                self.har_path_var.set(har_path)
                self._parse()
            elif har_path:
                self.har_path_var.set(har_path)

    def _browse_har(self):
        path = filedialog.askopenfilename(
            title="选择 HAR 文件",
            filetypes=[("HAR files", "*.har"), ("All files", "*.*")],
            initialdir=SELF_DIR,
        )
        if path:
            self.har_path_var.set(path)

    def _parse(self):
        har_path = self.har_path_var.get().strip()
        if not har_path:
            messagebox.showwarning("提示", "请先选择一个 HAR 文件")
            return
        if not os.path.exists(har_path):
            messagebox.showerror("错误", f"文件不存在:\n{har_path}")
            return

        self.status_var.set("正在解析...")
        self.root.update()

        try:
            info = parse_har_file(har_path)
            self._parsed_info = info
        except Exception as e:
            messagebox.showerror("解析失败", f"解析 HAR 文件时出错:\n{e}")
            self.status_var.set("解析失败")
            return

        self._populate_tree(info)

        # Merge: 只更新异变部分，保留已有的不易变部分
        self._env_content = merge_env_with_auth(info)
        self.env_text.delete("1.0", tk.END)
        self.env_text.insert("1.0", self._env_content)
        self.env_text.see("1.0")

        self.status_var.set(f"✓ 解析完成 — 共 {len(DISPLAY_FIELDS)} 个字段")

    def _populate_tree(self, info: dict):
        for item in self.info_tree.get_children():
            self.info_tree.delete(item)

        for label, key in DISPLAY_FIELDS:
            val = info.get(key)
            if val is None:
                val = ""
            if isinstance(val, bool):
                val = "是" if val else "否"
            val_str = str(val)
            if len(val_str) > 120:
                val_str = val_str[:120] + "..."
            self.info_tree.insert("", tk.END, text=label,
                                  values=(key, val_str))

    def _save_env(self):
        if not self._env_content:
            messagebox.showwarning("提示", "请先解析一个 HAR 文件")
            return
        try:
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write(self._env_content)
            self.status_var.set(f"✓ 已保存到 {ENV_PATH}")
            messagebox.showinfo(
                "保存成功",
                f"配置已保存到:\n{ENV_PATH}\n\n"
                "现在可以启动代理服务器了。\n\n"
                "如需更换 Cookie：\n"
                "1. 双击 config_tool.py\n"
                "2. 重新选择 HAR 文件 → 解析\n"
                "3. 保存 → 启动")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _launch_server(self):
        if not self._env_content:
            if not messagebox.askyesno("提示", "尚未保存配置，是否先保存？"):
                return
            self._save_env()

        server_script = os.path.join(SELF_DIR, "server.py")
        if not os.path.exists(server_script):
            messagebox.showerror("错误", f"找不到 server.py:\n{server_script}")
            return

        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", server_script],
                cwd=SELF_DIR,
                creationflags=subprocess.CREATE_NO_NEW_CONSOLE
                if hasattr(subprocess, "CREATE_NO_NEW_CONSOLE") else 0,
            )
            bat_path = os.path.join(SELF_DIR, "start_proxy.bat")
            with open(bat_path, "w", encoding="utf-8") as f:
                f.write(
                    f'@echo off\r\n'
                    f'cd /d "{SELF_DIR}"\r\n'
                    f'python server.py\r\n'
                    f'pause\r\n'
                )
            self.status_var.set(f"✓ 服务器已启动 (PID: {proc.pid})")
            messagebox.showinfo(
                "服务器已启动",
                f"代理服务器已在后台启动 (PID: {proc.pid})\n\n"
                f"访问地址: http://localhost:8000\n\n"
                f"测试命令:\n"
                f'curl http://localhost:8000/v1/chat/completions '
                f'-H "Content-Type: application/json" '
                f"-d '{json.dumps({'model': 'gpt-4o', 'messages': [{'role': 'user', 'content': 'Hello'}]})}'"
            )
        except Exception as e:
            messagebox.showerror("启动失败", str(e))


if __name__ == "__main__":
    ConfigToolGUI()
