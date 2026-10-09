"""配置与密钥脱敏的单元测试。

这些用例覆盖了本项目最敏感的一处逻辑：API Key 不能通过任何
对外接口泄露，但用户又必须能在网页上看到"是否已配置"。
"""

import io

import pytest
import yaml

from config import DEFAULTS, Config, mask_secret


class TestMaskSecret:
    def test_empty_and_placeholder_pass_through(self):
        # 这两种值不是真密钥，原样返回让前端能判断"尚未配置"
        assert mask_secret("") == ""
        assert mask_secret(None) == ""
        assert mask_secret("your-api-key-here") == "your-api-key-here"

    def test_typical_key_keeps_head_and_tail(self):
        masked = mask_secret("sk-abcdefghijklmnopqrstuvwxyz")
        assert masked == "sk-a...wxyz"
        # 中间部分绝不能出现
        assert "efghij" not in masked

    def test_short_secret_is_fully_masked(self):
        # 短密钥若还保留 4 位头尾，等于泄露了一大半
        assert mask_secret("abcd") == "****"
        assert mask_secret("abcdefg") == "*******"

    def test_length_boundary(self):
        # 恰好 2*keep+1 时可安全保留头尾
        assert mask_secret("123456789") == "1234...6789"


class TestConfigPublicDict:
    def test_public_dict_never_leaks_plaintext_key(self):
        cfg = Config(config_path="__nonexistent__.yaml")
        cfg.set("llm", "api_key", "sk-supersecretvalue1234")

        public = cfg.to_public_dict()
        blob = str(public)
        assert "sk-supersecretvalue1234" not in blob
        assert "..." in public["llm"]["api_key"]

        # 内部视图仍然保留明文，供实际请求使用
        assert cfg.to_dict()["llm"]["api_key"] == "sk-supersecretvalue1234"

    def test_public_dict_strips_query_string_from_api_base(self):
        # 部分网关把 key 放在 base 的查询参数里，回显时必须清掉
        cfg = Config(config_path="__nonexistent__.yaml")
        cfg.set("llm", "api_base", "https://example.com/v1?key=leak-me")
        assert "leak-me" not in str(cfg.to_public_dict())

    def test_public_dict_does_not_mutate_internal_state(self):
        cfg = Config(config_path="__nonexistent__.yaml")
        cfg.set("llm", "api_key", "sk-abcdefghijklmnop")
        cfg.to_public_dict()
        assert cfg.to_dict()["llm"]["api_key"] == "sk-abcdefghijklmnop"


class TestConfigUpdate:
    def test_masked_key_echo_is_ignored(self):
        """前端 GET 拿到的是掩码，保存时不能把掩码写回覆盖真密钥。"""
        cfg = Config(config_path="__nonexistent__.yaml")
        cfg.set("llm", "api_key", "sk-realkey-1234567890")

        cfg.update_from_dict({"llm": {"api_key": "sk-r...7890", "model": "glm-4.6v-flash"}})

        assert cfg.to_dict()["llm"]["api_key"] == "sk-realkey-1234567890"
        # 同批次里的其他字段应正常更新
        assert cfg.to_dict()["llm"]["model"] == "glm-4.6v-flash"

    def test_real_key_is_written(self):
        cfg = Config(config_path="__nonexistent__.yaml")
        cfg.update_from_dict({"llm": {"api_key": "sk-brandnewkey-0000"}})
        assert cfg.to_dict()["llm"]["api_key"] == "sk-brandnewkey-0000"


class TestConfigLoad:
    def test_partial_yaml_is_backfilled_from_defaults(self, tmp_path):
        """用户手写了不完整的 yaml 时，缺失键应回退到默认值而非None。"""
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.dump({"llm": {"api_key": "sk-xyz"}, "capture": {"interval": 3}}),
            encoding="utf-8",
        )

        cfg = Config(config_path=str(path))

        assert cfg.get("capture", "interval") == 3   # 用户写的值生效
        assert cfg.get("capture", "quality") == DEFAULTS["capture"]["quality"]  # 缺失的走默认
        assert cfg.get("server", "port") == DEFAULTS["server"]["port"]

    def test_rejects_non_mapping_yaml(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError):
            Config(config_path=str(path))

    def test_save_and_reload_roundtrip(self, tmp_path):
        path = tmp_path / "config.yaml"
        cfg = Config(config_path=str(path))
        cfg.set("llm", "api_key", "sk-roundtrip-1234")
        cfg.set("llm", "model", "qwen3-vl-plus")
        cfg.save()

        reloaded = Config(config_path=str(path))
        assert reloaded.to_dict()["llm"]["model"] == "qwen3-vl-plus"
        assert reloaded.to_dict()["llm"]["api_key"] == "sk-roundtrip-1234"

    def test_defaults_use_a_free_vision_model(self):
        """默认模型必须是确定支持图片输入的，否则新用户开箱即错。"""
        assert DEFAULTS["llm"]["model"] == "glm-4.6v-flash"

    def test_defaults_avoid_the_frozen_placeholder_key(self):
        assert "deepseek" not in DEFAULTS["llm"]["api_base"]
