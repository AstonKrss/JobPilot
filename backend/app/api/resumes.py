"""简历上传与查询接口。

上传为何做成异步
    一份简历要分段多次调用 LLM，每段最坏会经历 5 次退避重试（累计约 31 秒）。
    同步接口在最坏情况下要挂几分钟，前端和反向代理都会先超时。
    因此 POST 只落文件与记录并立即返回，解析交给后台任务，状态与结果走 GET 轮询。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from ..config import settings
from ..db import repo
from ..db.pool import get_pool
from ..ingest.docx_text import extract_docx_blocks
from ..ingest.extract import extract_profile
from ..ingest.quality import assess
from ..ingest.layout import restore_reading_order
from ..ingest.ocr import ocr_blocks
from ..ingest.pdf_text import extract_blocks
from ..ingest.segment import chunk_blocks
from ..llm.errors import AppError, PermanentError
from ..llm.sanitize import sanitize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/resumes", tags=["resumes"])

MAX_FILE_MB = 20
# DOCX 走原生段落解析（可编辑），PDF 走文本层/OCR（只读，改简历时按画像重建）
SUPPORTED_SUFFIXES = {".pdf", ".docx"}


@router.post("/upload")
async def upload_resume(file: UploadFile, background: BackgroundTasks) -> dict:
    """接收 PDF，落盘后立即返回。同一份文件（按 SHA256）不会重复解析，避免重复烧钱。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        # 明确说清支持什么，而不是笼统的"格式不支持"
        raise HTTPException(
            status_code=400,
            detail=f"支持 PDF / DOCX（.doc 请先另存为 .docx），收到的是 {suffix or '未知格式'}",
        )

    tmp_path = settings.upload_dir / f".upload-{uuid.uuid4().hex}{suffix}"
    size = 0
    with tmp_path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_MB * 1024 * 1024:
                out.close()
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"文件超过 {MAX_FILE_MB}MB 上限")
            out.write(chunk)

    file_hash = repo.file_sha256(tmp_path)
    with get_pool().connection() as conn:
        existing = repo.find_resume_by_hash(conn, file_hash)
        if existing:
            resume_id, status, _, existing_path = existing
            tmp_path.unlink(missing_ok=True)
            if status in ("done", "parsing"):
                logger.info("简历 %s 已存在（状态 %s），直接复用", resume_id, status)
                return {"resume_id": resume_id, "status": status, "reused": True}
            # 上次失败或只解析了一部分，允许重试
            repo.set_resume_status(conn, resume_id, "pending", error=None)
            conn.commit()
            background.add_task(_parse_resume, resume_id, Path(existing_path))
            return {"resume_id": resume_id, "status": "pending", "reused": True}

    final_path = settings.upload_dir / _file_name(file_hash, suffix)
    shutil.move(str(tmp_path), final_path)

    with get_pool().connection() as conn:
        resume_id = repo.create_resume(
            conn,
            file_hash=file_hash,
            file_path=str(final_path),
            file_name=_clean_file_name(file.filename),
            page_count=None,
            source_kind="text_layer",  # 真实类别在解析阶段判定后回写
        )
        conn.commit()

    background.add_task(_parse_resume, resume_id, final_path)
    return {"resume_id": resume_id, "status": "pending", "reused": False}


def _file_name(file_hash: str, suffix: str = ".pdf") -> str:
    """用 hash 命名，天然去重且不含用户文件名（避免中文文件名在路径里的编码问题）。

    保留真实后缀：DOCX 要能原样打开编辑，扩展名错了 Word 会拒绝。
    """
    return f"{file_hash[:32]}{suffix}"


def _clean_file_name(raw: str) -> str:
    """清洗用户上传的展示用文件名（第三轮测试 3.3）。

    落盘文件名是 SHA256，所以路径穿越"恰好"不成立；但 file_name 会进库、
    会被前端展示，将来一旦用它拼 Content-Disposition 或导出路径就是坑。
    不依赖"恰好安全"：剥掉路径成分与控制字符，并限制长度。
    """
    name = Path(raw).name
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '\\"')
    name = name.strip()
    return (name or "简历")[:120]


def _friendly_parse_error(exc: Exception, is_docx: bool) -> str:
    """把解析兜底异常映射成用户语言（第三轮测试 3.2）。

    类名（PackageNotFoundError / KeyError）是内部实现，进 UI 只会让用户
    以为系统坏了。按实测踩到的异常类型逐个映射；映射不上的统一说人话，
    技术细节已经由调用方记进日志，不需要暴露在界面上。
    """
    name = type(exc).__name__
    if name == "PackageNotFoundError" or isinstance(exc, zipfile.BadZipFile):
        return (
            "文件不是有效的 DOCX 结构（可能只是改了扩展名），"
            "请用 Word/WPS 打开确认后另存为 .docx 再上传"
        )
    if name == "KeyError" and is_docx:
        # 实测：宏文档 / 空 docx 在 python-docx 里都表现为 KeyError（缺必要的包部件）
        return "文档缺少必要的内容结构（可能是空文档或宏文档），无法解析"
    if isinstance(exc, MemoryError):
        return "文件内容过大，解析时内存不足，请精简后重试"
    return "解析出现内部错误，请稍后重试；若反复出现请换一份文件"


async def _parse_resume(resume_id: int, path: Path) -> None:
    """后台解析：提块 → 还原顺序 → 脱敏 → 分段 → LLM 抽取 → 入库。

    任何失败都必须落库成 failed 并写明原因，否则前端会一直停在 parsing。

    同步的重活（PDF 解析、OCR 推理）用 to_thread 丢到线程池：这些是 CPU 密集操作，
    直接放在协程里会把事件循环卡住，导致并发的状态查询请求一起挂起。
    """
    try:
        with get_pool().connection() as conn:
            repo.set_resume_status(conn, resume_id, "parsing")
            conn.commit()

        # DOCX 与 PDF 走两条不同的提取链路：前者有序段落，后者要还原版面
        is_docx = path.suffix.lower() == ".docx"
        if is_docx:
            blocks, source_kind = await asyncio.to_thread(extract_docx_blocks, path)
        else:
            blocks, source_kind = await asyncio.to_thread(extract_blocks, path)

        if source_kind == "scanned":
            # 扫描件走 OCR，与文本层共用同一套坐标与阅读顺序还原
            blocks = await asyncio.to_thread(ocr_blocks, path)
            if not blocks:
                raise PermanentError(
                    "扫描件没有识别出任何文字，请确认图片清晰度或换用文本层 PDF"
                )

        ordered = restore_reading_order(blocks)

        # 脱敏必须在送 LLM 之前，且只对首块做姓名识别
        removed: list[str] = []
        for index, block in enumerate(ordered):
            cleaned, hits = sanitize(block.text, detect_name=(index == 0))
            block.text = cleaned
            removed.extend(hits)

        chunks = chunk_blocks(ordered)
        if not chunks:
            raise PermanentError("这份 PDF 没有可抽取的文字内容")

        profile, meta = await extract_profile(chunks)
        source_text = "\n".join(block.text for block in ordered)

        # 质量自评 + 自主换策略重试。
        # 分段抽取最常见的失败是"安静地抽少了"（学历没抽到、技能全丢），
        # 系统照样返回成功，用户却只看到莫名其妙的匹配结果。
        # 这里自己先看一眼，差就换策略重来，最多两次。
        quality = assess(profile, source_text)
        attempts = [
            {"strategy": "segments", "score": quality.score, "ok": quality.ok,
             "detail": quality.summary}
        ]
        if not quality.ok:
            logger.info("简历 %s 首轮抽取质量不足：%s", resume_id, quality.summary)
            for alt in ("whole", "strict"):
                try:
                    alt_profile, alt_meta = await extract_profile(chunks, strategy=alt)
                except Exception as exc:  # noqa: BLE001 - 换策略失败不放弃整份简历
                    logger.warning("简历 %s 换策略 %s 失败：%s", resume_id, alt, exc)
                    attempts.append({"strategy": alt, "error": str(exc)[:120]})
                    continue
                alt_quality = assess(alt_profile, source_text)
                attempts.append(
                    {"strategy": alt, "score": alt_quality.score, "ok": alt_quality.ok,
                     "detail": alt_quality.summary}
                )
                if alt_quality.score > quality.score:
                    profile, meta, quality = alt_profile, alt_meta, alt_quality
                if quality.ok:
                    break

        meta["quality"] = quality.as_dict()
        meta["attempts"] = attempts

        # 页数从块坐标反推：page_no 从 0 递增，最大值 +1 即总页数
        page_count = max((b.page_no for b in ordered), default=0) + 1

        with get_pool().connection() as conn:
            repo.replace_blocks(conn, resume_id, ordered)
            repo.set_page_count(conn, resume_id, page_count)
            repo.save_profile(conn, resume_id, profile, raw_extract=meta)
            repo.set_resume_status(conn, resume_id, "done", source_kind=source_kind)
            conn.commit()

        logger.info(
            "简历 %s 解析完成：%d 块 / %d 段 / ¥%.4f，脱敏 %d 处，质量 %s",
            resume_id,
            len(ordered),
            meta["chunks"],
            meta["cost_cny"],
            len(removed),
            quality.summary,
        )
    except AppError as exc:
        _mark_failed(resume_id, exc.user_message)
    except Exception as exc:  # 后台任务里未捕获的异常不会有人看到，必须落库
        logger.exception("简历 %s 解析出现未预期异常", resume_id)
        _mark_failed(resume_id, _friendly_parse_error(exc, is_docx))


def _mark_failed(resume_id: int, message: str) -> None:
    logger.error("简历 %s 解析失败：%s", resume_id, message)
    try:
        with get_pool().connection() as conn:
            repo.set_resume_status(conn, resume_id, "failed", error=message)
            conn.commit()
    except Exception:
        # 连数据库都写不进去时只能记日志，不能在这里再抛，否则异常链会掩盖原始错误
        logger.exception("简历 %s 的失败状态写入数据库也失败了", resume_id)


@router.get("")
async def list_resumes(
    # 与 list_jobs 同理：负数 limit 会让 PostgreSQL 直接报错
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """简历列表，供界面选择。主简历排在最前并带 is_default 标记。"""
    with get_pool().connection() as conn:
        items = repo.list_resumes(conn, limit)
    return {"count": len(items), "items": items}


@router.put("/{resume_id}/default")
async def set_default_resume(resume_id: int) -> dict:
    """把这份简历设为主简历：匹配/建议/定制/HR 回复全部默认用它。

    主简历机制是为了根治「扩展取最新一份、结果用了示例简历」的问题。
    """
    with get_pool().connection() as conn:
        ok = repo.set_default_resume(conn, resume_id)
    if not ok:
        raise HTTPException(status_code=404, detail="简历不存在")
    return {"resume_id": resume_id, "is_default": True}


@router.get("/{resume_id}")
async def get_resume(resume_id: int) -> dict:
    """画像 + 抽取用的原文。

    带上 text 是有意为之：用户点开简历时既能看结构化字段，也能看系统"读到了什么"。
    没有这个，字段抽错了只能靠猜是版面问题还是模型问题。
    """
    with get_pool().connection() as conn:
        detail = repo.get_resume_detail(conn, resume_id)
        if detail is not None:
            detail["text"] = repo.get_resume_ordered_text(conn, resume_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="简历不存在")
    return detail


@router.get("/{resume_id}/file")
async def get_resume_file(resume_id: int) -> FileResponse:
    """返回原始 PDF，供界面直接预览。

    用 inline 而不是 attachment：用户点"查看原件"是要在浏览器里看，
    不是下载到本地文件夹。
    """
    with get_pool().connection() as conn:
        file_path = repo.get_resume_file_path(conn, resume_id)
    path = Path(file_path) if file_path else None
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="原始文件已不存在（可能已被删除）")
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="resume-{resume_id}.pdf"'},
    )


@router.post("/{resume_id}/reparse")
async def reparse_resume(resume_id: int, background: BackgroundTasks) -> dict:
    """按当前的 prompt 与模型版本重新解析。

    抽取 schema 变更（例如新增字段）后必须能重跑，否则旧画像是死数据。
    这里不删除原画像：新画像写入时会把旧记录置为 is_current=false，
    保留历史便于对比"改 prompt 前后差在哪"。
    """
    with get_pool().connection() as conn:
        file_path = repo.get_resume_file_path(conn, resume_id)
        if file_path is None:
            raise HTTPException(status_code=404, detail="简历不存在")
        repo.set_resume_status(conn, resume_id, "pending", error=None)
        conn.commit()

    background.add_task(_parse_resume, resume_id, Path(file_path))
    return {"resume_id": resume_id, "status": "pending"}


@router.delete("/{resume_id}")
async def delete_resume(resume_id: int) -> dict:
    """彻底删除一份简历：数据库记录 + 磁盘原文 + 阶段快照。

    为什么必须显式删磁盘文件：数据库侧的块与画像有 ON DELETE CASCADE，
    但原始 PDF 在 upload_dir 里，删库不会动它。用户以为"删了"，
    最敏感的那份文件却还留着 —— 这是记忆功能里最容易被忽略的一半。

    删除顺序：先删库、再删文件。反过来一旦删文件后删库失败，
    会留下指向不存在文件的记录，比留个孤儿文件更难处理。
    """
    with get_pool().connection() as conn:
        file_path = repo.get_resume_file_path(conn, resume_id)
        if file_path is None:
            raise HTTPException(status_code=404, detail="简历不存在")
        conn.execute("DELETE FROM resume WHERE id = %s", (resume_id,))
        # 阶段快照没有外键约束（它按 trace 维度记录，不跟业务表绑定），需手动清理
        conn.execute(
            "DELETE FROM stage_snapshot WHERE subject_type = 'resume' AND subject_id = %s",
            (resume_id,),
        )
        conn.commit()

    path = Path(file_path)
    removed_file = False
    if path.exists():
        path.unlink()
        removed_file = True
    logger.info("已删除简历 %s（磁盘文件%s）", resume_id, "已删" if removed_file else "本就不存在")
    return {"resume_id": resume_id, "deleted": True, "file_removed": removed_file}
