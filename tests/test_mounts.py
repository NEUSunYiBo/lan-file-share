"""mounts 模块测试：挂载增删、路径穿越防护。"""

import os

import pytest

from mounts import MountRegistry, PathEscapeError


def test_add_list_remove(tmp_path):
    reg = MountRegistry()
    folder = tmp_path / "分享文件夹"
    folder.mkdir()
    entry = reg.add(str(folder))
    assert entry["name"] == "分享文件夹"
    assert entry["id"]
    assert len(reg.list()) == 1
    assert reg.get(entry["id"])["path"] == str(folder)

    assert reg.remove(entry["id"]) is True
    assert reg.list() == []
    assert reg.remove("不存在") is False


def test_add_missing_raises(tmp_path):
    reg = MountRegistry()
    with pytest.raises(FileNotFoundError):
        reg.add(str(tmp_path / "不存在"))


def test_add_single_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi", encoding="utf-8")
    reg = MountRegistry()
    e = reg.add(str(f))
    assert e["name"] == "a.txt"


def test_init_assigns_ids_to_mounts_without_id():
    reg = MountRegistry([{"name": "x", "path": "C:/x"}])
    assert reg.list()[0]["id"]


def test_safe_join_inside(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    real = MountRegistry.safe_join(str(tmp_path), "sub")
    assert os.path.basename(real) == "sub"


def test_safe_join_empty_returns_root(tmp_path):
    real = MountRegistry.safe_join(str(tmp_path), "")
    assert os.path.samefile(real, str(tmp_path))


def test_safe_join_escape_blocked(tmp_path):
    for rel in ("..", "..\\..", "sub/../../..", "a/../../../../windows"):
        with pytest.raises(PathEscapeError):
            MountRegistry.safe_join(str(tmp_path), rel)


def test_safe_join_absolute_blocked(tmp_path):
    with pytest.raises(PathEscapeError):
        MountRegistry.safe_join(str(tmp_path), os.path.abspath(os.sep))
    with pytest.raises(PathEscapeError):
        MountRegistry.safe_join(str(tmp_path), "C:\\Windows")


def test_safe_join_symlink_escape_blocked(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    link = tmp_path / "link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境无创建符号链接权限")
    with pytest.raises(PathEscapeError):
        MountRegistry.safe_join(str(tmp_path), "link")


# ─────────────── 排除规则（取消共享子项） ───────────────

def test_exclude_and_is_excluded(tmp_path):
    reg = MountRegistry()
    m = reg.add(str(tmp_path))
    reg.exclude(m["id"], "私密文件夹")
    assert reg.is_excluded(m["id"], "私密文件夹") is True
    assert reg.is_excluded(m["id"], "私密文件夹/内部文件.txt") is True  # 祖先匹配：递归排除
    assert reg.is_excluded(m["id"], "其他文件夹") is False
    assert reg.is_excluded(m["id"], "") is False  # 根路径永不排除

def test_exclude_restore(tmp_path):
    reg = MountRegistry()
    m = reg.add(str(tmp_path))
    reg.exclude(m["id"], "a.txt")
    assert reg.restore(m["id"], "a.txt") is True
    assert reg.is_excluded(m["id"], "a.txt") is False
    assert reg.restore(m["id"], "不存在") is False

def test_exclude_root_rejected(tmp_path):
    reg = MountRegistry()
    m = reg.add(str(tmp_path))
    with pytest.raises(ValueError):
        reg.exclude(m["id"], "")

def test_exclude_normalizes_separators(tmp_path):
    """前端传来的路径统一按 / 存储与比较，Windows 反斜杠自动归一。"""
    reg = MountRegistry()
    m = reg.add(str(tmp_path))
    reg.exclude(m["id"], "子目录\\嵌套")
    assert "子目录/嵌套" in reg.get(m["id"])["excluded"]
    assert reg.is_excluded(m["id"], "子目录/嵌套/文件") is True

def test_exclude_persisted_in_list(tmp_path):
    reg = MountRegistry()
    m = reg.add(str(tmp_path))
    reg.exclude(m["id"], "x")
    entries = reg.list()
    assert entries[0]["excluded"] == ["x"]

def test_registry_init_without_excluded_field():
    """旧 config.json 无 excluded 字段：按空处理，不报错。"""
    reg = MountRegistry([{"id": "abc", "name": "x", "path": "C:/x"}])
    assert reg.is_excluded("abc", "anything") is False


# ─────────────── 系统挂载（上传目录自动共享） ───────────────

def test_system_mount_lifecycle(tmp_path):
    reg = MountRegistry()
    e = reg.add_system("uploads", str(tmp_path), "来自设备的上传")
    assert e["id"] == "uploads" and e["system"] is True
    assert reg.get("uploads")["name"] == "来自设备的上传"
    # 默认 list() 包含系统挂载（用户端可见）
    assert any(m["id"] == "uploads" for m in reg.list())
    # managed_only 过滤（保存/管理页用）
    assert reg.list(managed_only=True) == []
    # 移除：只有系统挂载可被 remove_system 移除
    assert reg.remove_system("uploads") is True
    assert reg.remove_system("uploads") is False


def test_system_mount_not_removable_by_normal_remove(tmp_path):
    reg = MountRegistry()
    reg.add_system("uploads", str(tmp_path), "up")
    assert reg.remove("uploads") is False  # 普通移除对系统挂载无效
    assert reg.get("uploads") is not None


def test_config_never_persists_system_flag(tmp_path):
    # 手改 config 注入 system:true → 加载时被剥掉
    reg = MountRegistry([{"id": "abc", "name": "n", "path": str(tmp_path), "system": True}])
    assert "system" not in reg.list()[0]
