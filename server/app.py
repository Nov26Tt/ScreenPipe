import asyncio
import json
import logging
import os
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config, get_base_dir
from capture import Capture
from llm_client import LLMClient

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

config = Config()
app = FastAPI(title="SnapStudy 学习助手")

# 全局状态
capture: Optional[Capture] = None
llm_client: Optional[LLMClient] = None
capture_running = False
capture_task: Optional[asyncio.Task] = None
history_records = []
connected_websockets = set()

# 实际监听端口：由 run_server 在启动前写入，可能不同于配置端口
actual_port: Optional[int] = None


def init_modules():
    global capture, llm_client

    cap_cfg = config.capture_config
    save_dir = cap_cfg.get("save_dir") or str(get_base_dir() / "screenshots")
    os.makedirs(save_dir, exist_ok=True)
    capture = Capture(
        save_dir=save_dir,
        quality=cap_cfg.get("quality", 60),
        region=cap_cfg.get("region")
    )

    llm_cfg = config.llm_config
    llm_client = LLMClient(
        api_base=llm_cfg["api_base"],
        api_key=llm_cfg["api_key"],
        model=llm_cfg["model"],
        system_prompt=llm_cfg.get("system_prompt")
    )


def get_local_ip() -> str:
    """获取本机局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def find_available_port(preferred: int, host: str = "0.0.0.0",
                        attempts: int = 20) -> int:
    """返回可用端口：优先使用 preferred，被占用则依次向后探测。

    这样即使配置端口被别的程序占用（例如 8000 被其他开发服务占用），
    服务也能自动换端口启动，而不是直接失败。
    """
    for offset in range(attempts):
        candidate = preferred + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((host, candidate))
            return candidate
        except OSError:
            continue

    raise RuntimeError(
        f"从 {preferred} 开始的 {attempts} 个端口均被占用，"
        f"请修改 config.yaml 中的 server.port"
    )


def current_port() -> int:
    """当前实际使用的端口。"""
    if actual_port:
        return actual_port
    return int(config.server_config.get("port", 8765))


@app.on_event("startup")
async def startup():
    init_modules()
    local_ip = get_local_ip()
    port = current_port()
    logger.info(f"服务已启动，手机浏览器访问: http://{local_ip}:{port}")


@app.get("/", response_class=HTMLResponse)
async def index():
    template_path = Path(__file__).parent / "templates" / "index.html"
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


# ==================== REST API ====================

@app.get("/api/status")
async def get_status():
    return {
        "capture_running": capture_running,
        "config": config.to_dict(),
        "local_ip": get_local_ip(),
        "port": current_port(),
        "history_count": len(history_records),
        "ws_connections": len(connected_websockets)
    }


@app.get("/api/config")
async def get_config():
    return config.to_dict()


@app.post("/api/config")
async def update_config(data: dict):
    config.update_from_dict(data)
    config.save()
    init_modules()
    return {"ok": True}


@app.post("/api/config/llm")
async def update_llm_config(data: dict):
    config.set("llm", value=data)
    config.save()
    init_modules()
    return {"ok": True, "message": "LLM 配置已更新"}


@app.post("/api/test-llm")
async def test_llm():
    init_modules()
    try:
        result = await llm_client.test_connection()
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/test-llm-direct")
async def test_llm_direct(data: dict):
    """使用请求中携带的配置直接测试 LLM 连接，无需先保存"""
    llm_cfg = data.get("llm", {})
    api_base = llm_cfg.get("api_base", "").strip()
    api_key = llm_cfg.get("api_key", "").strip()
    model = llm_cfg.get("model", "").strip()

    if not api_base:
        return {"ok": False, "error": "API 地址不能为空"}
    if not api_key:
        return {"ok": False, "error": "API Key 不能为空"}
    if not model:
        return {"ok": False, "error": "模型名称不能为空"}

    temp_client = LLMClient(
        api_base=api_base,
        api_key=api_key,
        model=model
    )
    result = await temp_client.test_connection()
    return result


@app.get("/api/history")
async def get_history():
    max_records = config.get("history", "max_records", default=200)
    return history_records[-max_records:]


@app.post("/api/clear-history")
async def clear_history():
    global history_records
    history_records = []
    return {"ok": True}


@app.post("/api/capture-once")
async def capture_once():
    """手动触发一次截图并分析"""
    if capture is None or llm_client is None:
        init_modules()

    try:
        img_b64 = capture.screenshot_base64(save_to_disk=True)
        if not img_b64:
            return {"ok": True, "skipped": True, "reason": "屏幕内容未变化"}

        img_size_kb = len(img_b64) * 3 // 4 // 1024
        logger.info(f"截图已捕获, Base64长度={len(img_b64)}, 约{img_size_kb}KB")

        content = await llm_client.chat_with_image(img_b64)

        vision_warning = _check_vision_issue(content)
        record = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "content": content,
            "vision_warning": vision_warning
        }
        history_records.append(record)
        await broadcast_to_phones({"type": "answer", "data": record})
        return {"ok": True, "skipped": False, "answer": content, "vision_warning": vision_warning}
    except Exception as e:
        logger.error(f"截图分析失败: {e}")
        return {"ok": False, "error": str(e)}


@app.post("/api/start-capture")
async def start_capture():
    global capture_running, capture_task

    if capture_running:
        return {"ok": True, "message": "已在运行中"}

    capture_running = True
    if capture:
        capture.reset_hash()
    capture_task = asyncio.create_task(auto_capture_loop())
    return {"ok": True, "message": "自动截图已启动"}


@app.post("/api/stop-capture")
async def stop_capture():
    global capture_running, capture_task

    capture_running = False
    if capture_task:
        capture_task.cancel()
        capture_task = None
    return {"ok": True, "message": "自动截图已停止"}


@app.get("/api/ip")
async def get_ip():
    return {"ip": get_local_ip(), "port": current_port()}


# ==================== WebSocket ====================

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_websockets.add(ws)
    logger.info(f"手机已连接，当前连接数: {len(connected_websockets)}")

    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            action = msg.get("action", "")

            if action == "capture_once":
                result = await capture_once_internal()
                await ws.send_json({"type": "capture_result", "data": result})

            elif action == "get_history":
                max_records = config.get("history", "max_records", default=200)
                await ws.send_json({
                    "type": "history",
                    "data": history_records[-max_records:]
                })

            elif action == "get_status":
                await ws.send_json({"type": "status", "data": await get_status()})

    except WebSocketDisconnect:
        logger.info("手机断开连接")
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
    finally:
        connected_websockets.discard(ws)


async def capture_once_internal() -> dict:
    if capture is None or llm_client is None:
        init_modules()
    try:
        img_b64 = capture.screenshot_base64(save_to_disk=True)
        if not img_b64:
            return {"ok": True, "skipped": True, "reason": "屏幕内容未变化"}

        img_size_kb = len(img_b64) * 3 // 4 // 1024
        logger.info(f"[WS] 截图已捕获, 约{img_size_kb}KB")

        content = await llm_client.chat_with_image(img_b64)
        vision_warning = _check_vision_issue(content)
        record = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "content": content,
            "vision_warning": vision_warning
        }
        history_records.append(record)
        return {"ok": True, "skipped": False, "record": record}
    except Exception as e:
        return {"ok": False, "error": str(e)}


VISION_ISSUE_PATTERNS = [
    "没有收到截图", "没有收到图片", "未看到图片", "看不到图片",
    "无法识别图片", "没有图片", "没有提供图片", "没有截图",
    "无法看到", "无图像", "未提供图像", "未检测到图片",
    "no image", "don't see an image", "cannot see the image",
    "did not receive an image", "no screenshot", "unable to see"
]


def _check_vision_issue(content: str) -> str:
    """检测 LLM 回复是否暗示未收到图片"""
    content_lower = content.lower()
    for pattern in VISION_ISSUE_PATTERNS:
        if pattern.lower() in content_lower:
            return (
                "⚠️ 大模型可能未识别到截图！"
                "请确认当前使用的模型支持图片识别（Vision/多模态模型），"
                "纯文本模型无法处理截图。推荐使用 gpt-5.6-terra、qwen3-vl-plus、"
                "doubao-seed-1.6-vision、glm-4.6v、deepseek-flash、kimi-k2.6。"
            )
    return ""


async def broadcast_to_phones(message: dict):
    """向所有连接的手机推送消息"""
    dead = set()
    for ws in connected_websockets:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    connected_websockets.difference_update(dead)


async def auto_capture_loop():
    """自动截图循环"""
    interval = config.capture_config.get("interval", 5)
    logger.info(f"自动截图已启动，间隔 {interval} 秒")

    while capture_running:
        try:
            if capture is None or llm_client is None:
                init_modules()

            img_b64 = capture.screenshot_base64(save_to_disk=True)

            if img_b64:
                img_size_kb = len(img_b64) * 3 // 4 // 1024
                logger.info(f"[Auto] 截图已捕获, 约{img_size_kb}KB")

                await broadcast_to_phones({
                    "type": "status",
                    "data": {"message": "正在分析截图...", "capture_running": True}
                })

                try:
                    answer = await llm_client.chat_with_image(img_b64)
                    vision_warning = _check_vision_issue(answer)
                except Exception as e:
                    answer = f"[分析失败] {str(e)}"
                    vision_warning = ""

                record = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "content": answer,
                    "vision_warning": vision_warning
                }
                history_records.append(record)

                await broadcast_to_phones({
                    "type": "answer",
                    "data": record
                })
            else:
                await broadcast_to_phones({
                    "type": "status",
                    "data": {"message": f"等待中（屏幕无变化）", "capture_running": True}
                })

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"自动截图循环出错: {e}")
            await broadcast_to_phones({
                "type": "error",
                "data": {"message": str(e)}
            })

        await asyncio.sleep(interval)

    logger.info("自动截图已停止")
    await broadcast_to_phones({
        "type": "status",
        "data": {"message": "自动截图已停止", "capture_running": False}
    })


def run_server(host: str = "0.0.0.0", port: int = 8765):
    global actual_port

    actual_port = find_available_port(port, host)
    if actual_port != port:
        logger.warning(f"端口 {port} 已被占用，已自动切换到 {actual_port}")

    uvicorn.run(app, host=host, port=actual_port, log_level="info")


if __name__ == "__main__":
    cfg = config.server_config
    run_server(host=cfg.get("host", "0.0.0.0"), port=cfg.get("port", 8765))
