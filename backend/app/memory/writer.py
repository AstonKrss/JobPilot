"""记忆写入的唯一通道。

为什么单独成模块
    写入逻辑原来长在 `api/profile.py` 的路由函数里。路由的语义是「可以被调用」，
    而产品需要的是「说完话一定发生」—— 两者验收标准不同：接口有测试就算过，
    副作用得看「用户说完这句话之后库里多了什么」。
    评测正是从这里撕开的口子：助手回答「记住了」，数据库里一条没变。

    现在只有这一个入口，对话链路、手动接口、评测走同一条路径，
    「有没有接上」这件事才有唯一答案。

两条入口
    apply_text()     一句话走完整链路：L0 预筛 → L1 抽取 → L2 裁决 → 落库
    apply_actions()  已经抽好的变更直接裁决落库（PlanAgent 顺带抽取走这条）

为什么 PlanAgent 不调 apply_text
    那会每轮多一次模型调用。实测服务商约 6 tok/s，等于每轮 +2~3 秒。
    PlanAgent 已经在读用户原话、已经在输出 intent 与 cities，做的是同一类
    理解工作 —— 让它顺带输出 profile_updates，等于零额外调用拿到同样的结果。
"""

from __future__ import annotations

import logging

import psycopg

from . import extract, profile_repo

logger = logging.getLogger(__name__)

# 给用户看的中文名。写入之后要回一句「已记下什么」——
# 不告诉用户改了什么，等于又变回黑箱
LABELS = {
    "cities": "目标城市",
    "job_nature": "岗位类型",
    "target_roles": "目标岗位",
    "salary_floor": "薪资底线",
    "edu_level": "学历",
    "work_auth": "届别/身份",
    "hard_constraints": "硬性要求",
    "excluded_companies": "排除的公司",
    "accept_outsourcing": "接受外包",
    "accept_relocation": "接受异地",
}


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "接受" if value else "不接受"
    if isinstance(value, (list, tuple)):
        return "、".join(str(v) for v in value) or "（空）"
    if value is None:
        return "（清空）"
    return str(value)


def describe(applied: list[dict]) -> str:
    """把已写入的变更拼成一行人话，用于回复末尾的确认。"""
    parts = []
    for item in applied:
        label = LABELS.get(item["key"], item["key"])
        parts.append(f"{label} → {_fmt(item['new_value'])}")
    return "；".join(parts)


def apply_actions(
    conn: psycopg.Connection,
    actions: list[dict],
    conversation_id: int | None = None,
) -> dict:
    """对一批已抽取的变更做裁决并落库。

    裁决规则仍由 `extract.decide` 决定（exclusive/additive → 写，tentative → 挂起）。
    这里只负责按顺序执行，并把**写完之后的当前值**回填给下一次裁决 ——
    一句话里改同一个槽位两次（「只找佛山，广州也留着」）时，
    第二次必须看到第一次的结果，否则两次都基于旧值合并。
    """
    if not actions:
        return {"applied": [], "pending": [], "skipped": []}

    current = profile_repo.get_flat_profile(conn)
    applied: list[dict] = []
    pending: list[dict] = []
    skipped: list[dict] = []

    for action in actions:
        key = action.get("key")
        decision = extract.decide(action, current.get(key))
        if decision == "apply":
            result = profile_repo.apply_update(conn, action, conversation_id)
            updated = {**result, "evidence": action.get("evidence")}
            applied.append(updated)
            current[key] = result.get("new_value")
        elif decision == "pending":
            # 表述不确定（"杭州可能也行吧"）：挂起等用户确认，先不写。
            # 直接写会把一句猜测变成长期设定
            pending.append(action)
        else:
            skipped.append(action)

    return {"applied": applied, "pending": pending, "skipped": skipped}


async def apply_text(
    conn: psycopg.Connection,
    text: str,
    conversation_id: int | None = None,
) -> dict:
    """一句话走完整写入链路。返回每一层的判定，便于解释"为什么记了/没记"。"""
    text = (text or "").strip()
    if not text:
        return {"triggered": False, "reason": "内容为空", "applied": [], "pending": []}

    if not extract.should_extract(text):
        return {
            "triggered": False,
            "reason": "L0 规则预筛未命中：没有同时出现槽位线索与偏好信号",
            "applied": [],
            "pending": [],
        }

    current = profile_repo.get_flat_profile(conn)
    updates = await extract.extract_updates(text, current)
    result = apply_actions(conn, updates, conversation_id)
    return {"triggered": True, **result}
