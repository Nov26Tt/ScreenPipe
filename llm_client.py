"""视觉模型客户端：统一 OpenAI 兼容接口，并特殊处理 MiniMax VLM 端点。

设计要点
--------
1. **单一抽象**：任何支持 OpenAI ``/chat/completions`` 协议且接受图片输入
   的服务都能接入（智谱、阿里百炼、火山方舟、Moonshot、Ollama、vLLM…），
   换模型只需改配置，不改代码。
2. **图片内联**：截图以 ``data:image/jpeg;base64,...`` 传入，避免依赖
   外部图床，也保证截图不出本机（隐私）。
3. **错误可诊断**：模型返回空内容、或明显"没看到图"时，抛出带修复建议的
   异常，而不是把原始 JSON 丢给用户。
"""

import logging
import re
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# 模型返回空内容时的统一提示。给出三条最可能的原因，避免用户面对空白卡片
EMPTY_REPLY_MESSAGE = (
    "模型返回了空内容。常见原因：\n"
    "① 当前模型不支持图片输入（纯文本模型无法处理截图）\n"
    "② API Key 无效或额度不足\n"
    "③ 服务端临时异常\n"
    "建议：在「设置」中点击「测试连接」确认模型与 Key。"
)

DEFAULT_SYSTEM_PROMPT = (
    "你是一个屏幕内容理解助手。请识别截图中的内容并输出结构化结果。\n\n"
    "输出规则：\n"
    "- 客观描述看到的信息，不要编造截图里没有的内容\n"
    "- 结构化内容（列表、表格、字段）用 Markdown 表达\n"
    "- 截图里有不确定或看不清的部分，明确指出而不是猜测\n"
    "- 使用与截图内容相同的语言作答\n"
)


class LLMClient:
    """视觉大模型客户端（OpenAI 兼容协议）。"""

    REQUEST_TIMEOUT = 60.0
    PROBE_TIMEOUT = 15.0

    # MiniMax 的 VLM 走独立端点，域名命中即自动切换
    MINIMAX_HOSTS = ("minimax.chat", "minimaxi.com", "minimax.io", "minimax.com")

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        system_prompt: Optional[str] = None,
        json_mode: bool = False,
    ):
        """
        :param api_base: OpenAI 兼容根地址，如 ``https://api.deepseek.com/v1``
                         程序会在其后拼接 ``/chat/completions``
        :param api_key:密钥
        :param model:   模型名，**必须是支持图片输入的视觉模型**
        :param system_prompt: 留空则使用内置的结构化输出提示
        :param json_mode:是否强制模型返回 JSON（对应 API 的 ``response_format``）。
                          开启后结果可被程序直接消费，而不只是给人阅读；
                          但并非所有服务都支持该参数，故做成可关闭。
        """
        self.api_base = (api_base or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.json_mode = bool(json_mode)
        self._is_minimax = any(h in self.api_base.lower() for h in self.MINIMAX_HOSTS)

    # ------------------------------------------------------------------
    # MiniMax 专用端点
    # ------------------------------------------------------------------

    def _resolve_minimax_base(self) -> str:
        """MiniMax VLM 端点需要去掉 /vN 前缀后重新拼接。"""
        return re.sub(r"/v\d+$", "", self.api_base).rstrip("/")

    async def _call_minimax(self, image_base64: str, user_prompt: Optional[str] = None) -> str:
        """调用 MiniMax Token Plan 的 VLM 专用端点（非 OpenAI 协议）。"""
        prompt = user_prompt or "请识别这张截图中的内容并结构化输出。"
        url = f"{self._resolve_minimax_base()}/v1/coding_plan/vlm"
        logger.info(f"MiniMax VLM请求 {url}")

        async with httpx.AsyncClient(timeout=self.REQUEST_TIMEOUT) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "prompt": f"{self.system_prompt}\n\n{prompt}",
                    "image_url": f"data:image/jpeg;base64,{image_base64}",
                },
            )

        if response.status_code != 200:
            detail = response.text[:500]
            logger.error(f"MiniMax VLM 错误 [{response.status_code}]: {detail}")
            raise RuntimeError(f"MiniMax VLM 返回错误 ({response.status_code}): {detail}")

        return self._extract_text(response.json(), "MiniMax VLM")

    # ------------------------------------------------------------------
    # OpenAI 兼容路径
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        image_base64: str,
        user_prompt: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """构造 OpenAI 多模态消息体。

        注意：图片只能放在 user 消息的 content 数组里，放进 system 或
        assistant 会返回 400。
        """
        prompt = user_prompt or "请识别这张截图中的内容并结构化输出。"
        return [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                            # high 走高精度分块，OCR 场景必要；纯文本模型会报错
                            "detail": "high",
                        },
                    },
                ],
            },
        ]

    async def chat_with_image(
        self,
        image_base64: str,
        user_prompt: Optional[str] = None,
    ) -> str:
        """发送截图给模型，返回完整文本结果。"""
        if not image_base64:
            raise ValueError("截图为空，未发送请求")

        logger.info(
            f"请求模型 {self.model}，Base64 长度 {len(image_base64)}"
        )

        if self._is_minimax:
            return await self._call_minimax(image_base64, user_prompt)

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(image_base64, user_prompt),
            "temperature": 0.2,   #截图理解需要稳定而非发散
            "max_tokens": 4096,
        }
        if self.json_mode:
            # 不少服务支持该参数；不支持时会返回 400，由调用方看到明确错误
            payload["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=self.REQUEST_TIMEOUT) as client:
            response = await client.post(
                f"{self.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if response.status_code != 200:
            detail = response.text[:500]
            logger.error(f"模型服务错误 [{response.status_code}]: {detail}")
            raise RuntimeError(f"模型服务返回错误 ({response.status_code}): {detail}")

        return self._extract_text(response.json(), "模型服务")

    # ------------------------------------------------------------------
    # 响应解析与连通性测试
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(payload: Any, source: str) -> str:
        """从响应中取出文本，取不到就抛带诊断信息的异常。

        需要兼容两种 content 形态：
        * 字符串 —— 绝大多数 OpenAI 兼容服务
        * 列表 —— 部分服务的多模态回复形如 ``[{"type": "text", "text": ...}]``
        """
        if isinstance(payload, dict):
            content = LLMClient._extract_content(payload)
            if content is None:
                logger.error(f"{source} 响应结构异常: {str(payload)[:300]}")
                raise RuntimeError(
                    f"{source} 响应格式异常，未取到内容：{str(payload)[:200]}"
                )

            if not content or not content.strip():
                logger.error(f"{source} 返回空内容")
                raise RuntimeError(EMPTY_REPLY_MESSAGE)
            return content

        if isinstance(payload, str) and payload.strip():
            return payload

        logger.error(f"{source} 未返回可用内容: {str(payload)[:300]}")
        raise RuntimeError(EMPTY_REPLY_MESSAGE)

    @staticmethod
    def _extract_content(payload: Dict[str, Any]) -> Optional[str]:
        """取出 ``choices[0].message.content`` 并归一化为字符串。

        :return: 文本内容；结构不符合预期时返回 ``None``
        """
        try:
            raw = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None

        if raw is None:
            return None
        if isinstance(raw, str):
            return raw
        if isinstance(raw, list):
            parts = [
                item.get("text", "")
                for item in raw
                if isinstance(item, dict) and item.get("text")
            ]
            return "".join(parts) or None
        return None

    async def test_connection(self) -> Dict[str, Any]:
        """测试 API 连通性。

        注意：这里只发一条**纯文本**探测消息。视觉模型同样会正常回复，
        所以通过并不代表支持图片输入——真正的确认方式是实际截一次图。
        """
        probe = "请只回复四个字：连接成功"
        try:
            async with httpx.AsyncClient(timeout=self.PROBE_TIMEOUT) as client:
                response = await client.post(
                    f"{self.api_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": probe}],
                        "max_tokens": 20,
                    },
                )

            if response.status_code == 200:
                return {
                    "ok": True,
                    "reply": self._extract_text(response.json(), "模型服务"),
                    "note": "连通性正常。若识别结果异常，请确认该模型支持图片输入。",
                }
            if response.status_code == 401:
                return {"ok": False, "error": "API Key 无效或未授权（401）"}
            if response.status_code == 404:
                return {
                    "ok": False,
                    "error": f"接口不存在（404）：{response.request.url}，请检查 API 地址与模型名",
                }
            if response.status_code == 429:
                return {"ok": False, "error": "触发限流或额度不足（429），请稍后重试"}
            return {"ok": False, "error": f"[{response.status_code}] {response.text[:500]}"}

        except httpx.TimeoutException:
            return {"ok": False, "error": "连接超时，请检查网络或 API 地址"}
        except httpx.RequestError as exc:
            return {"ok": False, "error": f"连接失败：{exc}"}
        except Exception as exc:
            return {"ok": False, "error": f"连接异常：{exc}"}
