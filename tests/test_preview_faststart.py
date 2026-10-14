"""虚拟 faststart 预览测试：>1GB 且 moov 在末尾的 MP4 按重排布局提供服务。

覆盖：
- 虚拟布局字节级正确性（[ftyp][moov'][mdat]，总长不变，stco 偏移 +moov 大小）
- Range 映射（头部/moov/数据区段与跨段拼接）
- /api/preview 走虚拟字节、/api/download 保持原始字节
- 各种不适用场景自动回退（moov 在前、分片、溢出、未达阈值）
"""

import struct

import pytest

import api_user
import server

# ─────────────── 合成 MP4 ───────────────


def _box(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _stco(offsets, co64=False):
    typ = b"co64" if co64 else b"stco"
    fmt = ">Q" if co64 else ">I"
    body = struct.pack(">I", len(offsets)) + b"".join(struct.pack(fmt, o) for o in offsets)
    return _box(typ, b"\x00\x00\x00\x00" + body)


def make_mp4(mdat_len=4096, moov_first=False, fragmented=False, co64=False):
    """返回 (file_bytes, head_len, mdat_payload_start, moov_size)。"""
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    free = _box(b"free", b"\x00" * 8)
    head = ftyp + free
    mdat = _box(b"mdat", b"\xAB" * mdat_len)
    payload_start = len(head) + 8
    tables = _stco([payload_start, payload_start + 100], co64=co64)
    trak = _box(b"trak", _box(b"mdia", _box(b"minf", _box(b"stbl", tables))))
    extra = _box(b"moof", b"\x00" * 16) if fragmented else b""
    moov = _box(b"moov", trak)
    if moov_first:
        data = ftyp + moov + free + mdat + extra
    else:
        data = head + mdat + extra + moov
    return data, len(head), payload_start, len(moov)


@pytest.fixture()
def env(tmp_path):
    state = server.AppState(
        {"port": 8000, "password_hash": None, "mounts": [],
         "upload_dir": str(tmp_path / "uploads"), "auto_share_uploads": True},
        config_path=str(tmp_path / "config.json"),
    )
    app = server.create_app(state)
    app.testing = True
    return app.test_client(), state, tmp_path


def _write(tmp_path, data, name="big.mp4"):
    p = tmp_path / "uploads"
    p.mkdir(exist_ok=True)
    f = p / name
    f.write_bytes(data)
    return str(f)


def _enable(monkeypatch):
    monkeypatch.setattr(api_user, "FASTSTART_PREVIEW_MIN", 0)


# ─────────────── 视图映射 ───────────────


def test_view_mapping(tmp_path):
    data, head_len, payload_start, moov_size = make_mp4(mdat_len=4096)
    p = _write(tmp_path, data)
    view = api_user._FaststartView(p, len(data))
    assert view.head_end == head_len
    assert view.moov_size == moov_size
    assert view.total == len(data)

    view.ensure_moov()
    total = len(data)

    # 整文件：3 段，长度守恒
    segs = view.segments(0, total - 1)
    assert [s[0] for s in segs] == ["file", "moov", "file"]
    assert sum(s[2] for s in segs) == total
    assert segs[0][1:] == (0, head_len)

    # 数据区中间：单段（真实偏移 = 虚拟偏移 - moov_size）
    v_mid = head_len + moov_size + 500
    segs = view.segments(v_mid, v_mid + 999)
    assert segs == [("file", head_len + 500, 1000)]

    # moov 区中间：单段，相对补丁后 moov 的偏移
    segs = view.segments(head_len + 16, head_len + 31)
    assert segs == [("moov", 16, 16)]


def test_virtual_bytes_layout_and_patch(tmp_path):
    """完整重组虚拟文件：必须是 [ftyp][free][moov'][mdat]，且 stco 值 +moov_size。"""
    data, head_len, payload_start, moov_size = make_mp4(mdat_len=4096)
    p = _write(tmp_path, data)
    view = api_user._FaststartView(p, len(data))
    view.ensure_moov()

    # 重组
    out = bytearray()
    for src, off, n in view.segments(0, len(data) - 1):
        if src == "moov":
            out += view.moov_patched_slice(off, n)
        else:
            with open(p, "rb") as f:
                f.seek(off)
                out += f.read(n)
    assert len(out) == len(data)

    # 结构：ftyp、free、moov、mdat
    boxes = []
    pos = 0
    while pos + 8 <= len(out):
        size = struct.unpack(">I", out[pos:pos + 4])[0]
        boxes.append(out[pos + 4:pos + 8])
        pos += size
    assert boxes == [b"ftyp", b"free", b"moov", b"mdat"]

    # stco 修补：原值 + moov_size
    idx = out.find(b"stco")
    assert idx > 0
    n = struct.unpack(">I", out[idx + 8:idx + 12])[0]
    vals = [struct.unpack(">I", out[idx + 12 + 4 * i:idx + 16 + 4 * i])[0] for i in range(n)]
    assert vals == [payload_start + moov_size, payload_start + 100 + moov_size]

    # 数据区字节原样保留（真实 mdat 盒子区间 = [head_len, len-moov_size)）
    assert bytes(out[head_len + moov_size:]) == data[head_len:len(data) - moov_size]


def test_co64_patch(tmp_path):
    data, _h, payload_start, moov_size = make_mp4(mdat_len=512, co64=True)
    p = _write(tmp_path, data)
    view = api_user._FaststartView(p, len(data))
    view.ensure_moov()
    patched = view.moov_patched_slice(0, view.moov_size)
    idx = patched.find(b"co64")
    vals = [struct.unpack(">Q", patched[idx + 12 + 8 * i:idx + 20 + 8 * i])[0] for i in range(2)]
    assert vals == [payload_start + moov_size, payload_start + 100 + moov_size]


# ─────────────── HTTP 行为 ───────────────


def test_preview_serves_virtual_layout(env, tmp_path, monkeypatch):
    _enable(monkeypatch)
    data, head_len, payload_start, moov_size = make_mp4(mdat_len=4096)
    p = _write(tmp_path, data)
    client, _, _ = env

    r = client.get("/api/preview?share=uploads&path=big.mp4")
    assert r.status_code == 200
    assert r.headers["Content-Length"] == str(len(data))
    body = r.data
    assert len(body) == len(data)
    # 头部即 moov（faststart 布局），不需要任何尾部请求
    assert body[4:8] == b"ftyp"
    assert body[head_len + 4:head_len + 8] == b"moov"
    # mdat 紧跟 moov 之后
    assert body[head_len + moov_size + 4:head_len + moov_size + 8] == b"mdat"

    # Range：跨段（头部+moov 前缀）
    r = client.get("/api/preview?share=uploads&path=big.mp4",
                   headers={"Range": "bytes=0-%d" % (head_len + 8 - 1)})
    assert r.status_code == 206
    assert r.headers["Content-Range"] == "bytes 0-%d/%d" % (head_len + 7, len(data))
    assert r.data[4:8] == b"ftyp"

    # Range：数据区中间 → 原文件同偏移字节
    v_mid = head_len + moov_size + 100
    r = client.get("/api/preview?share=uploads&path=big.mp4",
                   headers={"Range": "bytes=%d-%d" % (v_mid, v_mid + 63)})
    assert r.status_code == 206
    assert r.data == data[head_len + 100:head_len + 164]


def test_download_stays_raw(env, tmp_path, monkeypatch):
    """下载接口不受虚拟视图影响：字节与磁盘文件完全一致。"""
    _enable(monkeypatch)
    data, _h, _p, _m = make_mp4(mdat_len=4096)
    _write(tmp_path, data)
    client, _, _ = env
    r = client.get("/api/download?share=uploads&path=big.mp4")
    assert r.status_code == 200
    assert r.data == data  # 原始布局（moov 在末尾）


def test_preview_falls_back_when_not_applicable(env, tmp_path, monkeypatch):
    _enable(monkeypatch)
    client, _, _ = env
    # moov 在头部（已是 faststart）→ 原始服务
    data, _h, _p, _m = make_mp4(mdat_len=1024, moov_first=True)
    _write(tmp_path, data, "head.mp4")
    r = client.get("/api/preview?share=uploads&path=head.mp4")
    assert r.data == data
    # 分片（含 moof）→ 原始服务
    data2, _h, _p, _m = make_mp4(mdat_len=1024, fragmented=True)
    _write(tmp_path, data2, "frag.mp4")
    r = client.get("/api/preview?share=uploads&path=frag.mp4")
    assert r.data == data2


def test_threshold_and_overflow(tmp_path, monkeypatch):
    # 未达阈值 → 不做视图
    data, _h, _p, _m = make_mp4(mdat_len=512)
    p = _write(tmp_path, data)
    monkeypatch.setattr(api_user, "FASTSTART_PREVIEW_MIN", 1 << 40)
    assert api_user._faststart_view(p, len(data)) is None
    # stco 溢出（偏移 + moov 大小超 32 位）→ 视图构建失败
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    head = ftyp + _box(b"free", b"\x00" * 8)
    mdat = _box(b"mdat", b"\xAB" * 512)
    tables = _stco([0xFFFFFFF0, 0xFFFFFF00])  # 加 moov 大小后必然溢出
    trak = _box(b"trak", _box(b"mdia", _box(b"minf", _box(b"stbl", tables))))
    over = head + mdat + _box(b"moov", trak)
    p2 = _write(tmp_path, over, "over.mp4")
    monkeypatch.setattr(api_user, "FASTSTART_PREVIEW_MIN", 0)
    assert api_user._faststart_view(p2, len(over)) is None


def test_non_mp4_untouched(env, tmp_path, monkeypatch):
    _enable(monkeypatch)
    client, _, _ = env
    payload = b"hello world" * 100
    _write(tmp_path, payload, "note.txt")
    r = client.get("/api/preview?share=uploads&path=note.txt")
    assert r.data == payload
