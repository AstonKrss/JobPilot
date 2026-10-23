"""给简历补技能词：画像与文件同步改。

为什么不能只改画像
    画像决定系统怎么推荐（"缺 Linux"变成"命中 Linux"），
    但 HR 看到的是那份 Word/PDF。只改画像 = 系统替用户撒谎。
    所以这里两处都改：画像加一条 source=user_declared 的技能，
    文件里在技能段落末尾真的追加一个词。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import settings
from ..db import repo
from ..db.pool import get_pool
from ..rewrite import skills as skills_mod
from ..rewrite.pdf_convert import convert_pdf_to_docx

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/resumes", tags=["skills"])


class SkillAdd(BaseModel):
    name: str
    # 用户自述的依据，便于日后回看"当时为什么加这条"
    note: str | None = None


@router.post("/{resume_id}/skills")
async def add_skill(resume_id: int, payload: SkillAdd) -> dict:
    """在画像和简历文件里同时补一条技能。

    文件改不动时（PDF 转换失败、找不到技能段落）只更新画像，
    但会把原因明确返回 —— 不能让用户以为简历文件已经改好了。
    """
    name = (payload.name or "").strip()
    if not name or len(name) > 40:
        raise HTTPException(status_code=400, detail="技能名称为空或过长")

    evidence = payload.note or f"用户自述：我会 {name}"

    with get_pool().connection() as conn:
        detail = repo.get_resume_detail(conn, resume_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="简历不存在")
        result = repo.add_resume_skill(conn, resume_id, name, evidence)
        if result is None:
            raise HTTPException(status_code=409, detail="这份简历还没有解析出画像，无法补技能")
        src_path = Path(detail.get("file_path") or "")

    known = [
        (s.get("name") if isinstance(s, dict) else str(s)) or ""
        for s in (detail.get("profile", {}).get("skills") or [])
    ]

    out_dir = settings.upload_dir.parent / "tailored"
    stem = src_path.stem if src_path.exists() else f"resume{resume_id}"
    out_path = out_dir / f"{stem}-skills.docx"

    # 源文件是 PDF 时先按原版式转 Word，再在 Word 里加词（在 PDF 上改字会破坏后面的行）
    work_path = src_path
    converted = False
    if src_path.exists() and src_path.suffix.lower() == ".pdf":
        work_path = out_dir / f"{stem}-frompdf.docx"
        converted = convert_pdf_to_docx(src_path, work_path)

    file_result: dict
    if not src_path.exists():
        file_result = {"ok": False, "reason": f"原始文件已不在磁盘上：{src_path.name}"}
    elif work_path.suffix.lower() != ".docx":
        file_result = {"ok": False, "reason": "只支持 DOCX 或可转换的 PDF"}
    else:
        file_result = skills_mod.append_skill(work_path, out_path, name, known)

    suggestion = None
    if not file_result.get("ok"):
        suggestion = (
            "简历文件没有改动。这份简历里没有独立的技能段落，"
            "建议你自己加一行「技能：Java、MySQL、Linux…」，再重新上传。"
            "系统不会为了加一个词去改你的项目描述或荣誉栏 —— 那样等于替你编造。"
        )

    return {
        "name": name,
        "profile_updated": True,
        "profile_added": result["added"],
        "skill_count": len(result["skills"]),
        "file": {
            "ok": bool(file_result.get("ok")),
            "reason": file_result.get("reason"),
            "converted_from_pdf": converted,
            "download_url": f"/api/resumes/{resume_id}/skill-file" if file_result.get("ok") else None,
        },
        "suggestion": suggestion,
        "note": "匹配已立即生效；这条是你自述的，面试时要能讲清楚具体怎么用的",
    }


@router.get("/{resume_id}/skill-file")
async def download_skill_file(resume_id: int) -> FileResponse:
    with get_pool().connection() as conn:
        detail = repo.get_resume_detail(conn, resume_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="简历不存在")
    src_path = Path(detail.get("file_path") or "")
    out_path = settings.upload_dir.parent / "tailored" / f"{src_path.stem}-skills.docx"
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="还没有生成补技能的版本")
    return FileResponse(
        str(out_path),
        filename=f"{src_path.stem}-补技能.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
