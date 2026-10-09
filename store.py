"""解析记录的持久化。

选sqlite3 而非 JSON 文件的理由：
* **标准库自带**，不引入任何新依赖（对"无构建、开箱即跑"是硬约束）
* 单文件、零服务，删除/查询都是 SQL，天然适合"按时间倒序取 N 条"
* 并发安全（多线程共享连接需开 check_same_thread=False）

内存中仍保留一份 deque 作为热缓存，SQLite 只做落盘与冷启动回填，
这样 WebSocket 推送与渲染的读路径不受磁盘 IO 影响。
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 建表语句。字段与前端契约对应，改动需同步 index.html 的 buildAnswerCard。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT    NOT NULL,           -- ISO 8601，跨天可区分
    time_display   TEXT    NOT NULL,           -- HH:MM:SS，界面展示用
    ok             INTEGER NOT NULL DEFAULT 1,-- 0 表示模型调用失败
    content        TEXT    NOT NULL DEFAULT '',-- 模型输出或错误摘要
    vision_warning TEXT    NOT NULL DEFAULT '',
    screenshot     TEXT    NOT NULL DEFAULT '',-- 截图文件名（与磁盘文件对应）
    width          INTEGER NOT NULL DEFAULT 0,
    height         INTEGER NOT NULL DEFAULT 0,
    size_kb        INTEGER NOT NULL DEFAULT 0,-- 图片体积，用于成本归因
    elapsed        REAL    NOT NULL DEFAULT 0,-- 模型调用耗时（秒）
    tokens         INTEGER NOT NULL DEFAULT 0,-- 可选：服务返回时填充
    error          TEXT    NOT NULL DEFAULT '' -- 失败时的错误类型
);
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_records_created
ON records (created_at DESC);
"""


class RecordStore:
    """SQLite 记录仓库。

    线程模型：FastAPI 的事件循环是单线程，但 ``run_in_background`` 等场景
    可能从其他线程访问，故所有操作都加锁并在连接上关闭线程检查。
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.executescript(_INDEX)
            self._conn.commit()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def insert(self, record: Dict[str, Any]) -> int:
        """写入一条记录，返回自增 id。"""
        created = record.get("created_at") or datetime.now().isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO records
                   (created_at, time_display, ok, content, vision_warning,
                    screenshot, width, height, size_kb, elapsed, tokens, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    created,
                    record.get("time_display") or _fmt_time(created),
                    1 if record.get("ok", True) else 0,
                    record.get("content") or "",
                    record.get("vision_warning") or "",
                    record.get("screenshot") or "",
                    int(record.get("width") or 0),
                    int(record.get("height") or 0),
                    int(record.get("size_kb") or 0),
                    float(record.get("elapsed") or 0.0),
                    int(record.get("tokens") or 0),
                    record.get("error") or "",
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        """按时间倒序取最近 N 条（返回时转为时间正序，便于前端直接 append）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM records ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [self._to_dict(r) for r in reversed(rows)]

    def stats(self) -> Dict[str, Any]:
        """统计概览，用于成本归因与健康检查。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*)          AS total,
                          SUM(ok)            AS ok_count,
                          AVG(elapsed)       AS avg_elapsed,
                          SUM(size_kb)       AS total_kb
                   FROM records"""
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "ok": int(row["ok_count"] or 0),
            "failed": int(row["total"] or 0) - int(row["ok_count"] or 0),
            "avg_elapsed": round(float(row["avg_elapsed"] or 0.0), 2),
            "total_kb": int(row["total_kb"] or 0),
        }

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    def delete_all(self) -> int:
        """清空全部记录，返回删除条数。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM records")
            self._conn.commit()
            return int(cur.rowcount or 0)

    def trim(self, keep: int) -> int:
        """只保留最近 keep 条，删除更早的。返回删除条数。

        防止长期运行时数据库无限膨胀 —— 与截图的保留策略是同一个思路。
        """
        with self._lock:
            cur = self._conn.execute(
                """DELETE FROM records
                   WHERE id NOT IN (SELECT id FROM records ORDER BY id DESC LIMIT ?)""",
                (int(keep),),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        """数据库行 → 前端契约的 camelCase 字典。"""
        return {
            "created_at": row["created_at"],
            "time": row["time_display"],
            "ok": bool(row["ok"]),
            "content": row["content"],
            "vision_warning": row["vision_warning"],
            "screenshot": row["screenshot"],
            "width": row["width"],
            "height": row["height"],
            "size_kb": row["size_kb"],
            "elapsed": row["elapsed"],
            "tokens": row["tokens"],
            "error": row["error"],
        }


def _fmt_time(iso: str) -> str:
    """从 ISO 时间串取 HH:MM:SS，解析失败时退回当前时间。"""
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return datetime.now().strftime("%H:%M:%S")
