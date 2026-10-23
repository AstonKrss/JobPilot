"""改写建议接口。

为什么先做同步接口
    一次请求最多 4–8 次模型调用（生成 + 核查 + 逐条修正 + 复核），实测 20–40 秒。
    前端加 loading 可以接受。若日后条目变多导致超过 60 秒，再改成任务队列 ——
    现在为它先建一套异步任务状态表，属于为尚未发生的问题付复杂度。

定向与普适走同一接口
    job_id 传了就是定向改写（复用匹配引擎算出的缺口），不传就是普适体检。
    拆成两个接口会让"有没有岗位"这个唯一差异散落到两处维护。
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Query

from ..db import match_repo, repo
from ..db.pool import get_pool
from ..match.engine import match_one
from ..match.rules import Decision
from ..rewrite.pipeline import build_suggestions

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/resumes", tags=["review"])


def _build_gap(match_result) -> dict:
    """把匹配结果压成改写需要的缺口描述。

    只取对改写有意义的三类：缺哪些必备技能、年限差多少、城市是否不符。
    不把整个 checks 丢给模型，避免它把已通过的项也当成问题去改。
    """
    gap: dict = {"missed_skills": match_result.missed_skills}
    for part in match_result.parts:
        if part.name == "经验年限" and part.score < part.max_score:
            gap["experience_note"] = part.detail
    for check in match_result.checks:
        if check.rule == "city" and check.decision is not Decision.PASS:
            gap["city_note"] = check.detail
    return gap


@router.post("/{resume_id}/review")
async def create_review(
    resume_id: int,
    job_id: int | None = Query(default=None, description="传了就是针对该岗位的定向改写"),
) -> dict:
    """生成改写建议。不传 job_id 则做不依赖岗位的普适体检。"""
    started = time.monotonic()
    trace_id = uuid.uuid4()

    with get_pool().connection() as conn:
        profile = match_repo.get_current_profile(conn, resume_id)
        if profile is None:
            raise HTTPException(
                status_code=409, detail="该简历尚无可用画像，请等待解析完成或先 reparse"
            )
        source_text = repo.get_resume_ordered_text(conn, resume_id)
        job = None
        gap = None
        if job_id is not None:
            job = repo.get_job_profile(conn, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"岗位 {job_id} 不存在或无画像")
            # 复用匹配层结论，而不是让模型自己再对比一遍：
            # 匹配的筛选逻辑经过规则回归验证，重新推理可能得出不同结论
            gap = _build_gap(match_one(profile, job, {}))

    session_id = f"review-{trace_id.hex[:12]}"
    result = await build_suggestions(
        profile, source_text, job=job, gap=gap, session_id=session_id
    )

    with get_pool().connection() as conn:
        repo.save_suggestions(conn, resume_id, job_id, trace_id, result.suggestions)
        conn.commit()

    elapsed = round(time.monotonic() - started, 1)
    logger.info(
        "改写建议完成 resume=%s job=%s 条目=%d 未通过=%d 耗时=%.1fs",
        resume_id,
        job_id,
        len(result.suggestions),
        result.flagged_count,
        elapsed,
    )

    return {
        "resume_id": resume_id,
        "job_id": job_id,
        "trace_id": str(trace_id),
        "mode": "targeted" if job else "general",
        "rounds": result.rounds,
        "elapsed_s": elapsed,
        "total": len(result.suggestions),
        "flagged": result.flagged_count,
        "items": [
            {
                "category": s.item.category,
                "severity": s.item.severity,
                "anchor": s.item.anchor,
                "problem": s.item.problem,
                "direction": s.item.direction,
                "example": s.item.example,
                "jd_requirement": s.item.jd_requirement,
                # passed / flagged：flagged 不是隐藏，而是明确告诉用户"这条没通过校验"
                "fact_status": s.fact_status,
                "fact_issue": s.fact_issue,
            }
            for s in result.suggestions
        ],
    }


@router.get("/{resume_id}/review/latest")
async def latest_review(
    resume_id: int,
    job_id: int | None = Query(default=None),
) -> dict:
    """最近一次生成的建议，供页面打开时直接展示。

    必须先判失效：建议是在某个时间点针对某份画像生成的，
    简历重新解析过之后，段落位置和措辞都可能变了，旧建议会指向不存在的内容。
    """
    with get_pool().connection() as conn:
        batch = repo.get_latest_suggestion_batch(conn, resume_id, job_id)
        profile_at = repo.get_profile_created_at(conn, resume_id)

    if batch is None:
        return {"exists": False, "resume_id": resume_id, "job_id": job_id}

    stale = bool(profile_at and batch["created_at"] and profile_at > batch["created_at"])
    return {
        "exists": True,
        "stale": stale,
        "resume_id": resume_id,
        "job_id": job_id,
        "mode": "targeted" if job_id else "general",
        "trace_id": batch["trace_id"],
        "created_at": batch["created_at"].isoformat() if batch["created_at"] else None,
        "total": len(batch["items"]),
        "flagged": sum(1 for it in batch["items"] if it["fact_status"] != "passed"),
        "items": batch["items"],
    }


@router.get("/{resume_id}/review")
async def list_reviews(
    resume_id: int,
    job_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """查看历史建议。

    定向与普适分开查：job_id 为空时只返回普适体检，
    否则会把不同岗位的建议混在一起，用户无法判断某条是为哪个岗位提的。
    """
    with get_pool().connection() as conn:
        if job_id is None:
            rows = conn.execute(
                """
                SELECT category, severity, anchor, problem, direction, example,
                       jd_requirement, fact_status, fact_issue, created_at
                  FROM rewrite_suggestion
                 WHERE resume_id = %s AND job_id IS NULL
                 ORDER BY created_at DESC, id
                 LIMIT %s
                """,
                (resume_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT category, severity, anchor, problem, direction, example,
                       jd_requirement, fact_status, fact_issue, created_at
                  FROM rewrite_suggestion
                 WHERE resume_id = %s AND job_id = %s
                 ORDER BY created_at DESC, id
                 LIMIT %s
                """,
                (resume_id, job_id, limit),
            ).fetchall()

    return {
        "resume_id": resume_id,
        "job_id": job_id,
        "count": len(rows),
        "items": [
            {
                "category": r[0],
                "severity": r[1],
                "anchor": r[2],
                "problem": r[3],
                "direction": r[4],
                "example": r[5],
                "jd_requirement": r[6],
                "fact_status": r[7],
                "fact_issue": r[8],
                "created_at": r[9].isoformat() if r[9] else None,
            }
            for r in rows
        ],
    }
