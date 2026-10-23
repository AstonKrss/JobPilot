"""生成一份用于链路验证的中文简历 PDF。

刻意采用「左侧公司/职位 + 右侧时间」的版式 —— 这是版面还原最容易出错、
且真实简历里最常见的一种排布。同时放入手机号与邮箱，用于验证脱敏是否生效。

用法：.venv/Scripts/python.exe scripts/make_sample_resume.py
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "samples" / "sample_resume.pdf"

# (文本, x, y, 字号)  —— 右侧时间用大 x 坐标，模拟右对齐
LINES: list[tuple[str, float, float, float]] = [
    ("张伟", 50, 60, 20),
    ("13812345678 | zhangwei@example.com | 北京市海淀区中关村大街1号", 50, 88, 9),
    ("教育背景", 50, 120, 13),
    ("清华大学  计算机科学与技术  本科  统招  2015.09-2019.06", 50, 142, 10),
    ("工作经历", 50, 175, 13),
    ("字节跳动", 50, 197, 11),
    ("2019.07 - 2023.06", 400, 197, 10),
    ("后端工程师", 50, 214, 10),
    ("北京", 400, 214, 10),
    ("- 负责推荐系统后端服务，日均处理请求 2 亿次，P99 延迟从 120ms 降到 45ms", 50, 233, 9),
    ("- 主导特征平台重构，将特征上线周期从 2 周缩短到 1 天", 50, 249, 9),
    ("阿里巴巴", 50, 280, 11),
    ("2023.07 - 至今", 400, 280, 10),
    ("高级后端工程师", 50, 297, 10),
    ("杭州", 400, 297, 10),
    ("- 负责交易链路稳定性，主导同城双活改造", 50, 316, 9),
    ("项目经历", 50, 350, 13),
    ("分布式任务调度平台", 50, 372, 10),
    ("- 基于 Kubernetes 与 PostgreSQL 实现任务编排，支撑日均 50 万任务", 50, 389, 9),
    ("专业技能", 50, 420, 13),
    ("Python / Go / PostgreSQL / Redis / Kubernetes / Kafka", 50, 442, 10),
    ("自我评价", 50, 475, 13),
    ("五年后端开发经验，熟悉高并发系统设计与性能优化，具备跨团队协作经验", 50, 497, 10),
]


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4
    for text, x, y, size in LINES:
        page.insert_text((x, y), text, fontsize=size, fontname="china-s")
    doc.save(OUT)
    doc.close()
    print(f"已生成：{OUT}（{len(LINES)} 行，单页 A4）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
