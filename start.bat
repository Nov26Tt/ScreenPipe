@echo off
chcp 65001 >nul
title 测评辅助系统

cd /d "%~dp0"

echo ============================================
echo         测评辅助系统 v1.0
echo ============================================
echo.

REM 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 检查依赖是否安装
python -c "import fastapi" >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 正在安装依赖...
    pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if %errorlevel% neq 0 (
        echo [错误] 依赖安装失败，请检查网络连接
        pause
        exit /b 1
    )
)

echo [启动] 正在启动服务...
echo.
echo 手机浏览器请访问下方地址:
echo.

python main.py

pause
