"""技能自评的落库与跨岗位聚合。

三态语义（对用户展示的文案）
    can_explain   能讲清 —— 被追问原理与权衡也接得住
    used_shallow  用过但不深 —— 项目里调过 API，讲不出原理
    never         没接触

聚合口径（priority）
    全库在架岗位的必备技能 × 已展开条目 × 自评 ≠ can_explain
    → 按「要求这个能力的岗位数」降序。这回答的是"我先学什么"：
      被十个岗位同时要求的 Redis 条目，比只出现一次的冷门框架优先。
    刻意不 join 匹配结果（那是随简历现算的）：聚合的是"市场在要什么"，
    不掺入个人匹配口径 —— 两件事混在一个排序里会互相污染。
"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

ALLOWED_STATUS = ("can_explain", "used_shallow", "never")
STATUS_LABEL = {
    "can_explain": "能讲清",
    "used_shallow": "用过但不深",
    "never": "没接触",
}


def item_hash(item: str) -> str:
    return hashlib.sha256((item or "").strip().encode("utf-8")).hexdigest()


def save_assessments(conn, resume_id: int, rows: list[dict]) -> int:
    """upsert 自评。条目文本不变时同一 hash 覆盖旧状态（自评是"当前水平"，不是历史）。"""
    saved = 0
    for r in rows:
        status = (r.get("status") or "").strip()
        item = (r.get("item") or "").strip()
        if status not in ALLOWED_STATUS or not item:
            continue
        conn.execute(
            """
            INSERT INTO skill_self_assess (resume_id, item_hash, item, skill_norm, status, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (resume_id, item_hash)
            DO UPDATE SET status = EXCLUDED.status, updated_at = now()
            """,
            (resume_id, item_hash(item), item,
             (r.get("skill_norm") or "").strip() or "", status),
        )
        saved += 1
    conn.commit()
    return saved


def load_by_resume(conn, resume_id: int) -> dict[str, dict]:
    """该简历的全部自评。key = item_hash。"""
    rows = conn.execute(
        "SELECT item_hash, item, skill_norm, status FROM skill_self_assess WHERE resume_id = %s",
        (resume_id,),
    ).fetchall()
    return {
        r[0]: {"item": r[1], "skill_norm": r[2], "status": r[3]}
        for r in rows
    }


def priority(conn, resume_id: int, limit: int = 30) -> list[dict]:
    """跨岗位学习优先级：要求的岗位数降序，同数按状态更差者优先。

    聚合三路数据（内存中做，量级都在几百条以内）：
      1. 全库岗位的必备技能原文（job_profile）
      2. 展开缓存（skill_norm → 条目）
      3. 该简历的自评（item_hash → status）
    没有展开缓存的技能不进聚合（名词没有程度信息，混进去只会稀释排序）。
    """
    from ..normalize.fuzzy import norm_key

    jobs = conn.execute(
        "SELECT job_id, title, must_have_skills FROM job_profile WHERE is_current"
    ).fetchall()
    expansions = conn.execute(
        "SELECT skill_norm, skill, items FROM skill_expansion_cache"
    ).fetchall()
    assessed = load_by_resume(conn, resume_id)
    assessed_norm: dict[str, str] = {}
    for info in assessed.values():
        if info.get("skill_norm"):
            assessed_norm[info["skill_norm"]] = info["status"]

    # norm -> {display, items, jobs: set[(job_id, title)]}
    # 存储侧的 skill_norm 也再归一一次：缓存行理论上已是归一键，
    # 但聚合的健壮性不该依赖"每个写入方都记得归一"（check_gap 抓到的对不上）
    by_norm: dict[str, dict] = {}
    for norm_raw, skill, items in expansions:
        norm = norm_key(norm_raw or skill)
        if norm not in by_norm:
            by_norm[norm] = {"skill": skill, "norm": norm, "items": [], "jobs": set()}
        for i in items or []:
            text = str(i.get("text") or "").strip()
            if text:
                by_norm[norm]["items"].append(text)

    for job_id, title, must in jobs:
        for s in must or []:
            norm = norm_key(str(s))
            entry = by_norm.get(norm)
            if entry and len(entry["items"]):
                entry["jobs"].add((int(job_id), title or ""))

    # 条目级：一个技能的条目继承该技能的岗位集
    out: list[dict] = []
    seen_hash: set[str] = set()
    for norm, entry in by_norm.items():
        if not entry["items"]:
            continue
        # 没有任何在架岗位要求的条目不进清单：学习优先级回答的是"市场在要什么，
        # 我还不会什么"——0 岗条目是噪音，混进来会稀释排序
        if not entry["jobs"]:
            continue
        for text in entry["items"]:
            h = item_hash(text)
            if h in seen_hash:
                continue
            seen_hash.add(h)
            status = next(
                (info["status"] for ih, info in assessed.items()
                 if ih == h or info.get("item") == text),
                "never",
            )
            if status == "can_explain":
                continue
            out.append({
                "item": text,
                "skill": entry["skill"],
                "skill_norm": norm,
                "status": status,
                "jobs_n": len(entry["jobs"]),
                "sample_jobs": [t for _, t in sorted(entry["jobs"])[:3]],
                "item_hash": h,
            })

    out.sort(key=lambda x: (-x["jobs_n"], x["status"] != "never"))
    return out[: max(1, min(int(limit), 60))]
