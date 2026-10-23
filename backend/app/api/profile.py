"""用户画像接口（记忆架构 L1）。

除了读写，还有一条 /observe 用于完整链路演示与验证：
预筛 → 抽取 → 裁决 → 写入，每一步的结果都返回，方便看清是哪一环决定的。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db.pool import get_pool
from ..memory import extract, profile_repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/profile", tags=["profile"])


class ObserveRequest(BaseModel):
    text: str
    conversation_id: int | None = None


class SetRequest(BaseModel):
    value: object
    evidence: str | None = None


@router.get("")
async def get_profile(history_limit: int = 30) -> dict:
    with get_pool().connection() as conn:
        profile = profile_repo.get_profile(conn)
        history = profile_repo.get_history(conn, limit=history_limit)
    return {"count": len(profile), "profile": profile, "history": history}


@router.post("/observe")
async def observe(payload: ObserveRequest) -> dict:
    """完整链路：判断这句话要不要改画像，改了就说清依据。

    返回值里带着每一层的判定，这是它存在的意义——
    "为什么系统认为我接受广州" 必须能回答，否则记忆就成了黑箱。
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="内容为空")

    if not extract.should_extract(text):
        return {
            "triggered": False,
            "reason": "L0 规则预筛未命中：没有同时出现偏好信号词与槽位线索",
            "applied": [],
            "pending": [],
        }

    with get_pool().connection() as conn:
        current = profile_repo.get_flat_profile(conn)

    updates = await extract.extract_updates(text, current)

    applied: list[dict] = []
    pending: list[dict] = []
    skipped: list[dict] = []
    with get_pool().connection() as conn:
        for action in updates:
            decision = extract.decide(action, current.get(action["key"]))
            if decision == "apply":
                applied.append(profile_repo.apply_update(conn, action, payload.conversation_id))
            elif decision == "pending":
                pending.append(action)  # L2 挂起：表述不确定，等用户确认，先不写
            else:
                skipped.append(action)

    return {
        "triggered": True,
        "extracted": len(updates),
        "applied": applied,
        "pending": pending,
        "skipped": skipped,
    }


@router.put("/{key}")
async def set_slot(key: str, payload: SetRequest) -> dict:
    """手动设置某个槽位（界面上直接改，比靠对话推断可靠）。"""
    if key not in extract.ALLOWED_KEYS:
        raise HTTPException(status_code=400, detail=f"未知画像槽位 {key}")
    action = {
        "key": key,
        "op": "replace",
        "value": payload.value,
        "modality": "exclusive",
        "evidence": payload.evidence or "用户手动设置",
    }
    with get_pool().connection() as conn:
        return profile_repo.apply_update(conn, action)


@router.delete("/{key}")
async def delete_slot(key: str) -> dict:
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM user_profile WHERE key = %s", (key,))
        conn.commit()
    return {"deleted": key}
