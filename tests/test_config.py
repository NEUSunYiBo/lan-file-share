"""config 模块测试：读写、默认值、损坏容错。"""

import json
import os

import config
import config as config_mod


def test_load_missing_returns_defaults(tmp_path):
    cfg = config.load(tmp_path / "config.json")
    assert cfg["port"] == config.DEFAULT_PORT
    assert cfg["password_hash"] is None
    assert cfg["mounts"] == []


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "config.json"
    config.save({"port": 9001, "password_hash": "abc$def",
                 "mounts": [{"id": "m1", "name": "数据", "path": "D:/data"}]}, p)
    cfg = config.load(p)
    assert cfg["port"] == 9001
    assert cfg["password_hash"] == "abc$def"
    assert cfg["mounts"][0]["name"] == "数据"
    assert cfg["mounts"][0]["id"] == "m1"


def test_load_corrupted_returns_defaults(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ not valid json !!", encoding="utf-8")
    cfg = config.load(p)
    assert cfg["port"] == config.DEFAULT_PORT
    assert cfg["mounts"] == []
    assert cfg["password_hash"] is None


def test_normalize_rejects_bad_port(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"port": "abc"}), encoding="utf-8")
    assert config.load(p)["port"] == config.DEFAULT_PORT
    p.write_text(json.dumps({"port": 99999}), encoding="utf-8")
    assert config.load(p)["port"] == config.DEFAULT_PORT


# ─────────────── 排除规则持久化 ───────────────

def test_normalize_keeps_excluded():
    data = {
        "port": 8000, "password_hash": None,
        "mounts": [{"id": "m1", "name": "n", "path": "C:/x", "excluded": ["a", "b/c"]}],
    }
    out = config_mod._normalize(data)
    assert out["mounts"][0]["excluded"] == ["a", "b/c"]

def test_normalize_excluded_defaults_empty():
    """旧配置无 excluded 字段：不生成该键（保持 JSON 干净）。"""
    out = config_mod._normalize({"mounts": [{"id": "m1", "name": "n", "path": "C:/x"}]})
    assert "excluded" not in out["mounts"][0]

def test_normalize_excluded_drops_invalid():
    """excluded 非列表或元素非字符串：丢弃。"""
    out = config_mod._normalize({"mounts": [
        {"id": "m1", "name": "n", "path": "C:/x", "excluded": "not-a-list"},
    ]})
    assert "excluded" not in out["mounts"][0]


def test_normalize_drops_bad_password_hash(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"password_hash": "no-dollar-sign"}), encoding="utf-8")
    assert config.load(p)["password_hash"] is None


def test_normalize_drops_bad_mounts_keeps_good(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "mounts": [{"name": "缺 path"}, "bad", {"path": "D:/ok", "id": 5}, 123]
    }), encoding="utf-8")
    cfg = config.load(p)
    assert len(cfg["mounts"]) == 1
    m = cfg["mounts"][0]
    assert m["id"] == "5"
    assert m["name"] == "ok"  # 缺名称时取 basename


# ─────────────── 新增：上传 / 仪表板配置 ───────────────

def test_defaults_for_upload_and_dashboard():
    cfg = config_mod._normalize({})
    assert cfg["upload_dir"] == config_mod.DEFAULT_UPLOAD_DIR
    assert cfg["auto_share_uploads"] is True
    cards = cfg["dashboard"]["cards"]
    assert [c["id"] for c in cards] == [
        "stats", "type-pie", "type-size",
        "upload-trend", "download-trend", "recent"]
    assert all(c["enabled"] for c in cards)


def test_upload_dir_relative_resolved_to_abs():
    cfg = config_mod._normalize({"upload_dir": "my_uploads"})
    assert cfg["upload_dir"] == os.path.join(config_mod.BASE_DIR, "my_uploads")
    # 绝对路径原样保留
    cfg = config_mod._normalize({"upload_dir": r"D:\some\dir"})
    assert cfg["upload_dir"] == r"D:\some\dir"


def test_upload_dir_bad_values_fallback():
    assert config_mod._normalize({"upload_dir": 123})["upload_dir"] == config_mod.DEFAULT_UPLOAD_DIR
    assert config_mod._normalize({"upload_dir": "   "})["upload_dir"] == config_mod.DEFAULT_UPLOAD_DIR
    assert config_mod._normalize({"auto_share_uploads": "yes"})["auto_share_uploads"] is True


def test_dashboard_cards_partial_and_reorder():
    # 只配置两张卡（禁用 type-pie、recent 排最前），其余补默认到末尾
    cfg = config_mod._normalize({"dashboard": {"cards": [
        {"id": "type-pie", "enabled": False, "order": 2},
        {"id": "recent", "enabled": True, "order": 1},
    ]}})
    cards = cfg["dashboard"]["cards"]
    assert [c["id"] for c in cards][0] == "recent"      # order=1 排最前
    assert cards[1]["id"] == "type-pie" and cards[1]["enabled"] is False
    assert [c["id"] for c in cards][2:] == ["stats", "type-size", "upload-trend", "download-trend"]
    assert [c["order"] for c in cards] == [1, 2, 3, 4, 5, 6]  # order 重排为连续

    # 未知 id 忽略；坏结构回退默认
    cfg = config_mod._normalize({"dashboard": {"cards": [{"id": "nope"}, "junk"]}})
    assert [c["id"] for c in cfg["dashboard"]["cards"]][0] == "stats"
    cfg = config_mod._normalize({"dashboard": "bad"})
    assert len(cfg["dashboard"]["cards"]) == 6
