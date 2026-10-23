"""LLM 结构化抽取（简历侧 IE，不是 RAG）。

这个模块的定位是信息抽取：把分段文本映射成固定字段。
不检索、不生成、不润色 —— 生成类工作属于改写层，混在一起会让抽取结果不可复现。

三条硬约束写进了 prompt，因为它们是幻觉的主要入口：
  1) 只依据原文，缺失填 null —— 简历里"没写"和"没写清楚"是两件事，后者不能猜
  2) 技能只抽原文出现过的 —— 否则模型会把 JD 里的常见技能"补"给候选人
  3) edu_fulltime 三态 —— 没提统招就是 null，不能默认成"要求"或"不要求"
"""

from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..llm.deepseek import chat_json, parse_json_object
from ..llm.errors import PermanentError
from .segment import Chunk

logger = logging.getLogger(__name__)

EDU_RANK = {"未知": 0, "其他": 1, "大专": 2, "本科": 3, "硕士": 4, "博士": 5}

SYSTEM_PROMPT = """你是简历信息抽取引擎。从给定文本中抽取结构化字段，严格遵守：

1. 只能依据原文。原文没有的信息填 null 或空数组，禁止推测、补全、美化。
2. 禁止输出原文未出现的公司名、学校名、技能名、职称。
3. 时间统一格式：能确定到月写 "YYYY-MM"，只能确定年份写 "YYYY"，无法确定写 null。
4. edu_fulltime 必须三态：原文明确写"统招/全日制"填 true；明确写"非全日制/自考/成人/函授"填 false；没提填 null。
5. work_years 依据工作经历的起止时间累加估算，保留一位小数；无工作经历填 0。
6. 只输出 JSON 对象，不要任何解释文字或 Markdown 代码块。"""

# 强化提示版：首轮抽取质量差时用它重试。差别在于强调"逐段核对"与"宁缺勿滥"，
# 对分段抽取容易漏读的情况有帮助
SYSTEM_PROMPT_STRICT = """你是简历信息抽取引擎。这一次请格外仔细，逐字核对输入文本。

1. 只能依据原文。原文没有的信息填 null 或空数组，禁止推测、补全、美化。
2. **逐字核对**：把输入里的每一行都看一遍，不要跳过任何一行、不要只看开头。
3. 技能、经历、项目里的要点要抽全，不要只抽最显眼的一两条。
4. 时间统一格式：能确定到月写 "YYYY-MM"，只能确定年份写 "YYYY"，无法确定写 null。
5. edu_fulltime 必须三态：原文明确写"统招/全日制"填 true；明确写"非全日制/自考/成人/函授"填 false；没提填 null。
6. 只输出 JSON 对象，不要任何解释文字或 Markdown 代码块。"""

# 整份一次抽取：首轮分段抽取质量差时的另一种策略。
# 分段会把上下文切碎，跨段的同一条经历可能被拆到两段里；
# 整份一次能让模型看到全局，代价是单次输入更长、输出更容易漏字段
WHOLE_TEMPLATE = """简历全文：

<<<
{text}
>>>

请一次性输出如下结构的 JSON（字段名不可变，未知填 null 或 []）：
{{
  "edu_level": "大专|本科|硕士|博士|其他|未知",
  "edu_fulltime": true|false|null,
  "school": "学校名",
  "major": "专业",
  "graduation_year": 2023,
  "work_years": 3.5,
  "skills": [{{"name": "技能名", "level": "熟练/精通等原文措辞，无则 null", "evidence": "原文依据片段"}}],
  "experiences": [{{"company": "公司", "title": "职位", "city": "工作城市，原文未写则 null", "start": "YYYY-MM", "end": "YYYY-MM 或 null 表示至今", "highlights": ["职责或成果，逐条"]}}],
  "projects": [{{"name": "项目名", "role": "角色", "tech": ["技术栈"], "highlights": ["描述，逐条"]}}]
}}"""

# 可选的抽取策略。默认分段；质量自评不通过时换着试
STRATEGIES = ("segments", "whole", "strict")

USER_TEMPLATE = """简历文本（第 {index} 段，共 {total} 段）：

<<<
{text}
>>>

请输出如下结构的 JSON（字段名不可变，未知填 null 或 []）：
{{
  "edu_level": "大专|本科|硕士|博士|其他|未知",
  "edu_fulltime": true|false|null,
  "school": "学校名",
  "major": "专业",
  "graduation_year": 2023,
  "work_years": 3.5,
  "skills": [{{"name": "技能名", "level": "熟练/精通等原文措辞，无则 null", "evidence": "原文依据片段"}}],
  "experiences": [{{"company": "公司", "title": "职位", "city": "工作城市，原文未写则 null", "start": "YYYY-MM", "end": "YYYY-MM 或 null 表示至今", "highlights": ["职责或成果，逐条"]}}],
  "projects": [{{"name": "项目名", "role": "角色", "tech": ["技术栈"], "highlights": ["描述，逐条"]}}]
}}"""


class SkillItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    level: str | None = None
    evidence: str | None = None


class ExperienceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    company: str | None = None
    title: str | None = None
    # 工作城市。匹配的城市硬约束依赖它：简历侧没有城市，就无法判断 JD 的
    # 「北京」是否匹配。这是 M2 的必要输入，不能靠 JD 侧单方面决定
    city: str | None = None
    start: str | None = None
    end: str | None = None
    highlights: list[str] = Field(default_factory=list)


class ProjectItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str | None = None
    role: str | None = None
    tech: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)


class ResumeExtract(BaseModel):
    """单段抽取结果。字段全部可空：一份简历的某一段本来就可能只有项目没有教育。"""

    model_config = ConfigDict(extra="ignore")

    edu_level: str | None = None
    edu_fulltime: bool | None = None
    school: str | None = None
    major: str | None = None
    graduation_year: int | None = None
    work_years: float | None = None
    skills: list[SkillItem] = Field(default_factory=list)
    experiences: list[ExperienceItem] = Field(default_factory=list)
    projects: list[ProjectItem] = Field(default_factory=list)


def _validate(payload: dict, what: str) -> ResumeExtract:
    try:
        return ResumeExtract.model_validate(payload)
    except ValidationError as exc:
        # 结构不合约等于 prompt 与模型输出脱节，重试无意义，直接暴露
        raise PermanentError(
            "模型输出的字段结构不符合约定",
            detail=f"{what}: {exc.errors()[:3]}",
        ) from exc


async def extract_profile(chunks: list[Chunk], strategy: str = "segments") -> tuple[dict, dict]:
    """按指定策略抽取后合并。

    返回 (画像 dict, 元信息)。元信息含 token 成本，用于对照单份 0.5 元上限。

    三种策略
        segments（默认）：逐段抽再合并。稳，但段落切碎时可能漏。
        whole：整份一次抽。上下文完整，跨段的经历不会被拆散。
        strict：逐段抽，但提示词强调"逐字核对、抽全"。
    首轮质量自评不通过时，由调用方换策略重试。
    """
    partials: list[ResumeExtract] = []
    tokens_in = tokens_out = reasoning_tokens = 0
    cost = 0.0

    # 整次抽取共用一个会话 ID。分段之间的 system prompt 完全相同，
    # 网关的 prompt 缓存因此能命中，成本明显低于每次换会话
    session = f"resume-extract-{uuid.uuid4().hex[:12]}"

    if strategy == "whole":
        text = "\n".join(chunk.text for chunk in chunks)
        calls = [(SYSTEM_PROMPT, WHOLE_TEMPLATE.format(text=text))]
    elif strategy == "strict":
        calls = [
            (
                SYSTEM_PROMPT_STRICT,
                USER_TEMPLATE.format(index=c.index + 1, total=len(chunks), text=c.text),
            )
            for c in chunks
        ]
    else:
        calls = [
            (
                SYSTEM_PROMPT,
                USER_TEMPLATE.format(index=c.index + 1, total=len(chunks), text=c.text),
            )
            for c in chunks
        ]

    for index, (system, user) in enumerate(calls, start=1):
        result = await chat_json(
            system,
            user,
            what=f"简历抽取 {strategy} 第{index}/{len(calls)}段",
            session_id=session,
            # 同一份简历重新解析时复用上次的抽取结果。
            # 三种策略会对同一段文本各试一次，缓存能省掉后面的重复调用
            cache_tag="resume_extract",
        )
        tokens_in += result.tokens_in
        tokens_out += result.tokens_out
        reasoning_tokens += result.reasoning_tokens
        cost += result.cost_cny

        partials.append(_validate(parse_json_object(result.text, what="简历抽取"), "简历抽取"))

    merged = _merge(partials)
    meta = {
        "chunks": len(chunks),
        "strategy": strategy,
        "calls": len(calls),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "reasoning_tokens": reasoning_tokens,
        "cost_cny": round(cost, 4),
    }
    logger.info("简历抽取完成：策略 %s，%d 段，¥%.4f", strategy, len(calls), cost)
    return merged, meta


def _merge(partials: list[ResumeExtract]) -> dict:
    """合并多段结果。

    冲突处理原则：
      · 学历取最高层次（同一份简历里可能既写大专又写后续本科）
      · 工作年限取最大（分段抽时每段只能看到局部经历，各自的累加都偏小）
      · 统招状态一旦有明确值就保留 —— 这是硬约束字段，宁可保留明确信号
      · 技能/经历/项目按业务键去重后拼接
    """
    edu_level = "未知"
    edu_fulltime: bool | None = None
    school = major = None
    graduation_year: int | None = None
    work_years = 0.0
    skills: dict[str, SkillItem] = {}
    experiences: dict[tuple[str, str], ExperienceItem] = {}
    projects: dict[str, ProjectItem] = {}

    for part in partials:
        if part.edu_level and EDU_RANK.get(part.edu_level, 0) >= EDU_RANK.get(edu_level, 0):
            edu_level = part.edu_level
            # 学历变更时同步更新学校/专业/毕业年，避免出现"硕士学历 + 本科学校"
            if part.school:
                school = part.school
            if part.major:
                major = part.major
            if part.graduation_year:
                graduation_year = part.graduation_year

        if part.edu_fulltime is not None:
            edu_fulltime = part.edu_fulltime

        if part.work_years is not None:
            work_years = max(work_years, part.work_years)

        for skill in part.skills:
            key = skill.name.strip().lower()
            if key and key not in skills:
                skills[key] = skill

        for exp in part.experiences:
            key = ((exp.company or "").strip(), (exp.start or "").strip())
            if key == ("", ""):
                continue
            if key in experiences:
                # 同一段经历被两段文本各抽到一部分时，合并 highlights
                experiences[key].highlights.extend(exp.highlights)
            else:
                experiences[key] = exp

        for proj in part.projects:
            key = (proj.name or "").strip().lower()
            if not key:
                continue
            if key in projects:
                projects[key].highlights.extend(proj.highlights)
                for tech in proj.tech:
                    if tech not in projects[key].tech:
                        projects[key].tech.append(tech)
            else:
                projects[key] = proj

    # 当前所在城市取"最近一段有城市信息的经历"。用 start 排序而非 end：
    # end 为 None 表示"至今"，无法直接比较，而最近一段的开始时间最大这一规律成立
    current_city = None
    with_city = [e for e in experiences.values() if e.city]
    if with_city:
        current_city = max(with_city, key=lambda e: e.start or "").city

    return {
        "basic": {
            "edu_level": edu_level,
            "edu_fulltime": edu_fulltime,
            "school": school,
            "major": major,
            "graduation_year": graduation_year,
            "work_years": round(work_years, 1),
            "current_city": current_city,
        },
        "skills": [s.model_dump() for s in skills.values()],
        "experiences": [e.model_dump() for e in experiences.values()],
        "projects": [p.model_dump() for p in projects.values()],
    }
