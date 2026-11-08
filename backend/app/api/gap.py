"""技术栈补漏的接口层：展开（缓存分流）、自评保存、学习优先级聚合。

POST /expand 不是纯函数调用——它读 job_profile 并把结果合并已有自评，
是工作台懒展开区块的唯一数据来源。GET /priority 是建议页聚合清单的数据源。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db.pool import get_pool
from ..gap import assess, expand

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gap", tags=["gap"])


class ExpandRequest(BaseModel):
    job_id: int
    resume_id: int


class AssessRow(BaseModel):
    item: str
    skill: str = ""
    status: str


class AssessRequest(BaseModel):
    resume_id: int
    rows: list[AssessRow] = Field(default_factory=list)


@router.post("/expand")
async def expand_job_skills(payload: ExpandRequest) -> dict:
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT p.title, p.campus_type, p.must_have_skills, j.detail_level
              FROM job_profile p JOIN job j ON j.id = p.job_id
             WHERE p.job_id = %s AND p.is_current
            """,
            (payload.job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="岗位不存在或未解析")

    title, campus_type, must, detail_level = row[0], row[1], row[2] or [], row[3] or "unknown"
    result = await expand.expand_for_job([str(s) for s in must], title or "", campus_type)

    with get_pool().connection() as conn:
        existing = assess.load_by_resume(conn, payload.resume_id)
    for s in result["skills"]:
        for item in s["items"]:
            info = existing.get(assess.item_hash(item["text"]))
            item["status"] = info["status"] if info else None
            item["item_hash"] = assess.item_hash(item["text"])

    return {
        "job_id": payload.job_id,
        "resume_id": payload.resume_id,
        "title": title or "",
        "context": result["context"],
        "skills": result["skills"],
        "expanded_n": result["expanded_n"],
        "cache_hits": result["cache_hits"],
        "truncated": result["truncated"],
        "detail_level": detail_level,
        "disclaimer": "以下条目基于同类岗位的常见考察点推断，不是该企业的真实标准；自评只保存在本机。",
    }


@router.put("/assess")
async def save_assessments(payload: AssessRequest) -> dict:
    bad = [r.status for r in payload.rows if r.status not in assess.ALLOWED_STATUS]
    if bad:
        raise HTTPException(status_code=400, detail=f"未知的自评状态：{set(bad)}")
    with get_pool().connection() as conn:
        saved = assess.save_assessments(
            conn, payload.resume_id,
            [r.model_dump() for r in payload.rows],
        )
    return {"saved": saved}


@router.get("/priority")
async def gap_priority(resume_id: int | None = None, limit: int = 30) -> dict:
    with get_pool().connection() as conn:
        # 不传简历时用主简历：聚合清单在建议页看，不该要求用户先去别处选简历
        rid = resume_id
        if not rid:
            from ..db import repo

            rid = repo.get_default_resume_id(conn)
        if not rid:
            return {"resume_id": None, "count": 0, "items": [],
                    "note": "还没有可用的简历，先上传一份再做技能自查。"}
        items = assess.priority(conn, rid, limit)
    labeled = [
        {**it, "status_label": assess.STATUS_LABEL.get(it["status"], it["status"])}
        for it in items
    ]
    return {
        "resume_id": rid,
        "count": len(labeled),
        "items": labeled,
        "note": "按「要求这些能力的在架岗位数」排序；自评状态只存在本机。",
    }
