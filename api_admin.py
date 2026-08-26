"""管理 API：仅允许本机（localhost）调用，含系统文件选择对话框。"""

import ipaddress
import io
import logging
import os
import subprocess
import threading
import time

from flask import Blueprint, jsonify, request, send_file

import auth as auth_mod
import config as config_mod
import fs_search
import network
import winfocus
from mounts import MountRegistry, PathEscapeError

try:
    import qrcode
except ImportError:  # 未安装时二维码接口优雅降级，其余功能不受影响
    qrcode = None

log = logging.getLogger("lan_share.admin")

LOCAL_ADDRS = ("127.0.0.1", "::1")

# tkinter 对话框串行化；tkinter 延迟到真正弹出对话框时才导入（省常驻内存）
_dialog_lock = threading.Lock()


def _pick_paths(mode):
    """在服务器本机弹出系统文件选择对话框，返回所选路径列表。

    mode: "folder" -> askdirectory()；"files" -> askopenfilenames()
    会阻塞当前请求线程直至用户完成选择（waitress 线程池可继续服务其他请求）。
    """
    import tkinter as tk
    from tkinter import filedialog

    with _dialog_lock:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)  # 保证对话框置前
        try:
            if mode == "folder":
                chosen = filedialog.askdirectory(parent=root) or ""
                paths = [chosen] if chosen else []
            else:
                paths = list(filedialog.askopenfilenames(parent=root))
        finally:
            root.destroy()
    return paths


def _parse_range_hours(raw):
    """解析 range 参数：'24h' / '7d' / 纯数字（小时）；坏值回退 24h，上限 30 天。"""
    raw = str(raw or "24h").strip().lower()
    try:
        if raw.endswith("h"):
            hours = int(raw[:-1])
        elif raw.endswith("d"):
            hours = int(raw[:-1]) * 24
        else:
            hours = int(raw)
    except ValueError:
        hours = 24
    return max(1, min(hours, 24 * 30))


def make_admin_bp(state, service=None):
    """创建管理 API 蓝图。service 为 ServerManager（用于端口变更后重启）。"""
    bp = Blueprint("admin_api", __name__)

    @bp.before_request
    def local_only():
        if request.remote_addr not in LOCAL_ADDRS:
            return jsonify({"error": "管理接口仅允许本机访问"}), 403
        return None

    @bp.get("/admin/api/state")
    def api_state():
        """当前配置、挂载列表与局域网地址（管理页首屏数据）。"""
        return jsonify({
            "port": state.config["port"],
            "need_password": bool(state.config.get("password_hash")),
            "mounts": [
                {
                    "id": m["id"],
                    "name": m["name"],
                    "path": m["path"],
                    "type": "dir" if os.path.isdir(m["path"]) else "file",
                    "excluded": m.get("excluded", []),
                    "hidden": bool(m.get("hidden")),  # 暂时隐藏（用户端不可见）
                    "system": bool(m.get("system")),  # 上传文件夹（置顶、常驻）
                }
                for m in state.registry.list()
            ],
            "ips": network.get_lan_ips(),
            "upload_dir": state.config.get("upload_dir"),
            "auto_share_uploads": bool(state.config.get("auto_share_uploads", True)),
            "dashboard": state.config.get("dashboard", {"cards": []}),
        })

    @bp.get("/admin/api/qr")
    def api_qr():
        """访问地址二维码（PNG）：?ip=局域网 IP，手机扫码直接打开用户端页面。"""
        if qrcode is None:
            return jsonify({"error": "服务器未安装 qrcode 库"}), 503
        ip = request.args.get("ip", "").strip()
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return jsonify({"error": "无效的 IP 地址"}), 400
        url = "http://" + ip + ":" + str(state.config["port"])
        img = qrcode.make(url, box_size=6, border=3)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")

    @bp.get("/admin/api/list")
    def api_admin_list():
        """管理端目录列表：完整返回，排除项带 excluded:true 标记。"""
        mount = state.registry.get(request.args.get("share", ""))
        if not mount:
            return jsonify({"error": "挂载点不存在"}), 404
        rel = request.args.get("path", "")
        try:
            real = MountRegistry.safe_join(mount["path"], rel)
        except PathEscapeError:
            return jsonify({"error": "路径越界"}), 403
        if not os.path.isdir(real):
            return jsonify({"error": "不是目录"}), 400
        try:
            entries = []
            with os.scandir(real) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    rel_entry = ((rel.rstrip("/\\") + "/") if rel else "") + entry.name
                    entries.append({
                        "name": entry.name,
                        "size": 0 if entry.is_dir() else st.st_size,
                        "mtime": int(st.st_mtime),
                        "is_dir": entry.is_dir(),
                        "excluded": state.registry.is_excluded(mount["id"], rel_entry),
                    })
        except FileNotFoundError:
            return jsonify({"error": "挂载路径不存在（磁盘可能已移除）"}), 404
        except OSError:
            return jsonify({"error": "无法读取目录（无权限）"}), 403
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return jsonify(entries)

    @bp.post("/admin/api/exclude")
    def api_admin_exclude():
        """取消共享挂载点内的某个子项。body: {share, path}"""
        data = request.get_json(silent=True) or {}
        mount = state.registry.get(str(data.get("share", "")))
        if not mount:
            return jsonify({"error": "挂载点不存在"}), 404
        rel = MountRegistry._norm_rel(str(data.get("path", "")))
        if not rel:
            return jsonify({"error": "不能排除挂载点根目录（请直接移除整个挂载）"}), 400
        # 必须是挂载点内真实存在的子项
        try:
            real = MountRegistry.safe_join(mount["path"], rel)
        except PathEscapeError:
            return jsonify({"error": "路径越界"}), 403
        if not os.path.exists(real):
            return jsonify({"error": "路径不存在: " + rel}), 400
        state.registry.exclude(mount["id"], rel)
        state.save()
        log.info("排除共享: %s / %s", mount["name"], rel)
        return jsonify({"ok": True})

    @bp.delete("/admin/api/exclude")
    def api_admin_restore():
        """恢复共享某个被排除的子项。body: {share, path}"""
        data = request.get_json(silent=True) or {}
        mount = state.registry.get(str(data.get("share", "")))
        if not mount:
            return jsonify({"error": "挂载点不存在"}), 404
        rel = MountRegistry._norm_rel(str(data.get("path", "")))
        if not state.registry.restore(mount["id"], rel):
            return jsonify({"error": "该路径未被排除"}), 404
        state.save()
        log.info("恢复共享: %s / %s", mount["name"], rel)
        return jsonify({"ok": True})

    @bp.post("/admin/api/pick-folder")
    def api_pick_folder():
        paths = _pick_paths("folder")
        return jsonify({"ok": bool(paths), "paths": paths})

    @bp.post("/admin/api/locate")
    def api_locate():
        """定位挂载点内的文件/文件夹（管理端右键菜单）。

        body: {share, path, action}
        - action=copy   → 返回真实绝对路径（前端复制到剪贴板）
        - action=reveal → 资源管理器中打开所在目录并选中该项
        - action=open   → 用系统默认程序直接打开（文件夹则打开目录）
        """
        data = request.get_json(silent=True) or {}
        mount = state.registry.get(str(data.get("share", "")))
        if not mount:
            return jsonify({"error": "挂载点不存在"}), 404
        rel = MountRegistry._norm_rel(str(data.get("path", "")))
        try:
            real = MountRegistry.safe_join(mount["path"], rel)
        except PathEscapeError:
            return jsonify({"error": "路径越界"}), 403
        if not os.path.exists(real):
            return jsonify({"error": "路径不存在（磁盘可能已移除）"}), 404

        action = str(data.get("action", ""))
        if action == "copy":
            return jsonify({"ok": True, "path": real})
        if action == "reveal":
            # explorer /select,"路径"：在所在目录中选中该项；
            # 单文件挂载根 / 挂载根本身也是这样定位（在其父目录中选中）。
            # 窗口标题 = 父目录名，作为置前兜底的匹配提示
            parent = os.path.basename(os.path.dirname(real))
            winfocus.launch_and_focus(
                lambda: subprocess.run('explorer /select,"{}"'.format(real), check=False),
                title_hint=parent)
            return jsonify({"ok": True})
        if action == "open":
            # 文件夹 → 资源管理器（窗口标题 = 文件夹名）；文件 → 默认关联程序
            hint = os.path.basename(real) if os.path.isdir(real) else None
            winfocus.launch_and_focus(lambda: os.startfile(real), title_hint=hint)
            return jsonify({"ok": True})
        return jsonify({"error": "未知操作: " + action}), 400

    @bp.get("/admin/api/search")
    def api_admin_search():
        """管理端搜索：不过滤隐藏/未共享挂载（管理员看得到全部）；share= 可限定本共享。

        与用户端 /api/search 返回结构一致（fs_search.search_mounts 共用实现）。
        """
        return jsonify(fs_search.search_mounts(
            state, request.args.get("q", ""),
            share_id=request.args.get("share", "") or None,
            user_view=False))

    @bp.post("/admin/api/pick-files")
    def api_pick_files():
        paths = _pick_paths("files")
        return jsonify({"ok": bool(paths), "paths": paths})

    @bp.post("/admin/api/mount")
    def api_mount():
        """添加一个或多个挂载。body: {"path": "..."} 或 {"paths": ["...", ...]}"""
        data = request.get_json(silent=True) or {}
        paths = [str(p) for p in (data.get("paths") or []) if str(p)]
        if not paths and data.get("path"):
            paths = [str(data["path"])]
        if not paths:
            return jsonify({"error": "缺少 path 参数"}), 400

        added = []
        for p in paths:
            try:
                added.append(state.registry.add(p))
            except FileNotFoundError:
                return jsonify({"error": f"路径不存在: {p}"}), 400
        state.save()
        log.info("新增挂载: %s", [a["path"] for a in added])
        return jsonify({"ok": True, "added": added})

    @bp.delete("/admin/api/mount/<mount_id>")
    def api_unmount(mount_id):
        if not state.registry.remove(mount_id):
            return jsonify({"error": "挂载点不存在"}), 404
        state.save()
        log.info("移除挂载: %s", mount_id)
        return jsonify({"ok": True})

    @bp.post("/admin/api/mount/<mount_id>/hidden")
    def api_mount_hidden(mount_id):
        """暂时隐藏 / 恢复整个挂载。body: {hidden: bool}。
        隐藏 = 用户端列表/浏览/搜索均不可见，管理端仍显示并标注（代替临时删除）。"""
        data = request.get_json(silent=True) or {}
        if not state.registry.set_hidden(mount_id, bool(data.get("hidden"))):
            return jsonify({"error": "挂载点不存在"}), 404
        state.save()
        mount = state.registry.get(mount_id)
        log.info("挂载%s: %s", "已隐藏" if mount.get("hidden") else "已恢复", mount["name"])
        return jsonify({"ok": True, "hidden": bool(mount.get("hidden"))})

    @bp.post("/admin/api/settings")
    def api_settings():
        """修改密码 / 端口。端口变更后自动重启服务（延迟到响应发完之后）。"""
        data = request.get_json(silent=True) or {}
        port_changed = False

        # 密码：传非空字符串为设置密码，传空字符串为关闭密码
        if "password" in data:
            password = str(data.get("password") or "")
            if password:
                state.config["password_hash"] = auth_mod.hash_password(password)
            else:
                state.config["password_hash"] = None
            state.tokens.clear()  # 密码变更后旧 token 全部失效

        # 端口
        if "port" in data:
            try:
                port = int(data["port"])
            except (TypeError, ValueError):
                return jsonify({"error": "端口必须是 1-65535 的整数"}), 400
            if not (1 <= port <= 65535):
                return jsonify({"error": "端口必须是 1-65535 的整数"}), 400
            if port != state.config["port"]:
                state.config["port"] = port
                port_changed = True

        state.save()

        result = {"ok": True, "port": state.config["port"]}
        if port_changed and service is not None:
            new_port = state.config["port"]

            def _restart_job():
                try:
                    service.restart(new_port)
                    log.info("端口已切换为 %s，服务已重启", new_port)
                except OSError as e:
                    log.error("切换端口后重启服务失败: %s", e)

            # 延迟重启：先把本次响应完整发回浏览器
            threading.Timer(0.6, _restart_job).start()
            result["restarted"] = True
        return jsonify(result)

    # ── 上传设置 / 传输日志 / 仪表板 ──

    @bp.post("/admin/api/uploads-settings")
    def api_uploads_settings():
        """上传设置：auto_share 开关（即时生效并持久化）；upload_dir 修改（重启后生效）。"""
        data = request.get_json(silent=True) or {}
        if "upload_dir" in data:
            new_dir = str(data.get("upload_dir") or "").strip()
            if not new_dir:
                return jsonify({"error": "目录不能为空"}), 400
            # 相对路径由 save() 规范化到程序所在目录；已有文件不迁移
            state.config["upload_dir"] = new_dir
            state.save()
            log.info("上传接收目录已改为 %s（重启后生效）", state.config["upload_dir"])
        if "auto_share" in data:
            state.set_auto_share_uploads(bool(data["auto_share"]))
        return jsonify({
            "upload_dir": state.config.get("upload_dir"),
            "auto_share": bool(state.config.get("auto_share_uploads", True)),
        })

    @bp.get("/admin/api/logs/stats")
    def api_logs_stats():
        """趋势聚合（上传/下载次数与流量按时间桶）。range=24h/7d，bucket=hour/day。"""
        hours = _parse_range_hours(request.args.get("range", "24h"))
        bucket = "day" if request.args.get("bucket", "hour") == "day" else "hour"
        return jsonify({
            "range_hours": hours,
            "bucket": bucket,
            "buckets": state.log.stats(hours, bucket),
        })

    @bp.get("/admin/api/logs/recent")
    def api_logs_recent():
        """最近传输事件（最新在前，供仪表板滚动列表）。"""
        try:
            limit = int(request.args.get("limit", 50))
        except ValueError:
            limit = 50
        return jsonify(state.log.recent(limit))

    def _scan_shared_types():
        """扫描全部共享（含系统挂载，尊重排除规则）：文件类型计数与大小。30s 缓存。"""
        now = time.time()
        ts, cached = state._dash_cache
        if cached is not None and now - ts < 30:
            return cached

        counts, sizes, total = {}, {}, 0

        def _tally(real_path):
            nonlocal total
            try:
                st = os.stat(real_path)
            except OSError:
                return
            ext = os.path.splitext(real_path)[1].lower().lstrip(".") or "other"
            counts[ext] = counts.get(ext, 0) + 1
            sizes[ext] = sizes.get(ext, 0) + st.st_size
            total += 1

        for m in state.registry.list():
            if m.get("hidden"):
                continue  # 暂时隐藏的挂载用户端看不到，不计入共享统计
            root, mid = m["path"], m["id"]
            if os.path.isfile(root):  # 单文件挂载
                _tally(root)
                continue
            for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
                # 排除项剪枝：被排除的目录不进入（与用户端搜索一致的规则）
                rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
                if rel_dir != "." and state.registry.is_excluded(mid, rel_dir):
                    dirnames[:] = []
                    continue
                if rel_dir == ".":
                    dirnames[:] = [d for d in dirnames
                                   if not state.registry.is_excluded(mid, d)]
                    filenames = [f for f in filenames
                                 if not state.registry.is_excluded(mid, f)]
                else:
                    dirnames[:] = [d for d in dirnames
                                   if not state.registry.is_excluded(mid, rel_dir + "/" + d)]
                    filenames = [f for f in filenames
                                 if not state.registry.is_excluded(mid, rel_dir + "/" + f)]
                for name in filenames:
                    _tally(os.path.join(dirpath, name))

        result = {
            "shared_files": total,
            "type_counts": [{"type": k, "count": v}
                            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])],
            "type_sizes": [{"type": k, "size": v}
                           for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])],
        }
        state._dash_cache = (now, result)
        return result

    @bp.get("/admin/api/dashboard/summary")
    def api_dashboard_summary():
        """仪表板汇总：历史传输总量 + 当前共享文件的类型分布。"""
        return jsonify({"totals": state.log.totals(), **_scan_shared_types()})

    @bp.get("/admin/api/dashboard-config")
    def api_dashboard_config():
        """卡片布局配置（规范化：未知 id 丢弃、缺省补默认）。"""
        return jsonify(config_mod._normalize_dashboard(state.config))

    @bp.post("/admin/api/dashboard-config")
    def api_dashboard_config_save():
        """保存卡片布局：cards=[{id, enabled, order}]，重启后保持。"""
        data = request.get_json(silent=True) or {}
        cards = data.get("cards")
        state.config["dashboard"] = {"cards": cards if isinstance(cards, list) else []}
        saved = state.save()
        return jsonify(saved["dashboard"])

    return bp
