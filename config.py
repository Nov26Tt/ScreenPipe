"""配置加载、持久化与脱敏。

设计要点：
1. **密钥永不外泄**：`to_public_dict()` 是唯一允许暴露给 HTTP 接口的出口，
   它会把 api_key 替换成掩码。原始 `to_dict()` 仅在进程内部使用。
2. **默认值与config.example.yaml 保持一致**，避免"文档说10、代码跑5"这类漂移。
3. 默认模型选用**免费且确定支持图片输入**的视觉模型，让clone 下来的人
   不填任何 Key 也能先跑通链路。
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# ---------------------------------------------------------------------------
# 默认值：这里的每个字段都必须与 config.example.yaml 保持一致。
# 修改时请同步改两处，避免文档与行为漂移。
# ---------------------------------------------------------------------------
DEFAULTS: Dict[str, Any] = {
    "llm": {
        # deepseek-flash（实为 DeepSeek-V4.1-Flash）原生支持图片输入，
        # 上下文 1M，是本项目的默认模型。用法见 README「选择视觉模型」。
        "api_base": "https://api.deepseek.com/v1",
        "api_key": "your-api-key-here",
        "model": "deepseek-flash",
        "system_prompt": "",
        # 开启后模型强制返回 JSON，结果可被程序直接消费。
        # 并非所有服务都支持 response_format，遇到 400 时可关掉。
        "json_mode": False,
    },
    "capture": {
        "interval": 10,          # 自动截图间隔（秒）
        "quality": 60,           # JPEG 压缩质量（1-95）
        "save_dir": "",          # 留空则使用项目下的 screenshots/
        "region": None,          # None 表示全屏；否则为 [left, top, width, height]
        "region_preset": "full", # full/left_half/right_half/center
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8765,
    },
    "history": {
        "max_records": 200,      # 内存与数据库中保留的记录条数
    },
    "retention": {
        # 截图保留策略：双阈值，任一超出即清理最旧的文件。
        # 只按天数：高频运行时单日文件数依然可能很大
        # 只按张数：低频运行时磁盘占用会持续数月不回
        "max_days": 7,
        "max_files": 500,
    },
    "storage": {
        "db_path": "",           # 记录数据库路径，留空则用项目下的 records.db
    },
}

PLACEHOLDER_KEY = "your-api-key-here"

#视模型名列表：用于在 /api/test-llm 时给出更精确的失败提示
VISION_HINT = "请确认 llm.model 填写的是支持图片输入的视觉（多模态）模型。"


def get_base_dir() -> Path:
    """项目根目录。兼容 PyInstaller 打包后的运行路径。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def mask_secret(value: Optional[str], keep: int = 4) -> str:
    """把密钥掩码成 `sk-a...wxyz` 形式。

    这是**唯一**允许密钥离开进程的形态。GET /api/config 与 /api/status
    都必须走这条路径，否则局域网内任何人都能直接读到明文 Key。

    规则：
    - 空值 / 占位符 → 原样返回（前端据此判断"尚未配置"）
    - 长度不足2*keep+1 → 全部打码，避免短密钥因"保留位数过多"而泄露
    """
    if not value:
        return ""
    if value == PLACEHOLDER_KEY:
        return value
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


class Config:
    """YAML 配置的读写封装。"""

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = str(get_base_dir() / "config.yaml")

        self.config_path = config_path
        self._data: Dict[str, Any] = {}

        if os.path.exists(config_path):
            self.load(config_path)
        else:
            self._data = self._deep_copy_defaults()

    # ---------- 基础读写 ----------

    @staticmethod
    def _deep_copy_defaults() -> Dict[str, Any]:
        # 显式逐层拷贝，避免修改 _data 时污染模块级 DEFAULTS
        import copy

        return copy.deepcopy(DEFAULTS)

    def load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"配置文件格式错误，应为YAML 映射：{path}")
        # 用默认值兜底缺失的顶层/嵌套键，保证 get() 不会因为用户
        # 手写了不完整的 yaml 而返回 None
        merged = self._deep_copy_defaults()
        for key, value in loaded.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        self._data = merged

    def save(self, path: Optional[str] = None) -> None:
        target = path or self.config_path
        with open(target, "w", encoding="utf-8") as f:
            yaml.dump(self._data, f, allow_unicode=True, default_flow_style=False)

    def get(self, *keys: str, default=None):
        """按路径读取，如 config.get('capture', 'interval', default=10)。"""
        val: Any = self._data
        for k in keys:
            if not isinstance(val, dict):
                return default
            val = val.get(k)
            if val is None:
                return default
        return val

    def set(self, *keys_and_value: Any) -> None:
        """按路径写入，自动创建中间层字典。

        最后一个参数是值，前面的都是键路径::

            config.set("llm", "api_key", "sk-xxx")
            config.set("capture", "interval", 5)
        """
        if len(keys_and_value) < 2:
            raise ValueError("set() 至少需要「键路径 + 值」两个参数")

        keys, value = keys_and_value[:-1], keys_and_value[-1]

        d = self._data
        for k in keys[:-1]:
            if k not in d or not isinstance(d[k], dict):
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value

    # ---------- 派生视图 ----------

    @property
    def llm_config(self) -> Dict[str, Any]:
        return self._data.get("llm", {})

    @property
    def capture_config(self) -> Dict[str, Any]:
        return self._data.get("capture", {})

    @property
    def server_config(self) -> Dict[str, Any]:
        return self._data.get("server", {})

    # ---------- 输出 ----------

    def to_dict(self) -> Dict[str, Any]:
        """返回**含明文密钥**的完整配置。仅限进程内部使用，禁止直接挂到 HTTP 响应。"""
        return self._data

    def to_public_dict(self) -> Dict[str, Any]:
        """返回可安全暴露给 HTTP 客户端的配置，密钥已掩码。"""
        safe = self._deep_copy_defaults()
        for key, value in self._data.items():
            if isinstance(value, dict) and isinstance(safe.get(key), dict):
                safe[key].update(value)
            else:
                safe[key] = value

        llm = safe.get("llm")
        if isinstance(llm, dict) and "api_key" in llm:
            llm["api_key"] = mask_secret(llm.get("api_key"))
        # api_base 里有时会带查询串参数（部分网关要求），一并清掉
        if isinstance(llm, dict) and isinstance(llm.get("api_base"), str):
            llm["api_base"] = llm["api_base"].split("?")[0]
        return safe

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        """用外部字典覆盖配置。

        两处保护：
        1. **逐段合并而非整段替换**——否则前端只回传 ``{"llm": {...}}``
           时，未提及的顶层段（capture/history/server）会被丢掉。
        2. **忽略掩码形态的 api_key**——它来自 GET 回显而非用户真实输入，
           直接写入会把真密钥永久覆盖成一串乱码。
        """
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(self._data.get(key), dict):
                merged = dict(self._data[key])
                merged.update(value)
                if key == "llm" and isinstance(value.get("api_key"), str):
                    if "..." in value["api_key"]:
                        merged["api_key"] = self._data[key].get(
                            "api_key", DEFAULTS["llm"]["api_key"]
                        )
                self._data[key] = merged
            else:
                self._data[key] = value
