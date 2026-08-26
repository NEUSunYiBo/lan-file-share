"""Flask 应用工厂、共享状态（AppState）与 waitress 服务生命周期。"""

import logging
import os
import sys
import threading
import time

from flask import Flask, jsonify, request, send_file

import auth as auth_mod
import config as config_mod
import mounts
import network
import transfer_log
from api_admin import make_admin_bp
from api_upload import make_upload_bp
from api_user import make_user_bp

log = logging.getLogger("lan_share.server")

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：页面资源解压在 _MEIPASS 临时目录
    PAGES_DIR = os.path.join(sys._MEIPASS, "pages")
    APP_ICO = os.path.join(sys._MEIPASS, "app.ico")
else:
    PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages")
    APP_ICO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")

LOCAL_ADDRS = ("127.0.0.1", "::1")


class AppState:
    """进程内共享状态：配置、挂载表、token 存储、传输日志。"""

    def __init__(self, config_data, config_path=None):
        self.config = config_data
        self.config_path = config_path or config_mod.CONFIG_PATH
        self.registry = mounts.MountRegistry(config_data.get("mounts"))
        self.tokens = auth_mod.TokenStore()
        # 日志库与 config.json 同目录；初始化失败自动降级为不可用
        self.log = transfer_log.TransferLog(
            os.path.join(os.path.dirname(self.config_path), "logs.db"))
        self._dash_cache = (0.0, None)  # (时间戳, 共享类型扫描结果) 30s 缓存
        self._sync_uploads_mount()

    def _sync_uploads_mount(self):
        """始终注册上传目录系统挂载（服务端可见、列表置顶）；
        auto_share_uploads 仅控制用户端是否可见，不增删挂载。"""
        upload_dir = self.config.get("upload_dir") or os.path.join(
            os.path.dirname(self.config_path), "uploads")
        try:
            os.makedirs(upload_dir, exist_ok=True)  # 启动时自动创建
        except OSError as e:
            log.warning("创建上传目录失败: %s", e)
            return
        self.config["upload_dir"] = upload_dir
        self.registry.add_system("uploads", upload_dir, "来自设备的上传")

    def uploads_shared(self):
        """上传文件夹是否对外共享（用户端可见）。"""
        return bool(self.config.get("auto_share_uploads", True))

    def set_auto_share_uploads(self, enabled):
        """管理页开关：即时生效并持久化（挂载常驻，仅切换用户端可见性）。"""
        self.config["auto_share_uploads"] = bool(enabled)
        self.save()

    def save(self):
        """把当前挂载表与配置写回磁盘（系统挂载不持久化）。"""
        self.config["mounts"] = self.registry.list(managed_only=True)
        return config_mod.save(self.config, self.config_path)


class ServerManager:
    """waitress 服务的启动 / 停止 / 重启（供托盘菜单和端口变更使用）。"""

    def __init__(self, app=None, threads=8):
        self.app = app
        self.threads = threads  # 并发线程数：多路视频流/下载同时进行
        self.port = None
        self._server = None
        self._lock = threading.Lock()

    @property
    def running(self):
        return self._server is not None

    def start(self, port):
        """启动 HTTP 服务；端口被占用等错误抛 OSError。"""
        from waitress.server import create_server

        with self._lock:
            if self._server is not None:
                raise RuntimeError("服务已在运行")
            srv = create_server(self.app, host="0.0.0.0", port=int(port), threads=self.threads)
            threading.Thread(target=srv.run, name="http-server", daemon=True).start()
            self._server = srv
            self.port = int(port)
            # 输出可直接 Ctrl+点击 的完整 URL（终端会把 http:// 识别为链接）
            log.info("HTTP 服务已启动")
            log.info("浏览页: http://localhost:%s/", port)
            log.info("管理页: http://localhost:%s/admin", port)
            for ip in network.get_lan_ips():
                log.info("局域网访问: http://%s:%s/", ip, port)
            return srv

    def stop(self):
        with self._lock:
            srv, self._server = self._server, None
            if srv is None:
                return False
            srv.close()
            log.info("HTTP 服务已停止")
            return True

    def restart(self, port):
        """重启并切换端口。"""
        self.stop()
        threading.Event().wait(0.3)  # 等待端口释放
        self.start(port)


def create_app(state, service=None):
    """Flask 应用工厂。"""
    app = Flask(__name__)
    # 请求体上限 2GB（下载响应不受影响，仅限制上传等请求体）
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

    # ── CORS：聚合页需要跨服务器调用 /api/* ──
    @app.after_request
    def add_cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Auth-Token"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"  # 预检结果缓存一天
        return resp

    app.register_blueprint(make_user_bp(state))
    app.register_blueprint(make_admin_bp(state, service=service))
    app.register_blueprint(make_upload_bp(state))

    # ── 页面路由 ──
    @app.get("/")
    def browse_page():
        # no-cache：每次重验证（未变则 304），避免浏览器拿旧缓存导致改版后"看不到变化"
        resp = send_file(os.path.join(PAGES_DIR, "browse.html"))
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/admin")
    def admin_page():
        if request.remote_addr not in LOCAL_ADDRS:
            return jsonify({"error": "管理页仅允许本机访问"}), 403
        resp = send_file(os.path.join(PAGES_DIR, "admin.html"))
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/favicon.ico")
    def favicon():
        """网页 favicon（浏览器标签页图标）：与 exe / 托盘共用 app.ico。"""
        if not os.path.isfile(APP_ICO):
            return jsonify({"error": "资源不存在"}), 404
        resp = send_file(APP_ICO, mimetype="image/x-icon")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/echarts.min.js")
    def echarts_js():
        """内嵌图表库（仪表板用，离线可用）。"""
        resp = send_file(os.path.join(PAGES_DIR, "echarts.min.js"),
                         mimetype="application/javascript")
        resp.headers["Cache-Control"] = "public, max-age=86400"  # 内容固定，缓存一天
        return resp

    # ── 统一 JSON 错误 ──
    @app.errorhandler(404)
    def _404(_e):
        return jsonify({"error": "资源不存在"}), 404

    @app.errorhandler(500)
    def _500(e):
        log.exception("服务器内部错误: %s", e)
        return jsonify({"error": "服务器内部错误"}), 500

    return app
