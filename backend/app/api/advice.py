"""从已抓岗位的否决模式，反推「该搜什么、该避什么、该补什么」。

为什么不能让模型直接给建议
    「建议投递 Java 后端岗位」这种话没有信息量 —— 模型在没有数据时只能这么说。
    所以这里先用代码算出事实：哪个技能被最多岗位要求而你缺、哪些标题词几乎必然被否、
    通过的岗位标题里有哪几个共同词。事实摆出来之后，建议才有依据。

    和项目里其他地方的取舍一致：能用代码算清楚的，不交给模型。

一个必须说清的边界
    这个模块区分「会但没写」和「确实不会」，但只能做到「疑似相关」：
    岗位要 Kafka、你简历有 RabbitMQ，向量相似说明它们同属消息中间件，
    不等于你真的用过 Kafka。所以措辞一律是「如果你确实用过，建议写明」，
    而不会替用户断言「你会 Kafka」—— 那种断言在面试时是灾难。
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from fastapi import APIRouter, HTTPException

from ..db import match_repo
from ..db.pool import get_pool
from ..match import semantic

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/advice", tags=["advice"])

# 标题里出现这些词，基本等于筛掉了应届/初级的候选人。
# 来源是这批岗位的实测：含这些词的标题几乎全被否决，而且理由都是经验或技能门槛
TITLE_TRAPS = ("资深", "专家", "高级", "架构师", "主管", "经理", "TL", "Lead", "负责人")

# 提取共同词时要排掉的字眼，它们常出现在标题里但不代表方向
TITLE_STOPWORDS = {"岗位", "招聘", "（", "）", "(", ")", "·", "-", " "}

# 共同词必须是"技术方向词"才算数。
# 岗位库里混着无关岗位（"无人驾驶安全员""路测安全员"），它们能过硬约束
# 但跟求职方向毫无关系 —— 不加这层过滤，提取出的方向词会是「安全员」。
TECH_HINTS = (
    "开发", "工程", "后端", "前端", "全栈", "算法", "数据", "测试",
    "运维", "架构", "Java", "Python", "Go", "PHP", "C++", "安卓", "iOS",
)

# 判断「简历里有没有相关但不同名的技能」的阈值。
# 0.62 是实测下沿：bge 对同领域不同工具（Kafka ↔ RabbitMQ、MySQL ↔ PostgreSQL）
# 一般在 0.65~0.85，无关技能在 0.3 以下。取低一点，宁可多提示也不要漏掉，
# 因为措辞是「疑似相关，自行确认」，误报成本低，漏报成本高
RELATED_THRESHOLD = 0.62


def _reject_stats(items: list[dict]) -> list[dict]:
    """按硬约束类别统计否决次数。用 checks 而不是 reasons —— 前者是结构化的。"""
    counter: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for item in items:
        if item.get("decision") != "reject":
            continue
        for check in item.get("checks") or []:
            if check.get("decision") != "reject":
                continue
            rule = check.get("rule") or "other"
            counter[rule] += 1
            labels[rule] = check.get("label") or rule
    return [
        {"rule": rule, "label": labels[rule], "count": count}
        for rule, count in counter.most_common()
    ]


def _demanded_skills(items: list[dict], top_n: int = 8) -> list[tuple[str, int]]:
    """汇总「岗位要求了、但简历没覆盖」的技能，按被要求的岗位数排序。

    只统计 missed_skills 而不是全部必备技能：已经覆盖的技能不需要提醒，
    而「被 12 个岗位要求却一次都没命中」才是真正卡住投递的东西。
    """
    counter: Counter[str] = Counter()
    for item in items:
        for skill in item.get("missed_skills") or []:
            name = (skill or "").strip()
            if name:
                counter[name] += 1
    return counter.most_common(top_n)


async def _related_in_resume(
    demanded: list[str], resume_skills: list[str]
) -> dict[str, list[tuple[str, float]]]:
    """给每个「缺的技能」找出简历里语义相近的技能。

    返回 {岗位要求的技能: [(简历里的技能, 相似度), ...]}，按相似度降序。
    """
    demanded = [d for d in demanded if d]
    resume_skills = [s for s in resume_skills if s]
    if not demanded or not resume_skills:
        return {}

    vectors = await asyncio.to_thread(semantic.embed, demanded + resume_skills)
    if not vectors:
        return {}

    import numpy as np

    mat = np.array(vectors)
    job_vecs = mat[: len(demanded)]
    resume_vecs = mat[len(demanded) :]
    sims = job_vecs @ resume_vecs.T  # 已 L2 归一化，点积即余弦

    out: dict[str, list[tuple[str, float]]] = {}
    for i, skill in enumerate(demanded):
        pairs = sorted(
            ((resume_skills[j], float(sims[i][j])) for j in range(len(resume_skills))),
            key=lambda p: -p[1],
        )
        close = [(name, round(score, 3)) for name, score in pairs if score >= RELATED_THRESHOLD]
        if close:
            out[skill] = close[:2]
    return out


def _common_title_terms(titles: list[str], min_count: int = 2, limit: int = 5) -> list[str]:
    """从「通过/待确认」的岗位标题里提取共同词，作为搜索方向参考。

    用 n-gram 而不是分词：不引 jieba 这类依赖，而且岗位标题很短，
    2~5 字的滑动窗口已经够用。

    **必须按长度优先选，不能按频次优先**：先选中短词「开发」的话，
    更长的「开发工程师」就会被当成它的子串排除掉 —— 实测第一版就是这样，
    提取出来的方向词是「开发」「发工程师」（错位子串），完全没法用。
    """
    counter: Counter[str] = Counter()
    for title in titles:
        clean = (title or "").strip()
        for n in (5, 4, 3, 2):
            for i in range(len(clean) - n + 1):
                gram = clean[i : i + n]
                if any(ch in TITLE_STOPWORDS for ch in gram):
                    continue
                if not gram.strip():
                    continue
                counter[gram] += 1

    def overlap_ratio(a: str, b: str) -> float:
        """a 的字符有多大比例也出现在 b 里。

        「端开发工程」不是「开发工程师」的子串（少首字多尾字），
        但 5 个字里有 4 个重合 —— 这种错位子串必须靠比例判断才挡得住。
        """
        return sum(1 for ch in a if ch in b) / len(a) if a else 0.0

    picked: list[str] = []
    # 长词优先：5 字先试，命中就吃掉它的所有近亲
    for gram, count in sorted(counter.items(), key=lambda kv: (-len(kv[0]), -kv[1])):
        if count < min_count:
            continue
        if not any(hint.lower() in gram.lower() for hint in TECH_HINTS):
            continue
        if any(gram in longer or overlap_ratio(gram, longer) >= 0.6 for longer in picked):
            continue
        picked.append(gram)
        if len(picked) >= limit:
            break
    return picked


def _core_skill(items: list[dict], resume_skills: list[str]) -> str:
    """选一个「最像门面」的技能，用来拼搜索词。

    不能直接取简历技能列表的第一个：简历里技能顺序跟求职方向无关，
    实测第一个是 Python，而这批通过/待确认岗位里出现最多的是 Java。
    用「在有效岗位标题里出现最多的简历技能」来选，才有依据。
    """
    titles = " ".join(
        (i.get("title") or "") for i in items if i.get("decision") in ("pass", "confirm")
    ).lower()
    best_name, best_hits = "", 0
    for name in resume_skills:
        hits = titles.count(name.lower()) if name else 0
        if hits > best_hits:
            best_name, best_hits = name, hits
    return best_name or (resume_skills[0] if resume_skills else "")


def _build_search_terms(
    basic: dict, resume_skills: list[str], common_terms: list[str], items: list[dict]
) -> list[dict]:
    """拼搜索词。每个词都标出来源，让用户知道为什么建议这个词。"""
    terms: list[dict] = []

    # 方向词优先用「通过岗位的共同词」——那是这批数据里实际匹配上的方向
    direction = common_terms[0] if common_terms else ""
    core = _core_skill(items, resume_skills)

    if core and direction:
        terms.append({"term": f"{core} {direction}", "why": f"简历核心技能 + 通过岗位的共同方向「{direction}」"})
    if direction:
        terms.append({"term": direction, "why": "通过/待确认的岗位标题里反复出现的词"})

    # 岗位性质词：应届生搜「实习」「应届」的命中率显著高于裸搜技能名
    grad = basic.get("graduation_year")
    if grad:
        terms.append({"term": f"{core or '开发'} 应届", "why": f"你的毕业年份是 {grad}，多数平台有应届专区"})
    terms.append({"term": f"{core or '开发'} 实习", "why": "0 经验时实习岗的硬约束最容易过"})

    # 去重，保持顺序
    seen: set[str] = set()
    out: list[dict] = []
    for t in terms:
        key = t["term"].replace(" ", "").lower()
        if key and key not in seen:
            seen.add(key)
            out.append(t)
    return out[:4]


@router.get("/search-strategy")
async def search_strategy(resume_id: int) -> dict:
    """给出「该搜什么、该避什么、该补什么」。

    数据来源是最近一次匹配的缓存结果（match_run），不重新跑匹配 ——
    这个建议要在这几个数字的基础上才成立，重算一遍反而可能对不上。
    """
    with get_pool().connection() as conn:
        profile = match_repo.get_current_profile(conn, resume_id)
        if profile is None:
            raise HTTPException(status_code=409, detail="该简历还没有画像，先完成解析")

        row = conn.execute(
            "SELECT payload, created_at FROM match_run "
            "WHERE resume_id = %s ORDER BY created_at DESC LIMIT 1",
            (resume_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail="还没有跑过匹配，先到「匹配」页跑一次")

        payload, matched_at = row[0], row[1]

    items = payload.get("items") or []
    counts = payload.get("counts") or {}
    basic = profile.get("basic") or {}
    resume_skills = [
        str(s.get("name")).strip()
        for s in (profile.get("skills") or [])
        if isinstance(s, dict) and s.get("name")
    ]

    reject_stats = _reject_stats(items)

    # 缺口只统计「通过 + 待确认」的岗位。
    # 否决的岗位本来就不会投，它要求什么跟用户无关 —— 把它们算进缺口，
    # 会得出「你该去学 Premiere」这种荒唐建议（来自一条剪辑岗）。
    viable = [i for i in items if i.get("decision") in ("pass", "confirm")]
    demanded = _demanded_skills(viable, top_n=8)
    related = await _related_in_resume([name for name, _ in demanded], resume_skills)

    fill_resume: list[dict] = []
    change_direction: list[dict] = []
    for skill, jobs_count in demanded:
        label = skill if len(skill) <= 16 else skill[:16] + "…"
        close = related.get(skill)
        if close:
            fill_resume.append(
                {
                    "skill": label,
                    "asked_by": jobs_count,
                    "related": [{"name": name, "similarity": score} for name, score in close],
                    "hint": f"{jobs_count} 个岗位要求「{label}」，而你简历里有 "
                    f"「{'、'.join(name for name, _ in close)}」—— 同属一类，"
                    "如果你确实用过它，建议在简历里写明具体用法",
                }
            )
        else:
            change_direction.append(
                {
                    "skill": label,
                    "asked_by": jobs_count,
                    "hint": f"{jobs_count} 个岗位要求「{label}」，简历里找不到相关经历。"
                    "要么补这一项，要么避开以它为主要门槛的岗位",
                }
            )

    # 标题陷阱：被否决的岗位里，有多少标题带「资深/专家」这类词
    rejected = [i for i in items if i.get("decision") == "reject"]
    trap_hits = [
        i.get("title") or ""
        for i in rejected
        if any(word.lower() in (i.get("title") or "").lower() for word in TITLE_TRAPS)
    ]

    # 共同方向：把「通过 + 待确认」的标题一起算，样本才够（这批只有 3 + 10 个）
    promising = [i.get("title") or "" for i in items if i.get("decision") in ("pass", "confirm")]
    common_terms = _common_title_terms(promising)

    # ---------- 整体诊断 ----------
    # 这是整个模块最想说的一句话。三个数字放在一起，结论就出来了：
    #   否决里卡技能的占比、能投的岗位数、这些岗位的缺口有没有重复
    # 缺口全都不重复，说明「补一两个技能就能提高命中率」在这批数据里不成立。
    skill_reject_count = next(
        (r["count"] for r in reject_stats if r["rule"] == "must_skills"), 0
    )
    gap_total = sum(n for _, n in demanded)
    repeated = [s for s, n in demanded if n >= 2]

    if not items:
        verdict, reason = "没有数据", "还没跑过匹配"
    elif len(viable) == 0:
        verdict = "方向不对"
        reason = (
            f"{len(items)} 个岗位一个都没通过硬约束，其中 {skill_reject_count} 个卡在必备技能。"
            "这批岗位的要求整体高于你现在的画像，换搜索词比补技能更快见效"
        )
    elif gap_total >= 5 and not repeated:
        verdict = "方向偏了，补技能解决不了"
        reason = (
            f"{len(rejected)} 个否决里 {skill_reject_count} 个卡在必备技能；"
            f"而能投的 {len(viable)} 个岗位，缺口分散在 {gap_total} 项不同技能上，没有一项被重复要求 —— "
            "说明你抓来的岗位要求偏高或方向分散，补任何一项都只会改善一两个岗位"
        )
    elif repeated:
        verdict = "有共性缺口，值得补"
        reason = (
            f"「{'、'.join(repeated[:3])}」被多个能投的岗位要求，"
            "补上这几项能同时改善多个岗位的匹配"
        )
    else:
        verdict = "样本太少，先多抓一些"
        reason = f"能投的岗位只有 {len(viable)} 个，结论不可靠；先抓 20 个以上再看"

    return {
        "resume_id": resume_id,
        "matched_at": matched_at.isoformat() if matched_at else None,
        "diagnosis": {"verdict": verdict, "reason": reason},
        "summary": {
            "total": len(items),
            "pass": counts.get("pass", 0),
            "confirm": counts.get("confirm", 0),
            "reject": counts.get("reject", 0),
        },
        "reject_by_rule": reject_stats,
        "fill_resume": fill_resume,
        "change_direction": change_direction,
        "title_trap": {
            "count": len(trap_hits),
            "words": [w for w in TITLE_TRAPS if any(w.lower() in t.lower() for t in trap_hits)],
            "examples": trap_hits[:3],
        },
        "common_terms": common_terms,
        "search_terms": _build_search_terms(basic, resume_skills, common_terms, items),
        "avoid_terms": [w for w in TITLE_TRAPS][:4],
    }
