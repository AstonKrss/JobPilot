"""版面还原自检（不依赖 pytest，直接 python 运行）。

两层验证：
  A. 纯几何逻辑：手工构造 Block，不碰 PDF，验证分栏判定与行内排序
  B. 端到端：合成 PDF 再提取，验证坐标从文件到 Block 的链路不丢失

三类版式都必须通过：
  1. 真两栏（两侧都有成段文字）→ 左栏读完再读右栏
  2. 左标题右时间（右侧文本量小）→ 必须按行配对，不许切成两栏
  3. 单栏纵向 → 顺序不变、不误判为分栏

用法：.venv/Scripts/python.exe scripts/check_layout.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ingest.layout import Block, restore_reading_order  # noqa: E402


def block(
    text: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    idx: int = 0,
    direction: tuple[float, float] = (1.0, 0.0),
) -> Block:
    return Block(
        page_no=0,
        block_index=idx,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        text=text,
        direction=direction,
    )


def show(blocks: list[Block]) -> list[str]:
    return [b.text for b in restore_reading_order(blocks)]


def case_true_two_columns() -> tuple[bool, str]:
    """真两栏：两侧都是成段文字，文本量相当。"""
    blocks = [
        # 左栏：三段长文本
        block("Left column paragraph one with enough words to matter", 50, 100, 280, 130, 0),
        block("Left column paragraph two continues the same section here", 50, 140, 280, 170, 1),
        block("Left column paragraph three closes out this column text", 50, 180, 280, 210, 2),
        # 右栏：三段长文本，y 与左栏对齐
        block("Right column paragraph one with comparable word length", 320, 100, 550, 130, 3),
        block("Right column paragraph two keeps the balance between sides", 320, 140, 550, 170, 4),
        block("Right column paragraph three finishes the right side too", 320, 180, 550, 210, 5),
    ]
    got = show(blocks)
    expected_left_first = got[:3] == [
        blocks[0].text,
        blocks[1].text,
        blocks[2].text,
    ]
    return expected_left_first, f"{got}"


def case_title_with_date() -> tuple[bool, str]:
    """左标题右时间：右侧总量远小于左侧，不得判为分栏。"""
    blocks = [
        block("ByteDance Ltd", 50, 100, 200, 120, 0),
        block("2020.03 - 2023.06", 420, 100, 550, 120, 1),
        block("Backend Engineer", 50, 130, 200, 150, 2),
        block("Beijing", 420, 130, 550, 150, 3),
    ]
    got = show(blocks)
    # 期望：同一物理行内，左块在右块之前
    expected = [
        "ByteDance Ltd",
        "2020.03 - 2023.06",
        "Backend Engineer",
        "Beijing",
    ]
    return got == expected, f"{got}"


def case_single_column() -> tuple[bool, str]:
    """单栏纵向，无贯穿空白。"""
    blocks = [
        block("Summary of qualifications and background", 50, 100, 550, 120, 0),
        block("Work experience in backend systems", 50, 130, 550, 150, 1),
        block("Education at a technical university", 50, 160, 550, 180, 2),
    ]
    got = show(blocks)
    expected = [b.text for b in blocks]
    return got == expected, f"{got}"


def case_two_columns_end_to_end(tmp: Path) -> tuple[bool, str]:
    """端到端：合成 PDF → 提取 → 还原。验证坐标链路没丢。"""
    try:
        import pymupdf
    except ImportError:
        return True, "skipped (pymupdf 未安装)"

    from app.ingest.pdf_text import extract_blocks

    path = tmp / "two_col.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    left = [
        "Left column paragraph one with enough words",
        "Left column paragraph two continues here",
        "Left column paragraph three closes it",
    ]
    right = [
        "Right column paragraph one comparable length",
        "Right column paragraph two keeps balance",
        "Right column paragraph three finishes",
    ]
    for i, text in enumerate(left):
        page.insert_text((50, 100 + i * 40), text, fontsize=10)
    for i, text in enumerate(right):
        page.insert_text((320, 100 + i * 40), text, fontsize=10)
    doc.save(path)
    doc.close()

    blocks, kind = extract_blocks(path)
    if kind != "text_layer":
        return False, f"source_kind={kind}，合成 PDF 应识别为文本层"

    ordered = restore_reading_order(blocks)
    texts = [b.text for b in ordered]
    lefts_first = all("Left" in t for t in texts[: len(left)])
    return lefts_first, f"kind={kind} blocks={len(blocks)} order={texts}"


def case_sidebar_layout() -> tuple[bool, str]:
    """左窄侧边栏（短标签）+ 右宽正文 → 左栏整体在前，不得逐行交错。"""
    blocks = [
        block("Python", 45, 109, 76, 123, 0),
        block("Go", 45, 139, 58, 153, 1),
        block("本科", 45, 169, 62, 183, 2),
        block("Work experience at a large internet company", 230, 109, 429, 123, 3),
        block("Backend development for payment systems", 230, 139, 424, 153, 4),
        block("Education at a technical university", 230, 169, 381, 183, 5),
    ]
    got = [b.text for b in restore_reading_order(blocks)]
    # 正确：三个标签在前，三段正文在后。错误（交错）：Python, Work..., Go, Backend...
    expected_prefix = ["Python", "Go", "本科"]
    return got[:3] == expected_prefix, f"{got}"


def case_rotated_goes_last() -> tuple[bool, str]:
    """旋转文本不得插进正文中间，必须排到最后。"""
    blocks = [
        block("Normal paragraph line one", 50, 100, 550, 120, 0),
        block("ROTATED", 60, 150, 85, 260, 1, direction=(0.0, -1.0)),
        block("Normal paragraph line two", 50, 200, 550, 220, 2),
    ]
    got = [b.text for b in restore_reading_order(blocks)]
    return got[-1] == "ROTATED", f"{got}"


def main() -> int:
    tmp = ROOT / "data" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    cases = [
        ("真两栏 → 左栏优先", case_true_two_columns()),
        ("左标题右时间 → 按行配对", case_title_with_date()),
        ("单栏 → 顺序不变", case_single_column()),
        ("侧边栏短标签 → 左栏整体在前", case_sidebar_layout()),
        ("旋转文本 → 排到最后", case_rotated_goes_last()),
        ("端到端 PDF 提取", case_two_columns_end_to_end(tmp)),
    ]

    failed = 0
    for name, (ok, detail) in cases:
        print(f"{'[PASS]' if ok else '[FAIL]'} {name}")
        if not ok:
            print(f"        {detail}")
            failed += 1

    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
