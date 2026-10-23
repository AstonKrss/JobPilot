"""ChatAgent 的接口层。

/stream 是同一条链路的流式版本：agent 执行过程中每完成一步就推一条
进度事件（理解意图 → 选简历 → 过滤 → 扩展 → 语义 → 打分 → 回复），
前端据此实时渲染执行时间线。事件用 SSE 帧封装（data: {...}\n\n）。
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..chat.agent import handle_message
from ..db import conversation_repo
from ..db.pool import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None


@router.post("")
async def chat(payload: ChatRequest) -> dict:
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="消息为空")
    try:
        return await handle_message(payload.message.strip(), payload.conversation_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("对话处理失败")
        raise HTTPException(status_code=500, detail=f"对话处理失败：{exc}") from exc


@router.post("/stream")
async def chat_stream(payload: ChatRequest) -> StreamingResponse:
    """流式对话：进度事件实时推送，最后一条是完整结果。"""
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="消息为空")

    queue: asyncio.Queue = asyncio.Queue()

    async def notify(label: str) -> None:
        await queue.put({"type": "step", "label": label})

    async def run() -> None:
        try:
            result = await handle_message(
                payload.message.strip(), payload.conversation_id, notify=notify
            )
            await queue.put(
                {
                    "type": "done",
                    "conversation_id": result.get("conversation_id"),
                    "reply": result.get("reply") or "",
                    "jobs": result.get("jobs") or [],
                    "slots": result.get("slots") or {},
                    "steps": result.get("steps") or [],
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("对话流处理失败")
            await queue.put({"type": "error", "message": f"对话处理失败：{exc}"})
        finally:
            await queue.put(None)  # 结束哨兵

    async def gen():
        task = asyncio.create_task(run())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            # 客户端中途断开时也要回收任务，避免协程泄漏
            if not task.done():
                task.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("")
async def list_conversations() -> dict:
    with get_pool().connection() as conn:
        return {"items": conversation_repo.list_recent(conn)}


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: int) -> dict:
    with get_pool().connection() as conn:
        conv = conversation_repo.get(conn, conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        conv["messages"] = conversation_repo.get_messages(conn, conversation_id)
    return conv
