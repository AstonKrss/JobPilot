"""探测：竖排文字在当前版面还原下的实际表现。

背景问题：如果简历左侧是竖着读的（比如设计类简历的竖排标题或侧边栏），
右侧是正常横排，现有的「x 找空白分栏 + 栏内按 y 聚行」算法能不能正确处理？

三种版式对照：
  A. 纯横排（基线，确认不算错）
  B. 左侧逐字竖排 + 右侧横排（中文竖排的常见实现：每字独立文本对象，x 固定 y 递增）
  C. 左侧旋转 90° 文本框 + 右侧横排（设计工具导出 PDF 的常见实现）

关键观察点：PyMuPDF 的 line 结构里有 "dir" 字段（方向向量），
横排是 (1,0)，旋转文本会反映在 dir 上。当前实现没有读这个字段。

用法：.venv/Scripts/python.exe scripts/probe_vertical.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import pymupdf

from app.ingest.layout import restore_reading_order
from app.ingest.pdf_text import extract_blocks

TMP = ROOT / "data" / "tmp"
TMP.mkdir(parents=True, exist_ok=True)


def make_vertical_by_char(path: Path) -> None:
    """左侧逐字竖排：「个人简历」四字，x 固定，y 递增。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    # 左侧竖排标题，每字一个文本对象
    for i, ch in enumerate("个人简历"):
        page.insert_text((60, 100 + i * 26), ch, fontsize=16, fontname="china-s")
    # 右侧正常横排的多行正文（长度足够，避免被判定为"短语型布局"）
    lines = [
        "Work experience at a large internet company",
        "Backend development for payment systems",
        "Education at a technical university",
        "Skills include Go and PostgreSQL and Kafka",
    ]
    for i, text in enumerate(lines):
        page.insert_text((200, 440 + i * 34), text, fontsize=11)
    doc.save(path)
    doc.close()


def make_rotated(path: Path) -> None:
    """左侧旋转 90° 的文本块（设计类简历常见）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((80, 700), "RESUME 2026", fontsize=16, rotate=90)
    lines = [
        "Work experience at a large internet company",
        "Backend development for payment systems",
        "Education at a technical university",
        "Skills include Go and PostgreSQL and Kafka",
    ]
    for i, text in enumerate(lines):
        page.insert_text((200, 440 + i * 34), text, fontsize=11)
    doc.save(path)
    doc.close()


def dump(path: Path, title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")
    blocks, kind = extract_blocks(path)
    print(f"source_kind={kind}  blocks={len(blocks)}")

    # 原始块信息：这一层能看到 dir 字段，是判断竖排的关键
    doc = pymupdf.open(path)
    page = doc[0]
    raw = page.get_text("dict")
    for bi, blk in enumerate(raw.get("blocks", [])):
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", []))
            if not text.strip():
                continue
            print(f"  raw block[{bi}] dir={line.get('dir')} bbox={[round(v) for v in line['bbox']]} | {text[:36]}")
    doc.close()

    ordered = restore_reading_order(blocks)
    print("\n  还原后的阅读顺序：")
    for b in ordered:
        print(f"    [{b.read_order}] x={b.cx:5.0f} y={b.cy:5.0f} | {b.text[:40]}")


def make_sidebar(path: Path) -> None:
    """左窄侧边栏（技能标签/联系方式）+ 右宽正文。

    这是中文两栏简历最典型的形态，也是「中位块长 ≥30 才分栏」这条判据
    最可能误判的场景：左栏全是短标签，中位长度只有 3~5 字。
    """
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    sidebar = ["Python", "Go", "Kafka", "Redis", "5 年经验", "本科", "北京"]
    for i, text in enumerate(sidebar):
        page.insert_text((45, 120 + i * 30), text, fontsize=10)
    body = [
        "Work experience at a large internet company",
        "Backend development for payment systems",
        "Education at a technical university",
        "Skills include Go and PostgreSQL and Kafka",
    ]
    for i, text in enumerate(body):
        page.insert_text((230, 120 + i * 30), text, fontsize=10)
    doc.save(path)
    doc.close()


def main() -> int:
    a = TMP / "vertical_char.pdf"
    c = TMP / "vertical_rot.pdf"
    d = TMP / "sidebar.pdf"
    make_vertical_by_char(a)
    make_rotated(c)
    make_sidebar(d)
    dump(a, "B. 左竖排（逐字）+ 右横排")
    dump(c, "C. 左旋转 90° + 右横排")
    dump(d, "D. 左窄侧边栏（短标签）+ 右宽正文")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
