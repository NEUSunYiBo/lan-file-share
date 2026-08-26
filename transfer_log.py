"""传输日志：SQLite 结构化存储（上传 / 下载 / 预览事件）与统计聚合。

设计要点：
- 每次操作短事务（连接 → 执行 → 关闭），WAL 模式，waitress 多线程安全；
- 数据库不可用时整体降级（record 返回 False），不影响上传/下载主流程；
- 预览事件按 (ip, 真实路径) 去重，避免视频 Range 请求刷屏。
"""

import datetime as _dt
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL    NOT NULL,
    type     TEXT    NOT NULL,
    ip       TEXT    NOT NULL,
    filename TEXT    NOT NULL,
    size     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts);
"""


class TransferLog:
    """事件写入与查询。db_path 为 SQLite 文件路径。"""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._dedup = {}  # (ip, path) -> 上次记录时间戳
        self._dedup_lock = threading.Lock()
        try:
            self._init_db()
        except (sqlite3.Error, OSError, TypeError, ValueError):
            self.db_path = None  # 降级：日志功能关闭

    # ── 内部 ──

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _insert(self, ts, event_type, ip, filename, size):
        """底层插入（测试注入历史时间戳用）。"""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO events(ts, type, ip, filename, size) VALUES(?,?,?,?,?)",
                (float(ts), str(event_type), str(ip), str(filename), int(size)))
            conn.commit()
        finally:
            conn.close()

    # ── 写入 ──

    def record(self, event_type, ip, filename, size):
        """写入一条事件（upload/download/preview）；不可用时返回 False。"""
        if not self.db_path:
            return False
        try:
            self._insert(time.time(), event_type, ip, filename, size)
            return True
        except (sqlite3.Error, OSError, ValueError):
            return False

    def record_preview(self, ip, filename, size, real_path, window=300):
        """预览事件按 (ip, 路径) 去重：窗口期（默认 5 分钟）内合并为一条。"""
        key = (str(ip), str(real_path))
        now = time.time()
        with self._dedup_lock:
            if now - self._dedup.get(key, 0) < window:
                return False
            self._dedup[key] = now
        return self.record("preview", ip, filename, size)

    # ── 查询 ──

    def recent(self, limit=50):
        """最近事件，最新在前。"""
        if not self.db_path:
            return []
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT ts, type, ip, filename, size FROM events "
                    "ORDER BY ts DESC, id DESC LIMIT ?",
                    (max(1, min(int(limit), 500)),),
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, ValueError):
            return []
        return [{"ts": r[0], "type": r[1], "ip": r[2],
                 "filename": r[3], "size": r[4]} for r in rows]

    def stats(self, range_hours=24, bucket="hour"):
        """按时间桶聚合（下载含预览）；时间范围内无数据的桶补零，升序返回。"""
        if not self.db_path:
            return []
        now = time.time()
        start = now - max(1, int(range_hours)) * 3600
        if bucket == "day":
            def _bucket(ts):
                d = _dt.datetime.fromtimestamp(ts)
                return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            step = 86400
        else:
            def _bucket(ts):
                return ts - (ts % 3600)
            step = 3600
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT ts, type, size FROM events WHERE ts >= ?", (start,)
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, ValueError):
            return []

        agg = {}
        for ts, etype, size in rows:
            b = _bucket(ts)
            slot = agg.setdefault(b, {"uc": 0, "ub": 0, "dc": 0, "db_": 0})
            if etype == "upload":
                slot["uc"] += 1
                slot["ub"] += size
            else:  # download / preview 合并计为下载
                slot["dc"] += 1
                slot["db_"] += size

        result = []
        b, end = _bucket(start), _bucket(now) + step
        while b < end:
            s = agg.get(b)
            result.append({
                "bucket_start": b,
                "uploads_count": s["uc"] if s else 0,
                "uploads_bytes": s["ub"] if s else 0,
                "downloads_count": s["dc"] if s else 0,
                "downloads_bytes": s["db_"] if s else 0,
            })
            b += step
        return result

    def totals(self):
        """历史总量 + 今日事件数。"""
        zeros = {"uploads_count": 0, "uploads_bytes": 0,
                 "downloads_count": 0, "downloads_bytes": 0, "today_count": 0}
        if not self.db_path:
            return zeros
        today_start = _dt.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        try:
            conn = self._connect()
            try:
                by_type = conn.execute(
                    "SELECT type, COUNT(*), COALESCE(SUM(size),0) "
                    "FROM events GROUP BY type").fetchall()
                today = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE ts >= ?",
                    (today_start,)).fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return zeros
        out = dict(zeros)
        for etype, count, size in by_type:
            if etype == "upload":
                out["uploads_count"] = count
                out["uploads_bytes"] = size
            else:
                out["downloads_count"] += count
                out["downloads_bytes"] += size
        out["today_count"] = today
        return out
