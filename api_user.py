"""用户文件 API：信息 / 鉴权 / 挂载列表 / 目录浏览 / 下载 / 预览。"""

import mimetypes
import os
import socket

from flask import Blueprint, jsonify, request, send_file

import auth as auth_mod
from mounts import MountRegistry, PathEscapeError


def make_user_bp(state):
    """创建用户 API 蓝图。state 为 server.AppState。"""
    bp = Blueprint("user_api", __name__)

    def _authorized():
        """密码未开启直接放行；开启则校验 query key 或请求头 token。"""
        if not state.config.get("password_hash"):
            return True
        token = request.args.get("key") or request.headers.get("X-Auth-Token", "")
        return state.tokens.check(token)

    def _need_auth():
        return jsonify({"error": "需要密码"}), 401

    @bp.get("/api/info")
    def api_info():
        """服务器基本信息（聚合页探测在线状态用，免鉴权）。"""
        return jsonify({
            "name": socket.gethostname(),
            "version": "1.0.0",
            "need_password": bool(state.config.get("password_hash")),
        })

    @bp.post("/api/auth")
    def api_auth():
        """用密码换取访问 token；未开启密码时直接返回成功。"""
        data = request.get_json(silent=True) or request.form or {}
        password = str(data.get("password", ""))
        stored = state.config.get("password_hash")
        if not stored:
            return jsonify({"ok": True, "token": None})
        if auth_mod.verify_password(password, stored):
            return jsonify({"ok": True, "token": state.tokens.issue()})
        return jsonify({"ok": False, "error": "密码错误"}), 401

    def _user_mount(share_id):
        """取挂载供用户端访问；系统挂载未开共享、或被暂时隐藏时不可见。"""
        m = state.registry.get(share_id)
        if m and m.get("system") and not state.uploads_shared():
            return None
        if m and m.get("hidden"):
            return None
        return m

    @bp.get("/api/shares")
    def api_shares():
        """挂载点列表（不暴露真实磁盘路径）；上传文件夹置顶。"""
        if not _authorized():
            return _need_auth()
        result = []
        for m in state.registry.list():
            if m.get("system") and not state.uploads_shared():
                continue  # 上传文件夹未共享：用户端不可见
            if m.get("hidden"):
                continue  # 暂时隐藏的挂载：用户端不可见
            result.append({
                "id": m["id"],
                "name": m["name"],
                "type": "dir" if os.path.isdir(m["path"]) else "file",
            })
        return jsonify(result)

    @bp.get("/api/list")
    def api_list():
        """列出挂载点内某目录的内容（目录优先、名称排序）。"""
        if not _authorized():
            return _need_auth()
        mount = _user_mount(request.args.get("share", ""))
        if not mount:
            return jsonify({"error": "挂载点不存在"}), 404
        rel = request.args.get("path", "")
        try:
            real = MountRegistry.safe_join(mount["path"], rel)
        except PathEscapeError:
            return jsonify({"error": "路径越界"}), 403

        # 用户端请求的目录本身被排除 → 拒绝（防枚举）
        if rel and state.registry.is_excluded(mount["id"], rel):
            return jsonify({"error": "路径已被排除"}), 403

        # 定位到单个文件（含单文件挂载）：返回该文件自身
        if os.path.isfile(real):
            st = os.stat(real)
            return jsonify([{
                "name": os.path.basename(real),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "is_dir": False,
            }])

        try:
            entries = []
            with os.scandir(real) as it:
                for entry in it:
                    # 排除项（取消共享）对用户端隐藏
                    rel_entry = ((rel.rstrip("/\\") + "/") if rel else "") + entry.name
                    if state.registry.is_excluded(mount["id"], rel_entry):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                        entries.append({
                            "name": entry.name,
                            "size": 0 if entry.is_dir() else st.st_size,
                            "mtime": int(st.st_mtime),
                            "is_dir": entry.is_dir(),
                        })
                    except OSError:
                        continue  # 单个条目读不了就跳过
        except FileNotFoundError:
            return jsonify({"error": "挂载路径不存在（磁盘可能已移除）"}), 404
        except NotADirectoryError:
            return jsonify({"error": "不是目录"}), 400
        except OSError:
            return jsonify({"error": "无法读取目录（无权限）"}), 403

        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return jsonify(entries)

    @bp.get("/api/search")
    def api_search():
        """全局搜索：在挂载点内递归按文件名搜索（不区分大小写的子串匹配）。

        query: q=关键字（必填）；share=挂载id（可选，限定单个挂载）
        返回最多 500 条：[{share, share_name, path, name, size, mtime, is_dir}]
        """
        if not _authorized():
            return _need_auth()
        q = request.args.get("q", "").strip().lower()
        if not q:
            return jsonify([])
        share_filter = request.args.get("share", "") or None

        MAX_RESULTS = 500
        results = []
        for m in state.registry.list():
            if m.get("system") and not state.uploads_shared():
                continue  # 上传文件夹未共享：用户端搜不到
            if m.get("hidden"):
                continue  # 暂时隐藏的挂载：用户端搜不到
            if share_filter and m["id"] != share_filter:
                continue
            root = m["path"]
            if os.path.isfile(root):
                # 单文件挂载：只匹配自身文件名
                if q in os.path.basename(root).lower():
                    st = os.stat(root)
                    results.append({
                        "share": m["id"], "share_name": m["name"],
                        "path": "", "name": os.path.basename(root),
                        "size": st.st_size, "mtime": int(st.st_mtime), "is_dir": False,
                    })
                continue

            # 目录挂载：递归遍历（忽略无权限等错误，尽力返回能搜到的）
            for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
                # 排除项剪枝：跳过被排除的目录（不进入）与条目
                rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
                if rel_dir != "." and state.registry.is_excluded(m["id"], rel_dir):
                    dirnames[:] = []
                    continue
                if rel_dir == ".":
                    dirnames[:] = [d for d in dirnames if not state.registry.is_excluded(m["id"], d)]
                    filenames = [f for f in filenames if not state.registry.is_excluded(m["id"], f)]
                else:
                    dirnames[:] = [d for d in dirnames
                                   if not state.registry.is_excluded(m["id"], rel_dir + "/" + d)]
                    filenames = [f for f in filenames
                                 if not state.registry.is_excluded(m["id"], rel_dir + "/" + f)]
                for name in dirnames + filenames:
                    if q not in name.lower():
                        continue
                    real = os.path.join(dirpath, name)
                    try:
                        st = os.stat(real)
                    except OSError:
                        continue
                    rel = os.path.relpath(real, root).replace("\\", "/")
                    results.append({
                        "share": m["id"], "share_name": m["name"],
                        "path": rel, "name": name,
                        "size": 0 if os.path.isdir(real) else st.st_size,
                        "mtime": int(st.st_mtime), "is_dir": os.path.isdir(real),
                    })
                    if len(results) >= MAX_RESULTS:
                        return jsonify(results)
        return jsonify(results)

    def _resolve_mounted_file():
        """校验并解析要操作的文件，返回 (绝对路径, None) 或 (None, 错误响应)。"""
        mount = _user_mount(request.args.get("share", ""))
        if not mount:
            return None, (jsonify({"error": "挂载点不存在"}), 404)
        rel = request.args.get("path", "")
        try:
            real = MountRegistry.safe_join(mount["path"], rel)
        except PathEscapeError:
            return None, (jsonify({"error": "路径越界"}), 403)
        if state.registry.is_excluded(mount["id"], rel):
            return None, (jsonify({"error": "路径已被排除"}), 403)
        if not os.path.isfile(real):
            return None, (jsonify({"error": "文件不存在"}), 404)
        return real, None

    @bp.get("/api/download")
    def api_download():
        """下载文件（流式 + 断点续传）。"""
        if not _authorized():
            return _need_auth()
        real, err = _resolve_mounted_file()
        if err:
            return err
        try:
            _size = os.path.getsize(real)
        except OSError:
            _size = 0
        state.log.record("download", request.remote_addr or "?",
                         os.path.basename(real), _size)
        return send_file(
            real,
            as_attachment=True,
            download_name=os.path.basename(real),
            conditional=True,  # 支持 Range 请求
        )

    @bp.get("/api/preview")
    def api_preview():
        """在线预览（inline + 正确 Content-Type + Range 流式）。"""
        if not _authorized():
            return _need_auth()
        real, err = _resolve_mounted_file()
        if err:
            return err
        try:
            _size = os.path.getsize(real)
        except OSError:
            _size = 0
        # 预览按 (ip, 路径) 去重：视频拖动产生的多次 Range 请求不刷屏
        state.log.record_preview(request.remote_addr or "?",
                                 os.path.basename(real), _size, real)
        mime = mimetypes.guess_type(real)[0] or "application/octet-stream"
        return send_file(real, as_attachment=False, mimetype=mime, conditional=True)

    return bp
