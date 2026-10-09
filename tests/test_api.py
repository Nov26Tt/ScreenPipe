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
