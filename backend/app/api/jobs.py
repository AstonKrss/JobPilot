"""岗位入库与 JD 结构化抽取。

为什么抽取放在后端而不是插件里
    插件只负责把 JD 容器的原文搬出来。字段选择器一旦写在插件里，平台改版就得重新
    打包发布；放在后端则只需重启进程。爆掉的半径从"用户要更新扩展"缩小到"改一个
    prompt"。

三态字段（这是匹配正确性的关键）
    edu_level / edu_fulltime_required / job_nature 都允许 null，表示"原文没提"。
    没提 ≠ 不要求，也 ≠ 要求。把它压成布尔会直接制造误杀或漏杀，
    因此 prompt 里明确要求：没写就填 null，并给出原文依据。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..db import repo
from ..db.pool import get_pool
from ..llm.errors import TransientError

from ..ingest.jd_pipeline import (
    ALLOWED_DETAIL_LEVELS,
    ALLOWED_SOURCES,
    JdIngestError,
    ingest_jd_text,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobIngestRequest(BaseModel):
    """插件上报 / 手动粘贴的统一入参。

    raw_html 有两个用途：存档回溯"当时页面到底写了什么"；以及让完整性闸门
    检查有没有藏在 display:none 里的文本（innerText 拿不到那部分，但复制粘贴
    整段 HTML 的人会把它带进来）。
    """

    source: str = "manual"
    raw_text: str
    raw_html: str | None = None
    url: str | None = None
    source_job_id: str | None = None
    # full = 详情页完整 JD；digest = 列表页卡片摘要（无技能要求）
    detail_level: str = "unknown"


@router.post("/ingest")
async def ingest_job(payload: JobIngestRequest) -> dict:
    """存入岗位原文并就地抽取结构化字段。

    闸门/清洗/抽取/入库的完整链路在 ingest/jd_pipeline.py —— 这是唯一实现。
    这里只做两件事：入参校验、把管线错误翻译成 HTTP 状态码。
    插件/扩展与本项目的 DiscoverAgent 共用同一条链路，口径不会漂移。
    """
    try:
        result = await ingest_jd_text(
            source=payload.source,
            raw_text=payload.raw_text,
            raw_html=payload.raw_html,
            url=payload.url,
            source_job_id=payload.source_job_id,
            detail_level=payload.detail_level,
        )
    except JdIngestError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    except TransientError as exc:
        # 上游临时故障：告诉调用方可以稍后重试，而不是 500 让人以为是代码坏了
        raise HTTPException(status_code=503, detail=str(exc.user_message)) from exc
    return {
        "job_id": result["job_id"],
        "profile_id": result["profile_id"],
        "profile": result["profile"],
        "cost_cny": result["cost_cny"],
        # 自动发现定时扫描时靠它判断"要不要提醒用户"：老岗位被重新抓到不算新
        "is_new": result["is_new"],
        # 完整性结论随入库结果一起回给插件：插件在页面上直接显示
        # 「这份 JD 里有 2 处可疑内容」，用户在投之前就能看到。
        # 只回 level + summary + 引用，不回全部计数 —— 插件里没有地方展示细节
        "integrity": result["integrity"],
    }


@router.get("")
async def list_jobs(
    # limit 必须卡下界：PostgreSQL 对 LIMIT 负数直接报错，传 -1 就是 500
    limit: int = Query(default=200, ge=1, le=1000),
    source: str | None = None,
) -> dict:
    """岗位列表。source 非空时只返回该来源的岗位（boss / liepin / zhaopin / manual / careers）。"""
    with get_pool().connection() as conn:
        items = repo.list_jobs(conn, limit, source)
        counts = repo.count_jobs_by_source(conn)
    return {"count": len(items), "items": items, "sources": counts}


@router.get("/{job_id}")
async def get_job(job_id: int) -> dict:
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT j.id, j.source, j.url, j.raw_text, p.title, p.company, p.city,
                   p.edu_level, p.job_nature, p.exp_min, p.exp_max,
                   p.hard_constraint_evidence, j.detail_level,
                   p.must_have_skills, p.nice_to_have_skills, p.description
              FROM job j
              LEFT JOIN job_profile p ON p.job_id = j.id AND p.is_current
             WHERE j.id = %s
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="岗位不存在")
    return {
        "job_id": int(row[0]),
        "source": row[1],
        "url": row[2],
        "raw_text": row[3],
        "profile": None
        if row[4] is None
        else {
            "title": row[4],
            "company": row[5],
            "city": row[6],
            "edu_level": row[7],
            "job_nature": row[8],
            "exp_min": row[9],
            "exp_max": row[10],
            "hard_constraint_evidence": row[11],
            "must_have_skills": row[13] or [],
            "nice_to_have_skills": row[14] or [],
            "description": row[15],
        },
        # 完整度放在顶层：它描述的是"原文抓得全不全"，不是画像字段
        "detail_level": row[12] or "unknown",
    }


