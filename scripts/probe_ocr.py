"""探测 PaddleOCR 3.7 的真实返回结构。

为什么不直接照着文档写：3.x 的 predict() 返回的是 OCRResult 对象，
键名（rec_texts / dt_polys / rec_scores 等）在 3.0 → 3.7 之间有过调整。
先跑一次把真实键名、类型、坐标格式打印出来，再据此实现 ocr.py，
避免按记忆写出不存在的字段名然后靠猜去修。

用法：.venv/Scripts/python.exe scripts/probe_ocr.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import pymupdf


def make_probe_image() -> Path:
    """用 PyMuPDF 渲染一张带中文的简历片段，作为 OCR 输入。"""
    tmp = ROOT / "data" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    lines = [
        ("张三的简历", 18),
        ("教育背景", 14),
        ("清华大学 计算机科学与技术 2019-2023", 11),
        ("工作经历", 14),
        ("字节跳动 后端工程师 2023.07-至今", 11),
    ]
    y = 80
    for text, size in lines:
        page.insert_text((50, y), text, fontsize=size, fontname="china-s")
        y += 34

    img_path = tmp / "probe.png"
    page.get_pixmap(dpi=200).save(str(img_path))
    doc.close()
    print(f"probe image: {img_path}")
    return img_path


def main() -> int:
    img_path = make_probe_image()

    from paddleocr import PaddleOCR

    print("初始化 PaddleOCR（首次运行会下载模型，可能耗时数分钟）...")
    ocr = PaddleOCR(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )
    results = ocr.predict(str(img_path))

    print(f"\n返回类型: {type(results)} 长度: {len(results)}")
    for i, res in enumerate(results):
        print(f"\n--- item[{i}] type={type(res)} ---")
        try:
            keys = list(res.keys())
        except AttributeError:
            print("  (无 keys 方法) dir:", [a for a in dir(res) if not a.startswith("_")][:30])
            continue
        print("  keys:", keys)
        for key in keys:
            value = res[key]
            preview = str(value)
            if len(preview) > 300:
                preview = preview[:300] + "..."
            print(f"  · {key}: {type(value).__name__} = {preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
