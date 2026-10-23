"""用户画像：槽位化 + 证据链 + 表述强度裁决。

为什么不是"每句话都让模型判断要不要更新画像"
    那样一天上百次调用，性价比极低。改成三层漏斗：
      L0 规则预筛（零成本）—— 信号词 × 槽位词同现才放行，挡住绝大多数闲聊
      L1 异步抽取（一次小调用）—— 输出结构化槽位变更，带 modality
      L2 代码裁决 —— 按 modality 决定覆盖 / 合并 / 挂起，不依赖模型自评的信心分

为什么用 modality 而不是 confidence
    让模型给 0-1 的置信度基本是编的，同一个模型两次能差 0.3。
    但"用户是排他表述还是补充表述"是明确的语义类型：
      「以后只找广州」→ exclusive → 直接覆盖
      「广州也可以」 → additive  → 合并进城市数组，佛山保留
    这个区分置信度做不到，modality 一行代码就能分。
"""

from __future__ import annotations

import logging
import re

from ..llm.deepseek import chat_json, parse_json_object

logger = logging.getLogger(__name__)

# L0 预筛：表达偏好变化的信号词。宁可多放几个，误判代价只是多一次小调用
SIGNAL_WORDS = (
    "只", "仅", "必须", "一定", "不考虑", "排除", "以后", "以后都", "从现在",
    "记住", "改成", "换成", "也可以", "也行", "都行", "无所谓", "优先",
    "不想", "别", "只要", "非", "限定", "接受", "不接受",
)

# 槽位提示词：命中这些才可能是画像变更
SLOT_HINTS = {
    "cities": ("广州", "深圳", "佛山", "北京", "上海", "杭州", "成都", "武汉", "南京",
               "东莞", "珠海", "惠州", "中山", "苏州", "西安", "长沙", "合肥", "厦门"),
    "job_nature": ("实习", "校招", "社招", "全职", "兼职", "远程", "外包", "派遣"),
    "salary_floor": ("k", "K", "万", "薪", "工资", "月薪", "年薪", "底薪"),
    "edu_level": ("本科", "硕士", "研究生", "大专", "博士", "学历"),
    "work_auth": ("应届", "2028", "2027", "2026", "届", "毕业"),
}

# 允许写入的槽位，防止模型自造字段
ALLOWED_KEYS = set(SLOT_HINTS) | {
    "target_roles", "hard_constraints", "excluded_companies",
    "accept_outsourcing", "accept_relocation",
}

EXTRACT_PROMPT = """你是一个求职偏好的抽取器。判断用户这句话是否包含**需要长期记住的求职偏好变化**。

当前画像：
{profile}

只输出 JSON，格式：
{{"updates": [{{"key": "...", "op": "replace|add|remove", "value": ..., "modality": "...", "evidence": "用户原话片段"}}]}}

规则：
1. key 只能取：cities / job_nature / salary_floor / edu_level / work_auth /
   target_roles / hard_constraints / excluded_companies /
   accept_outsourcing / accept_relocation
2. modality 必须明确：
   - exclusive：排他表述（"只找""仅考虑""不考虑""以后都"）
   - additive：补充表述（"也可以""都行""顺便看看"）
   - tentative：不确定（"可能""也许""再看看""听说"）
3. evidence 必须是用户原话里的片段，不要自己组织语言
4. 闲聊、提问、对岗位的评论都不算偏好变化 → {{"updates": []}}
5. 不要把临时的搜索条件当成长期偏好（"这次先看看深圳的" ≠ 长期只要深圳）"""


def should_extract(text: str) -> bool:
    """L0 规则预筛。返回 True 才值得花一次模型调用。"""
    if not text or len(text) < 4 or len(text) > 500:
        return False
    if not any(w in text for w in SIGNAL_WORDS):
        return False
    # 信号词之外还要有具体槽位线索，否则"我只有一周时间"这种也会触发
    return any(hint in text for hints in SLOT_HINTS.values() for hint in hints)


async def extract_updates(text: str, current: dict) -> list[dict]:
    """L1 抽取。返回待裁决的变更列表（已过滤掉非法 key）。"""
    profile_text = "\n".join(
        f"- {k}: {v.get('value') if isinstance(v, dict) else v}" for k, v in current.items()
    ) or "（空）"
    result = await chat_json(
        EXTRACT_PROMPT.format(profile=profile_text),
        f"用户这句话：{text}",
        what="画像抽取",
        max_tokens=600,
    )
    data = parse_json_object(result.text, what="画像抽取")
    updates = data.get("updates") or []
    clean: list[dict] = []
    for u in updates:
        if not isinstance(u, dict):
            continue
        key = u.get("key")
        if key not in ALLOWED_KEYS:
            logger.debug("忽略非法画像槽位：%s", key)
            continue
        modality = u.get("modality")
        if modality not in ("exclusive", "additive", "tentative"):
            modality = "tentative"
        clean.append(
            {
                "key": key,
                "op": u.get("op") or "replace",
                "value": u.get("value"),
                "modality": modality,
                "evidence": (u.get("evidence") or "").strip()[:200],
            }
        )
    return clean


def decide(action: dict, current_value) -> str:
    """L2 代码裁决：返回 apply / pending / skip。

    故意不做"覆盖 vs 合并"的复杂推断，全部由 modality 决定：
    排他 → 覆盖；补充 → 合并；不确定 → 挂起等用户确认。
    """
    modality = action.get("modality")
    if modality == "exclusive":
        return "apply"
    if modality == "additive":
        return "apply"
    if modality == "tentative":
        return "pending"
    return "skip"
