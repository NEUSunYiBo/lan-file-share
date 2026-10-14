"""build_exe 的 PATH 净化测试：防止别家 Tcl/Tk DLL 污染打进版本不匹配的 DLL。"""
import os

import build_exe


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


def _mk_tcl_dir(base):
    d = base / "tkbin"
    d.mkdir(parents=True)
    (d / "tcl86t.dll").write_bytes(b"x")
    return d


def test_sanitize_drops_foreign_tcl_keeps_matching_dir(monkeypatch, tmp_path):
    """携带 tcl86t.dll 的外来目录被剔除，配套目录即使携带也保留。"""
    right = _mk_tcl_dir(tmp_path)          # 模拟配套 Tcl 目录（自身也有 tcl86t.dll）
    evil = _mk_tcl_dir(tmp_path / "evil")  # 模拟 conda pkgs 缓存等污染源
    other = tmp_path / "plain"
    other.mkdir()

    monkeypatch.setattr(build_exe, "_python_tcl_dir", lambda: str(right))
    monkeypatch.setenv("PATH", os.pathsep.join([str(evil), str(right), str(other)]))
    parts = [_norm(p) for p in build_exe._sanitize_build_path().split(os.pathsep) if p]

    assert _norm(str(evil)) not in parts    # 外来 tcl 目录剔除
    assert _norm(str(right)) in parts       # 配套目录保留
    assert _norm(str(other)) in parts       # 普通目录不受影响


def test_sanitize_noop_without_tcl_dirs(monkeypatch, tmp_path):
    """PATH 里没有 Tcl/Tk 携带者时原样保留。"""
    d1, d2 = tmp_path / "a", tmp_path / "b"
    d1.mkdir(); d2.mkdir()
    monkeypatch.setattr(build_exe, "_python_tcl_dir", lambda: str(tmp_path))
    monkeypatch.setenv("PATH", os.pathsep.join([str(d1), str(d2)]))
    parts = [_norm(p) for p in build_exe._sanitize_build_path().split(os.pathsep) if p]
    assert _norm(str(d1)) in parts and _norm(str(d2)) in parts


def test_sanitize_fallback_keeps_path_when_tcl_dir_unknown(monkeypatch, tmp_path):
    """定位不到配套 Tcl 目录时：不剔除任何目录（避免误伤），行为退回旧版。"""
    d = _mk_tcl_dir(tmp_path)
    monkeypatch.setattr(build_exe, "_python_tcl_dir", lambda: None)
    monkeypatch.setenv("PATH", str(d))
    parts = [_norm(p) for p in build_exe._sanitize_build_path().split(os.pathsep) if p]
    assert _norm(str(d)) in parts


def test_sanitize_ensures_paired_dll_dir_in_path(monkeypatch, tmp_path):
    """配套 DLL 目录不在 PATH 时自动补入。

    conda 系 Python 的 sqlite3.dll / libexpat / liblzma / ffi-8 等原生依赖都放在
    配套目录（如 Library\\bin）。构建 shell 未激活 conda 时 PATH 里没有它，
    PyInstaller 会漏收集这些 DLL——exe 构建成功但运行时报缺 DLL，必须由构建
    脚本兜底补入。
    """
    right = _mk_tcl_dir(tmp_path)          # 配套目录（模拟 Library\bin）
    other = tmp_path / "plain"
    other.mkdir()
    monkeypatch.setattr(build_exe, "_python_tcl_dir", lambda: str(right))
    monkeypatch.setenv("PATH", str(other))  # PATH 中不含配套目录
    parts = [_norm(p) for p in build_exe._sanitize_build_path().split(os.pathsep) if p]
    assert _norm(str(right)) in parts       # 已补入
    assert _norm(str(other)) in parts       # 原有目录不丢
