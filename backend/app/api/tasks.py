"""后台任务的接口层：列表 / 详情 / 取消 / 手动入队（白名单类型）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db.pool import get_pool
from ..tasks import queue
from ..tasks.handlers import USER_ENQUEUEABLE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    type: str
    payload: dict = Field(default_factory=dict)


@router.get("")
async def list_tasks(limit: int = 20) -> dict:
    with get_pool().connection() as conn:
        items = queue.list_tasks(conn, limit)
    running = sum(1 for t in items if t["status"] == "running")
    pending = sum(1 for t in items if t["status"] == "pending")
    return {"count": len(items), "running": running, "pending": pending, "items": items}


@router.get("/{task_id}")
async def get_task(task_id: int) -> dict:
    with get_pool().connection() as conn:
        task = queue.get_task(conn, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: int) -> dict:
    with get_pool().connection() as conn:
        return queue.cancel(conn, task_id)


@router.post("")
async def create_task(payload: TaskCreate) -> dict:
    """手动入队。只放行 USER_ENQUEUEABLE 白名单：
    parse_resume 由上传/重解析流程自动入队，不开放手动塞（会绕过文件校验）。"""
    if payload.type not in USER_ENQUEUEABLE:
        raise HTTPException(
            status_code=400,
            detail=f"不允许手动入队 {payload.type}（可入队：{sorted(USER_ENQUEUEABLE)}）",
        )
    with get_pool().connection() as conn:
        task_id = queue.enqueue(conn, payload.type, payload.payload)
    return {"id": task_id, "type": payload.type, "status": "pending"}
