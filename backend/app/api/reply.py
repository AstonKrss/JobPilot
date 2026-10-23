"""和 HR 沟通时的候选回复。

设计边界（为什么不做自动发送）
    回复代表求职者本人在承诺：薪资期望、到岗时间、是否接受外包。
    这些一旦由程序代答，说错的是求职者。所以这里只生成候选，发送永远由人点。

为什么必须带上 JD 正文
    只给简历不给岗位，"匹配"就无从谈起，模型只能把简历里的技能名词照抄一遍。
    实测过一次踩坑：岗位是餐厅经理，回复里写"我主要做后端，Python、Go、MySQL、
    Redis 都在用"——HR 一眼看出来是群发。根因是聊天页没传 JD，后端又当"未关联岗位"
    处理。所以现在三件事必须同时成立：
      1. 岗位侧一定要有正文（job_profile.description 或 job.raw_text 或页面直接抓）
      2. 简历技能与岗位要求无交集时，显式告诉模型"方向不对口，别硬凑"
      3. 提示词里把"不得搬不相关技术名词"写成铁律，而不是靠模型自觉
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db import repo
from ..db.pool import get_pool
from ..llm.deepseek import chat_json, parse_json_object
from ..match.skills import resume_token_set, split_hits

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reply", tags=["reply"])

# JD 正文进 prompt 的长度上限。太长会挤掉聊天记录，而聊天记录决定"这一句该回什么"
JD_CHAR_LIMIT = 700
RAW_CHAR_LIMIT = 700

SYSTEM_PROMPT = """你是求职者的聊天回复助手。根据岗位信息、候选人简历事实和最近聊天记录，
生成 3 条可以发给 HR 的候选回复。

三种风格各一条：
- concise：简洁版，20-45 字，直接回应，不寒暄
- formal：正式版，60-110 字，说清 1-2 个与岗位对得上的具体事实
- clarify：追问版，先回一句再问清一个关键信息（薪资范围 / 到岗时间 / 具体职责 / 团队规模 / 工作地点班次）

铁律（优先级高于下面所有要求）：
1. 只谈这个岗位真正关心的事。岗位要求什么，回复就回应什么。
2. 禁止拿简历里与岗位无关的名词充数。判断标准很硬：如果简历里的某个技能/技术栈
   在岗位描述里找不到任何对应关注点，就不要写进回复。
   典型错误：岗位是餐厅经理/门店店长/客服/销售，回复却写"我主要做后端，Python、Go、
   MySQL、Redis 用得比较多"——HR 只会觉得你在群发。
3. 方向不对口时怎么办：
   a. 优先找简历里可迁移的事实（带过人、管过预算、对接过客户、扛过指标、做过排期交付），
      用它回应岗位的关注点；
   b. 一条可迁移事实都找不到，就别硬凑匹配度，只做三件事：确认已投递、表明真实意向、
      追问一个能帮你判断要不要继续的信息（门店位置、班次、考核指标、是否有培训）。
   c. why 里要如实写明"岗位关注点是 X，简历里没有直接对应，所以只确认意向"，不要粉饰。
4. 只能用简历里真实存在的事实（技能、项目、经历）。不得编造经历、公司、数字。
5. 不得替求职者承诺：具体薪资数字、准确到岗日期、是否接受外包、是否愿意出差/倒班。
   这些留给求职者自己回答，可以用"可以聊"这类留白说法。
6. 中文口语，像人在聊天，不要书面模板腔，不要"您好，感谢您的关注"这种套话开头。
7. 每条给一句 why，说明这条回复依据的是岗位的哪句要求 + 简历里的哪一点。
8. 岗位信息里可能混进页面上别的岗位（招聘网站底部常有「猜你喜欢」「推荐职位」）。
   一律以【岗位】那一行的标题为准；不要把别的岗位的薪资、职级、工作年限
   当成本岗位的信息，更不要因此去质疑"这是实习还是社招"。
9. 不要编造简历里没有的经历。回复里提到的公司、项目、年限，必须能在给出的简历事实里找到。
10. 只输出 JSON：
{"candidates":[{"style":"concise","text":"...","why":"..."},
                {"style":"formal","text":"...","why":"..."},
                {"style":"clarify","text":"...","why":"..."}]}"""


class ChatTurn(BaseModel):
    role: str
    text: str


class SuggestRequest(BaseModel):
    messages: list[ChatTurn] = Field(default_factory=list)
    job_id: int | None = None
    resume_id: int | None = None
    job_title: str | None = None
    # 扩展在聊天页直接抓到的岗位文本。聊天页常常没有 job_id，
    # 但右侧岗位卡片里有标题/薪资/部分描述，这些比"未关联岗位"有用得多
    job_text: str | None = None


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


# 页面上"当前岗位"之后紧跟着的推荐区。实测猎聘沟通页底部有「猜你喜欢」，
# 里面是 35-65k 的资深 Java 岗，被当成当前岗位喂给模型后，
# 回复会问"这是实习还是社招"——完全跑偏。
_RECOMMEND_MARKS = (
    "猜你喜欢",
    "相关推荐",
    "热门职位",
    "推荐职位",
    "相似职位",
    "看过该职位的人还看了",
    "相关公司",
)


def _cut_recommendations(text: str) -> str:
    """把页面上推荐区之后的内容切掉。

    前端已经按"必须包含当前岗位标题"筛过一遍，这里是第二道防线：
    页面结构变了、前端筛漏了，至少别让推荐岗位污染当前岗位。
    """
    cut = len(text)
    for mark in _RECOMMEND_MARKS:
        idx = text.find(mark)
        if 0 < idx < cut:
            cut = idx
    return text[:cut].rstrip()


def build_job_block(job: dict | None, page_title: str | None, page_text: str | None) -> str:
    """组装岗位描述文本。

    岗位信息有三种来源，优先级：库里的结构化画像 > 库里的 JD 正文 > 页面抓的文本。
    三者都没有时必须让模型知道"岗位未知"，否则它会退化成复读简历。
    """
    parts: list[str] = []

    if job:
        head = [
            f"岗位：{job.get('title') or '未命名'}",
            f"公司：{job.get('company') or '未标注'}",
            f"城市：{job.get('city') or '未标注'}",
        ]
        if job.get("salary_raw"):
            head.append(f"薪资：{job['salary_raw']}")
        if job.get("job_nature"):
            head.append(f"性质：{job['job_nature']}")
        parts.append("\n".join(head))

        must = job.get("must_have_skills") or []
        nice = job.get("nice_to_have_skills") or []
        parts.append(f"必备要求：{'、'.join(must) or '未列出'}")
        if nice:
            parts.append(f"加分项：{'、'.join(nice)}")

        desc = _clip(job.get("description") or "", JD_CHAR_LIMIT)
        raw = _clip(job.get("raw_text") or "", RAW_CHAR_LIMIT)
        # 结构化的 description 通常就是从 raw_text 抽的，重复塞两遍没意义
        if desc and raw and (desc[:40] in raw or raw[:40] in desc):
            parts.append(f"岗位正文：\n{raw}")
        else:
            if desc:
                parts.append(f"岗位正文：\n{desc}")
            if raw:
                parts.append(f"页面原文：\n{raw}")
    else:
        if page_title:
            parts.append(f"岗位：{page_title}（未入库，只有标题）")
        if page_text:
            parts.append(f"页面抓到的岗位信息：\n{_clip(_cut_recommendations(page_text), JD_CHAR_LIMIT)}")

    block = "\n".join(p for p in parts if p)
    if not block:
        block = "（岗位信息完全缺失：既没关联到库里的岗位，页面也没抓到岗位内容）"
    return block


def judge_direction(job: dict | None, resume_skills: list[str]) -> str:
    """判断简历技能和岗位要求是否对得上，给模型一句明确的话。

    只做"有没有交集"这种硬判断，不猜语义相似度——语义那层交给模型自己看 JD 正文，
    这里的作用是防止模型在零交集时还硬凑。
    """
    if not job:
        return ""
    must = job.get("must_have_skills") or []
    if not must:
        return "岗位必备要求未抽取到，请以岗位正文为准判断该回应什么。"

    tokens = resume_token_set(resume_skills)
    hits, missed = split_hits(tokens, must)
    if hits:
        return f"简历能对上的岗位要求：{'、'.join(hits)}。写 formal 版时优先用这些。"
    return (
        "【方向不对口】简历技能与岗位必备要求没有任何一项对得上"
        f"（岗位要的是：{'、'.join(must[:8])}）。"
        "此时禁止把简历里的技术栈/技能名写进回复，按铁律 3 处理。"
    )


@router.post("/suggest")
async def suggest(payload: SuggestRequest) -> dict:
    if not payload.messages:
        raise HTTPException(status_code=400, detail="缺少聊天记录，无法生成回复")

    job: dict | None = None
    resume_text = "（未指定简历）"
    resume_skills: list[str] = []
    resume_name = ""
    resume_id = payload.resume_id
    with get_pool().connection() as conn:
        if payload.job_id:
            job = repo.get_job_profile(conn, payload.job_id)
        if resume_id is None:
            # 扩展没传时用主简历，而不是"最新一份"——
            # 取到示例简历会让回复里冒出用户本人没有的经历（实测踩过）
            resume_id = repo.get_default_resume_id(conn)
        if resume_id:
            detail = repo.get_resume_detail(conn, resume_id)
            # 文件名要回传：扩展不传 resume_id 时后端取的是"最新一份"，
            # 很可能不是用户想用的那份（实测用了示例简历，回复里冒出
            # 一段用户自己没有的大厂经历）。界面上必须让人看见用的是哪份
            resume_name = (detail or {}).get("file_name") or ""
            profile = (detail or {}).get("profile") or {}
            basic = profile.get("basic") or {}
            resume_skills = [
                (s if isinstance(s, str) else s.get("name"))
                for s in (profile.get("skills") or [])
            ]
            resume_skills = [s for s in resume_skills if s]
            exps = profile.get("experiences") or []
            latest = exps[0] if exps else {}
            # 经历只给最近两条，且带上职责里的量化事实——可迁移能力全靠这块
            exp_lines = []
            for e in exps[:2]:
                title = e.get("company") or e.get("org") or ""
                role = e.get("title") or e.get("role") or ""
                duties = e.get("highlights") or e.get("duties") or []
                if isinstance(duties, str):
                    duties = [duties]
                duty_txt = "；".join(str(d) for d in duties[:3])
                line = f"- {title} {role}".strip()
                if duty_txt:
                    line += f"：{_clip(duty_txt, 120)}"
                exp_lines.append(line)
            resume_text = (
                f"简历 #{resume_id}：{basic.get('edu_level') or ''} "
                f"{basic.get('school') or ''}，约 {basic.get('work_years') or '?'} 年经验\n"
                f"技能：{'、'.join(resume_skills) or '未抽取'}\n"
                f"最近经历：{latest.get('company') or ''} {latest.get('title') or ''}"
            )
            if exp_lines:
                resume_text += "\n经历明细：\n" + "\n".join(exp_lines)

    job_block = build_job_block(job, payload.job_title, payload.job_text)
    direction = judge_direction(job, resume_skills)

    history = "\n".join(f"{m.role}：{m.text}" for m in payload.messages[-8:])
    user = (
        f"===== 岗位 =====\n{job_block}\n\n"
        f"===== 方向判定（系统预先算过，请遵照） =====\n{direction or '（无岗位数据，无法判定。若聊天记录显示岗位与简历方向不同，按铁律 3 处理）'}\n\n"
        f"===== 候选人简历 =====\n{resume_text}\n\n"
        f"===== 最近聊天记录 =====\n{history or '（无）'}\n\n"
        "请给出 3 条候选回复。写之前先想清楚：这个岗位的 HR 现在最在意什么，"
        "简历里哪一条能真正回应它。想不出对应事实就按铁律 3c 处理。"
    )

    result = await chat_json(SYSTEM_PROMPT, user, what="聊天回复建议", max_tokens=1200)
    data = parse_json_object(result.text, what="聊天回复建议")
    candidates = []
    for item in data.get("candidates") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        candidates.append(
            {
                "style": str(item.get("style") or "concise"),
                "text": text,
                "why": str(item.get("why") or "").strip(),
            }
        )
    if not candidates:
        raise HTTPException(status_code=502, detail="模型没有给出可用回复，请重试")

    return {
        "candidates": candidates,
        "resume_id": resume_id,
        "resume_name": resume_name,
        "job_linked": job is not None,
        "cost_cny": round(result.cost_cny, 4),
    }
