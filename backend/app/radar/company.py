"""公司画像：把「这家厂值不值得去」从模糊感觉变成可解释的分数。

为什么单独一层，而不是继续往 job_profile 里塞字段
    用户判断「美云智数适合我」的四个理由——在佛山、不是互联网大厂、不卡学历、
    是美的子公司——全部是公司级事实。它们对同一家公司的所有岗位都成立，
    存在岗位级意味着每来一个新岗位就要重新猜一遍，而且大概率猜得不一致。

为什么用规则打分而不是让模型评
    两个原因：
    ① 稳定。同一家公司今天 72 分、明天 65 分，用户会不相信这个数。
    ② 可解释。分数必须能拆成「母公司背书 +10 / 制造业方向 +12 / 外包 -32」
       这样的人话，否则用户没法判断该不该信。模型给的分拆不出来。
    模型只用于一件规则做不到的事：从 JD 文本里认出这是哪家公司（抽取阶段已做）。

一个刻意的克制
    预置数据只写能公开核实的事实（母公司、城市、是否上市），
    「卡不卡学历」这类判断不写死，只从该公司历史 JD 的学历要求比例去统计。
    编一条假的学历门槛，比留空更糟——用户会信。
"""

from __future__ import annotations

import logging
import unicodedata

import psycopg
from psycopg.types.json import Jsonb

from ..normalize.fuzzy import norm_key as _norm
from .seed_companies import SEED_COMPANIES

logger = logging.getLogger(__name__)

# 行业对"这个用户"的友好度。
# 口径不是"行业好不好"，而是"双非本科 + 佛山 + 找实习/校招，进这个行业的
# 性价比高不高"。所以工业软件/制造业数字化最高（要项目经历、不卷学历），
# 纯互联网 0（简历筛得最狠），外包负分（去了简历贬值）。
INDUSTRY_BONUS: dict[str, float] = {
    "工业软件": 14,
    "工业互联网": 14,
    "产业互联网": 10,
    "家电": 12,
    "装备制造": 12,
    "机器人": 12,
    "建材": 8,
    "食品": 8,
    "汽车": 8,
    "新能源汽车": 8,
    "物流科技": 10,
    "环保": 8,
    "半导体": 8,
    "照明": 6,
    "通信设备": 6,
    "通信/ICT": 4,
    "金融科技": 6,
    "云计算": 8,
    "轨交信息化": 8,
    "轨交": 8,
    "智能硬件": 6,
    "物业": 4,
    "互联网": 0,
    "电商": -2,
    "游戏": -6,
    "IT外包": -30,
    "金融IT": -20,
}

NATURE_BONUS: dict[str, float] = {
    "上市国企": 9,
    "国企": 8,
    "港股上市": 6,
    "上市民企": 6,
    "上市": 6,
    "外企": 3,
    "合资": 3,
    "子公司": 2,
    "民企": 0,
}

# 分档阈值。78/62/45 是照着预置数据调出来的：制造业上市子公司落在 80 上下，
# 互联网大厂 58~62，外包 20~30。改阈值前先跑 scripts/check_radar.py
TIER_T0 = 78
TIER_T1 = 62
TIER_T2 = 45


def company_key(name: str | None) -> str | None:
    """公司归一键。空值返回 None，让调用方把"没有公司信息"当成一种状态。"""
    if not name:
        return None
    key = _norm(unicodedata.normalize("NFKC", name))
    return key or None


def ensure_seed(conn: psycopg.Connection) -> int:
    """把预置公司写进库。已存在的不覆盖 —— 用户改过的内容比预置值权威。

    返回新插入的条数。幂等，每次启动都能跑。
    """
    rows = conn.execute("SELECT norm_key FROM company").fetchall()
    existing = {r[0] for r in rows}
    added = 0
    for item in SEED_COMPANIES:
        key = company_key(item["name"])
        if not key or key in existing:
            continue
        conn.execute(
            """
            INSERT INTO company
                (name, norm_key, parent, hq_city, industry, nature, scale,
                 is_big_tech, is_outsourcing, campus_channel, edu_note, note,
                 tags, aliases, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'seed')
            """,
            (
                item["name"],
                key,
                item.get("parent"),
                item.get("hq_city"),
                item.get("industry"),
                item.get("nature"),
                item.get("scale"),
                bool(item.get("is_big_tech", False)),
                bool(item.get("is_outsourcing", False)),
                item.get("campus_channel"),
                item.get("edu_note"),
                item.get("note"),
                Jsonb(item.get("tags", [])),
                Jsonb(item.get("aliases", [])),
            ),
        )
        existing.add(key)
        added += 1
    if added:
        conn.commit()
        logger.info("公司画像预置：新增 %d 家", added)
    return added


def load_all(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, name, norm_key, parent, hq_city, industry, nature, scale,
               is_big_tech, is_outsourcing, campus_channel, edu_note, note,
               tags, aliases, source
          FROM company ORDER BY id
        """
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(r) -> dict:
    return {
        "id": int(r[0]),
        "name": r[1],
        "norm_key": r[2],
        "parent": r[3],
        "hq_city": r[4],
        "industry": r[5],
        "nature": r[6],
        "scale": r[7],
        "is_big_tech": r[8],
        "is_outsourcing": r[9],
        "campus_channel": r[10],
        "edu_note": r[11],
        "note": r[12],
        "tags": r[13] or [],
        "aliases": r[14] or [],
        "source": r[15],
    }


def build_index(companies: list[dict]) -> dict[str, dict]:
    """归一键 → 公司。别名也进索引，让「美的」和「美的集团」落到同一条。"""
    index: dict[str, dict] = {}
    for c in companies:
        index[c["norm_key"]] = c
    for c in companies:
        for alias in c.get("aliases") or []:
            key = company_key(alias)
            if key:
                index.setdefault(key, c)
    return index


def resolve(index: dict[str, dict], name: str | None) -> dict | None:
    """按公司名找画像。只做精确与别名命中，不做模糊匹配。

    模糊匹配在这里的收益低于风险：「广州地铁」与「广州地铁设计院」是两个法人，
    模糊匹上会把甲方和乙方混成一个。宁可认不出，走"未知"分支。
    """
    key = company_key(name)
    if not key:
        return None
    return index.get(key)


def learn(conn: psycopg.Connection, index: dict[str, dict], name: str | None) -> dict | None:
    """没见过的公司：建一条 inferred 记录，只记名字。

    为什么不顺手猜行业：猜错的行业会直接影响推荐排序，而用户没有渠道发现
    "为什么这家公司被打了低分"。留空 + 显示"信息不足"，用户还能自己补。
    """
    key = company_key(name)
    if not key:
        return None
    if key in index:
        return index[key]
    row = conn.execute(
        """
        INSERT INTO company (name, norm_key, source)
        VALUES (%s, %s, 'inferred')
        ON CONFLICT (norm_key) DO UPDATE SET updated_at = now()
        RETURNING id, name, norm_key, parent, hq_city, industry, nature, scale,
                  is_big_tech, is_outsourcing, campus_channel, edu_note, note,
                  tags, aliases, source
        """,
        (name, key),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    record = _row_to_dict(row)
    index[key] = record
    logger.info("公司画像：新记录 %s（未知属性）", name)
    return record


def score(company: dict | None, stats: dict | None = None) -> dict:
    """公司优质度。返回分数、分档、以及每一条加减分的理由。

    stats 由调用方从该公司历史 JD 统计：
        {"jobs": 12, "master_ratio": 0.25, "avg_exp": 2.1}
    没有 stats 时不惩罚也不奖励，分档照常 —— 未知不等于差。
    """
    if company is None:
        return {
            "score": 50.0,
            "tier": "T2",
            "label": "未知公司",
            "reasons": ["没有识别出公司，无法评价；补抓详情页通常就能认出来"],
        }

    base = 55.0
    reasons: list[str] = []
    tags = list(company.get("tags") or [])

    if company.get("is_outsourcing"):
        base -= 32
        reasons.append("外包/人力供应商 −32：签的是服务商不是甲方，简历增值低")
    if company.get("is_big_tech"):
        base -= 12
        reasons.append("互联网大厂 −12：简历筛得最狠，双非本科正式岗通过率低")

    industry = company.get("industry")
    if industry:
        bonus = INDUSTRY_BONUS.get(industry, 0)
        if bonus:
            base += bonus
            reasons.append(f"{industry}方向 {bonus:+.0f}")

    nature = company.get("nature")
    if nature:
        bonus = NATURE_BONUS.get(nature, 0)
        if bonus:
            base += bonus
            reasons.append(f"{nature} {bonus:+.0f}")

    if company.get("parent"):
        base += 10
        reasons.append(f"{company['parent']}旗下 +10：集团背书，行业认可度高")
    if company.get("campus_channel") is True:
        base += 5
        reasons.append("有校招/实习通道 +5")
    if (company.get("scale") or "").startswith("10000"):
        base += 3
        reasons.append("万人以上规模 +3：流程正规，实习可转正")

    # 从该公司历史 JD 学到的东西。样本少于 3 条时不动分 —— 两条 JD 的统计
    # 只是噪声，用它调分会让人对一个本来中性的公司产生错误印象
    if stats and stats.get("jobs", 0) >= 3:
        mr = stats.get("master_ratio") or 0
        if mr >= 0.3:
            base -= 8
            reasons.append(f"历史 JD 中 {mr:.0%} 要求硕士 −8")
        elif mr == 0:
            base += 4
            reasons.append("历史 JD 未出现硕士要求 +4")

    score_value = max(0.0, min(100.0, base))
    if score_value >= TIER_T0:
        tier, label = "T0", "首选"
    elif score_value >= TIER_T1:
        tier, label = "T1", "值得投"
    elif score_value >= TIER_T2:
        tier, label = "T2", "保底"
    else:
        tier, label = "X", "慎投"

    if not reasons:
        reasons.append("没有可用的公司属性，按中性处理")

    return {
        "score": round(score_value, 1),
        "tier": tier,
        "label": label,
        "reasons": reasons,
    }
