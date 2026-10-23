"""工作台的"下一步该做什么"。

为什么用规则而不是让模型写：
    这个提示要在工作台一打开就出现，模型一次要 10 秒以上（服务商生成速度约 6 tok/s），
    等它还不如不给。而且判断依据是明确的几个状态位（匹配结论 / 缺口数 /
    投递状态 / 最后一条消息是谁说的），规则能覆盖绝大多数情况，还免费、可解释。

    模型只在该写"面试怎么准备"这种需要结合岗位细节的地方才有优势，
    那属于另一个功能，不该拖慢这里的首屏。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workbench", tags=["workbench"])


class NextStepRequest(BaseModel):
    job_id: int
    # 匹配结论：pass / confirm / reject / none（没跑过匹配）
    match_decision: str = "none"
    missed_skills: list[str] = []
    # 投递状态：candidate / applied / talking / interview / offer / rejected / none
    app_status: str = "none"
    # 最后一条聊天记录是谁说的：HR / 我 / None
    last_role: str | None = None
    chat_count: int = 0


# 紧急程度决定界面上的颜色，不参与逻辑
def _step(action: str, reason: str, urgency: str = "normal") -> dict:
    return {"action": action, "reason": reason, "urgency": urgency}


@router.post("/next-step")
async def next_step(payload: NextStepRequest) -> dict:
    """按当前状态给一条"现在做什么"。只会给一条 —— 给三条等于没给。"""
    decision = payload.match_decision
    status = payload.app_status
    missed = payload.missed_skills or []

    # 1. 还没跑匹配：什么都不知道，先跑
    if decision == "none":
        return _step("先跑一次匹配", "还没有和这份简历的匹配结论，无法判断值不值得投")

    # 2. 硬约束不符：这是最该劝退的情况，别让用户白投
    if decision == "reject":
        return _step(
            "建议放弃",
            "硬约束不满足（学历 / 届别 / 城市 / 岗位性质），投了大概率不会有回音",
            urgency="muted",
        )

    # 3. 信息不足：结论本来就不可信，先补数据
    if decision == "confirm":
        return _step(
            "先确认岗位信息",
            "岗位关键字段有缺失，现在的结论不可信；点开原站核实学历、届别、工作性质再决定",
            urgency="warn",
        )

    # 到这里是 pass，按投递进度往下走
    if status in ("none", "candidate"):
        if missed:
            top = "、".join(missed[:2])
            return _step(
                f"补 {top} 再投",
                f"还差 {len(missed)} 项必备技能。补上其中任意一项，通过率都会明显不同；"
                "如果只是简历没写全（不是不会），可以在简历里补一句",
            )
        return _step("可以投了", "硬约束全通过、必备技能没有缺口，优先投这一家", urgency="high")

    if status == "applied":
        if payload.chat_count == 0:
            return _step(
                "主动打招呼",
                "已经投递但还没有任何沟通记录。招聘方每天看几十份简历，不主动基本会被埋",
                urgency="high",
            )
        return _step("等回复，3 天后没动静就再问一句", "已投递且有过沟通")

    if status == "talking":
        if payload.last_role == "HR":
            return _step("HR 最新消息还没回", "拖久了对方会转投别人", urgency="high")
        return _step("等对方回复", "最后一句是你说的，别连续追问")

    if status == "interview":
        if missed:
            return _step(
                "重点准备这几项",
                f"面试大概率会问：{'、'.join(missed[:3])}（岗位要求里有，你简历没体现）",
                urgency="high",
            )
        return _step("准备项目细节", "岗位要求你都覆盖了，重点讲清项目的量化结果")

    if status == "offer":
        return _step("谈 offer", "确认薪资结构、试用期、到岗时间，以及是否能接受")

    if status == "rejected":
        return _step("可以移出跟踪", "已经不合适，保留记录只是为了复盘")

    raise HTTPException(status_code=400, detail=f"未知状态 {status}")
