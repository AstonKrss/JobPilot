"""硬约束判定：这是门槛，不是评分项。

三条设计原则，都来自「匹配不是相似度问题」这个前提：

1. 不走向量
   「3 年经验」与「应届」在向量空间里可能很近，但结构上不可互换。
   硬约束必须用结构化字段做精确比较。

2. 不进加权和
   加权和的结构性缺陷是加分项可以无限补偿否决项 —— 学历不达标，技能再匹配也没人会看。
   所以这里只产出「通过 / 否决 / 待确认」三态，不产出分数。

3. 信息缺失时给「待确认」，不给「否决」
   简历没写「统招」不等于「非统招」。把缺失当成不满足，会静默淘汰掉大量本合格的人，
   而且用户完全看不出为什么。宁可多问一句，也不要不声不响地筛掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ..normalize.fuzzy import norm_key
from .skills import resume_token_set, split_hits

EDU_RANK: dict[str, int] = {"未知": 0, "其他": 1, "大专": 2, "本科": 3, "硕士": 4, "博士": 5}
EDU_NO_REQUIREMENT = {"", "不限", "无要求", "学历不限"}

# 必备技能的命中率门槛。低于此值否决。
#
# 为什么不是「缺一项就否决」：JD 的必备技能常写成「精通 Go 或 Java，熟悉 MySQL、Redis、Kafka」
# 这类组合，要求 4 项而候选人命中 3 项是很正常的合格候选人。全有全无会把他淘汰。
# 为什么不是「一项都不否决」：技能全部不符的岗位（例如 Java 岗对 Go 候选人）
# 仍然通过并排在列表里，是明显噪音。
# 0.5 是初始经验值，须在评测集上校准后固定。
MIN_MUST_SKILL_RATIO = 0.5

_REMOTE_RE = re.compile(r"远程|remote|全国|不限地点", re.I)
_CITY_NOISE = ("地区", "区域", "市")


class Decision(str, Enum):
    PASS = "pass"
    REJECT = "reject"
    NEED_CONFIRM = "confirm"


@dataclass(slots=True)
class RuleCheck:
    rule: str          # 机器标识：education / city / job_nature
    label: str         # 中文名，直接用于界面
    decision: Decision
    detail: str        # 人话结论
    jd_evidence: str | None = None  # JD 原文依据，让否决可追溯

    @property
    def blocked(self) -> bool:
        return self.decision is Decision.REJECT

    @property
    def uncertain(self) -> bool:
        return self.decision is Decision.NEED_CONFIRM


def city_key(text: str | None) -> str:
    """城市检索键：去标点空格、去「市/地区」后缀。

    「北京市」与「北京」必须归到同一个键，否则最基础的一条硬约束就失效。
    """
    if not text:
        return ""
    s = norm_key(text)
    for suffix in _CITY_NOISE:
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
    return s


def city_match(resume_city: str | None, jd_city: str | None) -> bool:
    a, b = city_key(resume_city), city_key(jd_city)
    if not a or not b:
        return False
    if a == b:
        return True
    # 「北京」与「北京海淀」视为同城。限定长度 >= 2，避免单字误命中
    return (len(a) >= 2 and a in b) or (len(b) >= 2 and b in a)


def is_remote(jd_city: str | None) -> bool:
    return bool(jd_city and _REMOTE_RE.search(jd_city))


def _evidence(job: dict, key: str) -> str | None:
    return (job.get("hard_constraint_evidence") or {}).get(key)


def check_education(resume_basic: dict, job: dict) -> RuleCheck:
    """学历 + 统招。两者合用一条判定，因为它们总是同时出现在同一句 JD 里。

    统招只在 JD 明写时校验（三态字段为 True 时）。这是刻意的：
    「本科及以上」不含统招含义，若把它当统招要求，会误杀所有非全日制学历的候选人。
    """
    jd_edu = (job.get("edu_level") or "").strip()
    evidence = _evidence(job, "edu_level")

    if jd_edu in EDU_NO_REQUIREMENT:
        return RuleCheck("education", "学历", Decision.PASS, "岗位未限制学历层次", evidence)

    resume_edu = (resume_basic.get("edu_level") or "未知").strip()
    if resume_edu not in EDU_RANK or resume_edu == "未知":
        return RuleCheck(
            "education",
            "学历",
            Decision.NEED_CONFIRM,
            f"岗位要求{jd_edu}，但未能从简历中识别出学历层次",
            evidence,
        )

    if EDU_RANK[resume_edu] < EDU_RANK.get(jd_edu, 0):
        return RuleCheck(
            "education",
            "学历",
            Decision.REJECT,
            f"岗位要求{jd_edu}，你的最高学历是{resume_edu}",
            evidence,
        )

    # 层次已达标，再查统招。只有 JD 明确写了统招（True）才校验
    if job.get("edu_fulltime_required") is True:
        fulltime = resume_basic.get("edu_fulltime")
        if fulltime is False:
            return RuleCheck(
                "education",
                "学历",
                Decision.REJECT,
                f"岗位要求统招全日制{jd_edu}，你的学历为非全日制",
                _evidence(job, "edu_fulltime_required") or evidence,
            )
        if fulltime is None:
            return RuleCheck(
                "education",
                "学历",
                Decision.NEED_CONFIRM,
                f"岗位要求统招全日制{jd_edu}，但简历未注明是否统招",
                _evidence(job, "edu_fulltime_required") or evidence,
            )

    return RuleCheck(
        "education", "学历", Decision.PASS, f"学历符合（要求{jd_edu}，你是{resume_edu}）", evidence
    )


def check_city(resume_basic: dict, job: dict, prefs: dict) -> RuleCheck:
    """城市判定：期望城市优先，简历推断的当前城市只作兜底。

    这个优先级不是可有可无的：简历里最近一段工作在杭州，但求职者完全可以想投北京的岗位。
    拿「当前城市」当「可接受城市」，会把他最想去的机会全部否掉 —— 而且他自己看不出原因，
    只会觉得这个工具不准。
    """
    jd_city = (job.get("city") or "").strip()
    evidence = _evidence(job, "city")

    if not jd_city:
        return RuleCheck("city", "城市", Decision.PASS, "岗位未标注工作城市", None)
    if is_remote(jd_city):
        return RuleCheck("city", "城市", Decision.PASS, f"岗位支持远程（{jd_city}）", evidence)

    targets = [t for t in (prefs.get("target_cities") or []) if t]
    if targets:
        if any(city_match(t, jd_city) for t in targets):
            return RuleCheck("city", "城市", Decision.PASS, f"城市符合你的期望（{jd_city}）", evidence)
        target_text = "、".join(targets)
        if prefs.get("accept_relocation"):
            return RuleCheck(
                "city",
                "城市",
                Decision.PASS,
                f"岗位在{jd_city}，不在期望城市（{target_text}），你已设置接受异地",
                evidence,
            )
        return RuleCheck(
            "city", "城市", Decision.REJECT, f"岗位在{jd_city}，你的期望城市是{target_text}", evidence
        )

    resume_city = resume_basic.get("current_city")
    if not resume_city:
        return RuleCheck(
            "city",
            "城市",
            Decision.NEED_CONFIRM,
            f"岗位在{jd_city}，且你尚未设置期望城市、简历中也未识别出城市",
            evidence,
        )

    if city_match(resume_city, jd_city):
        return RuleCheck(
            "city", "城市", Decision.PASS, f"城市匹配（按简历推断的当前城市：{resume_city}）", evidence
        )
    if prefs.get("accept_relocation"):
        return RuleCheck(
            "city",
            "城市",
            Decision.PASS,
            f"岗位在{jd_city}，你当前在{resume_city}（你已设置接受异地）",
            evidence,
        )
    return RuleCheck(
        "city",
        "城市",
        Decision.REJECT,
        f"岗位在{jd_city}，你当前在{resume_city}（未设置期望城市，先按当前城市判断）",
        evidence,
    )


def check_job_nature(job: dict, prefs: dict) -> RuleCheck:
    """工作性质。默认全接受，只有用户在偏好里明确拒绝时才否决。

    这条做成一票否决而不是扣分：不接受外包的人，看到外包岗完全没有意义，
    排在列表后段也只是噪音。
    """
    nature = (job.get("job_nature") or "").strip()
    evidence = _evidence(job, "job_nature")

    if not nature:
        return RuleCheck("job_nature", "工作性质", Decision.PASS, "岗位未标注工作性质", None)

    if nature == "外包" and not prefs.get("accept_outsourcing", True):
        return RuleCheck("job_nature", "工作性质", Decision.REJECT, "这是外包岗位，你已设置不接受外包", evidence)
    if nature == "实习" and not prefs.get("accept_internship", False):
        return RuleCheck("job_nature", "工作性质", Decision.REJECT, "这是实习岗位，你已设置只找全职", evidence)
    if nature == "兼职" and not prefs.get("accept_parttime", False):
        return RuleCheck("job_nature", "工作性质", Decision.REJECT, "这是兼职岗位，你已设置只找全职", evidence)

    return RuleCheck("job_nature", "工作性质", Decision.PASS, f"工作性质：{nature}", evidence)


def check_must_skills(
    resume_profile: dict,
    job: dict,
    prefs: dict,
    semantic_hits: dict[str, dict] | None = None,
) -> RuleCheck:
    """必备技能：按命中率阈值判定，不是全有全无。

    这一条补上了一个真实的漏洞：早先版本把必备技能当纯扣分项，结果某 Java 岗对
    Go 候选人命中 0/3，却因为学历城市都通过而排在推荐列表里 —— 用户看到的是噪音。
    """
    must = job.get("must_have_skills") or []
    if not must:
        return RuleCheck("must_skills", "必备技能", Decision.PASS, "岗位未列出必备技能")

    tokens = resume_token_set(resume_profile.get("skills") or [])
    hit, miss = split_hits(tokens, must)
    # 语义判定升级的项也算命中：否则"确实具备但换了个说法"的岗位
    # 会在硬约束这一关就被拦掉，后面的打分根本轮不到它
    semantic_hits = semantic_hits or {}
    upgraded = [s for s in miss if s in semantic_hits]
    if upgraded:
        hit = hit + upgraded
        miss = [s for s in miss if s not in semantic_hits]
    ratio = len(hit) / len(must)
    threshold = float(prefs.get("min_must_skill_ratio") or MIN_MUST_SKILL_RATIO)
    summary = f"必备技能命中 {len(hit)}/{len(must)}"
    if upgraded:
        summary += f"（{len(upgraded)} 项由语义判定补上）"

    if ratio < threshold:
        return RuleCheck(
            "must_skills",
            "必备技能",
            Decision.REJECT,
            f"{summary}，低于 {threshold:.0%} 的门槛（缺：{'、'.join(miss)}）",
        )
    return RuleCheck("must_skills", "必备技能", Decision.PASS, summary)


def evaluate_hard_constraints(
    resume_profile: dict,
    job: dict,
    prefs: dict,
    semantic_hits: dict[str, dict] | None = None,
) -> list[RuleCheck]:
    """四条硬约束一次算完。任一 REJECT 即出局，不进入评分。"""
    basic = resume_profile.get("basic") or {}
    return [
        check_education(basic, job),
        check_city(basic, job, prefs),
        check_job_nature(job, prefs),
        check_must_skills(resume_profile, job, prefs, semantic_hits),
    ]
