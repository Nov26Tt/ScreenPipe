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
