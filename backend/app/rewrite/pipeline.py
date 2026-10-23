"""改写编排：生成建议 → 事实核查 → 驳回修正 → 最多 2 轮。

循环出口（已决策）
    初稿 + 最多 1 次修正。末轮仍未通过的条目**不隐藏**，
    保留并标注「未通过事实校验」，让用户自己判断。

核查为什么分两层且顺序固定
    规则层（数字 / 英文专名）先跑：有客观证据、零成本、零误判。
    语义层（职责程度是否被抬高）后跑：只能靠模型，且要花钱。
    让规则层先挡掉硬造假，语义层只处理「参与→主导」这类需要理解的改写，
    调用次数和成本都能压下来。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..llm.deepseek import chat_json, parse_json_object
from ..llm.errors import PermanentError
from .factcheck import check_example
from .suggest import SuggestionItem, generate_suggestions

logger = logging.getLogger(__name__)

MAX_ROUNDS = 2  # 初稿 + 1 次修正

SEMANTIC_SYSTEM = """你在做简历改写的事实核查。给你简历原文和若干条改写示例，
判断每条示例是否引入了原文没有的事实。

判定标准（宽松但有底线）：
允许：通用表述、职责归纳、句式重组、把分散信息合并表达。
禁止：
- 把「参与」写成「主导」、「协助」写成「负责」这类抬高职责程度的改写
- 把「了解」「熟悉」升级为「精通」
- 出现原文没有的具体成果、规模、影响

只输出 JSON：{"results":[{"index":0,"ok":true,"issue":null}]}
issue 在 ok 为 false 时必须写明具体是哪处、原文是什么。"""

FIX_SYSTEM = """你是简历改写员。给你一条建议及其改写示例，以及核查未通过的原因。
请只修正示例，使其不再引入原文没有的事实。

如果这条建议在不编造的前提下根本无法给出示例，把 example 设为 null，
并在 note 里说明原因 —— 不要硬凑。

只输出 JSON：{"example": "修正后的示例或 null", "note": "说明"}"""


@dataclass(slots=True)
class CheckedSuggestion:
    item: SuggestionItem
    fact_status: Literal["passed", "flagged"]
    fact_issue: str | None = None
    attempts: int = 1


@dataclass(slots=True)
class PipelineResult:
    suggestions: list[CheckedSuggestion] = field(default_factory=list)
    rounds: int = 1
    cost_cny: float = 0.0
    flagged_count: int = 0


class _SemanticVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int
    ok: bool
    issue: str | None = None


class _SemanticBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    results: list[_SemanticVerdict] = Field(default_factory=list)


async def _semantic_check(
    candidates: list[tuple[int, str]], source_text: str, session_id: str
) -> dict[int, str]:
    """对规则层已通过的示例做语义核查，返回 {下标: 问题描述}（仅未通过的）。"""
    if not candidates:
        return {}

    listing = "\n".join(f"{idx}. {example}" for idx, example in candidates)
    user = f"""简历原文：
<<<
{source_text}
>>>

待核查的改写示例：
{listing}

请逐条判断，输出 JSON。"""
    result = await chat_json(
        SEMANTIC_SYSTEM, user, what="建议事实核查（语义）", session_id=session_id, max_tokens=2000
    )
    payload = parse_json_object(result.text, what="建议事实核查（语义）")
    try:
        batch = _SemanticBatch.model_validate(payload)
    except ValidationError as exc:
        # 核查结果本身格式坏了，不能当成"通过" —— 那等于放行未核查的内容
        raise PermanentError("事实核查结果结构异常", detail=f"{exc.errors()[:3]}") from exc

    issues: dict[int, str] = {}
    for verdict in batch.results:
        if not verdict.ok:
            issues[verdict.index] = verdict.issue or "语义核查未通过"
    return issues


async def _check_round(
    items: list[SuggestionItem], source_text: str, session_id: str
) -> list[str | None]:
    """返回与 items 等长的问题列表（None 表示通过）。

    只返回问题列表，不汇总成本：成本已在 chat_json 内逐次记录，
    这里再累加一遍容易与实际调用次数对不上。
    """
    issues: list[str | None] = [None] * len(items)
    pending: list[tuple[int, str]] = []  # (index, example)

    for index, item in enumerate(items):
        if not item.example:
            continue  # 没有示例就不存在示例造假
        rule = check_example(item.example, source_text)
        if not rule.passed:
            issues[index] = "规则核查：" + rule.summary
            continue
        pending.append((index, item.example))

    if not pending:
        return issues

    semantic = await _semantic_check(pending, source_text, session_id)
    for index, _ in pending:
        if index in semantic:
            issues[index] = "语义核查：" + semantic[index]
    return issues


async def _fix_one(
    item: SuggestionItem, issue: str, source_text: str, session_id: str
) -> tuple[str | None, str | None]:
    """修正单条示例。返回 (新示例, 说明)。"""
    user = f"""建议问题：{item.problem}
改写方向：{item.direction or '（未给出）'}
原改写示例：{item.example}
核查未通过原因：{issue}

简历原文：
<<<
{source_text}
>>>"""
    result = await chat_json(FIX_SYSTEM, user, what="建议示例修正", session_id=session_id, max_tokens=1500)
    payload = parse_json_object(result.text, what="建议示例修正")
    example = payload.get("example")
    note = payload.get("note")
    return (example if isinstance(example, str) and example.strip() else None), (
        note if isinstance(note, str) else None
    )


async def build_suggestions(
    profile: dict,
    source_text: str,
    *,
    job: dict | None,
    gap: dict | None,
    session_id: str,
) -> PipelineResult:
    items = await generate_suggestions(
        profile, source_text, job=job, gap=gap, session_id=session_id
    )
    issues = await _check_round(items, source_text, session_id)

    result = PipelineResult(rounds=1)
    rejected_idx = [i for i, issue in enumerate(issues) if issue]
    logger.info("初稿 %d 条，核查未通过 %d 条", len(items), len(rejected_idx))

    if rejected_idx:
        result.rounds = 2
        for index in rejected_idx:
            item = items[index]
            issue = issues[index] or "未知"
            new_example, note = await _fix_one(item, issue, source_text, session_id)
            if new_example is None:
                # 无法在不编造的前提下给出示例：清空示例，建议本身保留
                item.example = None
                item.direction = (item.direction or "") + (
                    f"（生成的示例未通过事实校验，已移除：{note or issue}）"
                )
                issues[index] = issue
                continue

            item.example = new_example
            recheck_rule = check_example(new_example, source_text)
            if not recheck_rule.passed:
                issues[index] = f"{issue}；修正后仍不合规：{recheck_rule.summary}"
                continue
            recheck_sem = await _semantic_check(
                [(index, new_example)], source_text, session_id
            )
            issues[index] = "语义核查：" + recheck_sem[index] if index in recheck_sem else None

    for index, item in enumerate(items):
        issue = issues[index]
        result.suggestions.append(
            CheckedSuggestion(
                item=item,
                # 末轮未通过不隐藏，标 flagged 并保留问题说明
                fact_status="flagged" if issue else "passed",
                fact_issue=issue,
                attempts=result.rounds,
            )
        )
        if issue:
            result.flagged_count += 1

    logger.info(
        "改写完成：%d 条，%d 条未通过事实校验，用了 %d 轮",
        len(result.suggestions),
        result.flagged_count,
        result.rounds,
    )
    return result
