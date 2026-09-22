import base64
import io
import os
import time
from datetime import datetime
from typing import Optional

import mss
from PIL import Image


class Capture:
    def __init__(self, save_dir: Optional[str] = None, quality: int = 60,
                 region: Optional[list] = None):
        self.save_dir = save_dir
        self.quality = quality
        self.region = region
        self._sct = mss.mss()
        self._last_hash = None

    def screenshot(self, save_to_disk: bool = False) -> bytes:
        """
        截取屏幕并返回 JPEG 字节数据。
        如果内容与上次相同（题目未变化），返回空字节，避免重复调用 LLM。
        """
        monitor = self._sct.monitors[1]  # 主显示器全屏

        if self.region and len(self.region) == 4:
            left, top, width, height = self.region
            monitor = {"left": left, "top": top, "width": width, "height": height}

        sct_img = self._sct.grab(monitor)

        pil_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        # 检查是否与上次截图相同
        if not save_to_disk:
            current_hash = hash(pil_img.tobytes())
            if current_hash == self._last_hash:
                return b""
            self._last_hash = current_hash

        buffer = io.BytesIO()
        pil_img.save(buffer, format="JPEG", quality=self.quality)
        jpg_bytes = buffer.getvalue()

        if save_to_disk and self.save_dir:
            self._save_to_disk(jpg_bytes)

        return jpg_bytes

    def screenshot_base64(self, save_to_disk: bool = False) -> str:
        """返回 Base64 编码的截图"""
        jpg_bytes = self.screenshot(save_to_disk)
        if not jpg_bytes:
            return ""
        return base64.b64encode(jpg_bytes).decode("utf-8")

    def _save_to_disk(self, jpg_bytes: bytes):
        if not self.save_dir:
            return
        os.makedirs(self.save_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(self.save_dir, f"screenshot_{timestamp}.jpg")
        with open(filename, "wb") as f:
            f.write(jpg_bytes)

    def reset_hash(self):
        """重置哈希缓存，强制下次截图"""
        self._last_hash = None

    def __del__(self):
        if hasattr(self, '_sct'):
            self._sct.close()
