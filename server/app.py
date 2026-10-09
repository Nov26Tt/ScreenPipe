"""FastAPI 服务：REST API + WebSocket 实时推送。

职责划分
--------
* ``capture.py`` 负责"怎么截屏"（含内容变更检测）
* ``llm_client.py``   负责"怎么调模型"（OpenAI 兼容 + MiniMax VLM 端点）
* 本模块            负责"编排与对外暴露"：REST 触发、WebSocket 广播、配置热更新

安全性说明
----------
服务默认监听 0.0.0.0（需要手机访问），且**不内置认证**。因此：
* 所有对外接口一律只返回 ``config.to_public_dict()``（密钥已掩码）
* 任何需要写入密钥的接口，都通过掩码识别来避免"回显覆盖"事故
* 若要暴露到公网，请自行在前面加反向代理并启用 TLS + 访问控制
"""

import asyncio
import json
import logging
import os
import socket
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Deque, Dict, List, Optional

# 让 `python server/app.py` 也能直接运行
sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from capture import Capture
from config import Config, get_base_dir
from llm_client import LLMClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

config = Config()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """FastAPI 生命周期钩子（替代已弃用的 on_event）。"""
    init_modules()
    _sync_history_limit()
    logger.info(
        f"服务已启动，手机浏览器访问: http://{get_local_ip()}:{current_port()}"
    )
    yield
    # 释放底层句柄，避免进程退出时 mss 的截图资源泄漏
    if capture is not None:
        capture.close()


app = FastAPI(
    title="ScreenPipe",
    description="屏幕内容实时结构化框架：捕获 → 变更检测 → 视觉模型理解 → 实时推送",
    version="1.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# 运行时状态
#
# 这些可变对象是进程级单例（单文件部署、无多进程需求）。使用 deque 而非 list
# 是为了给历史记录一个真正的上界——早期版本只在*读取* 时切片，内存里其实
# 一直在增长。
# ---------------------------------------------------------------------------
capture: Optional[Capture] = None
llm_client: Optional[LLMClient] = None
capture_running = False
capture_task: Optional[asyncio.Task] = None
connected_websockets: set = set()

_history: Deque[Dict[str, Any]] = deque(maxlen=200)
# 实际监听端口：由 run_server 在启动前写入，可能因端口避让而不同于配置值
actual_port: Optional[int] = None


def _history_limit() -> int:
    """读取历史容量上限，非法值回退到 200。"""
    try:
        value = int(config.get("history", "max_records", default=200))
    except (TypeError, ValueError):
        return 200
    return max(1, value)


def _sync_history_limit() -> None:
    """把历史容量同步到最新配置（用户可在网页端热调整）。

    ``deque.maxlen`` 是只读属性，改不了，只能重建一个新的 deque。
    收缩时保留最新的 N 条（丢最旧的），扩展时原样搬运。
    """
    global _history

    limit = _history_limit()
    if _history.maxlen == limit:
        return

    _history = deque(_history, maxlen=limit)


def history_snapshot() -> List[Dict[str, Any]]:
    """返回历史记录的深拷贝，供 HTTP / WebSocket 序列化使用。

    必须拷贝：记录是 dict，直接返回会让调用方（或 WebSocket 的
    序列化过程）有机会改动到内部状态。
    """
    return [dict(record) for record in _history]


def init_modules() -> None:
    """按当前配置重建捕获器与模型客户端（配置热更新后需重新调用）。"""
    global capture, llm_client

    cap_cfg = config.capture_config
    save_dir = cap_cfg.get("save_dir") or str(get_base_dir() / "screenshots")
    os.makedirs(save_dir, exist_ok=True)
    capture = Capture(
        save_dir=save_dir,
        quality=int(cap_cfg.get("quality", 60)),
        region=cap_cfg.get("region"),
    )

    llm_cfg = config.llm_config
    llm_client = LLMClient(
        api_base=llm_cfg.get("api_base", ""),
        api_key=llm_cfg.get("api_key", ""),
        model=llm_cfg.get("model", ""),
        system_prompt=llm_cfg.get("system_prompt"),
    )
    logger.info(f"模块已初始化，模型={llm_cfg.get('model')}")


def get_local_ip() -> str:
    """获取本机局域网 IP，用于给手机端打印可访问地址。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def find_available_port(preferred: int, host: str = "0.0.0.0", attempts: int = 20) -> int:
    """返回可用端口：优先 preferred，被占用则依次向后探测。

    开发时8000/8080 常被其他服务占用，直接启动失败体验很差，
    因此这里做自动避让。
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
    return actual_port or int(config.server_config.get("port", 8765))


# ---------------------------------------------------------------------------
# 捕获与识别的核心逻辑（REST 与 WebSocket 共用，避免早期版本的两份重复实现）
# ---------------------------------------------------------------------------

def _check_vision_issue(content: str) -> str:
    """检测模型回复是否暗示"没收到图片"。

    纯文本模型收到 base64 图片时会返回这类话术。与其让用户面对一段无意义的
    回答，不如主动指出真正的原因。
    """
    if not content:
        return ""
    lowered = content.lower()
    for pattern in VISION_ISSUE_PATTERNS:
        if pattern.lower() in lowered:
            return (
                "⚠️ 模型可能未识别到截图。"
                "请确认 llm.model 填写的是支持图片输入的视觉（多模态）模型——"
                "纯文本模型无法处理图片。可参考 README 选择模型。"
            )
    return ""


VISION_ISSUE_PATTERNS = [
    "没有收到截图", "没有收到图片", "未看到图片", "看不到图片",
    "无法识别图片", "没有图片", "没有提供图片", "没有截图",
    "无法看到", "无图像", "未提供图像", "未检测到图片",
    "no image", "don't see an image", "cannot see the image",
    "did not receive an image", "no screenshot", "unable to see",
]


async def do_capture(force: bool = False) -> Dict[str, Any]:
    """执行一次「截屏 → 视觉模型理解」，返回统一结构的结果。

    :param force: True 时忽略"内容未变化"判定，强制截屏并请求模型
                  （手动触发的语义；自动循环则传 False 以省Token）
    :return: {ok, skipped, answer, vision_warning, elapsed}
    """
    if capture is None or llm_client is None:
        init_modules()

    started = time.perf_counter()

    if force and capture is not None:
        # 手动触发时重置变更检测，否则用户看到的题没变、却什么都拿不到
        capture.reset_hash()

    img_b64 = capture.screenshot_base64(save_to_disk=True)
    if not img_b64:
        return {
            "ok": True,
            "skipped": True,
            "reason": "屏幕内容与上次一致，已跳过（节省一次模型调用）",
        }

    size_kb = len(img_b64) * 3 // 4 // 1024
    logger.info(f"截图已捕获，约 {size_kb}KB，开始请求模型")

    try:
        answer = await llm_client.chat_with_image(img_b64)
        vision_warning = _check_vision_issue(answer)
    except Exception as exc:
        logger.error(f"模型调用失败: {exc}")
        return {"ok": False, "error": str(exc)}

    record = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "content": answer,
        "vision_warning": vision_warning,
    }
    _history.append(record)
    _sync_history_limit()

    elapsed = round(time.perf_counter() - started, 2)
    return {
        "ok": True,
        "skipped": False,
        "answer": answer,
        "vision_warning": vision_warning,
        "record": record,
        "elapsed": elapsed,
    }


async def broadcast(message: Dict[str, Any]) -> None:
    """向所有已连接的客户端推送一条消息，顺带清理失效连接。"""
    dead = set()
    for ws in list(connected_websockets):
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    connected_websockets.difference_update(dead)


# ---------------------------------------------------------------------------
# 生命周期与页面
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    template = Path(__file__).parent / "templates" / "index.html"
    return template.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# REST API
#
# 约定：任何返回配置内容的接口都走 to_public_dict()，不返回明文密钥。
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def get_status() -> Dict[str, Any]:
    return {
        "capture_running": capture_running,
        "config": config.to_public_dict(),
        "local_ip": get_local_ip(),
        "port": current_port(),
        "history_count": len(_history),
        "history_limit": _history.maxlen,
        "ws_connections": len(connected_websockets),
    }


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    """返回配置（api_key 已掩码）。前端回填时掩码会被识别并忽略。"""
    return config.to_public_dict()


@app.post("/api/config")
async def update_config(data: Dict[str, Any]) -> Dict[str, Any]:
    config.update_from_dict(data)
    config.save()
    _sync_history_limit()
    init_modules()
    return {"ok": True, "message": "配置已保存并生效"}


@app.post("/api/config/llm")
async def update_llm_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """更新 llm 段。掩码形态的 api_key 会在 Config 层被忽略。"""
    config.update_from_dict(data)
    config.save()
    init_modules()
    return {"ok": True, "message": "模型配置已更新"}


@app.post("/api/test-llm")
async def test_llm() -> Dict[str, Any]:
    """用已保存的配置测试连通性。"""
    if llm_client is None:
        init_modules()
    try:
        return await llm_client.test_connection()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/test-llm-direct")
async def test_llm_direct(data: Dict[str, Any]) -> Dict[str, Any]:
    """用请求里携带的配置测试连通性，不落盘——用户可以先试再决定保存。"""
    llm_cfg = data.get("llm", {})
    api_base = (llm_cfg.get("api_base") or "").strip()
    api_key = (llm_cfg.get("api_key") or "").strip()
    model = (llm_cfg.get("model") or "").strip()

    missing = [
        name
        for name, value in (("API 地址", api_base), ("API Key", api_key), ("模型名称", model))
        if not value or "..." in value
    ]
    if missing:
        return {"ok": False, "error": f"请填写：{'、'.join(missing)}"}

    temp_client = LLMClient(
        api_base=api_base,
        api_key=api_key,
        model=model,
        system_prompt=llm_cfg.get("system_prompt"),
    )
    return await temp_client.test_connection()


@app.get("/api/history")
async def get_history() -> List[Dict[str, Any]]:
    return history_snapshot()


@app.post("/api/clear-history")
async def clear_history() -> Dict[str, Any]:
    _history.clear()
    return {"ok": True}


@app.post("/api/capture-once")
async def capture_once() -> Dict[str, Any]:
    """手动触发一次识别（忽略内容变更检测）。"""
    result = await do_capture(force=True)
    if result.get("ok") and not result.get("skipped"):
        await broadcast({"type": "answer", "data": result["record"]})
    return result


@app.post("/api/start-capture")
async def start_capture() -> Dict[str, Any]:
    global capture_running, capture_task

    if capture_running:
        return {"ok": True, "message": "已在运行中"}

    capture_running = True
    if capture is not None:
        capture.reset_hash()
    capture_task = asyncio.create_task(auto_capture_loop())
    return {"ok": True, "message": "自动截屏已启动"}


@app.post("/api/stop-capture")
async def stop_capture() -> Dict[str, Any]:
    global capture_running, capture_task

    capture_running = False
    if capture_task is not None:
        capture_task.cancel()
        capture_task = None
    return {"ok": True, "message": "自动截屏已停止"}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    connected_websockets.add(ws)
    logger.info(f"客户端已连接，当前连接数: {len(connected_websockets)}")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "data": {"message": "消息不是合法 JSON"}})
                continue

            action = msg.get("action", "")

            if action == "capture_once":
                result = await do_capture(force=True)
                # 广播给所有端（多手机同时看），而不只是发起方
                if result.get("ok") and not result.get("skipped"):
                    await broadcast({"type": "answer", "data": result["record"]})
                else:
                    await ws.send_json({"type": "capture_result", "data": result})

            elif action == "get_history":
                await ws.send_json({"type": "history", "data": history_snapshot()})

            elif action == "get_status":
                await ws.send_json({"type": "status", "data": await get_status()})

            elif action == "clear_history":
                _history.clear()
                await ws.send_json({"type": "status", "data": {"message": "历史已清空"}})

            else:
                await ws.send_json(
                    {"type": "error", "data": {"message": f"未知操作: {action}"}}
                )

    except WebSocketDisconnect:
        logger.info("客户端断开连接")
    except Exception as exc:
        logger.error(f"WebSocket 异常: {exc}")
    finally:
        connected_websockets.discard(ws)


async def auto_capture_loop() -> None:
    """自动截屏循环：定时捕获，变化才请求模型。"""
    interval = int(config.capture_config.get("interval", 10))
    logger.info(f"自动截屏已启动，间隔 {interval} 秒")

    while capture_running:
        try:
            result = await do_capture(force=False)

            if result.get("skipped"):
                await broadcast({
                    "type": "status",
                    "data": {
                        "message": f"等待中（屏幕无变化，{interval}s 后重试）",
                        "capture_running": True,
                    },
                })
            elif result.get("ok"):
                await broadcast({"type": "answer", "data": result["record"]})
            else:
                await broadcast({"type": "error", "data": {"message": result.get("error", "")}})

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error(f"自动截屏循环出错: {exc}")
            await broadcast({"type": "error", "data": {"message": str(exc)}})

        # 无论本轮成功与否都等待，避免错误时疯狂重试打爆 API
        await asyncio.sleep(interval)

    logger.info("自动截屏已停止")
    await broadcast({
        "type": "status",
        "data": {"message": "自动截屏已停止", "capture_running": False},
    })


def run_server(host: str = "0.0.0.0", port: int = 8765) -> None:
    global actual_port

    actual_port = find_available_port(port, host)
    if actual_port != port:
        logger.warning(f"端口 {port} 已被占用，已自动切换到 {actual_port}")

    print("\n" + "=" * 46)
    print(f"  ScreenPipe 已启动: http://{get_local_ip()}:{actual_port}")
    print("  手机需与电脑处于同一局域网，按 Ctrl+C 停止")
    print("=" * 46 + "\n")

    uvicorn.run(app, host=host, port=actual_port, log_level="info")


if __name__ == "__main__":
    cfg = config.server_config
    run_server(
        host=cfg.get("host", "0.0.0.0"),
        port=int(cfg.get("port", 8765)),
    )
