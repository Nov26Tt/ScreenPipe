"""SQLite 记录仓库的单元测试。

重点覆盖：
* 失败记录也要能落库（Key 过期这类问题只弹一次 toast 就消失，
  留痕后才能在历史里回看）
* 时间戳含日期，跨天记录可区分
* 截图文件名与记录绑定（P0 的核心）
* trim 按 id 保留最近 N 条
"""

import pytest

from store import RecordStore


@pytest.fixture
def store(tmp_path):
    s = RecordStore(str(tmp_path / "records.db"))
    yield s
    s.close()


def make_record(**overrides):
    base = {
        "created_at": "2026-10-09T15:04:12",
        "time_display": "15:04:12",
        "ok": True,
        "content": "识别结果",
        "vision_warning": "",
        "screenshot": "screenshot_20261009_150412_123.jpg",
        "width": 1920,
        "height": 1080,
        "size_kb": 118,
        "elapsed": 5.66,
        "tokens": 0,
        "error": "",
    }
    base.update(overrides)
    return base


class TestInsertAndRead:
    def test_roundtrip_preserves_all_fields(self, store):
        store.insert(make_record())
        rows = store.recent()

        assert len(rows) == 1
        r = rows[0]
        assert r["content"] == "识别结果"
        assert r["screenshot"] == "screenshot_20261009_150412_123.jpg"
        assert r["size_kb"] == 118
        assert r["elapsed"] == pytest.approx(5.66)
        assert r["ok"] is True

    def test_failed_record_is_persisted(self, store):
        """失败也要留痕 —— 否则 Key 过期的历史无从查起。"""
        store.insert(
            make_record(ok=False, content="401 Unauthorized", error="RuntimeError")
        )
        rows = store.recent()

        assert len(rows) == 1
        assert rows[0]["ok"] is False
        assert "401" in rows[0]["content"]
        assert rows[0]["error"] == "RuntimeError"

    def test_created_at_keeps_date(self, store):
        """只有时分秒无法区分跨天记录。"""
        store.insert(make_record(created_at="2026-10-09T08:00:00"))
        store.insert(make_record(created_at="2026-10-10T08:00:00"))
        rows = store.recent()

        dates = sorted(r["created_at"][:10] for r in rows)
        assert dates == ["2026-10-09", "2026-10-10"]

    def test_time_display_derived_when_missing(self, store):
        rec = make_record()
        rec.pop("time_display")
        store.insert(rec)
        assert store.recent()[0]["time"] == "15:04:12"

    def test_recent_returns_oldest_first(self, store):
        """返回时间正序，前端可直接 append 而无需 reverse。"""
        for i in range(5):
            store.insert(make_record(content=f"record-{i}"))

        rows = store.recent()
        assert [r["content"] for r in rows] == [f"record-{i}" for i in range(5)]

    def test_recent_respects_limit(self, store):
        for i in range(10):
            store.insert(make_record(content=f"r{i}"))
        assert len(store.recent(limit=3)) == 3


class TestStats:
    def test_stats_separates_success_from_failure(self, store):
        store.insert(make_record(ok=True, elapsed=2.0, size_kb=100))
        store.insert(make_record(ok=True, elapsed=4.0, size_kb=200))
        store.insert(make_record(ok=False, elapsed=1.0, size_kb=0))

        s = store.stats()
        assert s["total"] == 3
        assert s["ok"] == 2
        assert s["failed"] == 1
        assert s["total_kb"] == 300
        assert s["avg_elapsed"] == pytest.approx(2.33, abs=0.01)

    def test_stats_on_empty_store(self, store):
        s = store.stats()
        assert s["total"] == 0
        assert s["avg_elapsed"] == 0.0


class TestDeletion:
    def test_delete_all_clears_records(self, store):
        for i in range(5):
            store.insert(make_record(content=f"r{i}"))
        assert store.delete_all() == 5
        assert store.recent() == []

    def test_trim_keeps_newest(self, store):
        for i in range(10):
            store.insert(make_record(content=f"r{i}"))

        removed = store.trim(keep=3)
        rows = store.recent()

        assert removed == 7
        assert len(rows) == 3
        # 保留的是最近 3 条
        assert [r["content"] for r in rows] == ["r7", "r8", "r9"]


class TestPersistence:
    def test_records_survive_reopen(self, tmp_path):
        """冷启动回填的前提：数据真的落到了文件里。"""
        path = str(tmp_path / "records.db")

        first = RecordStore(path)
        first.insert(make_record(content="重启前写的"))
        first.close()

        second = RecordStore(path)
        try:
            rows = second.recent()
            assert len(rows) == 1
            assert rows[0]["content"] == "重启前写的"
        finally:
            second.close()

    def test_schema_is_idempotent(self, tmp_path):
        """重复打开不应报错（CREATE TABLE IF NOT EXISTS）。"""
        path = str(tmp_path / "records.db")
        RecordStore(path).close()
        RecordStore(path).close()  # 不抛异常即通过


class TestTokenTracking:
    """token 统计的持久化与聚合。

    背景：此前 record 里的 tokens 字段恒为 0——字段建了但从没填，
    所谓"成本可归因"实际只给了耗时和图片体积。
    """

    def test_tokens_roundtrip(self, store):
        store.insert(make_record(
            prompt_tokens=1165, completion_tokens=100, cached_tokens=0
        ))
        r = store.recent()[0]
        assert r["prompt_tokens"] == 1165
        assert r["completion_tokens"] == 100
        # total 是派生字段，不单独存
        assert r["total_tokens"] == 1265

    def test_cached_tokens_persisted(self, store):
        store.insert(make_record(prompt_tokens=2000, cached_tokens=1800))
        assert store.recent()[0]["cached_tokens"] == 1800

    def test_stats_aggregates_tokens(self, store):
        store.insert(make_record(prompt_tokens=1000, completion_tokens=50))
        store.insert(make_record(prompt_tokens=1500, completion_tokens=80))

        s = store.stats()
        assert s["prompt_tokens"] == 2500
        assert s["completion_tokens"] == 130
        assert s["total_tokens"] == 2630
        assert s["avg_prompt_tokens"] == 1250

    def test_stats_on_empty_store_has_zero_tokens(self, store):
        s = store.stats()
        assert s["total_tokens"] == 0
        assert s["prompt_tokens"] == 0



    def test_avg_prompt_tokens_ignores_legacy_zero_rows(self, store):
        """旧记录 token=0（统计上线前产生），不应拉低平均值。"""
        store.insert(make_record(prompt_tokens=0, completion_tokens=0))   # 旧记录
        store.insert(make_record(prompt_tokens=0, completion_tokens=0))   # 旧记录
        store.insert(make_record(prompt_tokens=1000, completion_tokens=100))

        s = store.stats()
        assert s["total"] == 3
        assert s["prompt_tokens"] == 1000
        # 平均值只基于有 token 的那 1 条，而不是 1000/3
        assert s["avg_prompt_tokens"] == 1000
        assert s["token_samples"] == 1


class TestSchemaMigration:
    """旧库升级：CREATE TABLE IF NOT EXISTS 不会给已有表加列。"""

    def _columns(self, store):
        return {r["name"] for r in store._conn.execute("PRAGMA table_info(records)")}

    def test_migration_adds_missing_columns(self, tmp_path):
        import sqlite3

        path = tmp_path / "old.db"
        # 造一个只有旧 schema 的库
        old = sqlite3.connect(str(path))
        old.execute(
            """CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL, time_display TEXT NOT NULL,
                ok INTEGER NOT NULL DEFAULT 1, content TEXT NOT NULL DEFAULT '',
                vision_warning TEXT NOT NULL DEFAULT '',
                screenshot TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
                size_kb INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0,
                tokens INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
            )"""
        )
        old.execute(
            """INSERT INTO records
               (created_at, time_display, ok, content, tokens, error)
               VALUES ('2026-10-09T10:00:00','10:00:00',1,'旧记录',999,'')"""
        )
        old.commit()
        old.close()

        # 打开时应自动补列，且旧数据不丢
        s = RecordStore(str(path))
        try:
            assert {"prompt_tokens", "completion_tokens", "cached_tokens"} <= self._columns(s)
            rows = s.recent()
            assert len(rows) == 1
            assert rows[0]["content"] == "旧记录"
            # 旧的 tokens 值迁到 prompt_tokens，而不是丢弃
            assert rows[0]["prompt_tokens"] == 999
        finally:
            s.close()

    def test_migration_is_idempotent(self, tmp_path):
        path = str(tmp_path / "r.db")
        RecordStore(path).close()
        s = RecordStore(path)   # 二次打开不应报错
        s.close()


