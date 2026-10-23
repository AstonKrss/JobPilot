"""PDF 文本层提取。

职责边界：只负责「从文件里取出带坐标的文本块」，并判断这份文件能不能读到字。
阅读顺序还原在 layout.py，字段抽取在 extract.py，本模块不掺和这两件事。

为什么先判文本层再决定后续分支：扫描件读出来是空的，若直接把空文本送进 LLM，
模型会基于文件名和上下文"合理编造"出一份简历 —— 这类幻觉比报错危险得多。
"""

from __future__ import annotations

import logging
from pathlib import Path

try:  # PyMuPDF 1.24+ 暴露 pymupdf 顶层模块；旧版只有 fitz
    import pymupdf
except ImportError:  # pragma: no cover - 兼容旧版本
    import fitz as pymupdf  # type: ignore[no-redef]

from ..llm.errors import PermanentError
from .layout import Block

logger = logging.getLogger(__name__)

# 少于这么多字符就当作没有文本层。取 20 是给"只写了姓名"的极端封面页留余地，
# 同时能挡住纯图片 PDF（通常只有页码或 0 字符）
MIN_TEXT_CHARS = 20


def extract_blocks(path: Path) -> tuple[list[Block], str]:
    """提取文本块。

    返回 (blocks, source_kind)：
        source_kind = "text_layer" → 正常文本层，blocks 可用
        source_kind = "scanned"    → 无文本层，blocks 为空，调用方应转 OCR
    永久性错误（加密、非 PDF、文件损坏）直接抛 PermanentError，不做重试。
    """
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        # 文件层面读不了：损坏、不是 PDF、被占用。重试不会让文件变好
        raise PermanentError(
            f"无法打开文件「{path.name}」，请确认是未损坏的 PDF",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

    try:
        if doc.needs_pass:
            raise PermanentError(
                f"「{path.name}」有密码保护，无法读取内容。请导出无密码版本后重试",
                detail=f"encrypted pdf, page_count={doc.page_count}",
            )

        blocks: list[Block] = []
        total_chars = 0
        for page_no, page in enumerate(doc):
            page_dict = page.get_text("dict")
            for index, raw in enumerate(page_dict.get("blocks", [])):
                if raw.get("type") != 0:  # 1 为图片块，OCR 分支另行处理
                    continue
                # 保留块内换行：同一块里的多行往往是一条完整描述，
                # 压成一行会让后续分段失去语义边界
                parts = [
                    "".join(span.get("text", "") for span in line.get("spans", []))
                    for line in raw.get("lines", [])
                ]
                text = "\n".join(p for p in parts if p.strip()).strip()
                if not text:
                    continue
                # 取第一个有方向的 line 作为整个块的方向（同一块内各行方向一致）。
                # 不读这个字段就无法区分横排文字与旋转 90° 的侧标
                direction: tuple[float, float] = (1.0, 0.0)
                for line in raw.get("lines", []):
                    raw_dir = line.get("dir")
                    if raw_dir:
                        direction = (float(raw_dir[0]), float(raw_dir[1]))
                        break
                x0, y0, x1, y1 = raw.get("bbox", (0.0, 0.0, 0.0, 0.0))
                blocks.append(
                    Block(
                        page_no=page_no,
                        block_index=index,
                        x0=float(x0),
                        y0=float(y0),
                        x1=float(x1),
                        y1=float(y1),
                        text=text,
                        direction=direction,
                    )
                )
                total_chars += len(text)
    finally:
        doc.close()

    if total_chars < MIN_TEXT_CHARS:
        logger.info("「%s」无有效文本层（%d 字符），转 OCR 分支", path.name, total_chars)
        return [], "scanned"

    logger.info("「%s」提取 %d 个文本块，共 %d 字符", path.name, len(blocks), total_chars)
    return blocks, "text_layer"
