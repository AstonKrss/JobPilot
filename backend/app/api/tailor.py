"""简历定制接口：按岗位生成可下载的 DOCX。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..db.pool import get_pool
from ..db import repo
from ..rewrite.tailor import TAILOR_DIR, tailor_for_job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/resumes", tags=["tailor"])


class TailorRequest(BaseModel):
    # 建议面板生成的问题清单。带上它，改的东西才和用户看到的建议对得上
    hints: list[str] = Field(default_factory=list)


@router.post("/{resume_id}/tailor/{job_id}")
async def tailor(resume_id: int, job_id: int, payload: TailorRequest | None = None) -> dict:
    """针对某个岗位定制简历。

    耗时 20-40 秒（一次模型调用 + 逐条事实核查），同步返回。
    生成的文件落在 data/tailored/，原简历不做任何修改。

    即使一处改动都没被采纳，也会输出一份文件（内容等同原文）：
    用户等了 40 秒却拿不到东西，是很糟的体验。
    """
    try:
        result = await tailor_for_job(
            resume_id, job_id, hints=(payload.hints if payload else None)
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("简历定制失败")
        raise HTTPException(status_code=500, detail=f"简历定制失败：{exc}") from exc

    if result.get("error"):
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@router.get("/{resume_id}/tailored/{job_id}/file")
async def download_tailored(resume_id: int, job_id: int) -> FileResponse:
    path = TAILOR_DIR / f"resume{resume_id}-job{job_id}.docx"
    if not path.exists():
        raise HTTPException(status_code=404, detail="还没有生成过这份定制简历")
    with get_pool().connection() as conn:
        detail = repo.get_resume_detail(conn, resume_id)
    base = (detail or {}).get("file_name") or f"resume{resume_id}"
    stem = Path(base).stem
    return FileResponse(
        path,
        filename=f"{stem}-定制版.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
