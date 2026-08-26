"""传输日志模块测试：写入/查询/去重/聚合。"""

import time

import transfer_log


def make_log(tmp_path):
    return transfer_log.TransferLog(str(tmp_path / "logs.db"))


def test_record_and_recent(tmp_path):
    log = make_log(tmp_path)
    assert log.record("upload", "10.0.0.1", "a.txt", 100) is True
    assert log.record("download", "10.0.0.2", "b.mp4", 2048) is True
    rows = log.recent(10)
    assert len(rows) == 2
    assert rows[0]["filename"] == "b.mp4"  # 最新在前
    assert rows[1] == {"ts": rows[1]["ts"], "type": "upload", "ip": "10.0.0.1",
                       "filename": "a.txt", "size": 100}
    assert abs(rows[0]["ts"] - time.time()) < 5


def test_record_persists_across_instances(tmp_path):
    log = make_log(tmp_path)
    log.record("upload", "10.0.0.1", "a.txt", 100)
    log2 = transfer_log.TransferLog(str(tmp_path / "logs.db"))  # 重新打开
    assert len(log2.recent(10)) == 1


def test_record_bad_db_degrades(tmp_path):
    # db 路径本身是目录 → 初始化失败 → 降级为不可用（record 返回 False，不抛异常）
    log = transfer_log.TransferLog(str(tmp_path))
    assert log.record("upload", "ip", "f", 1) is False
    assert log.recent() == []
    assert log.stats() == []
    assert log.totals()["uploads_count"] == 0


def test_preview_dedup_within_window(tmp_path):
    log = make_log(tmp_path)
    # 第一次预览记一条
    assert log.record_preview("10.0.0.1", "v.mp4", 5, "/real/v.mp4") is True
    # 窗口期内同一 (ip, path) 合并
    assert log.record_preview("10.0.0.1", "v.mp4", 5, "/real/v.mp4") is False
    # 不同 ip 或不同文件不受影响
    assert log.record_preview("10.0.0.2", "v.mp4", 5, "/real/v.mp4") is True
    assert log.record_preview("10.0.0.1", "o.mp4", 5, "/real/o.mp4") is True
    # 窗口期 0 → 每次都记
    assert log.record_preview("10.0.0.1", "v.mp4", 5, "/real/v.mp4", window=0) is True
    assert len(log.recent()) == 4  # 首次 + ip2 + o.mp4 + 窗口期 0 重记


def test_stats_hour_buckets(tmp_path):
    log = make_log(tmp_path)
    now = time.time()
    # 两小时前 1 次上传；当前小时 1 上传 2 下载（含预览）
    h2 = now - 2.5 * 3600
    for ts, etype, size in [(h2, "upload", 10), (now, "upload", 20),
                            (now, "download", 30), (now, "preview", 40)]:
        log._insert(ts, etype, "ip", "f.bin", size)
    buckets = log.stats(range_hours=6, bucket="hour")
    assert len(buckets) >= 3  # 覆盖两小时前到当前
    b_now = min(buckets, key=lambda b: abs(b["bucket_start"] - (now - now % 3600)))
    assert b_now["uploads_count"] == 1 and b_now["uploads_bytes"] == 20
    assert b_now["downloads_count"] == 2 and b_now["downloads_bytes"] == 70
    b_old = [b for b in buckets if abs(b["bucket_start"] - (h2 - h2 % 3600)) < 60][0]
    assert b_old["uploads_count"] == 1
    # 桶按时间升序
    starts = [b["bucket_start"] for b in buckets]
    assert starts == sorted(starts)


def test_totals(tmp_path):
    log = make_log(tmp_path)
    now = time.time()
    log._insert(now, "upload", "ip", "a", 100)
    log._insert(now, "upload", "ip", "b", 50)
    log._insert(now - 2 * 86400, "download", "ip", "c", 10)  # 前天，不计入今日
    t = log.totals()
    assert t["uploads_count"] == 2 and t["uploads_bytes"] == 150
    assert t["downloads_count"] == 1 and t["downloads_bytes"] == 10
    assert t["today_count"] == 2
