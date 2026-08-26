"""配置持久化：读写 config.json（端口 / 密码哈希 / 挂载列表）。"""

import json
import os
import sys
import threading

DEFAULT_PORT = 8000

# 打包成 exe 后配置放在 exe 同级目录（可持久化）；
# 源码运行时放在项目根目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

# 仪表板卡片默认顺序（id 与 admin.html 中 DASH_CARDS 一致）
DEFAULT_DASHBOARD_CARDS = [
    {"id": "stats", "enabled": True, "order": 1},
    {"id": "type-pie", "enabled": True, "order": 2},
    {"id": "type-size", "enabled": True, "order": 3},
    {"id": "upload-trend", "enabled": True, "order": 4},
    {"id": "download-trend", "enabled": True, "order": 5},
    {"id": "recent", "enabled": True, "order": 6},
]


def _safe_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _normalize_dashboard(data):
    """仪表板卡片配置：未知 id 丢弃、缺省补默认、按 order 排序并重排为连续序号。"""
    known = {c["id"] for c in DEFAULT_DASHBOARD_CARDS}
    configured, seen = [], set()
    raw_dash = data.get("dashboard")
    if isinstance(raw_dash, dict):
        raw_cards = raw_dash.get("cards")
        if isinstance(raw_cards, list):
            for c in raw_cards:
                if isinstance(c, dict) and c.get("id") in known and c["id"] not in seen:
                    seen.add(c["id"])
                    configured.append({
                        "id": str(c["id"]),
                        "enabled": bool(c.get("enabled", True)),
                        "order": _safe_int(c.get("order"), 99),
                    })
    for c in DEFAULT_DASHBOARD_CARDS:  # 未配置的已知卡片补到末尾（order 压过任何用户配置）
        if c["id"] not in seen:
            configured.append(dict(c, order=99 + len(configured)))
    configured.sort(key=lambda c: c["order"])
    for i, c in enumerate(configured):
        c["order"] = i + 1
    return {"cards": configured}

# 同一进程内可能多处调用保存，加锁避免并发写坏文件
_write_lock = threading.Lock()


def _normalize(data):
    """校验并规范配置字段，坏值回退默认。"""
    # 端口
    try:
        port = int(data.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    if not (1 <= port <= 65535):
        port = DEFAULT_PORT

    # 密码哈希：None 或 "salt$hex" 字符串
    password_hash = data.get("password_hash")
    if not isinstance(password_hash, str) or "$" not in password_hash:
        password_hash = None

    # 挂载列表：仅保留字段完整的条目
    mounts = []
    raw_mounts = data.get("mounts")
    if isinstance(raw_mounts, list):
        for m in raw_mounts:
            if not isinstance(m, dict):
                continue
            path = m.get("path")
            if not path or not isinstance(path, str):
                continue
            entry = {
                "id": str(m.get("id") or ""),
                "name": str(m.get("name") or os.path.basename(path.rstrip("\\/")) or path),
                "path": os.path.abspath(path),
            }
            # 暂时隐藏：用户端不可见（管理端仍显示并标注）
            if m.get("hidden"):
                entry["hidden"] = True
            # 排除规则：字符串列表，无效字段整体丢弃
            raw_excluded = m.get("excluded")
            if isinstance(raw_excluded, list):
                excluded = [str(e).replace("\\", "/") for e in raw_excluded if str(e).strip()]
                if excluded:
                    entry["excluded"] = excluded
            mounts.append(entry)
    # 上传接收目录：字符串路径；相对路径基于 BASE_DIR 解析，坏值回退默认
    upload_dir = data.get("upload_dir")
    if not isinstance(upload_dir, str) or not upload_dir.strip():
        upload_dir = DEFAULT_UPLOAD_DIR
    else:
        upload_dir = os.path.abspath(os.path.join(BASE_DIR, upload_dir.strip()))

    # 上传自动共享开关：仅接受布尔，其他回退 True
    auto_share_uploads = data.get("auto_share_uploads")
    if not isinstance(auto_share_uploads, bool):
        auto_share_uploads = True

    return {
        "port": port, "password_hash": password_hash, "mounts": mounts,
        "upload_dir": upload_dir,
        "auto_share_uploads": auto_share_uploads,
        "dashboard": _normalize_dashboard(data),
    }


def load(path=CONFIG_PATH):
    """读取配置；文件不存在或损坏时返回默认配置（不抛异常）。"""
    if not os.path.exists(path):
        return _normalize({})
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        data = {}
    return _normalize(data)


def save(cfg, path=CONFIG_PATH):
    """保存配置到磁盘（原子写：先写临时文件再替换），返回规范化后的数据。"""
    data = _normalize(cfg)
    with _write_lock:
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    return data
