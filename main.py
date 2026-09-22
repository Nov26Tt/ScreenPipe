import sys
import os
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from server.app import run_server


def main():
    config = Config()

    cfg = config.server_config
    host = cfg.get("host", "0.0.0.0")
    port = cfg.get("port", 8765)

    print("""
============================================
          SnapStudy 学习助手 v1.0
============================================""")

    run_server(host=host, port=port)


if __name__ == "__main__":
    main()
