"""上传 API 集成测试：成功/重命名/鉴权/净化/日志。"""

import io

import pytest

import server


@pytest.fixture()
def env(tmp_path):
    """开自动共享的测试环境：上传目录在 tmp_path 下。"""
    state = server.AppState(
        {"port": 8000, "password_hash": None, "mounts": [],
         "upload_dir": str(tmp_path / "uploads"), "auto_share_uploads": True},
        config_path=str(tmp_path / "config.json"),
    )
    app = server.create_app(state)
    app.testing = True
    return app.test_client(), state, tmp_path


def _file(storage, filename, content=b"hello"):
    return (io.BytesIO(content), filename)


def test_upload_success(env):
    client, state, tmp_path = env
    r = client.post("/api/upload", data={"file": _file(None, "a.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.json["ok"] is True
    assert r.json["filename"] == "a.txt"
    saved = tmp_path / "uploads" / "a.txt"
    assert saved.read_bytes() == b"hello"
    # 日志落库
    rows = state.log.recent(10)
    assert rows[0]["type"] == "upload" and rows[0]["filename"] == "a.txt"


def test_upload_same_name_auto_rename(env):
    client, state, tmp_path = env
    for _ in range(3):
        r = client.post("/api/upload", data={"file": _file(None, "f.bin", b"x")},
                        content_type="multipart/form-data")
        assert r.status_code == 200
    names = {p.name for p in (tmp_path / "uploads").iterdir()}
    assert names == {"f.bin", "f(1).bin", "f(2).bin"}


def test_upload_requires_auth_when_password_set(env):
    client, state, tmp_path = env
    import auth as auth_mod
    state.config["password_hash"] = auth_mod.hash_password("pw")
    r = client.post("/api/upload", data={"file": _file(None, "a.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 401
    # 带 token 成功
    token = state.tokens.issue()
    r = client.post(f"/api/upload?key={token}", data={"file": _file(None, "a.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 200


def test_upload_sanitizes_filename(env):
    client, state, tmp_path = env
    r = client.post("/api/upload",
                    data={"file": _file(None, '..\\..\\evil<>.txt')},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    name = r.json["filename"]
    assert ".." not in name and "<" not in name and ">" not in name
    # 文件确实落在 uploads 目录内（未穿越）
    assert (tmp_path / "uploads" / name).exists()
    assert not (tmp_path.parent / "evil.txt").exists()


def test_upload_missing_file_field(env):
    client, _, _ = env
    r = client.post("/api/upload", data={})
    assert r.status_code == 400


def test_upload_shows_in_shares_when_auto_shared(env):
    client, state, _ = env
    r = client.get("/api/shares")
    ids = [s["id"] for s in r.json]
    assert "uploads" in ids
    share = [s for s in r.json if s["id"] == "uploads"][0]
    assert share["name"] == "来自设备的上传"


def test_download_and_preview_logged(env, tmp_path):
    client, state, _ = env
    root = tmp_path / "share"
    root.mkdir()
    (root / "v.mp4").write_bytes(b"\x00" * 32)
    m = state.registry.add(str(root))

    client.get("/api/download?share=" + m["id"] + "&path=v.mp4")
    client.get("/api/preview?share=" + m["id"] + "&path=v.mp4")
    # 同一预览窗口期内再请求 3 次 → 合并为 1 条
    for _ in range(3):
        client.get("/api/preview?share=" + m["id"] + "&path=v.mp4")

    rows = state.log.recent(10)
    assert rows[0]["type"] == "preview"   # 最新在前
    assert rows[1]["type"] == "download"
    assert rows[0]["filename"] == "v.mp4"
    assert rows[0]["size"] == 32
    # 1 下载 + 1 预览（3 次重复预览被去重合并）
    assert len(rows) == 2


# ─────────────── 管理端日志 / 仪表板接口 ───────────────

def _seed_events(state):
    import time as _t
    now = _t.time()
    state.log._insert(now, "upload", "10.0.0.9", "a.txt", 100)
    state.log._insert(now, "download", "10.0.0.9", "a.txt", 100)
    state.log._insert(now - 7200, "preview", "10.0.0.8", "b.mp4", 500)


def test_admin_logs_endpoints(env):
    client, state, _ = env
    _seed_events(state)
    r = client.get("/admin/api/logs/stats?range=24h&bucket=hour")
    assert r.status_code == 200
    buckets = r.json["buckets"]
    assert sum(b["uploads_count"] for b in buckets) == 1
    assert sum(b["downloads_count"] for b in buckets) == 2

    r = client.get("/admin/api/logs/recent?limit=2")
    assert r.status_code == 200
    assert len(r.json) == 2
    assert r.json[0]["filename"] == "a.txt"


def test_admin_dashboard_summary(env, tmp_path):
    client, state, _ = env
    root = tmp_path / "share"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"\x00" * 10)
    (root / "b.mp4").write_bytes(b"\x00" * 20)
    (root / "c.txt").write_bytes(b"\x00" * 5)
    state.registry.add(str(root))
    _seed_events(state)

    r = client.get("/admin/api/dashboard/summary")
    assert r.status_code == 200
    j = r.json
    # 共享文件 = 3 个挂载文件（上传目录为空不计）
    assert j["shared_files"] == 3
    types = {t["type"]: t for t in j["type_counts"]}
    assert types["mp4"]["count"] == 2 and types["txt"]["count"] == 1
    sizes = {t["type"]: t for t in j["type_sizes"]}
    assert sizes["mp4"]["size"] == 30
    assert j["totals"]["uploads_count"] == 1
    assert j["totals"]["today_count"] >= 2


def test_admin_dashboard_summary_respects_exclusion(env, tmp_path):
    client, state, _ = env
    root = tmp_path / "share"
    root.mkdir()
    (root / "keep.txt").write_bytes(b"1")
    (root / "hide.txt").write_bytes(b"1")
    m = state.registry.add(str(root))
    state.registry.exclude(m["id"], "hide.txt")
    r = client.get("/admin/api/dashboard/summary")
    assert r.json["shared_files"] == 1


def test_admin_dashboard_config_roundtrip(env):
    client, state, _ = env
    r = client.get("/admin/api/dashboard-config")
    assert r.status_code == 200
    assert len(r.json["cards"]) == 6  # 默认全开

    r = client.post("/admin/api/dashboard-config",
                    json={"cards": [{"id": "recent", "enabled": False, "order": 1},
                                    {"id": "bogus"}]})
    assert r.status_code == 200
    cards = r.json["cards"]
    assert cards[0] == {"id": "recent", "enabled": False, "order": 1}
    assert all(c["id"] != "bogus" for c in cards)

    # 重启（重新 load）后保持
    import config as config_mod
    cfg = config_mod.load(state.config_path)
    assert cfg["dashboard"]["cards"][0]["id"] == "recent"
    assert cfg["dashboard"]["cards"][0]["enabled"] is False


def test_upload_folder_keeps_structure(env, tmp_path):
    """文件夹上传：path 为客户端相对路径，服务端按原目录结构保存。"""
    client, state, _ = env
    r = client.post("/api/upload",
                    data={"file": _file(None, "a.txt", b"hi"), "path": "docs/sub/a.txt"},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    assert r.json["filename"] == "docs/sub/a.txt"
    assert (tmp_path / "uploads" / "docs" / "sub" / "a.txt").read_bytes() == b"hi"
    # 日志记录带相对路径
    assert state.log.recent(5)[0]["filename"] == "docs/sub/a.txt"


def test_upload_folder_same_name_auto_rename(env, tmp_path):
    """文件夹内同名文件也自动重命名（不覆盖）。"""
    client, _, _ = env
    for _ in range(2):
        r = client.post("/api/upload",
                        data={"file": _file(None, "b.txt", b"x"), "path": "box/b.txt"},
                        content_type="multipart/form-data")
        assert r.status_code == 200
    names = {p.name for p in (tmp_path / "uploads" / "box").iterdir()}
    assert names == {"b.txt", "b(1).txt"}


def test_upload_folder_rejects_path_traversal(env, tmp_path):
    """相对路径含 .. 时被丢弃，永不越出 uploads 目录。"""
    client, _, _ = env
    r = client.post("/api/upload",
                    data={"file": _file(None, "e.txt"), "path": "../../evil/e.txt"},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    assert ".." not in r.json["filename"]
    # 落在 uploads 内的净化目录，未逃出
    assert (tmp_path / "uploads" / "evil" / "e.txt").exists()
    assert not (tmp_path.parent / "evil").exists()


def test_uploads_mount_pinned_first(env, tmp_path):
    """上传文件夹（系统挂载）在挂载列表固定置顶，不参与排序。"""
    client, state, _ = env
    root = tmp_path / "share"
    root.mkdir()
    state.registry.add(str(root), "Z挂载")
    state.registry.add(str(root), "A挂载")
    r = client.get("/api/shares")
    assert r.json[0]["id"] == "uploads"      # 置顶
    assert [s["id"] for s in r.json[1:]] != []  # 其余挂载跟随其后


def test_admin_uploads_settings_toggle(env):
    """关闭共享：用户端不可见（shares/list/search 均 404），服务端仍显示。"""
    client, state, _ = env
    r = client.post("/admin/api/uploads-settings", json={"auto_share": False})
    assert r.status_code == 200 and r.json["auto_share"] is False
    ids = [s["id"] for s in client.get("/api/shares").get_json()]
    assert "uploads" not in ids              # 用户端挂载列表不可见
    r = client.get("/api/list?share=uploads")
    assert r.status_code == 404               # 直接枚举也被拒
    r = client.get("/api/search?q=x&share=uploads")
    assert r.json == []
    # 管理端仍可见（系统挂载常驻）
    r = client.get("/admin/api/state")
    m = [m for m in r.json["mounts"] if m["id"] == "uploads"]
    assert m and m[0]["system"] is True

    r = client.post("/admin/api/uploads-settings", json={"auto_share": True})
    assert r.json["auto_share"] is True
    ids = [s["id"] for s in client.get("/api/shares").get_json()]
    assert "uploads" in ids


def test_admin_uploads_settings_change_dir(env, tmp_path):
    client, state, _ = env
    new_dir = str(tmp_path / "new_uploads")
    r = client.post("/admin/api/uploads-settings", json={"upload_dir": new_dir})
    assert r.status_code == 200
    assert r.json["upload_dir"] == new_dir
    # 持久化：重启（重新 load）后生效
    import config as config_mod
    cfg = config_mod.load(state.config_path)
    assert cfg["upload_dir"] == new_dir
    # 空目录被拒绝
    r = client.post("/admin/api/uploads-settings", json={"upload_dir": "  "})
    assert r.status_code == 400


def test_admin_state_includes_upload_fields(env):
    client, state, _ = env
    r = client.get("/admin/api/state")
    assert "upload_dir" in r.json
    assert "auto_share_uploads" in r.json
    assert "dashboard" in r.json
