"""内置来源种子：只放实测通过或来源明确的招聘页，不猜 URL。

内置清单刻意很小（v1 只有实测过的 Moka 站），原因是 honest 的：
    1. seed_companies.py 的 58 家公司没有任何官网 URL 字段，编一个 URL
       就是给未来埋 404；
    2. 用户自己的优先梯队（T0/T1）才是对的抓取清单 —— 发现页的表单
       「公司名 + 招聘页地址」两行就能加一个来源，adapter 自动探测。
    找招聘页地址的方法（README 同步说明）：官网底部的「加入我们/招聘」链接，
    或在搜索引擎里搜「{公司名} 校园招聘 官网」。

首抓失败不丢人：last_status/last_error 会把真实原因（404 / JS 渲染 / 403）
写在来源行上，用户能看到"为什么没抓到"。
"""

from __future__ import annotations

import logging

from ..db.pool import get_pool

logger = logging.getLogger(__name__)

SEED_SOURCES = [
    {
        "name": "锐捷网络招聘",
        "url": "https://app.mokahr.com/apply/ruijie/26518",
        "note": "Moka ATS；2026-11-08 实测探针通过（init-data 内嵌 15 条，站内共 659 条）",
    },
    {
        "name": "边锋网络招聘",
        "url": "https://app.mokahr.com/apply/bianfeng/47489",
        "note": "Moka ATS；官方渠道公示的网申地址，首抓时自动探测",
    },
    {
        "name": "空中云汇招聘",
        "url": "https://app.mokahr.com/apply/kongzhongyunhui/2265",
        "note": "Moka ATS；首抓时自动探测",
    },
]


def ensure_seed_sources(conn) -> int:
    """幂等写入内置来源。按 url 去重：用户删过/改过的来源不回填。返回本次新增数。"""
    inserted = 0
    for seed in SEED_SOURCES:
        row = conn.execute(
            "SELECT id FROM discover_source WHERE url = %s", (seed["url"],)
        ).fetchone()
        if row:
            continue
        conn.execute(
            "INSERT INTO discover_source (name, url, adapter, note)"
            " VALUES (%s, %s, 'auto', %s)",
            (seed["name"], seed["url"], seed["note"]),
        )
        inserted += 1
    if inserted:
        conn.commit()
        logger.info("内置发现来源已写入 %d 条", inserted)
    return inserted
