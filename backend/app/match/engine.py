"""匹配引擎：硬约束过滤 → 排序打分 → 生成解释。

分层是刻意的：否决与排序用两套机制，不共用一个分数。
如果把否决也表达成「扣很多分」，总分高的候选人仍可能顶着否决项排在前面，
用户看到的推荐里就会混进学历根本不达标的岗位。

解释是一等输出，不是事后补的说明
    否决理由直接取自 RuleCheck.detail，配合 JD 原文依据（evidence），
    全程不调用模型。求职者最想知道的恰好是「为什么不行」，让模型复述会编。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .rules import Decision, RuleCheck, evaluate_hard_constraints, split_hits
from .scoring import ScorePart, info_completeness, score_match, resume_token_set


@dataclass(slots=True)
class MatchResult:
    job_id: int | None
    title: str | None
    company: str | None
    decision: Decision
    score: float | None
    # 岗位数据完整度：full=完整 JD，digest=列表页摘要（无技能要求）。
    # 摘要岗位的分数不可与完整岗位直接比较，排序时统一排在后面
    detail_level: str = "unknown"
    checks: list[RuleCheck] = field(default_factory=list)
    parts: list[ScorePart] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    missed_skills: list[str] = field(default_factory=list)
    # 必备技能命中计数。给"只谈技能是否对口"的下游用（如打招呼话术／HR 回复），
    # 比只看 missed_skills 更准：must 为空时 missed 也为空，但那是"没数据"而非"全命中"
    skill_must_count: int = 0
    skill_hit_count: int = 0
    # 岗位信息完整度（0-1）与缺失字段。分数只在完整度足够时才对外展示：
    # 缺一半字段的岗位，算出来的分数是噪音，展示它等于误导
    confidence: float = 0.0
    info_missing: list[str] = field(default_factory=list)

    @property
    def display_score(self) -> float | None:
        """对外可见的分数。完整度不足 60% 一律不给分。"""
        return self.score if self.confidence >= 0.6 else None


def match_one(
    resume_profile: dict,
    job: dict,
    prefs: dict | None = None,
    semantic_hits: dict[str, dict] | None = None,
) -> MatchResult:
    prefs = prefs or {}
    checks = evaluate_hard_constraints(resume_profile, job, prefs, semantic_hits)

    base = MatchResult(
        job_id=job.get("job_id"),
        title=job.get("title"),
        company=job.get("company"),
        decision=Decision.PASS,
        score=None,
        detail_level=job.get("detail_level", "unknown"),
        checks=checks,
    )
    # 完整度先算好：后面所有分支（包括直接否决的）都要带上，
    # 否则界面上会出现"被否决但看起来信息很全"的假象
    base.confidence, base.info_missing = info_completeness(job)

    # 缺口必须在所有分支上都有值。
    # 之前只在打分分支里填，导致「技能不达标直接否决」的岗位 missed_skills 为空 ——
    # 界面看不到缺什么、简历定制也拿不到缺口（实测过的真实 bug）
    must = job.get("must_have_skills") or []
    if must:
        tokens = resume_token_set(resume_profile.get("skills") or [])
        _, base.missed_skills = split_hits(tokens, must)
        semantic_hits = semantic_hits or {}
        base.missed_skills = [s for s in base.missed_skills if s not in semantic_hits]
        # 语义命中同样算命中，所以要在过滤之后统计
        base.skill_must_count = len(must)
        base.skill_hit_count = len(must) - len(base.missed_skills)

    rejects = [c for c in checks if c.blocked]
    if rejects:
        base.decision = Decision.REJECT
        # 有几条否决就列几条：用户需要知道是「学历不够」还是「学历 + 城市都不对」，
        # 只报第一条会让他以为改一条就能过
        base.reasons = [
            f"{c.label}不满足：{c.detail}" + (f"（JD 原文：{c.jd_evidence}）" if c.jd_evidence else "")
            for c in rejects
        ]
        return base

    total, parts, missed, upgraded = score_match(resume_profile, job, semantic_hits)
    base.score = total
    base.parts = parts
    base.missed_skills = missed
    # 打分分支会把 missed 覆盖一遍，命中计数必须跟着重算，否则两处口径不一致
    if base.skill_must_count:
        base.skill_hit_count = base.skill_must_count - len(missed)

    # 岗位一条技能要求都没列（非技术岗，或列表页摘要没抓到要求）时，
    # 技能项只能给中性分。这时"通过"是没有依据的：学历/城市/年限全过，
    # 只说明"没被卡住"，不等于"匹配"。
    # 实测里"无人驾驶安全员（中专/3-5年/司机）"就这样混进了通过列表 —— 必须降级。
    no_skill_signal = not (job.get("must_have_skills") or []) and not (
        job.get("nice_to_have_skills") or []
    )
    base.decision = (
        Decision.NEED_CONFIRM
        if no_skill_signal or any(c.uncertain for c in checks)
        else Decision.PASS
    )

    reasons: list[str] = []
    for check in checks:
        if check.decision is Decision.PASS:
            # 必备技能的命中细节由下面的打分项给出，这里跳过避免同一句话说两遍
            if check.rule == "must_skills":
                continue
            reasons.append(check.detail)
        elif check.uncertain:
            reasons.append(f"{check.label}待确认：{check.detail}")
    for part in parts:
        reasons.append(f"{part.name} {part.score}/{part.max_score:.0f} — {part.detail}")
    if missed:
        # 未命中的必备技能单独列出。这是用户最需要采取行动的信息，
        # 埋在分数里等于没说
        reasons.append("未覆盖的必备技能：" + "、".join(missed))
    for skill, info in (upgraded or {}).items():
        # 语义升级必须写清依据是哪句简历证据，否则等于黑箱改分
        reasons.append(
            f"语义判定补上：JD「{skill}」← 简历「{info.get('evidence', '')}」"
        )
    if base.detail_level == "digest":
        reasons.append(
            "该岗位仅有列表页摘要，未抓到 JD 正文与技能要求，技能相关的结论不可信；"
            "建议进入详情页重新抓取"
        )
    if no_skill_signal and base.detail_level != "digest":
        reasons.append(
            "该岗位未列出任何技能要求（可能不是技术岗，或 JD 里没写具体要求）；"
            "学历/城市/年限通过只代表没被卡住，不能据此判断匹配，建议自行确认"
        )

    base.reasons = reasons
    return base


def rank(
    resume_profile: dict,
    jobs: list[dict],
    prefs: dict | None = None,
    semantic: dict[tuple[int, str], dict] | None = None,
) -> list[MatchResult]:
    """批量匹配并排序。

    排序规则：通过 > 待确认 > 否决。
    「待确认」排在通过之后而不是混在一起，因为它们的分数不可比 ——
    一个待确认的岗位可能只是因为简历没写统招，分数再高也不该压过信息完整的匹配项。
    """
    # 语义判定结果是 (job_id, 技能) 形态，先按岗位拆开
    per_job: dict[int, dict[str, dict]] = {}
    for (job_id, skill), info in (semantic or {}).items():
        per_job.setdefault(int(job_id), {})[skill] = info

    results = [
        match_one(
            resume_profile,
            job,
            prefs,
            per_job.get(int(job.get("job_id") or 0), {}),
        )
        for job in jobs
    ]

    order = {Decision.PASS: 0, Decision.NEED_CONFIRM: 1, Decision.REJECT: 2}
    # 排序键的设计意图：先按能不能投，再按"结论多可信"，再按"缺多少"，最后才看分数。
    # 曾经用 detail_level（full/digest）近似可信度，但它只有三档、太粗；
    # confidence 是连续值，能真正把"字段缺失的岗位"压到后面
    results.sort(
        key=lambda r: (
            order[r.decision],
            -r.confidence,          # 信息越完整越靠前
            len(r.missed_skills),   # 缺口越少越靠前
            -(r.score or 0.0),
        )
    )
    return results


def reject_summary(results: list[MatchResult]) -> dict[str, int]:
    """统计否决原因分布。

    用途是发现「某条规则把大部分岗位都毙了」这类结构性问题 ——
    单看一条条结果很难察觉，聚合之后一眼可见。
    """
    counter: dict[str, int] = {}
    for result in results:
        for check in result.checks:
            if check.blocked:
                counter[check.rule] = counter.get(check.rule, 0) + 1
    return counter
