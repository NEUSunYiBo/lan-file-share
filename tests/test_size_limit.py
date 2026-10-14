"""超大文件（>1GB）传输能力回归测试。

背景：waitress 的 max_request_body_size 默认 1073741824（恰好 1GB），且按
content_length >= 上限 判定——任何 ≥1GB 的上传在解析请求头阶段就被回 413，
请求根本到不了 Flask；此外 create_app 里原有的 MAX_CONTENT_LENGTH=2GB 会在
解除前者后成为新的瓶颈。两道限制都必须不存在，本文件锁定这一行为。
"""

import pytest

import server


def _make_state(tmp_path):
    return server.AppState(
        {"port": 8000, "password_hash": None, "mounts": [],
         "upload_dir": str(tmp_path / "uploads"), "auto_share_uploads": True},
        config_path=str(tmp_path / "config.json"),
    )


def test_app_has_no_content_length_limit(tmp_path):
    """Flask 层不得设置请求体上限（None = 不限制），否则会挡住超大文件上传。"""
    app = server.create_app(_make_state(tmp_path))
    assert app.config["MAX_CONTENT_LENGTH"] is None


def test_waitress_body_limit_raised(tmp_path, monkeypatch):
    """ServerManager.start 必须把 waitress 的请求体上限提升到远超 1GB。

    通过替换 waitress.server.create_server 捕获真实传入的参数，
    断言其显著大于 1GB（回归防线：默认值 1073741824 会让 >1GB 上传全部 413）。
    """
    import waitress.server

    captured = {}

    class _FakeServer:
        def run(self):
            pass

        def close(self):
            pass

    def fake_create_server(app, **kw):
        captured.update(kw)
        return _FakeServer()

    monkeypatch.setattr(waitress.server, "create_server", fake_create_server)
    mgr = server.ServerManager(app=server.create_app(_make_state(tmp_path)),
                               threads=4)
    mgr.start(18080)
    try:
        assert "max_request_body_size" in captured, "必须显式覆盖 waitress 默认 1GB 上限"
        assert captured["max_request_body_size"] > 1024 ** 4  # 至少 1TB
        # 原有参数不受影响
        assert captured["channel_timeout"] == 600
    finally:
        mgr.stop()


def test_upload_accepts_large_content_length(tmp_path):
    """真实 waitress 服务：声明 3GB Content-Length 的请求不再被 413 拒绝。

    不实际发送 3GB 数据——修复前 waitress 在解析阶段就回 413，修复后它会正常
    进入"等待请求体"状态（读取超时/连接关闭），两者可通过响应字节区分。
    """
    import socket

    state = _make_state(tmp_path)
    app = server.create_app(state)
    mgr = server.ServerManager(app, threads=4)

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    mgr.start(port)
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.sendall(
            b"POST /api/upload HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: multipart/form-data; boundary=----t\r\n"
            b"Content-Length: 3221225472\r\n"  # 3GB
            b"\r\n")
        conn.settimeout(2)
        try:
            head = conn.recv(256)
        except socket.timeout:
            head = b""  # 未立即拒绝 = 服务端在等待请求体（预期）
        conn.close()
        assert b"413" not in head, "服务端不应再以 413 拒绝 >1GB 的请求体"
    finally:
        mgr.stop()
