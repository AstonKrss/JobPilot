"""排序分：只对已通过硬约束的岗位计算。

这里才允许加权，因为参与排序的都是「已经合格」的岗位，不存在拿加分抵掉否决的问题。
用户最初的加权和方案之所以出问题，是把它用在了否决层；分层之后，加权回归到它该在的位置。

三个分项都返回可读理由。最终解释由这些理由直接拼接，不交给模型二次生成 ——
模型写解释时会顺着语气编出原文没有的匹配点。
"""

from __future__ import annotations

from dataclasses import dataclass

from .skills import resume_token_set, skill_hit

# 权重（合计 100）。数值是初始经验值，须在 30–50 条评测集上校准后固定，
# 改动时同步更新 docs/design-decisions.md
MUST_SKILL_POINTS = 60.0
NICE_SKILL_POINTS = 20.0
NICE_POINT_EACH = 5.0
EXPERIENCE_POINTS = 20.0


@dataclass(slots=True)
class ScorePart:
    name: str
    score: float
    max_score: float
    detail: str


def score_must_skills(
    resume_tokens: set[str],
    must: list[str],
    semantic_hits: dict[str, dict] | None = None,
) -> tuple[ScorePart, list[str], dict[str, dict]]:
    if not must:
        # 岗位没列必备技能时给中性分，而不是满分。
        # 满分会让"抓取不完整"变成优势：摘要岗位因为没抓到技能要求，
        # 反而排到完整 JD 的岗位前面（实测出现过，5 个通过的全是摘要岗位）。
        # 中性分既不奖励也不惩罚，让它在其他维度上正常竞争。
        return (
            ScorePart(
                "必备技能",
                round(MUST_SKILL_POINTS * 0.6, 1),
                MUST_SKILL_POINTS,
                "岗位未列出必备技能（按中性计分）",
            ),
            [],
            {},
        )
    semantic_hits = semantic_hits or {}
    hit = [s for s in must if skill_hit(resume_tokens, s)]
    # 语义判定升级：规则判为未命中、但模型确认简历里有对应证据的，算命中
    upgraded = {s: semantic_hits[s] for s in must if s not in hit and s in semantic_hits}
    hit = hit + list(upgraded.keys())
    miss = [s for s in must if s not in hit]
    ratio = len(hit) / len(must)
    # 分母太小会虚高：只列 1 项要求、而简历正好有，就是 1/1 满分。
    # 实测「编程开发工程师」只列了 1 项必备技能，拿到 60/60 成为全库最高分，
    # 明显荒谬。按绝对命中数封顶：1 项最多 70%，2 项最多 90%
    cap = {1: 0.7, 2: 0.9}.get(len(must), 1.0)
    if ratio > cap:
        ratio = cap
    detail = f"必备技能命中 {len(hit)}/{len(must)}"
    if len(must) <= 2:
        detail += "（要求项过少，已按绝对数封顶）"
    if upgraded:
        detail += f"（其中 {len(upgraded)} 项由语义判定补上）"
    part = ScorePart(
        "必备技能",
        round(MUST_SKILL_POINTS * ratio, 1),
        MUST_SKILL_POINTS,
        detail,
    )
    return part, miss, upgraded


def score_nice_skills(resume_tokens: set[str], nice: list[str]) -> ScorePart:
    if not nice:
        return ScorePart("加分技能", 0.0, NICE_SKILL_POINTS, "岗位未列出加分项")
    hit = [s for s in nice if skill_hit(resume_tokens, s)]
    points = min(NICE_POINT_EACH * len(hit), NICE_SKILL_POINTS)
    return ScorePart(
        "加分技能",
        round(points, 1),
        NICE_SKILL_POINTS,
        f"加分项命中 {len(hit)}/{len(nice)}",
    )


def score_experience(resume_years: float | None, job: dict) -> tuple[ScorePart, str | None]:
    """年限阶梯扣分（决策已定：≤1 年不扣 / 2–3 年中扣 / ≥4 年重扣）。

    为什么是阶梯而非线性：线性扣分下，「差 5 年」可能还不如「少一个技能」扣得多，
    排序会失真。阶梯保证差距一进入下一档，扣分立刻显著。

    超出年限不扣分：要求 3–5 年而候选人有 8 年，通常仍可通过，
    硬性排斥会造成大量「资历过高」的误杀。
    """
    exp_min = job.get("exp_min")
    years = resume_years if resume_years is not None else 0.0

    if exp_min is None:
        # 曾经给满分。结果是"没写年限要求的岗位自动拿 20 分"，
        # 与"必备技能没列就满分"是同一个错误：把信息缺失当成优势。
        # 改中性，同时在完整度里记为缺失，让分数带上不可信的标记
        return (
            ScorePart("经验年限", round(EXPERIENCE_POINTS * 0.6, 1), EXPERIENCE_POINTS,
                      "岗位未提年限要求（不计为优势）"),
            None,
        )

    gap = exp_min - years
    if gap <= 1:
        return (
            ScorePart(
                "经验年限",
                EXPERIENCE_POINTS,
                EXPERIENCE_POINTS,
                f"年限符合（要求 {exp_min} 年起，你约 {years} 年）",
            ),
            None,
        )
    if gap <= 3:
        note = f"年限差 {gap:.1f} 年（要求 {exp_min} 年起，你约 {years} 年）"
        return (
            ScorePart("经验年限", round(EXPERIENCE_POINTS * 0.5, 1), EXPERIENCE_POINTS, note),
            note,
        )
    note = f"年限差 {gap:.1f} 年，差距较大（要求 {exp_min} 年起，你约 {years} 年）"
    return (
        ScorePart("经验年限", round(EXPERIENCE_POINTS * 0.15, 1), EXPERIENCE_POINTS, note),
        note,
    )


def score_match(
    resume_profile: dict,
    job: dict,
    semantic_hits: dict[str, dict] | None = None,
) -> tuple[float, list[ScorePart], list[str], dict[str, dict]]:
    """返回 (总分, 分项, 未命中的必备技能, 语义升级项)。"""
    resume_tokens = resume_token_set(resume_profile.get("skills") or [])
    basic = resume_profile.get("basic") or {}

    must_part, missed, upgraded = score_must_skills(
        resume_tokens, job.get("must_have_skills") or [], semantic_hits
    )
    nice_part = score_nice_skills(resume_tokens, job.get("nice_to_have_skills") or [])
    exp_part, _ = score_experience(basic.get("work_years"), job)

    parts = [must_part, nice_part, exp_part]
    total = round(sum(p.score for p in parts), 1)
    return total, parts, missed, upgraded


# 决定"这个岗位值不值得评分"的字段。缺得越多，算出来的分数越是噪音。
INFO_FIELDS = ("title", "city", "edu_level", "job_nature", "must_have_skills", "exp_min")


def info_completeness(job: dict) -> tuple[float, list[str]]:
    """返回 (完整度 0-1, 缺失字段名列表)。

    为什么需要它：早先的实现里，字段缺失一律"不扣分"，
    等价于奖励信息不全的岗位 —— JD 写得越敷衍分数越高，
    而写得认真的岗位因为要求多、命中不满反而被扣。
    现在把"可评性"独立成一个维度：分数只在字段足够时才对外可见。
    """
    missing = [f for f in INFO_FIELDS if not job.get(f)]
    return round(1 - len(missing) / len(INFO_FIELDS), 2), missing
