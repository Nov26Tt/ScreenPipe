#!/usr/bin/env python3
"""SnapStudy 一键启动器（Windows，仅依赖标准库）。

由 start.bat 调用，正常使用时不需要手动运行。自动完成以下步骤，
可重复运行，已完成的步骤会自动跳过：

    1/5  校验 Python 版本（>= 3.10）
    2/5  创建或复用项目级虚拟环境 .venv
    3/5  校验并安装 requirements.txt 中的依赖
    4/5  从 config.example.yaml 生成 config.yaml
    5/5  启动服务

调试时也可以单独运行：py launcher.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VENV_DIR = BASE_DIR / ".venv"
REQUIREMENTS = BASE_DIR / "requirements.txt"
CONFIG_FILE = BASE_DIR / "config.yaml"
CONFIG_TEMPLATE = BASE_DIR / "config.example.yaml"
ENTRY = BASE_DIR / "main.py"

MIN_PYTHON = (3, 10)
REQUIRED_IMPORTS = ["fastapi", "uvicorn", "mss", "PIL", "httpx", "yaml", "websockets"]
PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
PLACEHOLDER_KEY = "your-api-key-here"


def _setup_stdout() -> None:
    """让中文在各类终端下都能正常输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


def warn(step: str, message: str) -> None:
    print(f"[{step}] [警告] {message}", flush=True)


def run(cmd: list) -> int:
    """执行子进程，返回退出码。"""
    try:
        return subprocess.call([str(part) for part in cmd], cwd=str(BASE_DIR))
    except OSError as exc:
        warn("--", f"执行失败 {cmd[0]}: {exc}")
        return 1


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_python() -> None:
    if sys.version_info < MIN_PYTHON:
        print(
            f"[错误] 需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更高版本，"
            f"当前为 {sys.version.split()[0]}",
            flush=True,
        )
        print("       下载地址: https://www.python.org/downloads/", flush=True)
        sys.exit(1)
    log("1/5", f"Python 版本 {sys.version.split()[0]}")


def ensure_venv() -> Path:
    target = venv_python()
    if target.exists():
        log("2/5", f"复用虚拟环境：{VENV_DIR}")
        return target

    log("2/5", "创建虚拟环境（首次运行，稍等片刻）...")
    run([sys.executable, "-m", "venv", str(VENV_DIR)])

    if target.exists():
        log("2/5", f"虚拟环境就绪：{VENV_DIR}")
        return target

    warn("2/5", "无法创建虚拟环境，回退使用系统解释器")
    return Path(sys.executable)


def ensure_deps(python: Path) -> None:
    check = "import " + ", ".join(REQUIRED_IMPORTS)
    if run([python, "-c", check]) == 0:
        log("3/5", "依赖完整")
        return

    log("3/5", "安装依赖（首次运行，耗时较长）...")
    run([python, "-m", "pip", "install", "--upgrade", "pip"])

    code = run([python, "-m", "pip", "install", "-r", REQUIREMENTS, "-i", PIP_MIRROR])
    if code != 0:
        print("[3/5] 镜像源失败，改用默认源重试...", flush=True)
        code = run([python, "-m", "pip", "install", "-r", REQUIREMENTS])

    if code != 0:
        print("[3/5] [错误] 依赖安装失败，请检查网络连接。", flush=True)
        sys.exit(1)

    log("3/5", "依赖安装完成")


def ensure_config() -> None:
    if not CONFIG_FILE.exists():
        log("4/5", "未找到 config.yaml，正在从模板生成...")
        try:
            shutil.copyfile(CONFIG_TEMPLATE, CONFIG_FILE)
        except OSError as exc:
            warn("4/5", f"生成 config.yaml 失败：{exc}")
            return

    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
    except OSError:
        text = ""

    if PLACEHOLDER_KEY in text:
        print("[4/5] [注意] config.yaml 中的 api_key 仍是占位符，", flush=True)
        print("       请填入自己的密钥，或稍后在手机网页端设置。", flush=True)
    else:
        log("4/5", "配置就绪")


def main() -> int:
    _setup_stdout()

    print("=" * 44, flush=True)
    print("     SnapStudy - 一键启动", flush=True)
    print("=" * 44, flush=True)
    print(flush=True)

    ensure_python()
    python = ensure_venv()
    ensure_deps(python)
    ensure_config()

    log("5/5", "启动服务...")
    print(flush=True)
    print("请在手机浏览器打开下方打印的地址：", flush=True)
    print(flush=True)

    try:
        return subprocess.call([str(python), str(ENTRY)], cwd=str(BASE_DIR))
    except KeyboardInterrupt:
        print("\n已取消。", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
