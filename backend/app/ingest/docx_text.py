"""DOCX 文本提取：把 Word 简历变成带坐标的文本块。

为什么需要单独一条链路
    PDF 的坐标是真实排版坐标，需要几何还原阅读顺序；DOCX 是有序的段落流，
    顺序天然正确。所以这里直接按文档顺序生成 Block，坐标只是占位
    （y 递增），交给同一套分块与抽取逻辑复用。

    这样 DOCX 简历既不用 OCR、也不用版面还原，0.05 秒就能出块，
    且段落边界天然干净 —— 后续"在原文件上改某一段"正依赖这一点。
"""

from __future__ import annotations

import logging
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from .layout import Block

logger = logging.getLogger(__name__)

# 每个段落占的虚拟高度，仅用于让 Block 的排序语义成立
LINE_HEIGHT = 20.0


def _iter_block_items(doc):
    """按文档真实顺序产出段落与表格。

    python-docx 的 doc.paragraphs 会跳过表格，而中文简历常用表格排「教育/工作经历」，
    跳过就会丢内容。所以直接遍历 body 的子元素。
    """
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, doc)
        elif child.tag.endswith("}tbl"):
            yield Table(child, doc)


def extract_docx_blocks(path: Path) -> tuple[list[Block], str]:
    """返回 (文本块列表, 来源类型)。来源类型固定为 'docx'。"""
    doc = Document(str(path))
    blocks: list[Block] = []
    index = 0

    for item in _iter_block_items(doc):
        if isinstance(item, Paragraph):
            text = (item.text or "").strip()
            if not text:
                continue
            # 样式名为空时不猜，统一记 text；抽取阶段只看文本内容
            style = item.style.name if item.style is not None else ""
            blocks.append(
                Block(
                    page_no=0,
                    block_index=index,
                    x0=0.0,
                    y0=index * LINE_HEIGHT,
                    x1=500.0,
                    y1=(index + 1) * LINE_HEIGHT,
                    text=text,
                    block_type="heading" if style.lower().startswith("heading") else "text",
                )
            )
            index += 1
            continue

        # 表格：逐行拼成一段，保留"单元格之间用分隔符"的语义
        for row in item.rows:
            cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
            text = " | ".join(c for c in cells if c)
            if not text:
                continue
            blocks.append(
                Block(
                    page_no=0,
                    block_index=index,
                    x0=0.0,
                    y0=index * LINE_HEIGHT,
                    x1=500.0,
                    y1=(index + 1) * LINE_HEIGHT,
                    text=text,
                    block_type="table",
                )
            )
            index += 1

    logger.info("DOCX 提取：%d 块", len(blocks))
    return blocks, "docx"
