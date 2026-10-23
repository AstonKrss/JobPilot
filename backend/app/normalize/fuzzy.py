"""检索式归并（归一化 L2）。

为什么这一层不用向量
    机构名与校名是短专名，字面对应关系比语义更可靠：
      · 「清华大学」与「清华附中」字面极近、实体完全不同 —— 向量容易判近
      · 「抖音集团」与「字节跳动」语义同源、字面毫无交集 —— 向量容易判远
    这两类错误在简历里都会造成实质伤害（把附中当清华、把集团当无关公司）。
    检索式方法（后缀归一 + 子串包含 + n-gram + 拼音）结果可解释、可复现，
    且阈值可以在评测集上直接调。向量留给真正存在同义表达的场景：技能名
    （K8s / Kubernetes）、项目描述。

三层分工
    L1 别名表：精确命中，最高优先级
    L2 本模块：给出相似度与理由，自动归并 or 抬给人工
    L3 人工回写：确认结果写回别名表，下次走 L1 —— 这是"越用越准"的闭环
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from pypinyin import Style, lazy_pinyin

logger = logging.getLogger(__name__)

# 自动归并阈值。高于此值直接合并，不再打扰用户
AUTO_MERGE_THRESHOLD = 0.86
# 人工确认阈值。低于此值不打扰用户，直接当新实体入队待日后处理
CONFIRM_THRESHOLD = 0.62

# 法律实体后缀。从长到短依次剥离，"北京字节跳动科技有限公司" → "北京字节跳动科技"
LEGAL_SUFFIXES: tuple[str, ...] = (
    "股份有限公司",
    "有限责任公司",
    "集团有限公司",
    "有限公司",
    "控股集团",
    "股份公司",
    "分公司",
    "公司",
    "集团",
    "控股",
)

# 需要剔除的干扰字符：空白、标点、括号。这些不携带实体信息，却会把同一实体拆成多个键
_NOISE_RE = re.compile(r"[\s\u3000·•\.\-_()（）\[\]【】,，、/\\|:：;；\"'“”‘’*#]+")


def norm_key(text: str) -> str:
    """生成检索键：全半角归一 → 小写 → 去干扰字符 → 剥离法律后缀。

    刻意不做"去地名、去行业词"这类激进归一：
    「北京银行」剥掉「北京」会变成「银行」，与「中国银行」撞车。
    地名差异交给相似度层的子串包含去处理。
    """
    s = unicodedata.normalize("NFKC", text).lower()
    s = _NOISE_RE.sub("", s)

    changed = True
    while changed:
        changed = False
        for suffix in LEGAL_SUFFIXES:
            # 保留至少 2 个字符，避免「公司」这种输入被剥成空串
            if s.endswith(suffix) and len(s) - len(suffix) >= 2:
                s = s[: -len(suffix)]
                changed = True
                break
    return s


def _ngrams(s: str, n: int = 2) -> set[str]:
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: str, b: str) -> float:
    ga, gb = _ngrams(a), _ngrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _pinyin_seq(s: str) -> str:
    return "".join(lazy_pinyin(s, style=Style.NORMAL))


@dataclass(slots=True)
class MatchResult:
    canonical: str | None
    score: float
    reason: str
    needs_confirm: bool


def similarity(a: str, b: str) -> tuple[float, str]:
    """返回 (分数, 理由)。理由要能写进日志与人工确认界面，便于解释为什么这么归。"""
    if not a or not b:
        return 0.0, "empty"
    if a == b:
        return 1.0, "exact"

    shorter, longer = sorted((a, b), key=len)
    # 子串包含是机构名最可靠的信号：「字节跳动」⊂「北京字节跳动科技」。
    # 但不能给满分 —— 「中国银行」⊂「中国银行保险监督管理委员会」是两家不同机构，
    # 用长度占比压低分数，短串占比越低越可疑
    if len(shorter) >= 2 and shorter in longer:
        ratio = len(shorter) / len(longer)
        return 0.80 + 0.19 * ratio, f"substring({ratio:.2f})"

    # 同音不同字：「字节」/「子节」、「清华」/「青华」，拼音完全相同
    if len(a) >= 2 and _pinyin_seq(a) == _pinyin_seq(b):
        return 0.88, "pinyin_equal"

    jac = _jaccard(a, b)
    if jac >= 0.5:
        return 0.85 * jac, f"jaccard({jac:.2f})"

    return jac * 0.6, f"jaccard_low({jac:.2f})"


def best_match(raw: str, candidates: list[tuple[str, str]]) -> MatchResult:
    """在候选别名中找最佳匹配。

    candidates 是 (canonical, alias) 列表。对 raw 与每个 alias 的检索键做比较，
    取分数最高者。同分时取更短的标准名 —— 短名通常是通行叫法。
    """
    key = norm_key(raw)
    if not key:
        return MatchResult(None, 0.0, "empty_key", needs_confirm=False)

    best: tuple[float, str, str] | None = None  # (score, canonical, reason)
    for canonical, alias in candidates:
        score, reason = similarity(key, norm_key(alias))
        if best is None or score > best[0] or (
            score == best[0] and len(canonical) < len(best[1])
        ):
            best = (score, canonical, reason)

    if best is None:
        return MatchResult(None, 0.0, "no_candidate", needs_confirm=False)

    score, canonical, reason = best
    if score >= AUTO_MERGE_THRESHOLD:
        return MatchResult(canonical, score, reason, needs_confirm=False)
    if score >= CONFIRM_THRESHOLD:
        # 有把握但不够确定 → 抬给用户。这一步是 L3 闭环的入口
        return MatchResult(canonical, score, reason, needs_confirm=True)
    # 分数过低：猜错比不猜更糟，交给用户直接指定
    return MatchResult(None, score, reason, needs_confirm=True)
