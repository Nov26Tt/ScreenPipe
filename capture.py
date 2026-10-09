"""屏幕捕获与内容变更检测。

变更检测是整个框架里最省 Token 的设计：屏幕静止不动时反复请求视觉模型
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import mss
from PIL import Image

# mss 10.x 起``mss.mss()`` 标记为弃用，改用 ``mss.MSS``；
# 低版本回退到旧接口，保证 requirements 里的下限仍然可用。
_MSS_FACTORY = getattr(mss, "MSS", None) or mss.mss

# 指纹计算的输入上限：超长文本会拖慢 blake2b，而画面差异通常出现在
# 前部像素上。只取前 2MB 像素数据，兼顾速度与准确性。
_HASH_SAMPLE_BYTES = 2 * 1024 * 1024

# 常见屏幕分辨率的区域预设：只截目标区域能显著降低图片体积与识别噪音。
# 值为 None 时表示全屏。用户在网页端按需选择，无需手填坐标。
REGION_PRESETS = {
    "full": None,  # 主显示器全屏
    "left_half": "left",  # 左半屏（适合"左文档右 AI"布局）
    "right_half": "right",  # 右半屏
    "center": "center",  # 居中 1200x800 区域
}


@dataclass
class Screenshot:
    """一次截屏的完整结果。

    把「图片数据」和「落盘文件名」一起返回，是为了让调用方能把记录
    与截图关联起来—— 否则截图只是磁盘上的孤儿文件，既无法在界面上
    展示，也无法在删除记录时同步清理。
    """

    data: bytes = b""  # JPEG 原始字节
    filename: str = ""  # 落盘文件名，如 screenshot_20261009_150732.jpg
    width: int = 0
    height: int = 0
    size_kb: int = 0  # 图片体积（KB），用于成本归因

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("utf-8") if self.data else ""

    @property
    def is_empty(self) -> bool:
        return not self.data


class Capture:
    """屏幕捕获器。

    线程安全说明：``mss`` 实例与 Pillow 对象未加锁。本项目由 FastAPI 的
    单个事件循环串行调用（REST 与 WebSocket 复用同一协程），不会并发进入；
    如需多线程调用请自行加锁。
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
        :param region: 截屏区域 ``[left, top, width, height]``；``None`` 表示全屏
        """
        self.save_dir = save_dir
        self.quality = max(1, min(95, int(quality)))
        self.region = region
        # mss 实例**惰性创建**，不在 __init__ 里。
        # 原因：mss 在无图形环境的机器上（Linux CI、容器、Docker）
        # 实例化即抛异常。若在构造时就创建，任何纯逻辑测试
        # （区域预设换算、指纹计算、保留策略）都会连带失败。
        # 惰性化后，这些测试可在任意平台运行，只有真正需要
        # 截屏的用例才会触发 mss —— 而那部分本来就只能在有屏幕的机器上跑。
        self._sct = None
        self._last_hash: Optional[str] = None

    @property
    def sct(self):
        """惰性创建并返回 mss 实例；无图形环境时给出明确错误。"""
        if self._sct is None:
            try:
                self._sct = _MSS_FACTORY()
            except Exception as exc:
                raise RuntimeError(
                    "无法初始化屏幕捕获：mss 需要图形环境。"
                    "请在有显示器的机器上运行（Windows / 带 X11 或 Wayland 的 Linux）。"
                    f"原始错误：{exc}"
                ) from exc
        return self._sct

    def has_display(self) -> bool:
        """当前环境是否具备截屏能力。

        CI 用它来跳过真正需要显示器的用例，而不是让整组测试红掉。
        """
        try:
            self.sct
            return True
        except RuntimeError:
            return False

    def _resolve_monitor(self) -> dict:
        """确定要抓取的显示器区域。"""
        if self.region and len(self.region) == 4:
            left, top, width, height = self.region
            return {"left": left, "top": top, "width": width, "height": height}
        # monitors[0] 是整块虚拟桌面（含多显示器），[1] 才是主显示器
        return self.sct.monitors[1]

    def _monitor_geometry(self) -> dict:
        """主显示器的几何信息，取不到时回退到一组中性默认值。

        区域预设换算只需要 width/height，不需要真实截屏能力 ——
        这样在没有显示器的机器上也能验证坐标计算是否正确。
        """
        try:
            mon = self.sct.monitors[1]
            return {"left": mon["left"], "top": mon["top"],
                    "width": mon["width"], "height": mon["height"]}
        except Exception:
            # 1920x1080 是最常见的桌面分辨率，用它做默认值不影响
            # 坐标换算的正确性验证（比例关系与实际分辨率无关）
            return {"left": 0, "top": 0, "width": 1920, "height": 1080}

    def resolve_region_preset(self, preset: str) -> Optional[List[int]]:
        """把预设名解析为具体坐标。

        放在 Capture 内部是因为预设依赖当前显示器尺寸，必须运行时才知道。

        :param preset: REGION_PRESETS 的键；未知值或"full"返回 None（全屏）
        """
        if preset in (None, "", "full"):
            return None

        mon = self._monitor_geometry()
        left, top = mon["left"], mon["top"]
        width, height = mon["width"], mon["height"]

        if preset == "left_half":
            return [left, top, width // 2, height]
        if preset == "right_half":
            return [left + width // 2, top, width - width // 2, height]
        if preset == "center":
            w, h = min(1200, width), min(800, height)
            return [left + (width - w) // 2, top + (height - h) // 2, w, h]
        return None

    def set_region_preset(self, preset: str) -> List[int]:
        """切换区域预设，返回生效后的实际区域（None 表示全屏）。

        配置变更时调用，会同时重置指纹——换了区域等于换了画面，
        指纹必须重新计算，否则首帧会被误判为"内容未变化"。
        """
        self.region = self.resolve_region_preset(preset)
        self.reset_hash()
        return self.region or []

    def _fingerprint(self, pil_img: "Image.Image") -> str:
        """计算画面指纹（稳定的跨进程哈希）。"""
        raw = pil_img.tobytes()[:_HASH_SAMPLE_BYTES]
        return hashlib.blake2b(raw, digest_size=16).hexdigest()

    def grab(self, save_to_disk: bool = False) -> Screenshot:
        """截取屏幕，返回带元数据的结果对象。

        若画面与上次完全一致且 ``save_to_disk`` 为 False，返回空的
        Screenshot（``is_empty`` 为 True），调用方据此跳过模型调用。

        :param save_to_disk: True 时强制截图并跳过变更检测（手动触发场景）
        """
        sct_img = self.sct.grab(self._resolve_monitor())
        pil_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        if not save_to_disk:
            current = self._fingerprint(pil_img)
            if current == self._last_hash:
                return Screenshot()
            self._last_hash = current

        buffer = io.BytesIO()
        pil_img.save(buffer, format="JPEG", quality=self.quality)
        jpg_bytes = buffer.getvalue()

        shot = Screenshot(
            data=jpg_bytes,
            width=pil_img.width,
            height=pil_img.height,
            size_kb=len(jpg_bytes) // 1024,
        )

        if save_to_disk and self.save_dir:
            shot.filename = self._save_to_disk(jpg_bytes)

        return shot

    def screenshot(self, save_to_disk: bool = False) -> bytes:
        """向后兼容的旧接口：只返回 JPEG 字节。"""
        return self.grab(save_to_disk).data

    def screenshot_base64(self, save_to_disk: bool = False) -> str:
        """向后兼容的旧接口：返回 Base64 字符串。"""
        return self.grab(save_to_disk).base64

    def _save_to_disk(self, jpg_bytes: bytes) -> str:
        """落盘并返回文件名。"""
        if not self.save_dir:
            return ""

        directory = Path(self.save_dir)
        directory.mkdir(parents=True, exist_ok=True)
        # 精确到毫秒：自动截屏间隔可能短到 1 秒，只用秒会覆盖同名文件
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"screenshot_{stamp}.jpg"
        (directory / filename).write_bytes(jpg_bytes)
        return filename

    def reset_hash(self) -> None:
        """重置指纹缓存，强制下次截图一定被判定为"已变化"。

        手动触发识别时必须调用，否则用户看到的画面没变、点"识别一次"
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


class RetentionPolicy:
    """截图保留策略：按天 + 按张数双阈值清理，防止磁盘无限增长。

    只保留最近 ``max_days`` 天且不超过 ``max_files`` 张。任一阈值超出
    都会触发清理。两个阈值并存的理由：
    * 只按天数：高频运行时单日文件数依然可能很大
    * 只按张数：低频运行时磁盘占用会持续数月不回
    """

    def __init__(
        self,
        directory: str,
        max_days: int = 7,
        max_files: int = 500,
        pattern: str = "screenshot_*.jpg",
    ):
        self.directory = Path(directory)
        self.max_days = max(1, int(max_days))
        self.max_files = max(1, int(max_files))
        self.pattern = pattern

    def list_files(self) -> List[Path]:
        """按修改时间升序列出所有截图（旧的在前）。"""
        if not self.directory.is_dir():
            return []
        return sorted(self.directory.glob(self.pattern), key=lambda p: p.stat().st_mtime)

    def purge(self) -> Tuple[int, int]:
        """执行清理。

        :return: (删除数量, 释放字节数)
        """
        files = self.list_files()
        if not files:
            return 0, 0

        now = datetime.now().timestamp()
        cutoff = now - self.max_days * 86400

        # 升序 = 旧 → 新。超期的直接标记删除
        doomed = {p for p in files if p.stat().st_mtime < cutoff}

        # 仍在有效期内但张数超了：保留最新的 max_files 张。
        # list_files 是旧→新序，因此要删的是列表头部而非尾部。
        within = [p for p in files if p not in doomed]
        overflow = len(within) - self.max_files
        if overflow > 0:
            doomed.update(within[:overflow])

        freed = 0
        count = 0
        for path in doomed:
            try:
                size = path.stat().st_size
                path.unlink()
                freed += size
                count += 1
            except OSError:
                # 单个文件删不掉不应中断整体清理
                continue
        return count, freed

    def used_bytes(self) -> int:
        """截图目录总占用（字节）。

        统计目录内**所有**文件而非只匹配 pattern —— 目录占用是整体概念，
        用户关心的是"这个目录占了多少空间"，而不是"符合某个命名规则的有几个"。
        清理仍然只动 pattern 匹配的文件，避免误删用户放进去的其他东西。
        """
        if not self.directory.is_dir():
            return 0
        return sum(
            p.stat().st_size
            for p in self.directory.iterdir()
            if p.is_file()
        )
