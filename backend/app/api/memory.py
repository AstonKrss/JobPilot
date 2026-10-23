"""记忆接口：生成情节摘要、查看召回结果。

主要用途是让"记忆"这件事可观测 —— 用户要能看到系统记住了什么、
召回时为什么选中那几条，否则记忆就是黑箱。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..db.pool import get_pool
from ..memory import episode as episode_mem
from ..memory import profile_repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.post("/summarize/{conversation_id}")
async def summarize(conversation_id: int) -> dict:
    """把一次会话压成情节记忆。消息太少时返回 skipped。"""
    with get_pool().connection() as conn:
        result = await episode_mem.summarize_conversation(conn, conversation_id)
    if result is None:
        return {
            "created": False,
            "reason": "会话消息太少或没有实质结论，不值得记",
        }
    return {"created": True, **result}


@router.get("/recall")
async def recall(q: str, top_k: int = 3) -> dict:
    """按一句话召回相关记忆，返回打分明细（分数怎么来的都能看到）。"""
    if not q.strip():
        raise HTTPException(status_code=400, detail="缺少查询内容")
    with get_pool().connection() as conn:
        items = episode_mem.recall(conn, q, top_k=top_k)
        block = episode_mem.build_recall_block(items)
    return {
        "query": q,
        "count": len(items),
        "items": [
            {
                **i,
                "created_at": i["created_at"].isoformat() if i["created_at"] else None,
            }
            for i in items
        ],
        # 拼给模型的实际文本，方便一眼看出"模型到底看到了什么"
        "block": block,
    }


@router.get("/overview")
async def overview() -> dict:
    """记忆总览：画像条数、情节条数、最近几条情节。"""
    with get_pool().connection() as conn:
        profile = profile_repo.get_profile(conn)
        rows = conn.execute(
            """
            SELECT id, topic, summary, created_at, hit_count
              FROM episode ORDER BY id DESC LIMIT 10
            """
        ).fetchall()
        total = conn.execute("SELECT count(*) FROM episode").fetchone()[0]
    return {
        "profile_count": len(profile),
        "profile": profile,
        "episode_total": int(total),
        "episodes": [
            {
                "id": r[0], "topic": r[1], "summary": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
                "hit_count": r[4],
            }
            for r in rows
        ],
    }
