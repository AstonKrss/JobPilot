"""抽取质量自评：判断这份画像能不能直接用。

为什么要有这一步
    分段抽取最常见的失败不是报错，而是**安静地抽少了**：
    学历层次没抽到、经历只抽到一半、技能全丢。系统照样返回一份"成功"的画像，
    用户以为解析没问题，但匹配结果莫名其妙地被大批否决。
    所以抽取完必须自己看一眼质量，差就换个策略重来，而不是把问题留给用户。

判定原则
    **每一项都能从原文与画像直接核对，不需要模型参与。**
    引入模型自评会引入"模型说没问题但其实有问题"的新问题，
    而这里是能算出来的就算。

六个检查项
    1. 学历层次缺失 —— 它是硬约束，缺了会让所有要求本科以上的岗位被误杀
    2. 技能为空 —— 技能是匹配的另一半，全空等于这份简历没法匹配
    3. 经历为空 —— 年限、技能证据都从这里来
    4. 抽取内容相对原文过少 —— 说明大段内容没被抽出来
    5. 年限与经历起止自相矛盾 —— 模型估的和简历写的不一致
    6. 画像里出现原文找不到的字段 —— 模型编造，必须当成严重问题
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..normalize.fuzzy import norm_key

# 抽取内容 / 原文 的字符数下限。低于它说明大部分内容没被抽出来
MIN_COVERAGE = 0.12
# 年限自述与经历跨度允许的差（年）。超过就认为自相矛盾
MAX_YEARS_GAP = 1.5

_WEIGHT = {"high": 3.0, "medium": 1.5, "low": 0.5}
_DATE_RE = re.compile(r"(\d{4})[-/.年](\d{1,2})?")


@dataclass(slots=True)
class QualityIssue:
    code: str
    severity: str
    detail: str


@dataclass(slots=True)
class QualityReport:
    score: float
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.score >= 7.0 and not any(i.severity == "high" for i in self.issues)

    @property
    def summary(self) -> str:
        if not self.issues:
            return "抽取质量正常"
        return "；".join(f"{i.detail}" for i in self.issues)

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 2),
            "ok": self.ok,
            "issues": [
                {"code": i.code, "severity": i.severity, "detail": i.detail} for i in self.issues
            ],
        }


def _parse_ym(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    match = _DATE_RE.search(value)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    return year, month


def _months(start: tuple[int, int], end: tuple[int, int]) -> int:
    return max(0, (end[0] - start[0]) * 12 + (end[1] - start[1]))


def assess(profile: dict, source_text: str) -> QualityReport:
    """对抽取结果打分。profile 是合并后的画像，source_text 是脱敏后的原文。"""
    issues: list[QualityIssue] = []
    basic = profile.get("basic") or {}
    skills = profile.get("skills") or []
    experiences = profile.get("experiences") or []
    projects = profile.get("projects") or []

    edu = (basic.get("edu_level") or "").strip()
    if not edu or edu == "未知":
        issues.append(
            QualityIssue("edu_missing", "high", "没有抽到学历层次（硬约束会误杀大量岗位）")
        )

    if not skills:
        issues.append(QualityIssue("skills_empty", "high", "没有抽到任何技能（匹配的一半依据缺失）"))

    if not experiences:
        issues.append(QualityIssue("exp_empty", "high", "没有抽到任何工作经历"))

    if basic.get("work_years") is None:
        issues.append(QualityIssue("years_missing", "medium", "没有抽到工作年限"))

    # 覆盖率：画像里能看到的文字量 vs 原文长度
    text_len = len(source_text or "")
    if text_len >= 200:
        collected = 0
        for item in skills:
            if isinstance(item, dict):
                collected += len(str(item.get("name") or "")) + len(str(item.get("evidence") or ""))
            else:
                collected += len(str(item))
        for exp in experiences:
            collected += len(str(exp.get("company") or "")) + len(str(exp.get("title") or ""))
            for line in exp.get("highlights") or []:
                collected += len(str(line))
        for proj in projects:
            collected += len(str(proj.get("name") or ""))
            for line in proj.get("highlights") or []:
                collected += len(str(line))
        coverage = collected / text_len
        if coverage < MIN_COVERAGE:
            issues.append(
                QualityIssue(
                    "low_coverage",
                    "high",
                    f"抽出的内容只占原文的 {coverage:.0%}，可能有整段没被读到",
                )
            )

    # 年限自述 vs 经历跨度
    years = basic.get("work_years")
    spans: list[int] = []
    # "至今"用当前时间，不能用 2099 —— 否则算出 73 年这种荒唐跨度
    today = (date.today().year, date.today().month)
    for exp in experiences:
        start = _parse_ym(exp.get("start"))
        if not start:
            continue
        end = _parse_ym(exp.get("end")) or today
        spans.append(_months(start, end))
    # 跨度超过 40 年的一律视为解析错误，不参与矛盾判断
    spans = [m for m in spans if m <= 40 * 12]
    if years is not None and spans:
        span_years = max(spans) / 12.0
        if abs(float(years) - span_years) > MAX_YEARS_GAP:
            issues.append(
                QualityIssue(
                    "years_conflict",
                    "medium",
                    f"工作年限写 {years} 年，但经历跨度约 {span_years:.1f} 年，两处对不上",
                )
            )

    # 编造检查：技能名必须能在原文里找到
    norm_source = norm_key(source_text or "")
    for item in skills:
        name = item if isinstance(item, str) else item.get("name")
        if not name:
            continue
        key = norm_key(name)
        if len(key) < 2:
            continue
        if key not in norm_source:
            issues.append(
                QualityIssue("fabricated_skill", "high", f"技能「{name}」在原文中找不到，可能被编造")
            )
            break

    score = 10.0 - sum(_WEIGHT.get(i.severity, 1.0) for i in issues)
    return QualityReport(score=max(0.0, score), issues=issues)
