"""事实核查：判断改写示例是否引入了原文没有的内容。

分两层，顺序是有意的
    第一层规则（本模块）：比对数字、英文专名。这类违规有客观证据，一条正则就能抓，
    不花钱也不会误判。跑在 LLM 之前，能挡掉大部分硬造假。
    第二层语义（见 pipeline.py）：规则通过后再交给模型判断"职责程度"是否被抬高。
    先规则后模型，是因为让模型去数「3000万」在不在原文里，既贵又不可靠。

边界设定（用户决策为「宽松」，但保留一条硬下限）
    允许：补充通用表述、职责归纳、句式重组（「负责后端开发」→「负责后端服务的设计与实现」）
    禁止：新增数字、英文专名（技术名 / 公司名 / 项目代号）

为什么硬下限不能放宽
    通用表述被追问时还能圆回来；「日均 3000 万笔」被追问一次就穿。
    后者不是表述问题，是证据问题，而对求职者来说，面试穿帮的代价远大于简历好看。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 数字：整数、小数、千分位、带单位（万 / 亿 / % / K）
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*\s*(?:万|亿|%|K|k|ms|s\b)?")
# 中文数字串。「三千万」与「3000万」必须被视为同一个数值，
# 否则示例只是把中文换成阿拉伯数字，就会被误判成凭空编造
_CN_NUM_RE = re.compile(r"[零一二两三四五六七八九十百千万亿]{2,10}")
# 英文专名：技术栈、公司代号、指标名（P99 / QPS / Kafka / Kubernetes）
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+#_-]{1,}")
# 英文虚词与常见非专名，避免把 "the" "and" 当成新事实
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "have", "has", "had", "will", "can", "our", "your", "their", "its", "all",
    "not", "but", "you", "use", "used", "using", "based", "into", "over",
}

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG_UNITS = {"万": 10_000, "亿": 100_000_000}

# 包含中文数字字符但并非数量的常见词，直接跳过避免误报
_CN_FALSE_POSITIVES = {"万一", "千万不要", "百分之", "一般", "一致", "统一", "一线", "十分"}


def cn_to_number(text: str) -> float | None:
    """把「三千万」「一百二十」转成数值。无法解析时返回 None。"""
    if not text or text in _CN_FALSE_POSITIVES:
        return None
    total = 0.0
    section = 0.0
    number = 0.0
    for ch in text:
        if ch in _CN_DIGITS:
            number = float(_CN_DIGITS[ch])
        elif ch in _CN_SMALL_UNITS:
            unit = _CN_SMALL_UNITS[ch]
            if number == 0:
                number = 1.0  # 「十」单独出现表示 10
            section += number * unit
            number = 0.0
        elif ch in _CN_BIG_UNITS:
            unit = _CN_BIG_UNITS[ch]
            section = (section + number) * unit
            total += section
            section = 0.0
            number = 0.0
        else:
            return None
    value = total + section + number
    return value if value > 0 else None


def extract_number_values(text: str) -> set[float]:
    """提取全部数值（阿拉伯数字 + 中文数字），用于跨写法比对。

    统一成数值而不是字符串：这是「3000万 = 三千万」能被正确识别的唯一办法。
    误报的代价是让用户困惑（明明没编造却被标红），比漏报更伤信任。
    """
    values: set[float] = set()

    for match in _NUMBER_RE.findall(text or ""):
        token = re.sub(r"\s+", "", match)
        m = re.match(r"(\d+(?:[.,]\d+)*)(万|亿|K|k|%|ms)?", token)
        if not m:
            continue
        try:
            base = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = m.group(2)
        if unit == "万":
            base *= 10_000
        elif unit == "亿":
            base *= 100_000_000
        elif unit in ("K", "k"):
            base *= 1_000
        values.add(base)

    for match in _CN_NUM_RE.findall(text or ""):
        number = cn_to_number(match)
        if number is not None:
            values.add(number)

    return values


@dataclass(slots=True)
class FactCheckResult:
    passed: bool
    issues: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return "；".join(self.issues) if self.issues else "通过"


def _normalize(text: str) -> str:
    """归一去空白，避免「3000 万」与「3000万」被判成不同。"""
    return re.sub(r"\s+", "", text or "")


def extract_latin_terms(text: str) -> set[str]:
    out: set[str] = set()
    for match in _LATIN_RE.findall(text or ""):
        if match.lower() in _STOPWORDS:
            continue
        if len(match) < 2:
            continue
        out.add(match)
    return out


def _fmt_num(value: float) -> str:
    """把数值还原成易读写法，用于错误提示（30000000 → 3000万）。"""
    if value >= 100_000_000:
        return f"{value / 100_000_000:g}亿"
    if value >= 10_000:
        return f"{value / 10_000:g}万"
    return f"{value:g}"


def check_example(example: str, source_text: str) -> FactCheckResult:
    """规则层核查。source_text 是简历原文（按阅读顺序拼成）。

    判定口径：示例里出现的数值与英文专名，必须能在原文里找到。
    数值按「数值」而非字符串比对 —— 「三千万」与「3000万」是同一个事实的两种写法，
    按字符串比会把正常改写判成造假。
    「找不到」即视为新增事实，这是硬下限，不因「宽松」而放宽。
    """
    if not example or not example.strip():
        return FactCheckResult(True, [])

    norm_source = _normalize(source_text)
    issues: list[str] = []

    source_values = extract_number_values(source_text)
    for value in extract_number_values(example):
        if value not in source_values:
            issues.append(f"新增数字「{_fmt_num(value)}」，原文中未出现")

    source_lower = norm_source.lower()
    for term in extract_latin_terms(example):
        if term.lower() not in source_lower:
            issues.append(f"新增专名「{term}」，原文中未出现")

    return FactCheckResult(not issues, issues)
