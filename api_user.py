"""用户文件 API：信息 / 鉴权 / 挂载列表 / 目录浏览 / 下载 / 预览。"""

import io
import mimetypes
import os
import socket
import threading
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


# ── 虚拟 faststart 预览：让 >1GB 的 MP4 在手机浏览器里也能预览 ──
#
# 背景：部分手机浏览器（Android 媒体栈）无法预览「moov 原子位于文件末尾、
# 体积超过 1GB」的 MP4——播放器初始化时必须先向文件尾部（偏移 >2^30）
# 发起 Range 请求读取 moov，此类实现处理不了该请求，表现为"无法预览"。
# 实测证据：同一批手机拍摄的 MP4，<800MB 的同结构文件可连续播放，
# 1.06GB 的文件数秒内失败；桌面 Chrome 无此问题（会正常做尾部引导请求）。
#
# 方案：预览时按「虚拟重排」提供字节流——对 [ftyp…][mdat][moov] 的非分片
# MP4，映射为 [ftyp…][moov(块偏移已修正)][mdat]，总长度不变：
#   · 播放器从文件头部即可取得全部元数据，无需任何尾部请求（faststart 效果）；
#   · moov 内 stco/co64 块偏移表整体后移 moov 盒子大小，保持指向真实数据；
#   · 请求的 Range 在"虚拟空间"解析（长度/Content-Range 与原始文件一致），
#     服务时反查真实文件拼接（大多数区间落在单一段，仍走快速文件通道）；
#   · 仅预览接口启用；下载接口永远返回原始字节（文件完整性不受影响）。
# 任一前提不满足（分片 MP4、多 mdat、偏移溢出、补丁失败等）时，自动回退
# 为原始字节服务，行为与修复前一致。

FASTSTART_PREVIEW_MIN = 1000 ** 3  # 仅处理 >1GB 的文件（更小的文件各端本就正常）
_MOOV_PATCH_LIMIT = 4              # moov 补丁的内存缓存条数（每条约几百 KB）
_moov_patch_cache = {}             # (路径, mtime_ns, 文件大小) -> 补丁后的 moov
_moov_patch_lock = threading.Lock()


def _box_iter(f, start, end):
    """遍历 [start, end) 内的 ISO-BMFF 顶层盒子。

    yield (type, box_start, payload_start, box_end)；支持 32/64 位尺寸。
    结构异常（越界/尺寸非法）时停止遍历（调用方按"未找到"处理）。
    """
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size = int.from_bytes(hdr[0:4], "big")
        btype = hdr[4:8]
        payload = pos + 8
        if size == 1:  # 64 位尺寸
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = int.from_bytes(ext, "big")
            payload = pos + 16
        elif size == 0:  # 延续到文件末尾
            size = end - pos
        if size < payload - pos or pos + size > end:
            return
        yield btype, pos, payload, pos + size
        pos += size


def _patch_chunk_offsets(payload, shift):
    """把 moov 负载内所有 stco/co64 块偏移表整体 +shift。

    成功返回修补后的 bytes；找不到偏移表或修补会溢出 32/64 位时返回 None。
    """
    buf = bytearray(payload)
    hits = []

    def walk(lo, hi):
        pos = lo
        while pos + 8 <= hi:
            size = int.from_bytes(buf[pos:pos + 4], "big")
            typ = bytes(buf[pos + 4:pos + 8])
            hdr = 8
            if size == 1:
                if pos + 16 > hi:
                    raise ValueError
                size = int.from_bytes(buf[pos + 8:pos + 16], "big")
                hdr = 16
            elif size == 0:
                size = hi - pos
            if size < hdr or pos + size > hi:
                raise ValueError
            if typ in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                walk(pos + hdr, pos + size)
            elif typ in (b"stco", b"co64"):
                w = 4 if typ == b"stco" else 8
                n = int.from_bytes(buf[pos + hdr + 4:pos + hdr + 8], "big")
                base = pos + hdr + 8
                if n > (size - hdr - 8) // w:  # 条目数与盒子长度不一致
                    raise ValueError
                for i in range(n):
                    o = base + i * w
                    v = int.from_bytes(buf[o:o + w], "big") + shift
                    if v >= 1 << (w * 8):
                        raise ValueError  # stco 32 位放不下 → 放弃（走原始服务）
                    buf[o:o + w] = v.to_bytes(w, "big")
                hits.append(True)
            pos += size

    try:
        walk(0, len(buf))
    except ValueError:
        return None
    return bytes(buf) if hits else None


class _FaststartView:
    """[ftyp…][mdat…][moov] → [ftyp…][moov][mdat…] 的虚拟文件视图（长度不变）。

    只做映射与 moov 修补，不复制大文件数据。segments() 把虚拟区间
    [start, start+length) 拆成 1~3 个真实来源段："file" 段直接从原文件
    seek 读取；"moov" 段读取（补丁后的）moov 内存切片。
    """

    def __init__(self, real, size):
        self.real = real
        self.total = size
        st = os.stat(real)
        self._mtime_ns = st.st_mtime_ns
        with open(real, "rb") as f:
            boxes = list(_box_iter(f, 0, size))
        mdats = [b for b in boxes if b[0] == b"mdat"]
        moovs = [b for b in boxes if b[0] == b"moov"]
        if len(mdats) != 1 or len(moovs) != 1:
            raise ValueError("需要恰好一个 mdat 与一个 moov")
        if any(b[0] in (b"moof", b"sidx", b"styp") for b in boxes):
            raise ValueError("分片 MP4 不适用")
        mdat, moov = mdats[0], moovs[0]
        if moov[1] <= mdat[1]:
            raise ValueError("moov 不在 mdat 之后，无需重排")
        if moov[3] != size:
            raise ValueError("moov 不是末尾盒子，布局不适用")
        self.head_end = mdat[1]                    # 头部（ftyp/free…）长度 = mdat 盒子起点
        self.moov_off = moov[1]
        self.moov_size = moov[3] - moov[1]         # moov 盒子总长（含头）
        self.moov_hdr = moov[2] - moov[1]          # moov 盒子头长度（8 或 16）
        self._patched = None
        self._lock = threading.Lock()

    # ── moov 补丁（带跨请求缓存）──

    def ensure_moov(self):
        """读取并修补 moov（失败抛 ValueError，调用方回退原始服务）。"""
        with self._lock:
            if self._patched is not None:
                return
            key = (self.real, self._mtime_ns, self.total)
            with _moov_patch_lock:
                hit = _moov_patch_cache.get(key)
            if hit is None:
                with open(self.real, "rb") as f:
                    f.seek(self.moov_off)
                    raw = f.read(self.moov_size)
                if len(raw) != self.moov_size:
                    raise ValueError("moov 读取不完整")
                patched = _patch_chunk_offsets(raw[self.moov_hdr:], self.moov_size)
                if patched is None:
                    raise ValueError("moov 块偏移表无法修补")
                hit = raw[:self.moov_hdr] + patched
                with _moov_patch_lock:
                    _moov_patch_cache[key] = hit
                    while len(_moov_patch_cache) > _MOOV_PATCH_LIMIT:
                        _moov_patch_cache.pop(next(iter(_moov_patch_cache)))
            self._patched = hit

    def moov_patched_slice(self, off, n):
        return self._patched[off:off + n]

    # ── 虚拟区间 → 真实段 ──

    def segments(self, start, end_incl):
        """虚拟区间（闭区间）→ [(source, off, n)]；source ∈ {"file","moov"}。"""
        if self._patched is None:
            raise ValueError("moov 未就绪")
        stop = end_incl + 1
        h = self.head_end
        mv_s, mv_e = h, h + self.moov_size              # moov 虚拟区间
        md_s, md_e = mv_e, self.total                   # mdat 虚拟区间
        segs = []
        if start < h:
            n = min(stop, h) - start
            segs.append(("file", start, n))
        if start < mv_e and stop > mv_s:
            s = max(start, mv_s)
            n = min(stop, mv_e) - s
            segs.append(("moov", s - mv_s, n))
        if start < md_e and stop > md_s:
            s = max(start, md_s)
            n = min(stop, md_e) - s
            segs.append(("file", h + (s - md_s), n))
        if sum(n for _src, _off, n in segs) != stop - start:
            raise ValueError("虚拟区间映射不完整")
        return segs


class _SegmentReader:
    """跨段请求的惰性读取器（iterable，close 关闭底层文件）。"""

    def __init__(self, view, real, segs):
        self._view = view
        self._segs = list(segs)
        self._f = open(real, "rb")

    def __iter__(self):
        try:
            for src, off, n in self._segs:
                if src == "moov":
                    data = self._view.moov_patched_slice(off, n)
                    if data:
                        yield data
                else:
                    self._f.seek(off)
                    remain = n
                    while remain > 0:
                        b = self._f.read(min(1 << 20, remain))
                        if not b:
                            return
                        remain -= len(b)
                        yield b
        finally:
            self.close()

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


def _faststart_view(real, size):
    """满足条件时返回虚拟视图；否则 None（调用方走原始字节服务）。"""
    if size <= FASTSTART_PREVIEW_MIN:
        return None
    try:
        with open(real, "rb") as f:
            head = f.read(8)
        if len(head) < 8 or head[4:8] != b"ftyp":  # 非 ISO-BMFF（MP4/MOV 系）
            return None
        view = _FaststartView(real, size)
        view.ensure_moov()  # 提前修补：失败在此抛错，不会污染响应
        return view
    except (OSError, ValueError):
        return None


def _file_response(real, mime, size, rng, attachment_name=None, view=None):
    """构造文件流式响应；rng 为 (start, end) 或 None（整文件）。

    waitress 环境下用其提供的 wsgi.file_wrapper（ReadOnlyFileBasedBuffer）裸包装，
    命中异步快速文件通道；其他环境（如 Flask 测试客户端）退化为 FileWrapper，
    并以限长读取器保证迭代器不会送出超过 Content-Length 的字节。

    view：虚拟 faststart 视图（见 _FaststartView）。区间落在单一段时仍走
    快速文件通道（moov 段用内存字节流）；跨段时退化为多段惰性读取器。
    """
    if rng:
        start, end = rng
        length = end - start + 1
        status = 206
    else:
        start, length, status = 0, size, 200

    file_wrapper = request.environ.get("wsgi.file_wrapper")
    if view is not None:
        segs = view.segments(start, end if rng else size - 1)
        if len(segs) == 1:
            src, off, _n = segs[0]
            if src == "moov":  # 纯 moov 区间：直接送内存切片
                bio = io.BytesIO(view.moov_patched_slice(off, length))
                wrapper = (file_wrapper(bio) if file_wrapper is not None
                           else FileWrapper(_LimitedFileReader(bio, length)))
                resp = Response(wrapper, status=status, mimetype=mime,
                                direct_passthrough=True)
            else:  # 纯数据区间：原文件 seek 后走快速通道
                f = open(real, "rb")
                try:
                    f.seek(off)
                    wrapper = (file_wrapper(f) if file_wrapper is not None
                               else FileWrapper(_LimitedFileReader(f, length)))
                    resp = Response(wrapper, status=status, mimetype=mime,
                                    direct_passthrough=True)
                except Exception:
                    f.close()
                    raise
        else:  # 跨段（如整文件请求）：多段惰性读取器
            resp = Response(_SegmentReader(view, real, segs), status=status,
                            mimetype=mime, direct_passthrough=True)
    else:
        f = open(real, "rb")
        try:
            if start:
                f.seek(start)
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
                "ctime": int(st.st_ctime),
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
                            "ctime": int(st.st_ctime),
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
        返回最多 500 条：[{share, share_name, path, name, size, ctime, is_dir}]
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
        # >1GB 且 moov 在末尾的 MP4：按虚拟 faststart 提供（手机播放器兼容）
        view = _faststart_view(real, _size)
        try:
            return _file_response(real, mime, _size, rng, view=view)
        except ValueError:
            # 虚拟视图映射异常：回退为原始字节服务（行为同修复前）
            return _file_response(real, mime, _size, rng)

    return bp
