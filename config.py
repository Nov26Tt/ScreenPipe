import yaml
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any


def get_base_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


class Config:
    _instance = None

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = str(get_base_dir() / "config.yaml")

        self.config_path = config_path
        self._data: Dict[str, Any] = {}

        if os.path.exists(config_path):
            self.load(config_path)
        else:
            self._set_defaults()

    def load(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}

    def save(self, path: Optional[str] = None):
        target = path or self.config_path
        with open(target, "w", encoding="utf-8") as f:
            yaml.dump(self._data, f, allow_unicode=True, default_flow_style=False)

    def _set_defaults(self):
        self._data = {
            "llm": {
                "api_base": "https://api.deepseek.com/v1",
                "api_key": "your-api-key-here",
                "model": "deepseek-flash",
                "system_prompt": "你是一个专业的学习辅助AI。请识别题目并给出解析。"
            },
            "capture": {
                "interval": 5,
                "quality": 60,
                "save_dir": ""
            },
            "server": {
                "port": 8765,
                "host": "0.0.0.0"
            },
            "history": {
                "max_records": 200
            }
        }

    def get(self, *keys: str, default=None):
        val = self._data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
                if val is None:
                    return default
            else:
                return default
        return val

    def set(self, *keys: str, value):
        d = self._data
        for k in keys[:-1]:
            if k not in d or not isinstance(d[k], dict):
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value

    @property
    def llm_config(self) -> Dict[str, Any]:
        return self._data.get("llm", {})

    @property
    def capture_config(self) -> Dict[str, Any]:
        return self._data.get("capture", {})

    @property
    def server_config(self) -> Dict[str, Any]:
        return self._data.get("server", {})

    def to_dict(self) -> Dict[str, Any]:
        return self._data

    def update_from_dict(self, data: Dict[str, Any]):
        self._data.update(data)
