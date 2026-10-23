"""扫描件 OCR（PaddleOCR 3.7，本机 CPU 推理）。

必须知道的一个坑
    PaddlePaddle 3.3.1 的 oneDNN 后端在 CPU 上加载 PP-OCRv6 模型时会抛
    `ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>]`。
    实测 enable_mkldnn=False 可绕过（这是本机实测结论，非猜测）。代价是推理变慢，
    对简历这种单页、低频的场景可以接受 —— 换来的是不必降级 Paddle 版本。

坐标量纲必须统一
    OCR 在渲染图（200 DPI）的像素坐标里工作，文本层在 PDF point（72 DPI）里工作。
    这里统一换算回 point，让两条输入路径共用同一套阅读顺序还原逻辑与容差参数。
    否则两边各调一遍阈值，行为分叉且难以对比。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
import pymupdf

from .layout import Block

logger = logging.getLogger(__name__)

# 渲染精度：200 DPI 对小五号字（约 9pt）足够，再高收益有限而耗时线性上升
RENDER_DPI = 200
# 低于此置信度的识别结果直接丢弃：简历里主要是印刷体，低分项基本是噪点或水印
MIN_REC_SCORE = 0.5
PDF_POINT_DPI = 72.0

_engine = None
_engine_lock = threading.Lock()
# predict 不是线程安全的，同一个引擎实例同时被两个请求调用会串结果
_predict_lock = threading.Lock()


def _get_engine():
    """懒加载单例。模型加载要几秒，不能在每次请求里重建。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from paddleocr import PaddleOCR

                _engine = PaddleOCR(
                    use_doc_orientation_classify=False,  # 简历不存在整页旋转问题
                    use_doc_unwarping=False,             # 不做曲面矫正，多一步只会更慢
                    use_textline_orientation=False,      # 不做逐行方向分类
                    enable_mkldnn=False,                 # 见模块文档：绕过 oneDNN 崩溃
                )
                logger.info("PaddleOCR 引擎已加载（PP-OCRv6, CPU, oneDNN 关闭）")
    return _engine


def _to_bgr(pix: pymupdf.Pixmap) -> np.ndarray:
    """PyMuPDF 给的是 RGB，PaddleOCR 内部按 BGR 处理，需转换。"""
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:  # 带 alpha 通道
        img = img[:, :, :3]
    return np.ascontiguousarray(img[:, :, ::-1])


def ocr_blocks(path: Path, *, dpi: int = RENDER_DPI) -> list[Block]:
    """把 PDF 逐页渲染后 OCR，返回与文本层同构的 Block 列表。

    单页失败（如某页渲染异常）不影响其他页：记录 warning 后继续。
    整份都失败则返回空列表，由调用方按 PermanentError 处理。
    """
    engine = _get_engine()
    scale = PDF_POINT_DPI / dpi

    blocks: list[Block] = []
    doc = pymupdf.open(path)
    try:
        for page_no, page in enumerate(doc):
            try:
                pix = page.get_pixmap(dpi=dpi)
                image = _to_bgr(pix)
            except Exception as exc:
                logger.warning("第 %d 页渲染失败，跳过：%s", page_no + 1, exc)
                continue

            with _predict_lock:
                results = engine.predict(image)
            if not results:
                continue

            res = results[0]
            texts = res["rec_texts"]
            boxes = res["rec_boxes"]
            scores = res["rec_scores"]

            for index, (text, box, score) in enumerate(zip(texts, boxes, scores)):
                text = (text or "").strip()
                if not text or float(score) < MIN_REC_SCORE:
                    continue
                # rec_boxes 为 [x_min, y_min, x_max, y_max] 像素坐标，换算回 PDF point
                x0, y0, x1, y1 = (float(v) * scale for v in box[:4])
                blocks.append(
                    Block(
                        page_no=page_no,
                        block_index=index,
                        x0=x0,
                        y0=y0,
                        x1=x1,
                        y1=y1,
                        text=text,
                        block_type="text",
                    )
                )
            logger.info("第 %d 页 OCR 识别 %d 条有效文本", page_no + 1, len(texts))
    finally:
        doc.close()

    return blocks
