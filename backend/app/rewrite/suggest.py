"""生成改写建议（M3 第一段）。

两条约束决定了这个 prompt 的形状，都来自已确认的决策：

1. 不报「表述质量」类问题
   用户明确没勾这一类。理由是那属于主观评价（「动词弱」「缺量化」），
   不同人看法不同，而且容易滑向代写。所以每条建议必须锚定到
   「某处硬伤」或「某条岗位要求」，否则不报。

2. 技能缺口只能建议「确认并补充真实经历」，不能建议直接写上
   这是防止变相造假的最后一环。匹配说缺 Kafka，改写如果说「把 Kafka 加到技能里」，
   事实核查也拦不住 —— 因为它确实是用户主动同意加的。所以措辞上必须在源头卡死。
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..llm.deepseek import chat_json, parse_json_object
from ..llm.errors import PermanentError

logger = logging.getLogger(__name__)

CATEGORY_LABEL = {
    "resume_defect": "简历硬伤",
    "jd_gap": "岗位必备要求缺证据",
    "nice_gap": "加分项未体现",
}

SYSTEM_PROMPT = """你是简历顾问，为求职者指出简历中可以改进的问题。

硬性规则：
1. 每条建议必须锚定具体对象：或指向简历中的某一段（anchor 填公司名/字段名），
   或对应岗位的某条要求（jd_requirement 填 JD 原文）。禁止「整体表达可以更好」这类空话。
2. 只报三类问题：
   resume_defect = 简历硬伤（缺关键字段、时间断层、篇幅严重失衡）
   jd_gap        = 岗位必备要求在简历中找不到证据
   nice_gap      = 岗位加分项未体现
   不要评价文笔、语气、主观印象。
3. example 是改写示例，只能使用简历原文已有的信息。
   严禁新增数字、技术名、项目名、公司名；严禁把「参与」改成「主导」这类抬高职责程度的改写。
4. 技能缺口只能这样建议：让用户确认自己是否真的具备，若具备则补充真实经历证据。
   绝对不得建议直接把某项技能写进技能列表 —— 那等于教人编造。
5. jd_gap 的 jd_requirement 必须逐字引用 JD 中对应的那条要求，且该要求本身必须是
   技能、学历、年限数值、工作性质之一，并使用户能看出「这条要求在简历里找不到证据」。
   不要用「年限要求 3-5 年」去支撑「这段经历缺少量化描述」这类结论 —— 两者没有因果关系。
   描述是否量化属于表述问题，不在本次报告范围内，不要报。
6. 严重程度：resume_defect 中缺失关键信息为 high；jd_gap 缺必备技能为 high，
   年限差距为 medium；nice_gap 一律 low。
7. 只输出 JSON 对象，不要任何解释文字或 Markdown 代码块。"""

USER_TEMPLATE = """简历画像：
{profile_json}

简历原文（改写示例只能引用这里的措辞）：
<<<
{source_text}
>>>
{job_section}{gap_section}
请输出如下结构的 JSON：
{{
  "suggestions": [
    {{
      "category": "resume_defect|jd_gap|nice_gap",
      "severity": "high|medium|low",
      "anchor": "指向简历的哪一段（公司名或字段名）",
      "problem": "问题是什么（陈述事实，不做主观评价）",
      "direction": "往哪个方向改（可操作的动作）",
      "example": "改写示例；若这条不适合给示例则填 null",
      "jd_requirement": "对应的 JD 原文要求；普适体检时填 null"
    }}
  ]
}}"""


class SuggestionItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: Literal["resume_defect", "jd_gap", "nice_gap"]
    severity: Literal["high", "medium", "low"] = "medium"
    anchor: str | None = None
    problem: str
    direction: str | None = None
    example: str | None = None
    jd_requirement: str | None = None


class SuggestionBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    suggestions: list[SuggestionItem] = Field(default_factory=list)


def build_job_section(job: dict | None) -> str:
    if not job:
        return ""
    must = job.get("must_have_skills") or []
    nice = job.get("nice_to_have_skills") or []
    parts = [
        "\n目标岗位：",
        f"- 名称：{job.get('title') or '未标注'}",
        f"- 城市：{job.get('city') or '未标注'}",
        f"- 学历要求：{job.get('edu_level') or '未标注'}",
        f"- 工作性质：{job.get('job_nature') or '未标注'}",
        f"- 年限：{job.get('exp_min') if job.get('exp_min') is not None else '未标注'}"
        f"~{job.get('exp_max') if job.get('exp_max') is not None else '不限'}",
        f"- 必备技能：{'、'.join(must) if must else '未列出'}",
        f"- 加分项：{'、'.join(nice) if nice else '未列出'}",
    ]
    return "\n".join(parts)


def build_gap_section(gap: dict | None) -> str:
    """把匹配引擎已经算出的缺口直接给模型，避免它自己再对比一遍。

    复用而不是重新推理：匹配层的筛选逻辑经过规则回归验证，
    让模型重新对比既可能得出不同结论，也白花一次推理。
    """
    if not gap:
        return ""
    lines = ["\n系统已算出的差距（请以此为准，不要自行重新判断）："]
    missed = gap.get("missed_skills") or []
    if missed:
        lines.append(f"- 必备技能未覆盖：{'、'.join(missed)}")
    if gap.get("experience_note"):
        lines.append(f"- {gap['experience_note']}")
    if gap.get("city_note"):
        lines.append(f"- {gap['city_note']}")
    if len(lines) == 1:
        lines.append("- 未发现硬性差距")
    return "\n".join(lines)


async def generate_suggestions(
    profile: dict,
    source_text: str,
    *,
    job: dict | None,
    gap: dict | None,
    session_id: str,
    extra_instruction: str | None = None,
) -> list[SuggestionItem]:
    """生成建议。extra_instruction 用于驳回重写时带入具体的核查未通过原因。"""
    user = USER_TEMPLATE.format(
        profile_json=json.dumps(profile, ensure_ascii=False, indent=1),
        source_text=source_text,
        job_section=build_job_section(job),
        gap_section=build_gap_section(gap),
    )
    if extra_instruction:
        user += f"\n\n上一版建议存在以下问题，必须修正后重新生成：\n{extra_instruction}"

    what = "简历建议生成" + ("（定向）" if job else "（普适体检）")
    result = await chat_json(SYSTEM_PROMPT, user, what=what, session_id=session_id, max_tokens=6000)
    payload = parse_json_object(result.text, what=what)

    try:
        batch = SuggestionBatch.model_validate(payload)
    except ValidationError as exc:
        raise PermanentError(
            "建议结果结构不符合约定", detail=f"{what}: {exc.errors()[:3]}"
        ) from exc

    logger.info("%s：生成 %d 条，¥%.4f", what, len(batch.suggestions), result.cost_cny)
    return batch.suggestions
