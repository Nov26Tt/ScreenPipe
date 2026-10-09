"""HTTP 接口的集成测试：用 TestClient 验证对外行为。

这些用例主要防止**回归**——尤其是密钥脱敏，一旦被改回去
就是安全事故，所以在这里钉死。
"""

import pytest
from fastapi.testclient import TestClient

import server.app as app_module


@pytest.fixture
def client():
    # 每个用例前重置模块级状态，避免相互污染
    app_module._history.clear()
    app_module.connected_websockets.clear()
    app_module.capture_running = False
    return TestClient(app_module.app)


class TestSecretExposure:
    def test_status_endpoint_masks_api_key(self, client):
        app_module.config.set("llm", "api_key", "sk-verysecret-abcdef123456")

        resp = client.get("/api/status")
        assert resp.status_code == 200

        body = resp.text
        assert "sk-verysecret-abcdef123456" not in body
        assert "sk-v...3456" in body

    def test_config_endpoint_masks_api_key(self, client):
        app_module.config.set("llm", "api_key", "sk-anothersecret-xyz789")

        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert "sk-anothersecret-xyz789" not in resp.text
        # 掩码保留头尾各4 位
        assert resp.json()["llm"]["api_key"] == "sk-a...z789"

    def test_api_base_query_string_is_stripped(self, client):
        app_module.config.set("llm", "api_base", "https://x.com/v1?token=leak")
        resp = client.get("/api/config")
        assert "leak" not in resp.text


class TestCaptureEndpoints:
    def test_start_is_idempotent(self, client):
        first = client.post("/api/start-capture").json()
        assert first["ok"] is True
        # 重复启动不应报错，也不应起第二个循环
        second = client.post("/api/start-capture").json()
        assert second["ok"] is True

        client.post("/api/stop-capture")

    def test_history_endpoints(self, client):
        app_module._history.clear()

        assert client.get("/api/history").json() == []
        assert client.post("/api/clear-history").json()["ok"] is True


class TestStatusShape:
    def test_status_reports_history_limit(self, client):
        """有界历史：状态里应能看到当前容量，方便前端展示。"""
        body = client.get("/api/status").json()
        assert "history_limit" in body
        assert body["history_limit"] >= 1
        assert body["capture_running"] is False

    def test_index_page_is_served(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "ScreenPipe" in resp.text


class TestApiSurface:
    """钉住对外端点清单。

    背景：重写 ``server/app.py`` 时曾漏掉 ``/api/ip``，前端因此一直显示
    「地址获取失败」而没人发现。端点被静默删掉是这类重构最容易犯的错，
    用测试守住清单，新加端点时也会被提醒同步这里。
    """

    EXPECTED_PATHS = {
        "/",
        "/api/status",
        "/api/config",
        "/api/test-llm",
        "/api/test-llm-direct",
        "/api/history",
        "/api/clear-history",
        "/api/capture-once",
        "/api/start-capture",
        "/api/stop-capture",
        "/api/ip",
        "/ws",
    }

    def _registered(self) -> set:
        paths = set()
        for route in app_module.app.routes:
            path = getattr(route, "path", None)
            if path:
                paths.add(path)
        return paths

    def test_no_endpoint_is_missing(self):
        missing = self.EXPECTED_PATHS - self._registered()
        assert not missing, f"缺少端点：{sorted(missing)}"

    def test_ip_endpoint_returns_usable_address(self, client):
        """手机不能用 127.0.0.1 访问电脑，报头必须给出局域网 IP。"""
        resp = client.get("/api/ip")
        assert resp.status_code == 200

        body = resp.json()
        assert body.get("ip"), "未返回 IP"
        assert isinstance(body.get("port"), int)
        # 兜底值之外都应是真实局域网地址
        assert body["ip"].count(".") == 3

    def test_front_end_referenced_endpoints_all_exist(self):
        """页面里 fetch 到的每个 /api 路径都必须在服务端真实存在。"""
        import re
        from pathlib import Path

        page = (
            Path(__file__).parent.parent / "server" / "templates" / "index.html"
        ).read_text(encoding="utf-8")

        called = set(re.findall(r"fetch\(\s*['\"](/[^'\"]*)['\"]", page))
        # 只校验我们自己定义的路径，静态资源等非 API 路径跳过
        api_paths = {p for p in called if p.startswith("/api")}

        missing = api_paths - self._registered()
        assert not missing, (
            f"前端调用了服务端不存在的端点：{sorted(missing)}"
        )


class TestVisionIssueDetection:
    @pytest.mark.parametrize(
        "reply",
        [
            "我没有收到截图，请再试一次",
            "I don't see an image in your message",
            "未检测到图片内容",
        ],
    )
    def test_detects_missing_image_replies(self, reply):
        """模型没看到图时要主动提示，而不是让用户面对无意义回答。"""
        warning = app_module._check_vision_issue(reply)
        assert warning != ""
        assert "视觉" in warning or "图片" in warning

    def test_normal_reply_produces_no_warning(self):
        assert app_module._check_vision_issue("这是一道选择题，答案是 B") == ""
        assert app_module._check_vision_issue("") == ""
