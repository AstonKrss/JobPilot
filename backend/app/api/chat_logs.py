"""HR 聊天记录：扩展自动同步 + 工作台展示。

同步是「全量发、库里去重」：扩展每次打开聊天面板都把最近 20 条发过来，
靠 (platform, job_title, role, content) 唯一索引去重。这样扩展侧不需要记
"上次同步到哪"，逻辑最简单；代价是每次多几十条 INSERT，本地单用户无所谓。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..db import repo
from ..db.pool import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat-logs", tags=["chat-logs"])


class ChatLogSync(BaseModel):
    platform: str
    page_url: str | None = None
    job_title: str | None = None
    messages: list[dict] = Field(default_factory=list)  # [{role, text}]


class ChatLogSyncResult(BaseModel):
    saved: int


def _match_job_id(conn, job_title: str | None) -> int | None:
    """按标题精确匹配库里的岗位。

    宁可不关联也不猜：模糊匹配（contains）会把「Java开发实习生」匹配到
    库里任何一个带这个词的岗位，工作台上就会张冠李戴。
    """
    if not job_title:
        return None
    row = conn.execute(
        """
        SELECT j.id FROM job j
          JOIN job_profile p ON p.job_id = j.id AND p.is_current
         WHERE p.title = %s
         ORDER BY j.id DESC LIMIT 1
        """,
        (job_title,),
    ).fetchone()
    return int(row[0]) if row else None


def _ensure_skeleton_job(conn, job_title: str) -> int:
    """岗位没入库时建一条骨架记录，让聊天记录有地方挂。

    不调 LLM、不抽画像（detail_level=digest），所以不花钱；
    source_job_id 用 chat:{标题} 做幂等键，同一岗位重复同步只会建一次。
    用户之后在岗位上正式抓取时会生成完整画像，聊天记录的关联不受影响。
    """
    idempotent_key = f"chat:{job_title}"
    row = conn.execute(
        "SELECT id FROM job WHERE source = 'manual' AND source_job_id = %s",
        (idempotent_key,),
    ).fetchone()
    if row:
        return int(row[0])
    cur = conn.execute(
        """
        INSERT INTO job (source, source_job_id, raw_text, detail_level)
        VALUES ('manual', %s, %s, 'digest') RETURNING id
        """,
        (idempotent_key, job_title),
    )
    return int(cur.fetchone()[0])


@router.post("/sync")
async def sync_chat_logs(payload: ChatLogSync) -> dict:
    messages = [m for m in payload.messages if (m.get("text") or "").strip()]
    if not messages:
        return {"saved": 0, "total": 0}

    with get_pool().connection() as conn:
        job_id = _match_job_id(conn, payload.job_title)
        if job_id is None and payload.job_title:
            # 库里没有这个岗位：建一条骨架记录，让聊天和工作台有地方挂。
            # 只存标题不做 LLM 抽取，所以不花钱
            job_id = _ensure_skeleton_job(conn, payload.job_title)
            logger.info("聊天同步自动建档 job=%s title=%s", job_id, payload.job_title)
        saved = 0
        added_hr = 0
        added_me = 0
        last_hr = ""
        for m in messages:
            role = (m.get("role") or "HR").strip()
            text = (m.get("text") or "").strip()
            cur = conn.execute(
                """
                INSERT INTO chat_log (platform, page_url, job_title, job_id, role, content)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (platform, coalesce(job_title, ''), role, content) DO NOTHING
                """,
                (payload.platform, payload.page_url, payload.job_title, job_id, role, text),
            )
            if cur.rowcount:
                saved += cur.rowcount
                # 按角色分开计数：只有 HR 新说的才值得弹通知。
                # 混在一起的话，用户自己刚打完一句话也会触发一条「HR 有新消息」，
                # 通知一旦误报过两次，就再没人会点它了
                if role == "我":
                    added_me += 1
                else:
                    added_hr += 1
                    last_hr = text
        conn.commit()
        total = conn.execute(
            "SELECT count(*) FROM chat_log WHERE job_id = %s",
            (job_id,),
        ).fetchone()[0] if job_id else 0

    if saved:
        logger.info(
            "聊天记录同步 platform=%s job=%s 新增 %d/%d（HR %d）",
            payload.platform, job_id, saved, len(messages), added_hr,
        )
    return {
        "saved": saved,
        "total": int(total),
        # 下面三个字段只服务于「要不要提醒」这一个判断。
        # 扩展拿不到它们就只能靠"有新增"来弹窗，而"有新增"里有一半是自己发的话
        "added_hr": added_hr,
        "added_me": added_me,
        "last_hr": last_hr[:120],
        "job_title": payload.job_title,
    }


@router.get("")
async def list_chat_logs(job_id: int, limit: int = 50) -> dict:
    """工作台用：这个岗位关联的聊天记录，按同步顺序正序展示。"""
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT role, content, synced_at
              FROM chat_log
             WHERE job_id = %s
             ORDER BY id
             LIMIT %s
            """,
            (job_id, limit),
        ).fetchall()
    return {
        "count": len(rows),
        "items": [
            {
                "role": r[0],
                "content": r[1],
                "synced_at": r[2].isoformat() if r[2] else None,
            }
            for r in rows
        ],
    }
