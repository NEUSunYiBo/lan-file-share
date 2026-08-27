"""用户文件 API：信息 / 鉴权 / 挂载列表 / 目录浏览 / 下载 / 预览。"""

import mimetypes
import os
import socket
import unicodedata
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, request
from werkzeug.wsgi import FileWrapper

import auth as auth_mod
import fs_search
from mounts import MountRegistry, PathEscapeError

# ── 文件流式响应（下载 / 预览共用）──
#
# 不用 send_file(conditional=True) 的原因：werkzeug 对 Range 请求会把响应包成
# _RangeWrapper，waitress 因此认不出文件对象、走不了 wsgi.file_wrapper 快速
# 通道，退化为"工作线程按小块读写"的慢路径，且该线程被整条流独占（客户端缓冲
# 满/暂停时阻塞不释放）。浏览器播放视频会发起多路 Range 请求，线程池（默认仅 8）
# 被占满后新媒体请求排队，表现为播放间歇性卡死。这里自行解析 Range，用
# wsgi.file_wrapper（waitress 下即 ReadOnlyFileBasedBuffer）包装，发送由 waitress
# 异步主循环完成，不占工作线程。

_RANGE_UNSATISFIABLE = "unsatisfiable"  # start 越界 → 416 的哨兵值


def _parse_range(header_value, size):
    """解析 Range: bytes=...（仅支持单段）。

    返回 (start, end)（闭区间）；空值/非法/多段返回 None（回退整文件 200）；
    start 越界返回 _RANGE_UNSATISFIABLE（调用方返回 416）。
    """
    if size <= 0 or not header_value or not header_value.startswith("bytes="):
        return None
    spec = header_value[len("bytes="):].strip()
    if not spec or "," in spec:
        return None  # 空 / 多段范围：忽略 Range 头，按整文件处理
    start_s, sep, end_s = spec.partition("-")
    if not sep:
        return None
    start_s, end_s = start_s.strip(), end_s.strip()
    try:
        if not start_s:
            if not end_s:
                return None
            suffix = int(end_s)  # bytes=-N：末尾 N 字节（探测 moov 等场景）
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
            if start < 0 or end < 0:
                return None
            end = min(end, size - 1)
    except ValueError:
        return None
    if start >= size or start > end:
        return _RANGE_UNSATISFIABLE
    return start, end


def _content_disposition(name):
    """attachment Disposition：纯 ASCII 直接引用；否则 ASCII 兜底 + RFC 5987 filename*。"""
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        simple = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
        # safe 与 RFC 5987 attr-char 一致；中文等以百分号编码传输
        return "attachment; filename=%s; filename*=UTF-8''%s" % (
            quote(simple) if simple else "download", quote(name, safe="!#$&+-.^_`|~"))
    return 'attachment; filename="%s"' % name.replace("\\", "\\\\").replace('"', '\\"')


class _LimitedFileReader:
    """限长文件读取器（无 wsgi.file_wrapper 的退化路径用）。

    最多读 length 字节即返回空串（避免迭代器送出超过 Content-Length 的数据），
    close() 连带关闭底层文件。
    """

    def __init__(self, f, length):
        self._f = f
        self._remain = length

    def read(self, size=-1):
        if self._remain <= 0:
            return b""
        if size is None or size < 0 or size > self._remain:
            size = self._remain
        data = self._f.read(size)
        self._remain -= len(data)
        return data

    def close(self):
        self._f.close()

    def __del__(self):
        try:
            self._f.close()
        except Exception:
            pass


def _file_response(real, mime, size, rng, attachment_name=None):
    """构造文件流式响应；rng 为 (start, end) 或 None（整文件）。

    waitress 环境下用其提供的 wsgi.file_wrapper（ReadOnlyFileBasedBuffer）裸包装，
    命中异步快速文件通道；其他环境（如 Flask 测试客户端）退化为 FileWrapper，
    并以限长读取器保证迭代器不会送出超过 Content-Length 的字节。
    """
    if rng:
        start, end = rng
        length = end - start + 1
        status = 206
    else:
        start, length, status = 0, size, 200

    f = open(real, "rb")
    try:
        if start:
            f.seek(start)
        file_wrapper = request.environ.get("wsgi.file_wrapper")
        if file_wrapper is not None:
            wrapper = file_wrapper(f)
        else:
            wrapper = FileWrapper(_LimitedFileReader(f, length))
        resp = Response(wrapper, status=status, mimetype=mime,
                        direct_passthrough=True)
    except Exception:
        f.close()
        raise
    resp.headers["Content-Length"] = str(length)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = "no-cache"
    if status == 206:
        resp.headers["Content-Range"] = "bytes %d-%d/%d" % (start, start + length - 1, size)
    if attachment_name:
        resp.headers["Content-Disposition"] = _content_disposition(attachment_name)
    return resp


def _range_not_satisfiable(size):
    """416：Range 越界，按 RFC 7233 携带 Content-Range: bytes */size。"""
    resp = jsonify({"error": "请求的范围无法满足"})
    resp.status_code = 416
    resp.headers["Content-Range"] = "bytes */%d" % size
    return resp


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
        return jsonify(fs_search.search_mounts(
            state, request.args.get("q", ""),
            share_id=request.args.get("share", "") or None,
            user_view=True))

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
        """下载文件（流式 + 断点续传，走 waitress 快速文件通道）。"""
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
        rng = _parse_range(request.headers.get("Range"), _size)
        if rng == _RANGE_UNSATISFIABLE:
            return _range_not_satisfiable(_size)
        mime = mimetypes.guess_type(real)[0] or "application/octet-stream"
        return _file_response(real, mime, _size, rng,
                              attachment_name=os.path.basename(real))

    @bp.get("/api/preview")
    def api_preview():
        """在线预览（inline + 正确 Content-Type + Range 流式，快速文件通道）。"""
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
        rng = _parse_range(request.headers.get("Range"), _size)
        if rng == _RANGE_UNSATISFIABLE:
            return _range_not_satisfiable(_size)
        mime = mimetypes.guess_type(real)[0] or "application/octet-stream"
        return _file_response(real, mime, _size, rng)

    return bp
