"""工作台的"下一步该做什么"。

为什么用规则而不是让模型写：
    这个提示要在工作台一打开就出现，模型一次要 10 秒以上（服务商生成速度约 6 tok/s），
    等它还不如不给。而且判断依据是明确的几个状态位（匹配结论 / 缺口数 /
    投递状态 / 最后一条消息是谁说的），规则能覆盖绝大多数情况，还免费、可解释。

    模型只在该写"面试怎么准备"这种需要结合岗位细节的地方才有优势，
    那属于另一个功能，不该拖慢这里的首屏。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from ..db.pool import get_pool
from ..match import semantic

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/workbench", tags=["workbench"])

# 相似岗位的阈值。实测标题向量的分布：同义不同写法 0.90~1.00
# （高级后端 ↔ 资深后端 0.908、后端实习 ↔ Java后端实习 0.947），
# 无关岗位 0.20。取 0.70 是保守下沿 —— 宁可少召回几个，
# 也不要把"短视频剪辑"混进"Java 后端"的同类参考里。
SIMILAR_THRESHOLD = 0.70


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

    # 到这里是 pass，按投递进度往下走。
    #
    # 投递状态只保留两个：未投 / 已投。原来有六个（沟通中 / 面试 / offer /
    # 不合适），实测一次都没被手动点过 —— 手动维护状态是纯负担，而平台自带
    # 投递记录。现在只需要"投了没"这一个事实，其余信息由聊天记录带出来。
    # 旧数据里的 talking / interview / offer 一律视为"已投"。
    applied = status in ("applied", "talking", "interview", "offer")

    if not applied:
        if missed:
            top = "、".join(missed[:2])
            return _step(
                f"补 {top} 再投",
                f"还差 {len(missed)} 项必备技能。补上其中任意一项，通过率都会明显不同；"
                "如果只是简历没写全（不是不会），可以在简历里补一句",
            )
        return _step("可以投了", "硬约束全通过、必备技能没有缺口，优先投这一家", urgency="high")

    # 已投递
    if payload.chat_count == 0:
        return _step(
            "主动打招呼",
            "已经投递但还没有任何沟通记录。招聘方每天看几十份简历，不主动基本会被埋",
            urgency="high",
        )
    if payload.last_role == "HR":
        return _step("HR 最新消息还没回", "拖久了对方会转投别人", urgency="high")
    return _step("等对方回复", "最后一句是你说的，别连续追问；3 天没动静再问一句")


@router.get("/similar/{job_id}")
async def similar_jobs(job_id: int, limit: int = 3) -> dict:
    """找跟这个岗位相似的岗位，并带上它们各自的投递结果。

    为什么用标题向量而不是必备技能重合：技能名是自由文本（「Java」/
    「Go 或 Java」/「Java 后端」），按精确重合 ≥2 项筛，实测只能覆盖
    34% 的岗位，功能大部分时候会显示为空。标题向量的覆盖率接近 100%，
    且区分度好（同义 0.9+，无关 0.2）。

    为什么要把"投了几家、几家有回应"摆进来：这是本系统唯一能提供的
    真实经验数据。它比一个算出来的匹配分更能改变决定 ——
    "这类岗位我投了 3 家都没回音"是事实，"你 78 分"是推测。
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT job_id, title, coalesce(must_have_skills, '[]'::jsonb) FROM job_profile WHERE is_current"
        ).fetchall()
        target_row = next((r for r in rows if int(r[0]) == job_id), None)
        if target_row is None:
            return {"job_id": job_id, "items": [], "reason": "该岗位还没有画像"}
        target = target_row[1]
        target_skills = set(target_row[2] or [])

        titles = [r[1] for r in rows]
        # embed 是同步 CPU 计算（约 10ms/条，且有库缓存），扔到线程里避免阻塞事件循环
        vectors = await asyncio.to_thread(semantic.embed, titles + [target])
        if not vectors:
            return {"job_id": job_id, "items": [], "reason": "语义层不可用，无法比较相似度"}

        import numpy as np

        mat = np.array(vectors[:-1])
        sims = mat @ np.array(vectors[-1])  # 向量已 L2 归一化，点积即余弦

        # 光看标题会误判：「前端开发工程师」与「高级后端工程师」相似度 0.84，
        # 因为模板化标题的语义天然接近，但两者根本不是同一类岗位。
        # 所以再加一条技能约束：共同必备技能 ≥ 1 项，或者标题相似度足够高
        # （≥0.85 那种基本是同一标题的不同写法，技能名写法不统一很正常）。
        #
        # 但只在目标岗位自己抽到了技能时才这样筛。目标岗位技能为空时
        # （摘要级岗位、JD 没写要求的岗位），"共同技能"恒为 0，
        # 这个约束会把所有候选都误杀 —— 那种情况只能退回纯标题相似度。
        has_skill_data = bool(target_skills)
        candidates: list[tuple[int, str, float, int]] = []
        for idx in np.argsort(-sims):
            row = rows[int(idx)]
            jid = int(row[0])
            score = float(sims[int(idx)])
            if jid == job_id:
                continue
            if score < SIMILAR_THRESHOLD:
                break
            overlap = len(target_skills & set(row[2] or []))
            if has_skill_data and overlap == 0 and score < 0.85:
                continue
            candidates.append((jid, row[1], score, overlap))

        # 技能重合多的排前面：标题像但技能不搭的，价值低于标题一般但技能几乎一样的
        candidates.sort(key=lambda c: (-c[3], -c[2]))
        picked = candidates[:limit]

        ids = [p[0] for p in picked]
        status_map: dict[int, str] = {}
        replied: set[int] = set()
        if ids:
            for jid, status in conn.execute(
                "SELECT job_id, status FROM application WHERE job_id = ANY(%s)", (ids,)
            ).fetchall():
                status_map[int(jid)] = status
            for (jid,) in conn.execute(
                "SELECT DISTINCT job_id FROM chat_log "
                "WHERE job_id = ANY(%s) AND role = 'HR'",
                (ids,),
            ).fetchall():
                replied.add(int(jid))

    items = [
        {
            "job_id": jid,
            "title": title,
            "sim": round(sim, 3),
            "overlap": overlap,
            "status": status_map.get(jid),
            "replied": jid in replied,
        }
        for jid, title, sim, overlap in picked
    ]
    return {
        "job_id": job_id,
        "items": items,
        "total": len(items),
        "applied": sum(1 for i in items if i["status"] not in (None, "candidate")),
        "replied": sum(1 for i in items if i["replied"]),
    }
