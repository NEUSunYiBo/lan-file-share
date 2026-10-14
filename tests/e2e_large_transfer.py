"""超大文件端到端验证脚本（手动运行，不属于 pytest 用例集）。

用途：验证"任意大小的文件都能正确共享/传输"，覆盖历史上两道限制：
  - waitress max_request_body_size 默认 1GB（≥1GB 的上传被 413 拒绝）
  - Flask MAX_CONTENT_LENGTH 2GB（>2GB 的上传被拒）

流程：生成指定大小的测试文件（内容含逐块递增标记，非全零——可发现
截断/补零类错误）→ 启动真实 waitress 服务（随机端口，仅本机访问）→
HTTP multipart 上传 → 校验落盘文件 SHA-256 → HTTP 下载 → 再次校验
SHA-256 → 输出结论与吞吐数据。

用法（在项目根目录）：
    python tests/e2e_large_transfer.py                # 默认 2.5 GiB
    python tests/e2e_large_transfer.py --size-gb 1.2  # 自定义大小
    python tests/e2e_large_transfer.py --dir D:/tmp   # 指定工作目录
    python tests/e2e_large_transfer.py --keep         # 保留测试文件

退出码：0 = 全部校验通过；1 = 存在失败项。
"""

import argparse
import hashlib
import http.client
import os
import shutil
import socket
import struct
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

MB = 1024 * 1024
CHUNK = 4 * MB


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return "%.2f %s" % (n, unit)
        n /= 1024.0


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _get(port, path, headers=None, read_bytes=0):
    """GET 请求，返回 ((status, Content-Length, Content-Range), 读到的字节)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3600)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    info = (resp.status, resp.getheader("Content-Length"),
            resp.getheader("Content-Range"))
    data = resp.read(read_bytes) if read_bytes else b""
    conn.close()
    return info, data


def expect_equal(src, offset, data):
    """校验 data 与 src 文件自 offset 起的字节一致。"""
    with open(src, "rb") as f:
        f.seek(offset)
        return f.read(len(data)) == data


def make_test_file(path, size):
    """逐块写入带序号标记的伪随机数据（块首 8 字节为块号，保证块间不同）。"""
    base = os.urandom(MB)  # 同一随机底块，每块仅改写块首 8 字节为块号（块间不同即可）
    written = 0
    t0 = time.time()
    with open(path, "wb") as f:
        i = 0
        while written < size:
            n = min(MB, size - written)
            f.write((struct.pack("<Q", i) + base[8:])[:n])
            written += n
            i += 1
        assert written == size
    return time.time() - t0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def progress(tag, done, total, t0):
    speed = done / max(1e-6, time.time() - t0) / MB
    pct = done * 100.0 / total
    sys.stdout.write("\r  %s %5.1f%%  %s / %s  (%.1f MiB/s)"
                     % (tag, pct, human(done), human(total), speed))
    sys.stdout.flush()


def http_upload(port, src, name, size):
    """流式 multipart 上传（与浏览器 XHR 行为一致），返回 (状态码, JSON/文本)。"""
    boundary = "----LanShareE2E" + uuid.uuid4().hex
    head = ("--%s\r\n"
            'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            % (boundary, name)).encode("utf-8")
    tail = ("\r\n--%s\r\n"
            'Content-Disposition: form-data; name="path"\r\n\r\n'
            "%s\r\n--%s--\r\n" % (boundary, name, boundary)).encode("utf-8")
    total = len(head) + size + len(tail)          # 请求体 = 文件 + 边界开销
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3600)
    conn.putrequest("POST", "/api/upload")
    conn.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
    conn.putheader("Content-Length", str(total))
    conn.endheaders()
    conn.send(head)
    sent, t0 = 0, time.time()
    with open(src, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            conn.send(b)
            sent += len(b)
            progress("上传", sent, size, t0)
    conn.send(tail)
    print()
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def http_download(port, share, relpath, dest):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3600)
    conn.request("GET", "/api/download?share=%s&path=%s"
                 % (share, relpath.replace("\\", "/")))
    resp = conn.getresponse()
    if resp.status != 200:
        body = resp.read()
        conn.close()
        return resp.status, body
    total = int(resp.getheader("Content-Length") or 0)
    done, t0 = 0, time.time()
    with open(dest, "wb") as f:
        while True:
            b = resp.read(CHUNK)
            if not b:
                break
            f.write(b)
            done += len(b)
            progress("下载", done, total or done, t0)
    print()
    conn.close()
    return 200, b""


def main():
    ap = argparse.ArgumentParser(description="超大文件端到端传输验证")
    ap.add_argument("--size-gb", type=float, default=2.5,
                    help="测试文件大小（GiB），默认 2.5（同时跨过旧的 1GB/2GB 两道限制）")
    ap.add_argument("--dir", default=None, help="工作目录（默认系统临时目录）")
    ap.add_argument("--keep", action="store_true", help="保留测试文件")
    args = ap.parse_args()

    size = int(args.size_gb * 1024 ** 3)
    work = args.dir or tempfile.mkdtemp(prefix="lanshare_e2e_")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "big_source.bin")
    dl = os.path.join(work, "big_downloaded.bin")
    upload_dir = os.path.join(work, "uploads")
    results = []

    print("=" * 72)
    print("超大文件端到端传输验证")
    print("  文件大小: %s (%.2f GiB)" % (human(size), args.size_gb))
    print("  工作目录: %s" % work)
    print("  覆盖限制: waitress 1GB 请求体上限 / Flask 2GB 上传上限")
    print("=" * 72)

    state = server.AppState(
        {"port": 8000, "password_hash": None, "mounts": [],
         "upload_dir": upload_dir, "auto_share_uploads": True},
        config_path=os.path.join(work, "config.json"))
    app = server.create_app(state)
    mgr = server.ServerManager(app, threads=8)
    port = free_port()
    mgr.start(port)
    print("服务已启动: http://127.0.0.1:%d" % port)

    try:
        print("\n[1/5] 生成测试文件…")
        t = make_test_file(src, size)
        src_hash = sha256_of(src)
        print("  完成（%.1fs），SHA-256 = %s" % (t, src_hash))

        print("\n[2/5] HTTP 上传 %s…" % human(size))
        t0 = time.time()
        status, body = http_upload(port, src, "big_source.bin", size)
        up_time = time.time() - t0
        ok = status == 200
        results.append(("HTTP 状态 200（未被 413 拒绝）", ok, "status=%s %s"
                        % (status, body[:120].decode("utf-8", "replace"))))
        if ok:
            print("  上传完成：%.1fs，平均 %.1f MiB/s"
                  % (up_time, size / MB / up_time))

        print("\n[3/5] 校验服务端落盘文件…")
        landed = os.path.join(upload_dir, "big_source.bin")
        if not os.path.isfile(landed):  # 同名已存在时服务端会加 (1)
            alt = os.path.join(upload_dir, "big_source(1).bin")
            landed = alt if os.path.isfile(alt) else landed
        ok = os.path.isfile(landed) and os.path.getsize(landed) == size
        results.append(("服务端文件大小一致", ok,
                        "%s -> %s 字节" % (os.path.basename(landed),
                                          os.path.getsize(landed) if os.path.isfile(landed) else "缺失")))
        if ok:
            landed_hash = sha256_of(landed)
            results.append(("服务端文件 SHA-256 与源一致",
                            landed_hash == src_hash, landed_hash[:24] + "…"))

        print("\n[4/6] HTTP 下载回本机…")
        t0 = time.time()
        status, body = http_download(port, "uploads",
                                     os.path.basename(landed), dl)
        dl_time = time.time() - t0
        ok = status == 200
        results.append(("下载状态 200", ok, "status=%s %s"
                        % (status, body[:120].decode("utf-8", "replace"))))
        if ok:
            print("  下载完成：%.1fs，平均 %.1f MiB/s"
                  % (dl_time, size / MB / dl_time))
            ok2 = os.path.getsize(dl) == size
            results.append(("下载文件大小一致", ok2,
                            "%s 字节" % os.path.getsize(dl)))
            if ok2:
                dl_hash = sha256_of(dl)
                results.append(("下载文件 SHA-256 与源一致",
                                dl_hash == src_hash, dl_hash[:24] + "…"))

        print("\n[5/6] 预览接口（浏览器 Range 请求路径）…")
        info, data = _get(port, "/api/preview?share=uploads&path=" + os.path.basename(landed),
                          {"Range": "bytes=0-"}, read_bytes=2 * MB)
        ok = (info[0] == 206 and info[1] == str(size) and expect_equal(src, 0, data))
        results.append(("预览 bytes=0- → 206，首 2MB 一致", ok,
                        "%s CL=%s CR=%s" % (info[0], info[1], info[2])))
        off = size - (4 * MB)
        info, data = _get(port, "/api/preview?share=uploads&path=" + os.path.basename(landed),
                          {"Range": "bytes=%d-" % off}, read_bytes=4 * MB)
        ok = (info[0] == 206 and expect_equal(src, off, data))
        results.append(("预览尾部 Range（>1GB 偏移）内容一致", ok,
                        "%s CR=%s" % (info[0], info[2])))

        print("\n[6/6] 检查传输日志…")
        rows = [r for r in state.log.recent(20)
                if r["filename"].startswith("big_source")]
        ok = any(r["type"] == "upload" and r["size"] == size for r in rows)
        results.append(("日志记录上传（大小正确）", ok, repr(rows[:2])[:160]))
    finally:
        mgr.stop()

    print("\n" + "=" * 72)
    print("结果汇总")
    print("=" * 72)
    all_ok = True
    for name, ok, detail in results:
        print("  [%s] %s\n        %s" % ("PASS" if ok else "FAIL", name, detail))
        all_ok &= ok

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
        print("\n测试文件已清理（--keep 可保留）")
    else:
        print("\n测试文件保留在: %s" % work)

    print("\n结论: %s" % ("全部校验通过 —— 超大文件传输正常"
                         if all_ok else "存在失败项，请检查上方明细"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
