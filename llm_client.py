import json
import logging
import re
from typing import AsyncGenerator, Optional

import httpx

logger = logging.getLogger(__name__)

# 模型返回空内容时统一使用这个提示，避免前端出现「有卡片、无内容」的空白
EMPTY_REPLY_MESSAGE = (
    "大模型返回了空内容。常见原因："
    "① 当前模型不支持图片输入；"
    "② API Key 无效或额度不足；"
    "③ 服务端临时异常。请在「设置」中检查模型与 Key 后重试。"
)


class LLMClient:
    """
    兼容 OpenAI API 格式的大模型客户端。
    支持 OpenAI、DeepSeek、通义千问、Ollama 等所有 OpenAI 兼容 API。
    同时支持 MiniMax Token Plan 的专用 VLM 端点。
    """

    MINIMAX_VLM_ENDPOINTS = [
        "minimax.chat", "minimaxi.com", "minimax.io", "minimax.com"
    ]

    def __init__(self, api_base: str, api_key: str, model: str,
                 system_prompt: Optional[str] = None):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt or (
            "你是一个学习辅助AI。请识别截图中的题目，按以下格式输出：\n\n"
            "选择题格式：题号. 题目一句话概括 - 正确答案选项内容\n"
            "  例：3. 企业文化的核心特征 - 独特性、稳定性、整合性\n"
            "判断题格式：题号. 题目一句话概括 - 对/错\n"
            "  例：5. SWOT中O代表机会 - 对\n"
            "填空题格式：题号. 题目关键词 - 填写内容\n"
            "  例：2. 规模经济定义 - 产量增加导致单位成本下降\n"
            "问答题格式：题号. 问题要点 - 一句话答案\n"
            "  例：1. 目标管理法核心 - 通过设定明确目标评估绩效\n\n"
            "铁则：\n"
            "- 只显示正确选项的内容，不要列出全部选项\n"
            "- 不要解释、不要解析、不要分析\n"
            "- 每题占一行"
        )
        self._is_minimax = any(h in self.api_base.lower() for h in self.MINIMAX_VLM_ENDPOINTS)

    def _resolve_vlm_base(self) -> str:
        """MiniMax Token Plan 的 VLM 根地址。
        用户通常填 https://api.minimax.chat/v1，VLM 端点需要去掉 /v1 前缀重新拼接。
        """
        base = re.sub(r'/v\d+$', '', self.api_base)
        return base.rstrip("/")

    async def _call_minimax_vlm(self, image_base64: str,
                                user_prompt: Optional[str] = None) -> str:
        """通过 MiniMax Token Plan 专用 VLM 端点识别图片"""
        prompt = user_prompt or "请识别这张截图中的题目，并给出正确答案或最佳选择。"
        if self.system_prompt:
            prompt = f"{self.system_prompt}\n\n{prompt}"

        vlm_url = f"{self._resolve_vlm_base()}/v1/coding_plan/vlm"
        img_url = f"data:image/jpeg;base64,{image_base64}"

        logger.info(f"MiniMax VLM 请求: {vlm_url}, 图片大小={len(image_base64)}")

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                vlm_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "prompt": prompt,
                    "image_url": img_url
                }
            )

            if response.status_code != 200:
                error_text = response.text[:500]
                logger.error(f"MiniMax VLM error [{response.status_code}]: {error_text}")
                raise Exception(f"MiniMax VLM 返回错误 ({response.status_code}): {error_text}")

            data = response.json()
            logger.info(f"MiniMax VLM 响应 keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")

            for field in ("text", "content", "response", "result", "answer"):
                if isinstance(data, dict) and data.get(field):
                    return data[field]

            if isinstance(data, str) and data.strip():
                return data

            # 取不到任何可用字段时明确报错，而不是把原始 JSON 当成答案显示
            logger.error(f"MiniMax VLM 未返回可用内容: {str(data)[:300]}")
            raise Exception(EMPTY_REPLY_MESSAGE)

    async def chat_with_image(self, image_base64: str,
                              user_prompt: Optional[str] = None) -> str:
        """发送图片给 LLM，返回完整回答"""
        if not image_base64:
            raise ValueError("截图为空，无法发送给 LLM")

        img_len = len(image_base64)
        logger.info(f"发送截图到 LLM, 模型={self.model}, 图片Base64长度={img_len}")

        if self._is_minimax:
            logger.info("检测到 MiniMax API，使用 Token Plan VLM 端点")
            return await self._call_minimax_vlm(image_base64, user_prompt)

        messages = self._build_messages(image_base64, user_prompt)

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": 4096
                }
            )

            if response.status_code != 200:
                error_text = response.text[:500]
                logger.error(f"LLM API error [{response.status_code}]: {error_text}")
                raise Exception(f"LLM API 返回错误 ({response.status_code}): {error_text}")

            data = response.json()
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                logger.error(f"LLM 响应格式异常: {str(data)[:300]}")
                raise Exception(
                    f"大模型响应格式异常，未取到内容：{str(data)[:200]}"
                ) from exc

            if not content or not str(content).strip():
                logger.error("LLM 返回了空内容")
                raise Exception(EMPTY_REPLY_MESSAGE)

            return content

    async def chat_with_image_stream(self, image_base64: str,
                                     user_prompt: Optional[str] = None) -> AsyncGenerator[str, None]:
        """发送图片给 LLM，流式返回回答"""
        messages = self._build_messages(image_base64, user_prompt)

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{self.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": 4096,
                    "stream": True
                }
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    raise Exception(f"LLM API 返回错误 ({response.status_code}): {error_body[:500]}")

                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue

    def _build_messages(self, image_base64: str,
                        user_prompt: Optional[str] = None) -> list:
        prompt = user_prompt or "请识别这张截图中的题目，并给出正确答案或最佳选择。如果有多道题目，请逐一作答。"

        return [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                            "detail": "high"
                        }
                    }
                ]
            }
        ]

    async def test_connection(self) -> dict:
        """测试 API 连接是否正常"""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                url = f"{self.api_base}/chat/completions"
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "您好，请回复：连接成功"}],
                        "max_tokens": 20
                    }
                )
                if response.status_code == 200:
                    data = response.json()
                    return {"ok": True, "reply": data["choices"][0]["message"]["content"]}
                elif response.status_code == 401:
                    return {"ok": False, "error": "API Key 无效或未授权，请检查 Key 是否正确"}
                elif response.status_code == 404:
                    return {"ok": False, "error": f"API 地址不存在 (404): {url}，请检查 API 地址是否正确"}
                else:
                    return {"ok": False, "error": f"[{response.status_code}] {response.text[:500]}"}
        except Exception as e:
            return {"ok": False, "error": f"连接异常: {str(e)}"}
