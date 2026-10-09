"""历史记录的有界性测试。

早期版本只在*读取*时做切片，内存里的list 一直在增长 ——
这类"看起来限制了其实没限制"的 bug 靠肉眼很难发现，用测试钉住。
"""

import pytest

import server.app as app_module


class TestBoundedHistory:
    def test_deque_has_an_upper_bound(self):
        assert app_module._history.maxlen is not None
        assert app_module._history.maxlen > 0

    def test_appending_beyond_limit_drops_oldest(self, monkeypatch):
        app_module._history.clear()
        monkeypatch.setattr(app_module, "_history_limit", lambda: 5)
        app_module._sync_history_limit()

        for i in range(20):
            app_module._history.append({"content": f"record-{i}"})
            app_module._sync_history_limit()

        assert len(app_module._history) == 5
        # 保留的是最新的 5 条
        assert app_module._history[-1]["content"] == "record-19"
        assert app_module._history[0]["content"] == "record-15"

    def test_shrinking_limit_trims_immediately(self, monkeypatch):
        app_module._history.clear()
        monkeypatch.setattr(app_module, "_history_limit", lambda: 50)
        app_module._sync_history_limit()
        for i in range(40):
            app_module._history.append({"content": str(i)})
        assert len(app_module._history) == 40

        monkeypatch.setattr(app_module, "_history_limit", lambda: 10)
        app_module._sync_history_limit()
        assert len(app_module._history) == 10

    def test_snapshot_is_a_copy(self):
        """snapshot 必须是拷贝，否则调用方改动会污染内部状态。"""
        app_module._history.clear()
        app_module._history.append({"content": "original"})

        snapshot = app_module.history_snapshot()
        snapshot[0]["content"] = "mutated"

        assert app_module._history[0]["content"] == "original"


class TestPortFallback:
    def test_finds_free_port_after_occupied_one(self):
        """端口避让：占用首选端口时应自动向后找到可用端口。"""
        import socket

        # 真实占住一个端口，验证探测逻辑会跳到下一个
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            occupied = taken.getsockname()[1]

            found = app_module.find_available_port(occupied, host="127.0.0.1", attempts=5)
            assert found != occupied
            assert found > occupied

    def test_returns_preferred_port_when_free(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]

        assert app_module.find_available_port(free, host="127.0.0.1") == free

    def test_raises_when_all_ports_taken(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            occupied = taken.getsockname()[1]

            with pytest.raises(RuntimeError, match="均被占用"):
                app_module.find_available_port(occupied, host="127.0.0.1", attempts=1)
