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
    # 补的是评测里漏掉的几种说法。「我是」出现在 M05「我是 2028 届本科」——
    # 自我介绍式陈述也是长期信息，但它不含"以后/只/必须"这类祈使语气，
    # 原来的词表一条都不命中
    "我是", "暂时不", "暂不", "最好",
)

# 槽位提示词：命中这些才可能是画像变更
SLOT_HINTS = {
    "cities": ("广州", "深圳", "佛山", "北京", "上海", "杭州", "成都", "武汉", "南京",
               "东莞", "珠海", "惠州", "中山", "苏州", "西安", "长沙", "合肥", "厦门"),
    "job_nature": ("实习", "校招", "社招", "全职", "兼职", "远程", "外包", "派遣"),
    "salary_floor": ("k", "K", "万", "薪", "工资", "月薪", "年薪", "底薪"),
    "edu_level": ("本科", "硕士", "研究生", "大专", "博士", "学历"),
    "work_auth": ("应届", "2028", "2027", "2026", "届", "毕业"),
    # 下面四个槽位在 ALLOWED_KEYS 里，却一直没有 hint 词 ——
    # should_extract 的第二道判断查的就是这张表，所以它们**永远写不进去**。
    # 评测里 5 条漏检有 4 条出在这里：不是模型抽错，是门禁把它们挡在门外
    "target_roles": ("大厂", "国企", "外企", "上市公司", "创业公司", "研究院",
                     "甲方", "乙方", "外包公司", "方向", "岗位类型"),
    "hard_constraints": ("双休", "五险一金", "不加班", "加班", "远程", "弹性",
                         "班车", "包住", "提供住宿", "转正", "965", "955"),
    "excluded_companies": ("字节", "腾讯", "阿里", "美团", "华为", "京东", "百度",
                           "网易", "小米", "滴滴", "拼多多", "这家公司"),
    "accept_relocation": ("异地", "搬迁", "外地", "跨城", "离开", "relocate"),
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
5. 不要把临时的搜索条件当成长期偏好（"这次先看看深圳的" ≠ 长期只要深圳）
6. 槽位语义（这几个容易混，按下面的来）：
   - target_roles：倾向的公司类型或平台，如"大厂""国企""研究院""创业公司"
   - hard_constraints：对工作条件的要求，如"双休""不加班""五险一金""远程"
   - excluded_companies：明确排除的公司或公司类型
   - accept_relocation：是否接受异地或搬迁
   - work_auth：届别、应届身份、毕业年份
   - edu_level：学历
7. 自我介绍里的客观信息也算（"我是 2028 届本科" → work_auth 2028、edu_level 本科）。
   它们是判断岗位资格的基础，不是闲聊。"""


# 强槽位词：本身就是「一个具体的偏好值」，出现即可认为在陈述偏好。
# 和弱提示词（"学历""工资"这类只是话题词）分开，是因为弱提示词单独出现
# 大多是在提问（"学历要求高吗"），强词单独出现大多是在表态。
STRONG_HINTS = frozenset(
    tuple(SLOT_HINTS["cities"])
    + ("实习", "校招", "社招", "全职", "兼职", "远程", "外包", "派遣",
       "双休", "不加班", "大厂", "异地")
)

# 明确指向单次行为的词。命中就按临时条件处理，不进 L1 ——
# 这是放宽门禁的配套：不放这一条，「这次先看看深圳的」会顺畅通过，
# 而它明确说的是"这次"
TEMPORARY_MARKERS = ("这次", "临时", "先随便看看")


def should_extract(text: str) -> bool:
    """L0 规则预筛。返回 True 才值得花一次模型调用。

    判据从「信号词 AND 槽位词」改成「槽位词 AND（信号词 OR 强槽位词）」。
    原因：原来两个词表都要命中，而 10 个槽位里有 4 个根本没配 hint 词，
    它们在任何输入下都不可能通过 —— recall 一直在为一张不完整的表买单。
    放宽的代价是偶尔多调一次模型（"佛山有什么岗位"也会进来，
    模型返回空更新），不会写错数据。

    注意：这条只服务 /observe 手动入口与评测。对话链路走 PlanAgent 顺带抽取，
    不经过 L0 —— 那次模型调用已经付过了，再拿规则砍一刀只会把模型抽对的
    结果又丢掉（评测里 5 条漏检全是这么来的）。
    """
    if not text or len(text) < 4 or len(text) > 500:
        return False
    if any(m in text for m in TEMPORARY_MARKERS):
        return False
    has_slot = any(hint in text for hints in SLOT_HINTS.values() for hint in hints)
    if not has_slot:
        return False
    if any(w in text for w in SIGNAL_WORDS):
        return True
    return any(h in text for h in STRONG_HINTS)


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
    return clean_updates(data.get("updates") or [])


def clean_updates(updates: list) -> list[dict]:
    """校验并规整一批变更：白名单 + modality 兜底 + 证据截断。

    抽成独立函数是因为现在有两条入口会产出变更：L1 抽取，和 PlanAgent
    顺带输出的 profile_updates。两处各写一份校验，迟早一边收了一边没收；
    而"模型自造槽位"一旦落库，画像就成了不可信的黑箱。
    """
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
