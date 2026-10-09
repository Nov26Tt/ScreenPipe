"""ScreenPipe —— 屏幕内容实时结构化框架。

将「屏幕上的内容」实时转换为「结构化文本」的轻量管道：

    屏幕捕获 → 像素级变更检测 → 视觉模型理解 → WebSocket 实时推送

典型场景：在线课程笔记提取、监控看板读数、UI 回归检查、
表单/票据信息结构化。

启动：双击 start.bat（Windows），或 `python main.py`（任意平台）。
"""

import io
import sys
from pathlib import Path

# Windows 终端默认 GBK，输出中文或模型返回内容时可能抛UnicodeEncodeError。
# 必须在导入业务模块之前重配��编码。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # 某些环境（如被重定向的管道）不支持 reconfigure，退回 TextIOWrapper
            try:
                _stream = io.TextIOWrapper(_stream.buffer, encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

# 支持 `python server/app.py` 与 `python main.py` 两种启动方式
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from server.app import run_server


def main() -> None:
    server_cfg = Config().server_config
    run_server(
        host=server_cfg.get("host", "0.0.0.0"),
        port=int(server_cfg.get("port", 8765)),
    )


if __name__ == "__main__":
    main()
