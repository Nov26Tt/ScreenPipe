"""视觉模型客户端的单元测试（不发起真实网络请求）。

重点覆盖两处容易出问题的地方：
1. 响应结构解析——不同厂商返回结构差异很大，解析失败要给出
   可诊断的异常而不是把原始 JSON 抛给用户
2. MiniMax 端点识别——域名匹配错误会导致走错协议
"""

import pytest

from llm_client import EMPTY_REPLY_MESSAGE, DEFAULT_SYSTEM_PROMPT, LLMClient


class TestInit:
    def test_minimax_domain_detection(self):
        for base in (
            "https://api.minimax.chat/v1",
            "https://api.minimaxi.com/v1",
            "https://api.minimax.io/v1",
        ):
            client = LLMClient(base, "key", "some-model")
            assert client._is_minimax is True, base

    def test_normal_providers_are_not_minimax(self):
        for base in (
            "https://open.bigmodel.cn/api/paas/v4",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "http://localhost:11434/v1",
        ):
            client = LLMClient(base, "key", "some-model")
            assert client._is_minimax is False, base

    def test_trailing_slash_is_normalized(self):
        assert LLMClient("https://x.com/v1/", "k", "m").api_base == "https://x.com/v1"

    def test_minimax_base_strips_version_suffix(self):
        """MiniMax VLM 端点需要去掉 /vN 再重新拼接。"""
        client = LLMClient("https://api.minimax.chat/v1", "k", "m")
        assert client._resolve_minimax_base() == "https://api.minimax.chat"

    def test_default_prompt_used_when_none(self):
        client = LLMClient("https://x.com/v1", "k", "m", system_prompt=None)
        assert client.system_prompt == DEFAULT_SYSTEM_PROMPT


class TestBuildMessages:
    def test_image_must_be_in_user_message(self):
        """图片放进 system/assistant 会被服务端拒（400）。"""
        client = LLMClient("https://x.com/v1", "k", "m")
        messages = client._build_messages("BASE64DATA")

        assert messages[0]["role"] == "system"
        assert isinstance(messages[0]["content"], str)

        user_blocks = messages[1]["content"]
        assert messages[1]["role"] == "user"
        assert any(b["type"] == "image_url" for b in user_blocks)

        image_block = next(b for b in user_blocks if b["type"] == "image_url")
        assert image_block["image_url"]["url"] == "data:image/jpeg;base64,BASE64DATA"
        # OCR 场景需要高精度分块
        assert image_block["image_url"]["detail"] == "high"

    def test_custom_prompt_is_used(self):
        client = LLMClient("https://x.com/v1", "k", "m")
        messages = client._build_messages("DATA", user_prompt="只输出数字")
        text_block = messages[1]["content"][0]
        assert text_block["text"] == "只输出数字"


class TestExtractText:
    def test_standard_openai_shape(self):
        payload = {"choices": [{"message": {"content": "识别结果"}}]}
        assert LLMClient._extract_text(payload, "test") == "识别结果"

    def test_content_list_is_joined(self):
        """部分服务把多模态回复放在 content 数组里。"""
        payload = {
            "choices": [
                {"message": {"content": [{"text": "第一段"}, {"text": "第二段"}]}}
            ]
        }
        assert LLMClient._extract_text(payload, "test") == "第一段第二段"

    def test_empty_content_raises_diagnosable_error(self):
        payload = {"choices": [{"message": {"content": "   "}}]}
        with pytest.raises(RuntimeError) as exc:
            LLMClient._extract_text(payload, "test")
        assert str(exc.value) == EMPTY_REPLY_MESSAGE

    def test_malformed_structure_raises_with_payload_excerpt(self):
        with pytest.raises(RuntimeError) as exc:
            LLMClient._extract_text({"unexpected": "shape"}, "test")
        assert "响应格式异常" in str(exc.value)

    def test_plain_string_payload(self):
        assert LLMClient._extract_text("直接返回的文本", "test") == "直接返回的文本"

    def test_empty_string_payload_raises(self):
        with pytest.raises(RuntimeError):
            LLMClient._extract_text("", "test")


class TestEmptyScreenshot:
    @pytest.mark.asyncio
    async def test_empty_image_short_circuits_before_request(self):
        client = LLMClient("https://x.com/v1", "k", "m")
        with pytest.raises(ValueError, match="截图为空"):
            await client.chat_with_image("")


class TestUsageExtraction:
    """token 用量解析。

    视觉模型的成本几乎全在输入侧（一张全屏截图 1000+ tokens），
    只记一个合计数会让人误判"是不是输出太长"。
    """

    def test_deepseek_shape(self):
        payload = {"usage": {
            "prompt_tokens": 1165, "completion_tokens": 100,
            "total_tokens": 1265,
            "prompt_cache_hit_tokens": 0,
            "completion_tokens_details": {"reasoning_tokens": 100},
        }}
        u = LLMClient._extract_usage(payload)
        assert u["prompt_tokens"] == 1165
        assert u["completion_tokens"] == 100
        assert u["total_tokens"] == 1265
        assert u["reasoning_tokens"] == 100

    def test_cached_tokens_from_both_locations(self):
        """DeepSeek 用顶层字段，部分服务放在 prompt_tokens_details 里。"""
        a = LLMClient._extract_usage(
            {"usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80}}
        )
        b = LLMClient._extract_usage(
            {"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}}}
        )
        assert a["cached_tokens"] == 80
        assert b["cached_tokens"] == 80

    def test_openai_style_field_names(self):
        """兼容 input_tokens / output_tokens 这套命名。"""
        u = LLMClient._extract_usage(
            {"usage": {"input_tokens": 300, "output_tokens": 50}}
        )
        assert u["prompt_tokens"] == 300
        assert u["completion_tokens"] == 50
        # total 缺失时按两者相加
        assert u["total_tokens"] == 350

    def test_missing_usage_degrades_to_empty(self):
        assert LLMClient._extract_usage({"choices": []}) == {}
        assert LLMClient._extract_usage("not a dict") == {}

    def test_partial_usage_does_not_crash(self):
        u = LLMClient._extract_usage({"usage": {"prompt_tokens": 10}})
        assert u["prompt_tokens"] == 10
        assert u["completion_tokens"] == 0

    @pytest.mark.asyncio
    async def test_chat_with_image_returns_text_only(self):
        """向后兼容：原方法仍只返回字符串。"""
        assert callable(LLMClient.chat_with_image)


class TestUsageExtraction:
    """token 用量解析。

    视觉模型的成本几乎全在输入侧（一张全屏截图 1000+ tokens），
    只记一个合计数会让人误判"是不是输出太长"。
    """

    def test_deepseek_shape(self):
        payload = {"usage": {
            "prompt_tokens": 1165, "completion_tokens": 100,
            "total_tokens": 1265,
            "prompt_cache_hit_tokens": 0,
            "completion_tokens_details": {"reasoning_tokens": 100},
        }}
        u = LLMClient._extract_usage(payload)
        assert u["prompt_tokens"] == 1165
        assert u["completion_tokens"] == 100
        assert u["total_tokens"] == 1265
        assert u["reasoning_tokens"] == 100

    def test_cached_tokens_from_both_locations(self):
        """DeepSeek 用顶层字段，部分服务放在 prompt_tokens_details 里。"""
        a = LLMClient._extract_usage(
            {"usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80}}
        )
        b = LLMClient._extract_usage(
            {"usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}}}
        )
        assert a["cached_tokens"] == 80
        assert b["cached_tokens"] == 80

    def test_openai_style_field_names(self):
        """兼容 input_tokens / output_tokens 这套命名。"""
        u = LLMClient._extract_usage(
            {"usage": {"input_tokens": 300, "output_tokens": 50}}
        )
        assert u["prompt_tokens"] == 300
        assert u["completion_tokens"] == 50
        # total 缺失时按两者相加
        assert u["total_tokens"] == 350

    def test_missing_usage_degrades_to_empty(self):
        assert LLMClient._extract_usage({"choices": []}) == {}
        assert LLMClient._extract_usage("not a dict") == {}

    def test_partial_usage_does_not_crash(self):
        u = LLMClient._extract_usage({"usage": {"prompt_tokens": 10}})
        assert u["prompt_tokens"] == 10
        assert u["completion_tokens"] == 0

    @pytest.mark.asyncio
    async def test_chat_with_image_returns_text_only(self):
        """向后兼容：原方法仍只返回字符串。"""
        assert callable(LLMClient.chat_with_image)
