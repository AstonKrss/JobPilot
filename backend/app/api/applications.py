"""投递跟踪接口。

状态由用户手动推进（不自动推断）：从聊天记录里猜「HR 约了面试」误判的代价
是错过跟进，手动点一下的成本远低于补救一次误判。每次推进记一条时间线。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db import repo
from ..db.pool import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/applications", tags=["applications"])

# 投递状态只有两个。
# 原来有六个（候选 / 已投 / 沟通中 / 面试 / Offer / 不合适），实测一次都没被
# 手动点过 —— 招聘平台自带投递记录且是自动的，手动维护状态是纯负担。
# 系统真正需要知道的只有"投了没"；"有没有回应"由聊天记录带出来，不靠人填。
STATUS_LABEL = {
    "candidate": "未投",
    "applied": "已投",
}


class ApplicationCreate(BaseModel):
    job_id: int
    # 从岗位页一键「标记已投」是最常见的入口，所以默认已投递；
    # 想先加进意向清单的可以显式传 candidate
    status: str = "applied"
    note: str | None = None


class ApplicationUpdate(BaseModel):
    status: str
    note: str | None = None


def _check_status(status: str) -> None:
    if status not in repo.APPLICATION_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"未知状态 {status}，可选：{'、'.join(repo.APPLICATION_STATUSES)}",
        )


@router.get("")
async def list_applications(status: str | None = None) -> dict:
    if status and status not in repo.APPLICATION_STATUSES:
        raise HTTPException(status_code=400, detail=f"未知状态 {status}")
    with get_pool().connection() as conn:
        items = repo.list_applications(conn, status)
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "count": len(items),
        "counts": counts,
        "status_labels": STATUS_LABEL,
        "items": [
            {
                **item,
                "created_at": item["created_at"].isoformat() if item["created_at"] else None,
                "updated_at": item["updated_at"].isoformat() if item["updated_at"] else None,
                "status_label": STATUS_LABEL.get(item["status"], item["status"]),
            }
            for item in items
        ],
    }


@router.post("")
async def create_application(payload: ApplicationCreate) -> dict:
    _check_status(payload.status)
    with get_pool().connection() as conn:
        job = repo.get_job_profile(conn, payload.job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"岗位 {payload.job_id} 不存在或还没有画像")
        app_id = repo.create_application(conn, payload.job_id, payload.status, payload.note)
        detail = repo.get_application_with_timeline(conn, app_id)
    return {
        "id": detail["id"],
        "job_id": detail["job_id"],
        "status": detail["status"],
        "status_label": STATUS_LABEL.get(detail["status"], detail["status"]),
        "created": True,
    }


@router.get("/stats")
async def get_stats() -> dict:
    """投递进展聚合：状态分布、回复率、按平台分组。工作台顶部的「整体进展」用。"""
    with get_pool().connection() as conn:
        stats = repo.get_application_stats(conn)
    return {
        **stats,
        "status_labels": {
            s: STATUS_LABEL[s] for s in repo.APPLICATION_STATUSES
        },
    }


@router.get("/followups")
async def list_followups(days: int = repo.FOLLOWUP_DAYS) -> dict:
    """投出去 N 天没有任何动静的岗位。

    这是「投完就失忆」的兜底：用户不会数日子，系统替他数。
    默认 10 天，可传参 —— 有人习惯一周一催。
    """
    if days < 1:
        raise HTTPException(status_code=400, detail="days 至少为 1")
    with get_pool().connection() as conn:
        items = repo.get_followups(conn, days)
    return {
        "days": days,
        "count": len(items),
        "items": items,
        "hint": (
            "这些岗位投出去后没有任何 HR 消息。先看一眼工作台里的沟通记录再决定要不要问 —— "
            "有些岗位确实不回复，催一次没有损失"
        )
        if items
        else None,
    }


@router.get("/calibration")
def reply_calibration(resume_id: int | None = None) -> dict:
    """用真实的回复情况反过来看打分准不准。

    不返回"准确率"这种词：这里的样本量天然很小，能说的只有
    "哪一档的回复更多"，以及样本不够时直说不够。
    """
    with get_pool().connection() as conn:
        return repo.get_reply_calibration(conn, resume_id)


@router.get("/{app_id}")
async def get_application(app_id: int) -> dict:
    with get_pool().connection() as conn:
        detail = repo.get_application_with_timeline(conn, app_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"跟踪记录 {app_id} 不存在")
    return {
        **detail,
        "status_label": STATUS_LABEL.get(detail["status"], detail["status"]),
        "created_at": detail["created_at"].isoformat() if detail["created_at"] else None,
        "updated_at": detail["updated_at"].isoformat() if detail["updated_at"] else None,
        "timeline": [
            {
                "status": e["status"],
                "status_label": STATUS_LABEL.get(e["status"], e["status"]),
                "note": e["note"],
                "created_at": e["created_at"].isoformat() if e["created_at"] else None,
            }
            for e in detail["timeline"]
        ],
    }


@router.patch("/{app_id}")
async def update_application(app_id: int, payload: ApplicationUpdate) -> dict:
    _check_status(payload.status)
    with get_pool().connection() as conn:
        ok = repo.update_application_status(conn, app_id, payload.status, payload.note)
    if not ok:
        raise HTTPException(status_code=404, detail=f"跟踪记录 {app_id} 不存在")
    return {"id": app_id, "status": payload.status, "status_label": STATUS_LABEL[payload.status]}


@router.delete("/{app_id}")
async def delete_application(app_id: int) -> dict:
    with get_pool().connection() as conn:
        ok = repo.delete_application(conn, app_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"跟踪记录 {app_id} 不存在")
    return {"deleted": True, "id": app_id}


@router.get("/by-job/{job_id}")
async def get_application_by_job(job_id: int) -> dict:
    """工作台用：这个岗位当前是否在跟踪中、跟踪到哪一步。"""
    with get_pool().connection() as conn:
        row = repo.get_application_by_job(conn, job_id)
    if row is None:
        return {"exists": False, "job_id": job_id}
    return {
        "exists": True,
        "id": row["id"],
        "status": row["status"],
        "status_label": STATUS_LABEL.get(row["status"], row["status"]),
        "note": row["note"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }
