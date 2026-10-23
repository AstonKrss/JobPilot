"""生成一份结构接近真实的中文两栏简历，用于验证版面还原。

刻意包含三个让简单算法翻车的特征：
  1. 顶部姓名标题横跨全宽 —— 它横跨栏间空白，按中心 x 归栏会把它塞进某一栏
  2. 左窄侧边栏放短标签（技能 / 联系方式 / 语言）—— 中位块长很短
  3. 左右两栏的 y 分布不对齐 —— 侧边栏和正文各自有自己的段落间距

用法：.venv/Scripts/python.exe scripts/make_sidebar_resume.py
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "samples" / "sidebar_resume.pdf"

# 页眉：横跨全宽（x 从 40 起，文字本身较长，bbox 会越过中线）
HEADER = [
    ("李强  高级后端工程师", 40, 62, 19),
    ("六年互联网后端经验 · 专注高并发交易系统 · 期望城市 北京", 40, 92, 10),
]

# 左窄侧边栏
SIDEBAR = [
    ("技能", 40, 130, 12),
    ("Python", 40, 152, 9),
    ("Go", 40, 172, 9),
    ("Kafka", 40, 192, 9),
    ("Redis", 40, 212, 9),
    ("Kubernetes", 40, 232, 9),
    ("联系方式", 40, 272, 12),
    ("13800138000", 40, 294, 9),
    ("liqiang@example.com", 40, 314, 9),
    ("北京", 40, 334, 9),
    ("语言", 40, 374, 12),
    ("英语 CET-6", 40, 396, 9),
]

# 右宽正文
BODY = [
    ("个人简介", 250, 130, 12),
    ("六年互联网后端开发经验，专注高并发交易系统设计。", 250, 152, 9),
    ("主导过日均十亿级请求的网关重构，P99 延迟下降 60%。", 250, 170, 9),
    ("工作经历", 250, 206, 12),
    ("字节跳动 | 后端工程师 | 2019.07 - 2023.06 | 北京", 250, 228, 9),
    ("负责支付核心链路，支撑日均三千万笔交易。", 250, 248, 9),
    ("主导风控引擎重构，规则生效时间从小时级降到分钟级。", 250, 266, 9),
    ("将网关 P99 延迟从 120ms 优化到 45ms。", 250, 284, 9),
    ("阿里巴巴 | 高级后端工程师 | 2023.07 - 至今 | 杭州", 250, 320, 9),
    ("负责交易链路稳定性，主导同城双活改造。", 250, 340, 9),
    ("推动可观测性建设，故障定位时间缩短 70%。", 250, 358, 9),
    ("教育背景", 250, 394, 12),
    ("清华大学 计算机科学与技术 本科 统招 2015.09-2019.06", 250, 416, 9),
    ("项目经历", 250, 452, 12),
    ("分布式任务调度平台", 250, 474, 9),
    ("基于 Kubernetes 与 PostgreSQL 实现任务编排，支撑日均五十万任务。", 250, 492, 9),
]


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for text, x, y, size in HEADER + SIDEBAR + BODY:
        page.insert_text((x, y), text, fontsize=size, fontname="china-s")
    doc.save(OUT)
    doc.close()
    print(f"已生成：{OUT}")
    print(f"  页眉 {len(HEADER)} 行 | 侧边栏 {len(SIDEBAR)} 行 | 正文 {len(BODY)} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
