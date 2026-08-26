"""上传 API：接收用户端文件/文件夹，保存到 upload_dir（同名自动重命名）。"""

import logging
import os
import re

from flask import Blueprint, jsonify, request

log = logging.getLogger("lan_share.upload")

# Windows 文件名非法字符 + 控制字符
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(name):
    """净化文件名：取 basename、非法字符替换为 _、限制长度。"""
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = _ILLEGAL.sub("_", name).strip().strip(".")
    return name[:200] or "unnamed"


def _safe_rel_dirs(rel):
    """客户端相对路径 → 安全目录段列表（文件夹上传按原结构保存）。

    逐段净化并丢弃 . / .. 等穿越段，最后一段是文件名（由上传字段决定），
    所以这里只返回目录段，永不越出 upload_dir。
    """
    dirs = []
    for seg in str(rel or "").replace("\\", "/").split("/")[:-1]:
        seg = seg.strip()
        if not seg or seg in (".", ".."):
            continue
        name = _safe_name(seg)
        if name and name not in (".", ".."):
            dirs.append(name)
    return dirs


def _unique_path(directory, filename):
    """同名自动重命名：file.txt → file(1).txt → file(2).txt（永不覆盖）。"""
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base}({i}){ext}")
        i += 1
    return candidate


def make_upload_bp(state):
    """创建上传蓝图。state 为 server.AppState（含 config / tokens / log）。"""
    bp = Blueprint("upload_api", __name__)

    def _authorized():
        """与 api_user 相同的鉴权规则：未设密码放行，否则校验 token。"""
        if not state.config.get("password_hash"):
            return True
        token = request.args.get("key") or request.headers.get("X-Auth-Token", "")
        return state.tokens.check(token)

    @bp.post("/api/upload")
    def api_upload():
        if not _authorized():
            return jsonify({"error": "需要密码"}), 401
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "缺少文件"}), 400

        directory = state.config.get("upload_dir") or "."
        filename = _safe_name(f.filename)
        # 文件夹上传：path = 客户端相对路径（如 folder/sub/a.txt），按原目录结构保存
        dirs = _safe_rel_dirs(request.form.get("path", ""))
        try:
            dest_dir = os.path.join(directory, *dirs) if dirs else directory
            os.makedirs(dest_dir, exist_ok=True)  # 运行中被删时兜底重建
            dest = _unique_path(dest_dir, filename)
            f.save(dest)
            size = os.path.getsize(dest)
        except OSError as e:
            log.warning("上传保存失败: %s", e)
            return jsonify({"error": f"保存失败：{e}"}), 500

        saved = "/".join(dirs + [os.path.basename(dest)])
        state.log.record("upload", request.remote_addr or "?", saved, size)
        log.info("接收上传: %s (%d 字节, 来自 %s)", saved, size, request.remote_addr)
        return jsonify({"ok": True, "filename": saved, "size": size})

    return bp
