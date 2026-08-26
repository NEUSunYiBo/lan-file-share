"""API 集成测试（Flask test client）：鉴权、浏览、下载、Range/206、管理接口。"""

import os

import pytest

import auth
import config as config_mod
import server


@pytest.fixture()
def env(tmp_path):
    """返回 (test_client, state)。配置写到临时目录，不污染真实 config.json。"""
    state = server.AppState(
        {"port": 8000, "password_hash": None, "mounts": [],
         "upload_dir": str(tmp_path / "uploads"), "auto_share_uploads": False},
        config_path=str(tmp_path / "config.json"),
    )
    app = server.create_app(state)
    app.testing = True
    return app.test_client(), state


# ─────────────── 用户 API ───────────────

def test_api_info_free_of_auth(env):
    client, _ = env
    r = client.get("/api/info")
    assert r.status_code == 200
    assert r.json["need_password"] is False
    assert r.json["name"]
    assert r.headers["Access-Control-Allow-Origin"] == "*"


def test_auth_disabled_returns_ok(env):
    client, _ = env
    r = client.post("/api/auth", json={"password": "任意"})
    assert r.status_code == 200
    assert r.json["ok"] is True
    assert r.json["token"] is None


def test_shares_and_list(env, tmp_path):
    client, state = env
    root = tmp_path / "share"
    (root / "子目录").mkdir(parents=True)
    (root / "b.txt").write_text("hello", encoding="utf-8")
    (root / "a.mp4").write_bytes(b"\x00" * 16)
    m = state.registry.add(str(root))

    r = client.get("/api/shares")
    assert r.status_code == 200
    shares = r.json
    assert shares[0]["id"] == m["id"]
    assert shares[0]["type"] == "dir"
    assert "path" not in shares[0]  # 不暴露真实磁盘路径

    r = client.get("/api/list?share=" + m["id"])
    assert r.status_code == 200
    names = [i["name"] for i in r.json]
    assert names == ["子目录", "a.mp4", "b.txt"]  # 目录优先 + 名称排序
    assert r.json[0]["is_dir"] is True

    # 进入子目录
    r = client.get("/api/list?share=" + m["id"] + "&path=" + "子目录")
    assert r.status_code == 200
    assert r.json == []


def test_list_single_file_mount(env, tmp_path):
    client, state = env
    f = tmp_path / "单文件.log"
    f.write_text("x", encoding="utf-8")
    mf = state.registry.add(str(f))
    r = client.get("/api/list?share=" + mf["id"])
    assert r.status_code == 200
    assert r.json[0]["name"] == "单文件.log"
    assert r.json[0]["is_dir"] is False


def test_list_unknown_share_404(env):
    client, _ = env
    assert client.get("/api/list?share=nope").status_code == 404


def test_list_path_escape_403(env, tmp_path):
    client, state = env
    m = state.registry.add(str(tmp_path))
    r = client.get("/api/list?share=" + m["id"] + "&path=..%2F..%2Fetc")
    assert r.status_code == 403


def test_download_and_range(env, tmp_path):
    client, state = env
    f = tmp_path / "video.mp4"
    payload = bytes(range(256)) * 40  # 10240 字节
    f.write_bytes(payload)
    m = state.registry.add(str(f))

    # 完整下载
    r = client.get("/api/download?share=" + m["id"])
    assert r.status_code == 200
    assert r.data == payload
    assert "attachment" in r.headers["Content-Disposition"]

    # Range 请求 → 206 且分片内容正确（视频拖动进度条依赖此行为）
    r = client.get("/api/download?share=" + m["id"], headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.data == payload[100:200]
    assert r.headers["Content-Range"].startswith("bytes 100-199/")
    assert int(r.headers["Content-Range"].split("/")[-1]) == len(payload)

    # 预览：inline
    r = client.get("/api/preview?share=" + m["id"])
    assert r.status_code == 200
    assert r.data == payload
    assert "attachment" not in r.headers.get("Content-Disposition", "")


def test_preview_content_type(env, tmp_path):
    client, state = env
    f = tmp_path / "说明.txt"
    f.write_text("你好", encoding="utf-8")
    m = state.registry.add(str(f))
    r = client.get("/api/preview?share=" + m["id"])
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/plain")


def test_download_missing_file_404(env, tmp_path):
    client, state = env
    f = tmp_path / "gone.txt"
    f.write_text("x", encoding="utf-8")
    m = state.registry.add(str(f))
    f.unlink()
    assert client.get("/api/download?share=" + m["id"]).status_code == 404


def test_single_file_mount_download_preview(env, tmp_path):
    """单文件挂载：path 为空时指向挂载文件本身（此前 bug：前端拼了文件名导致 404）。"""
    client, state = env
    f = tmp_path / "电影.mp4"
    payload = b"\x00" * 128
    f.write_bytes(payload)
    m = state.registry.add(str(f))

    # path 为空 → 文件本身
    r = client.get("/api/download?share=" + m["id"])
    assert r.status_code == 200
    assert r.data == payload

    r = client.get("/api/preview?share=" + m["id"])
    assert r.status_code == 200
    assert r.data == payload

    # 旧的错误拼法（path=文件名）应 404，而不是 500
    assert client.get("/api/download?share=" + m["id"] + "&path=电影.mp4").status_code == 404


# ─────────────── 搜索 ───────────────

def test_search_recursive_case_insensitive(env, tmp_path):
    client, state = env
    root = tmp_path / "share"
    (root / "电影" / "2024").mkdir(parents=True)
    (root / "电影" / "2024" / "Movie.File.mp4").write_bytes(b"x")
    (root / "MOVIE_2.mp4").write_bytes(b"y")
    (root / "音乐.mp3").write_bytes(b"z")
    m = state.registry.add(str(root))

    r = client.get("/api/search?q=movie")
    assert r.status_code == 200
    results = r.json
    names = sorted(x["name"] for x in results)
    assert names == ["MOVIE_2.mp4", "Movie.File.mp4"]
    # 结果带完整相对路径（含子目录），统一用 / 分隔便于前端处理
    deep = next(x for x in results if x["name"] == "Movie.File.mp4")
    assert deep["path"].replace("\\", "/") == "电影/2024/Movie.File.mp4"
    assert deep["share"] == m["id"]
    assert deep["is_dir"] is False
    assert deep["size"] == 1

    # 中文关键字
    r = client.get("/api/search?q=音乐")
    assert [x["name"] for x in r.json] == ["音乐.mp3"]

    # 空关键字返回空列表
    assert client.get("/api/search?q=").status_code == 200
    assert client.get("/api/search?q=").json == []


def test_search_finds_directory(env, tmp_path):
    client, state = env
    root = tmp_path / "share"
    (root / "我的文件夹").mkdir(parents=True)
    state.registry.add(str(root))
    r = client.get("/api/search?q=文件夹")
    assert len(r.json) == 1
    assert r.json[0]["is_dir"] is True
    assert r.json[0]["path"].replace("\\", "/") == "我的文件夹"


def test_search_share_filter(env, tmp_path):
    client, state = env
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    (a / "target.txt").write_text("x", encoding="utf-8")
    (b / "target.txt").write_text("y", encoding="utf-8")
    ma = state.registry.add(str(a))
    state.registry.add(str(b))

    r = client.get("/api/search?q=target&share=" + ma["id"])
    assert len(r.json) == 1
    assert r.json[0]["share"] == ma["id"]


def test_search_single_file_mount(env, tmp_path):
    client, state = env
    f = tmp_path / "纪录片.mkv"
    f.write_bytes(b"x")
    m = state.registry.add(str(f))
    r = client.get("/api/search?q=纪录片")
    assert len(r.json) == 1
    assert r.json[0]["path"] == ""
    assert r.json[0]["name"] == "纪录片.mkv"


def test_search_password_401(env):
    client, state = env
    state.config["password_hash"] = auth.hash_password("pw")
    assert client.get("/api/search?q=x").status_code == 401
    token = client.post("/api/auth", json={"password": "pw"}).json["token"]
    assert client.get("/api/search?q=x&key=" + token).status_code == 200


# ─────────────── 排除（取消共享子项） ───────────────

def _make_share(state, tmp_path):
    root = tmp_path / "share"
    (root / "公开目录").mkdir(parents=True)
    (root / "私密文件夹").mkdir(parents=True)
    (root / "私密文件夹" / "秘密.txt").write_text("s", encoding="utf-8")
    (root / "机密.zip").write_bytes(b"z")
    (root / "公开目录" / "ok.txt").write_text("ok", encoding="utf-8")
    return state.registry.add(str(root)), root

def test_user_list_filters_excluded(env, tmp_path):
    client, state = env
    m, _ = _make_share(state, tmp_path)
    state.registry.exclude(m["id"], "私密文件夹")
    state.registry.exclude(m["id"], "机密.zip")

    r = client.get("/api/list?share=" + m["id"])
    names = [i["name"] for i in r.json]
    assert names == ["公开目录"]  # 排除项对用户端不可见

def test_user_download_excluded_403(env, tmp_path):
    client, state = env
    m, _ = _make_share(state, tmp_path)
    state.registry.exclude(m["id"], "机密.zip")
    r = client.get("/api/download?share=" + m["id"] + "&path=机密.zip")
    assert r.status_code == 403  # 防猜 URL

def test_user_download_excluded_dir_child_403(env, tmp_path):
    """排除文件夹内的文件也不能下载（祖先匹配）。"""
    client, state = env
    m, _ = _make_share(state, tmp_path)
    state.registry.exclude(m["id"], "私密文件夹")
    r = client.get("/api/download?share=" + m["id"] + "&path=私密文件夹/秘密.txt")
    assert r.status_code == 403

def test_user_list_excluded_dir_itself_403(env, tmp_path):
    """直接列出被排除的目录 → 403（防目录内容枚举）。"""
    client, state = env
    m, _ = _make_share(state, tmp_path)
    state.registry.exclude(m["id"], "私密文件夹")
    r = client.get("/api/list?share=" + m["id"] + "&path=私密文件夹")
    assert r.status_code == 403

def test_user_search_filters_excluded(env, tmp_path):
    client, state = env
    m, root = _make_share(state, tmp_path)
    state.registry.exclude(m["id"], "私密文件夹")
    r = client.get("/api/search?q=秘密&share=" + m["id"])
    assert r.json == []  # 排除目录下的内容搜不到

def test_user_preview_excluded_403(env, tmp_path):
    client, state = env
    m, _ = _make_share(state, tmp_path)
    state.registry.exclude(m["id"], "机密.zip")
    r = client.get("/api/preview?share=" + m["id"] + "&path=机密.zip")
    assert r.status_code == 403


# ─────────────── 密码模式 ───────────────

def test_password_flow(env, tmp_path):
    client, state = env
    state.config["password_hash"] = auth.hash_password("123456")
    root = tmp_path / "p"
    root.mkdir()
    m = state.registry.add(str(root))

    # 未带 token → 401
    assert client.get("/api/shares").status_code == 401
    assert client.get("/api/list?share=" + m["id"]).status_code == 401
    assert client.get("/api/download?share=" + m["id"]).status_code == 401

    # info 仍免鉴权（探测在线用）
    assert client.get("/api/info").status_code == 200

    # 错误密码 → 401
    r = client.post("/api/auth", json={"password": "bad"})
    assert r.status_code == 401

    # 正确密码 → token；header 与 query 两种方式都可用
    r = client.post("/api/auth", json={"password": "123456"})
    assert r.status_code == 200
    token = r.json["token"]
    assert client.get("/api/shares", headers={"X-Auth-Token": token}).status_code == 200
    assert client.get("/api/list?share=" + m["id"] + "&key=" + token).status_code == 200
    assert client.get("/api/shares?key=" + token).status_code == 200


# ─────────────── 管理接口（仅 localhost） ───────────────

def test_admin_state_local_ok(env):
    client, _ = env
    r = client.get("/admin/api/state")  # test client 默认 remote_addr 为 127.0.0.1
    assert r.status_code == 200
    assert r.json["port"] == 8000
    assert "ips" in r.json
    assert isinstance(r.json["mounts"], list)


def test_admin_remote_403(env):
    client, _ = env
    remote = {"REMOTE_ADDR": "192.168.1.50"}
    assert client.get("/admin/api/state", environ_base=remote).status_code == 403
    assert client.get("/admin", environ_base=remote).status_code == 403
    assert client.post("/admin/api/mount", environ_base=remote,
                       json={"path": "C:/"}).status_code == 403


def test_admin_pages(env):
    client, _ = env
    assert client.get("/").status_code == 200
    assert client.get("/admin").status_code == 200


# ─────────────── 访问地址二维码 ───────────────

def test_admin_qr_png_and_validation(env):
    import api_admin
    client, state = env
    r = client.get("/admin/api/qr?ip=127.0.0.1")
    if api_admin.qrcode is None:
        assert r.status_code == 503   # 未安装 qrcode 时优雅降级
        return
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("image/png")
    assert r.data[:8] == b"\x89PNG\r\n\x1a\n"   # PNG 魔数
    # 可用 PIL 打开且尺寸合理（≥ 21 模块 × 6px/模块 + 边框）
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(r.data))
    assert img.size[0] >= 150 and img.size[1] >= 150
    # 非 IP 入参 → 400
    assert client.get("/admin/api/qr?ip=not-an-ip").status_code == 400
    assert client.get("/admin/api/qr").status_code == 400


def test_admin_mount_persist_and_unmount(env, tmp_path):
    client, state = env
    folder = tmp_path / "mnt"
    folder.mkdir()

    r = client.post("/admin/api/mount", json={"path": str(folder)})
    assert r.status_code == 200
    mid = r.json["added"][0]["id"]
    assert state.registry.get(mid) is not None

    # 已落盘到 config.json
    saved = config_mod.load(state.config_path)
    assert any(m["id"] == mid for m in saved["mounts"])

    # 挂载不存在的路径 → 400
    r = client.post("/admin/api/mount", json={"path": str(tmp_path / "没有")})
    assert r.status_code == 400

    # 移除挂载
    r = client.delete("/admin/api/mount/" + mid)
    assert r.status_code == 200
    assert state.registry.get(mid) is None
    assert client.delete("/admin/api/mount/" + mid).status_code == 404


# ─────────────── 暂时隐藏挂载（用户端不可见，管理端仍显示） ───────────────

def test_mount_hidden_toggle(env, tmp_path):
    client, state = env
    m, root = _make_share(state, tmp_path)

    # 隐藏前：用户端可见、可浏览
    assert any(s["id"] == m["id"] for s in client.get("/api/shares").json)

    # 隐藏 → 接口返回 hidden=True 并落盘
    r = client.post("/admin/api/mount/" + m["id"] + "/hidden", json={"hidden": True})
    assert r.status_code == 200 and r.json["hidden"] is True
    saved = config_mod.load(state.config_path)
    assert saved["mounts"][0].get("hidden") is True

    # 用户端：列表不可见、浏览/下载 404、全局搜索搜不到
    assert all(s["id"] != m["id"] for s in client.get("/api/shares").json)
    assert client.get("/api/list?share=" + m["id"]).status_code == 404
    assert client.get("/api/download?share=" + m["id"] + "&path=机密.zip").status_code == 404
    assert client.get("/api/search?q=机密").json == []

    # 管理端：仍可见且带 hidden 标注，可继续浏览
    mounts = client.get("/admin/api/state").json["mounts"]
    mm = next(x for x in mounts if x["id"] == m["id"])
    assert mm["hidden"] is True
    assert client.get("/admin/api/list?share=" + m["id"]).status_code == 200

    # 恢复 → 用户端重新可见；config 里 hidden 字段被清掉
    r = client.post("/admin/api/mount/" + m["id"] + "/hidden", json={"hidden": False})
    assert r.status_code == 200 and r.json["hidden"] is False
    assert any(s["id"] == m["id"] for s in client.get("/api/shares").json)
    saved = config_mod.load(state.config_path)
    assert "hidden" not in saved["mounts"][0]


def test_mount_hidden_system_mount_rejected(env, tmp_path):
    """系统挂载（上传文件夹）不走 hidden 接口（由 auto_share_uploads 开关控制）。"""
    client, state = env
    r = client.post("/admin/api/mount/uploads/hidden", json={"hidden": True})
    assert r.status_code == 404
    assert state.registry.get("uploads").get("hidden") is None


# ─────────────── 管理端：浏览 + 排除 ───────────────

def test_admin_list_marks_excluded(env, tmp_path):
    client, state = env
    m, _ = _make_share(state, tmp_path)
    state.registry.exclude(m["id"], "私密文件夹")

    r = client.get("/admin/api/list?share=" + m["id"])
    assert r.status_code == 200
    items = {i["name"]: i for i in r.json}
    assert items["私密文件夹"]["excluded"] is True
    assert items["机密.zip"].get("excluded") is not True

def test_admin_exclude_restore_roundtrip(env, tmp_path):
    client, state = env
    m, _ = _make_share(state, tmp_path)

    # 排除
    r = client.post("/admin/api/exclude", json={"share": m["id"], "path": "机密.zip"})
    assert r.status_code == 200 and r.json["ok"] is True
    assert state.registry.is_excluded(m["id"], "机密.zip") is True

    # 用户端立刻看不到
    r = client.get("/api/list?share=" + m["id"])
    assert "机密.zip" not in [i["name"] for i in r.json]

    # 落盘
    saved = config_mod.load(state.config_path)
    assert "机密.zip" in saved["mounts"][0]["excluded"]

    # 恢复后用户端重新可见
    r = client.delete("/admin/api/exclude", json={"share": m["id"], "path": "机密.zip"})
    assert r.status_code == 200 and r.json["ok"] is True
    assert state.registry.is_excluded(m["id"], "机密.zip") is False
    assert "机密.zip" in [i["name"] for i in client.get("/api/list?share=" + m["id"]).json]


# ─────────────── 右键定位（复制路径 / 在文件夹中显示 / 打开） ───────────────

def test_admin_locate_copy_returns_real_path(env, tmp_path):
    client, state = env
    m, root = _make_share(state, tmp_path)

    def locate(payload):
        return client.post("/admin/api/locate", json=payload)

    # 文件（含子目录相对路径）
    r = locate({"share": m["id"], "path": "公开目录/ok.txt", "action": "copy"})
    assert r.status_code == 200 and r.json["ok"] is True
    assert os.path.realpath(r.json["path"]) == os.path.realpath(str(root / "公开目录" / "ok.txt"))

    # 目录
    r = locate({"share": m["id"], "path": "公开目录", "action": "copy"})
    assert r.status_code == 200
    assert os.path.realpath(r.json["path"]) == os.path.realpath(str(root / "公开目录"))

    # 挂载根（path 为空）
    r = locate({"share": m["id"], "path": "", "action": "copy"})
    assert r.status_code == 200
    assert os.path.realpath(r.json["path"]) == os.path.realpath(str(root))

    # 单文件挂载：根即文件本身
    f = tmp_path / "单文件.log"
    f.write_text("x", encoding="utf-8")
    mf = state.registry.add(str(f))
    r = locate({"share": mf["id"], "path": "", "action": "copy"})
    assert r.status_code == 200
    assert os.path.realpath(r.json["path"]) == os.path.realpath(str(f))


def test_admin_locate_errors(env, tmp_path):
    client, state = env
    m, _ = _make_share(state, tmp_path)

    # 挂载不存在
    assert client.post("/admin/api/locate",
                       json={"share": "nope", "path": "", "action": "copy"}).status_code == 404
    # 路径越界
    r = client.post("/admin/api/locate",
                    json={"share": m["id"], "path": "../../etc", "action": "copy"})
    assert r.status_code == 403
    # 路径不存在
    r = client.post("/admin/api/locate",
                    json={"share": m["id"], "path": "没有这个.txt", "action": "copy"})
    assert r.status_code == 404
    # 未知操作
    r = client.post("/admin/api/locate",
                    json={"share": m["id"], "path": "机密.zip", "action": "run"})
    assert r.status_code == 400
    # 非本机调用 → 403
    remote = {"REMOTE_ADDR": "192.168.1.50"}
    assert client.post("/admin/api/locate", environ_base=remote,
                       json={"share": m["id"], "path": "", "action": "copy"}).status_code == 403


def test_admin_locate_reveal_and_open(env, tmp_path, monkeypatch):
    client, state = env
    m, root = _make_share(state, tmp_path)
    target = str(root / "机密.zip")

    import api_admin
    calls = {"explorer": [], "startfile": []}
    monkeypatch.setattr(api_admin.subprocess, "run", lambda cmd, **kw: calls["explorer"].append(cmd))
    monkeypatch.setattr(os, "startfile", lambda p: calls["startfile"].append(p), raising=False)

    # 在文件夹中显示 → explorer /select,"真实路径"
    r = client.post("/admin/api/locate",
                    json={"share": m["id"], "path": "机密.zip", "action": "reveal"})
    assert r.status_code == 200 and r.json["ok"] is True
    assert len(calls["explorer"]) == 1
    assert calls["explorer"][0] == 'explorer /select,"{}"'.format(os.path.realpath(target))

    # 打开 → os.startfile(真实路径)
    r = client.post("/admin/api/locate",
                    json={"share": m["id"], "path": "机密.zip", "action": "open"})
    assert r.status_code == 200 and r.json["ok"] is True
    assert calls["startfile"] == [os.path.realpath(target)]

def test_admin_exclude_root_400(env, tmp_path):
    client, state = env
    m, _ = _make_share(state, tmp_path)
    r = client.post("/admin/api/exclude", json={"share": m["id"], "path": ""})
    assert r.status_code == 400

def test_admin_exclude_unknown_share_404(env):
    client, _ = env
    r = client.post("/admin/api/exclude", json={"share": "nope", "path": "x"})
    assert r.status_code == 404

def test_admin_exclude_path_must_exist(env, tmp_path):
    """只能排除真实存在的子项。"""
    client, state = env
    m, _ = _make_share(state, tmp_path)
    r = client.post("/admin/api/exclude", json={"share": m["id"], "path": "不存在的文件"})
    assert r.status_code == 400

def test_admin_state_includes_excluded(env, tmp_path):
    """state 接口附带排除列表（左栏「已排除」折叠区数据源）。"""
    client, state = env
    m, _ = _make_share(state, tmp_path)
    state.registry.exclude(m["id"], "私密文件夹")
    r = client.get("/admin/api/state")
    mount = next(x for x in r.json["mounts"] if x["id"] == m["id"])
    assert mount["excluded"] == ["私密文件夹"]


def test_admin_settings_password_toggle(env):
    client, state = env

    # 开启密码 → 用户 API 需要鉴权
    r = client.post("/admin/api/settings", json={"password": "pw"})
    assert r.status_code == 200
    assert state.config["password_hash"]
    assert client.get("/api/shares").status_code == 401

    # 开启后旧 token 全部失效
    token = client.post("/api/auth", json={"password": "pw"}).json["token"]
    assert client.get("/api/shares", headers={"X-Auth-Token": token}).status_code == 200
    client.post("/admin/api/settings", json={"password": "pw2"})
    assert client.get("/api/shares", headers={"X-Auth-Token": token}).status_code == 401

    # 关闭密码 → 直接放行
    r = client.post("/admin/api/settings", json={"password": ""})
    assert r.status_code == 200
    assert state.config["password_hash"] is None
    assert client.get("/api/shares").status_code == 200


def test_admin_settings_bad_port_400(env):
    client, _ = env
    assert client.post("/admin/api/settings", json={"port": "abc"}).status_code == 400
    assert client.post("/admin/api/settings", json={"port": 70000}).status_code == 400
