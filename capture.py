"""屏幕捕获与内容变更检测。

变更检测是整个框架里最省Token 的设计：屏幕静止不动时反复请求视觉模型
既慢又烧钱。本模块用像素指纹判断"内容是否真的变了"，让调用方能在
无变化时直接跳过模型调用。

为什么用 hashlib.blake2b 而不是内置 hash()：
* ``hash()`` 对 bytes 会被 PYTHONHASHSEED 随机化，进程重启后同一画面
  得到的值不同 —— 意味着重启服务后第一帧永远被判定为"已变化"；
* 内置 hash 的桶数很小，理论上存在碰撞。
blake2b 是稳定的密码学哈希，跨进程可复现，且对小内存依然快。
"""

import base64
import hashlib
import io
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import mss
from PIL import Image

# mss 10.x 起``mss.mss()`` 标记为弃用，改用 ``mss.MSS``；
# 低版本回退到旧接口，保证 requirements 里的下限仍然可用。
_MSS_FACTORY = getattr(mss, "MSS", None) or mss.mss

# 指纹计算的输入上限：超长文本会拖慢 blake2b，而画面差异通常出现在
# 前部像素上。只取前 2MB 像素数据，兼顾速度与准确性。
_HASH_SAMPLE_BYTES = 2 * 1024 * 1024


class Capture:
    """屏幕捕获器。

    线程安全说明：``mss.mss()`` 实例与 Pillow 对象未加锁。本项目由
    FastAPI 的单个事件循环串行调用（REST 与 WebSocket 复用同一协程），
    不会并发进入；如需多线程调用请自行加锁。
    """

    def __init__(
        self,
        save_dir: Optional[str] = None,
        quality: int = 60,
        region: Optional[List[int]] = None,
    ):
        """
        :param save_dir: 截图落盘目录；``None`` 表示不落盘
        :param quality: JPEG 质量（1-95），越低体积越小但文字越糊
        :param region: 截屏区域 ``[left, top, width, height]``；``None`` 表示主显示器全屏
        """
        self.save_dir = save_dir
        self.quality = max(1, min(95, int(quality)))
        self.region = region
        self._sct = _MSS_FACTORY()
        self._last_hash: Optional[str] = None

    def _resolve_monitor(self) -> dict:
        """确定要抓取的显示器区域。"""
        if self.region and len(self.region) == 4:
            left, top, width, height = self.region
            return {"left": left, "top": top, "width": width, "height": height}
        # monitors[0] 是整块虚拟桌面（含多显示器），[1] 才是主显示器
        return self._sct.monitors[1]

    def _fingerprint(self, pil_img: "Image.Image") -> str:
        """计算画面指纹（稳定的跨进程哈希）。"""
        raw = pil_img.tobytes()[:_HASH_SAMPLE_BYTES]
        return hashlib.blake2b(raw, digest_size=16).hexdigest()

    def screenshot(self, save_to_disk: bool = False) -> bytes:
        """截取屏幕并返回 JPEG 字节。

        若画面与上次完全一致且 ``save_to_disk`` 为 False，返回**空字节**，
        调用方据此跳过模型调用。

        :param save_to_disk: True 时强制截图并跳过变更检测（手动触发场景）
        """
        sct_img = self._sct.grab(self._resolve_monitor())
        pil_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        if not save_to_disk:
            current = self._fingerprint(pil_img)
            if current == self._last_hash:
                return b""
            self._last_hash = current

        buffer = io.BytesIO()
        pil_img.save(buffer, format="JPEG", quality=self.quality)
        jpg_bytes = buffer.getvalue()

        if save_to_disk and self.save_dir:
            self._save_to_disk(jpg_bytes)

        return jpg_bytes

    def screenshot_base64(self, save_to_disk: bool = False) -> str:
        """返回 Base64 编码的截图；画面无变化且非强制时返回空串。"""
        jpg_bytes = self.screenshot(save_to_disk)
        if not jpg_bytes:
            return ""
        return base64.b64encode(jpg_bytes).decode("utf-8")

    def _save_to_disk(self, jpg_bytes: bytes) -> None:
        if not self.save_dir:
            return
        directory = Path(self.save_dir)
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (directory / f"screenshot_{timestamp}.jpg").write_bytes(jpg_bytes)

    def reset_hash(self) -> None:
        """重置指纹缓存，强制下次截图一定被判定为"已变化"。

        手动触发识别时必须调用，否则用户看到的题目没变、点"识别一次"
        只会拿到一句"屏幕内容未变化"。
        """
        self._last_hash = None

    def close(self) -> None:
        """释放 mss 持有的底层句柄。"""
        sct = getattr(self, "_sct", None)
        if sct is not None:
            sct.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
