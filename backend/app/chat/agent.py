"""ChatAgent：唯一面向用户的对话入口；PlanAgent 负责拆意图与分发任务。

分层（见 docs/design-decisions.md 十二.4）
    ChatAgent 只对话，不算数 —— 理解用户、澄清缺口、汇报结果。
    PlanAgent 把意图拆成确定的任务序列交给能力模块执行。

    意图拆解用一次模型调用（输出槽位更新与是否需要澄清），
    **任务执行本身是确定性代码**：检索、匹配、排序全走已有规则与向量，
    不让模型即兴发挥。agent 的自主循环必须有出口 —— 这里出口就是
    "意图清楚就执行，不清楚就只问一句"。

上下文处理
    每轮上下文 = 结构化槽位 + 最近 8 条对话 + 本轮工具结果。
    岗位 JD 这类大对象不进上下文，只进 job_id，前端按需加载。
    全量回放会让上下文随会话线性膨胀（之前算过：全量岗位就 305K token）。
"""

from __future__ import annotations

import logging

from ..config import settings
from ..db import conversation_repo
from ..memory import episode as episode_mem
from ..memory import extract, profile_repo, writer
from ..db import match_repo
from ..db.pool import get_pool
from ..llm.deepseek import chat_json, parse_json_object
from ..match.engine import Decision, rank
from ..match.semantic import build_evidence_items, embed, cosine
from ..match.domain_vocab import expand_query
from ..match.semantic_judge import align_skills

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """你是求职助手的规划器。根据用户消息和会话状态，输出 JSON 对象（不要解释）：

{
  "intent": "用户想找的岗位方向，一句话概括；这轮没表达新意图就沿用已有意图，填 null 表示无变化",
  "cities": ["目标城市"...],
  "resume_id": 数字或 null,
  "clarify": "缺少关键信息时，先问用户的一句话；信息足够执行检索则填 null",
  "chat": "不需要检索时（闲聊、追问、感谢），给一句直接回复；需要检索则填 null",
  "profile_updates": [
    {"key": "槽位名", "op": "replace|add|remove", "value": 值,
     "modality": "exclusive|additive|tentative", "evidence": "用户原话片段"}
  ]
}

判定标准：
- 用户表达了岗位方向（如"想找 agent 相关的"）或目标城市，就更新对应字段。
- intent 只有一个字段，装不下就概括成"XX 方向"。
- 若既有 intent 又有城市（或历史槽位里已有城市），信息足够，clarify 填 null。
- 若 intent 和历史槽位里都没有岗位方向，clarify 要问"想找什么方向的岗位"。
- 用户在追问上次的结果、说谢谢、闲聊时，chat 给一句自然回复，clarify 填 null。

profile_updates 只记**需要长期记住的求职偏好**，没有就填空数组 []：
- key 只能取：cities / job_nature / salary_floor / edu_level / work_auth /
  target_roles / hard_constraints / excluded_companies /
  accept_outsourcing / accept_relocation
- modality：exclusive 排他（"只找""不考虑""以后都"）；additive 补充
  （"也可以""都行"）；tentative 不确定（"可能""也许""听说"）
- evidence 必须是用户原话片段，不要自己组织语言
- 临时条件不记："这次先看看深圳的" ≠ 长期只要深圳
- 对岗位的评论、提问、闲聊都不算偏好（"这个岗位怎么样""简历怎么改"）
- 槽位语义容易混，按这个来：target_roles 是倾向的公司类型（大厂/国企/研究院），
  hard_constraints 是对工作条件的要求（双休/不加班/五险一金），
  work_auth 是届别与应届身份，edu_level 是学历
- 自我介绍里的客观信息也算（"我是 2028 届本科" → work_auth 2028、edu_level 本科）
- 用户说完"记住了"时，除了 chat 回一句，**必须同时在 profile_updates 里
  给出对应的槽位变更** —— 只说记下了、不写数据，等于骗人"""

REPLY_SYSTEM_PROMPT = """你是求职助手的对话回复。基于给定的检索结果写一段自然回复。

要求：
1. 直接回应用户这轮的问题，先给结论（找到几个、最值得看哪个），再列岗位。
2. 每个岗位一行：「岗位名（城市，分数）— 亮点或差距」，最多列 5 个。
3. 不要在回复文本里贴任何 URL —— 投递入口会以链接卡片展示，回复里说"点卡片上的去投递"即可。
4. 不要编造任何结果里没有的信息。没有就说没有，并说明差在哪。
5. 控制在 250 字以内。
6. 只输出 JSON 对象：{"reply": "你的回复内容"}，不要 Markdown 代码块。"""


def _job_doc(job: dict) -> str:
    parts = [job.get("title") or "", job.get("city") or ""]
    parts += [str(s) for s in (job.get("must_have_skills") or [])]
    parts += [str(s) for s in (job.get("nice_to_have_skills") or [])]
    return " ".join(p for p in parts if p)


async def _semantic_alignment(profile: dict, jobs: list[dict]) -> dict:
    """语义判定：向量召回候选对，模型逐对裁定。失败降级为纯规则。"""
    if not settings.enable_semantic:
        return {}
    try:
        return await align_skills(profile, jobs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("语义判定失败，本次仅用规则匹配：%s", exc)
        return {}


def _pick_resume(profiles: dict[int, dict], intent: str) -> tuple[int | None, dict]:
    """多份简历时，按意图与简历证据的向量相似度选最相关的。"""
    if not profiles:
        return None, {}
    if len(profiles) == 1:
        resume_id = next(iter(profiles))
        return resume_id, {"reason": "库里只有这一份"}
    if not intent:
        first = max(profiles)
        return first, {"reason": "未指定方向，默认用最新一份"}
    evidence = [" ".join(build_evidence_items(p)[:8]) for p in profiles.values()]
    vectors = embed([intent] + evidence)
    if vectors is None:
        first = max(profiles)
        return first, {"reason": "语义模型不可用，默认用最新一份"}
    intent_vector = vectors[0]
    best_id, best_score = None, -1.0
    for resume_id, vector in zip(profiles, vectors[1:]):
        score = cosine(intent_vector, vector)
        if score > best_score:
            best_id, best_score = resume_id, score
    return best_id, {"reason": "按意图与简历内容的语义相关度选出", "similarity": round(best_score, 3)}


def _semantic_order(intent: str, queries: list[str], jobs: list[dict]) -> dict[int, float]:
    """意图 + 扩展词 对岗位文档的向量相似度，用于在规则分之外看"方向对不对"。"""
    queries = [q for q in [intent, *queries] if q]
    if not queries or not jobs:
        return {}
    # 标题缺失的岗位会拼出空文档：空文档的向量没有意义，直接跳过
    docs = [(j, _job_doc(j)) for j in jobs]
    docs = [(j, d) for j, d in docs if d]
    if not docs:
        return {}
    vectors = embed(queries + [d for _, d in docs])
    if vectors is None:
        return {}
    query_vectors = vectors[: len(queries)]
    out: dict[int, float] = {}
    for (job, _), vector in zip(docs, vectors[len(queries):]):
        out[int(job.get("job_id") or 0)] = round(
            max(cosine(q, vector) for q in query_vectors), 4
        )
    return out


async def _expand_queries(intent: str, profile: dict | None, used_llm: bool) -> tuple[list[str], bool]:
    """查询扩展：词表优先，词表没命中才用模型。返回 (扩展词列表, 是否用了模型)。"""
    vocab = expand_query(intent)
    if vocab:
        return vocab, False
    if used_llm or not intent:
        return [], used_llm
    skills: list[str] = []
    if profile:
        for skill in profile.get("skills") or []:
            name = skill if isinstance(skill, str) else skill.get("name")
            if name:
                skills.append(str(name))
    try:
        result = await chat_json(
            "你是检索词扩展器。给定求职意图与候选人技能，输出 JSON："
            '{"variants": ["用于检索岗位的扩展词", ...]}。'
            "只输出 3-8 个与意图同方向、不同措辞的词，不要解释。",
            f"意图：{intent}\n技能：{'、'.join(skills[:12])}",
            what="查询扩展",
            max_tokens=600,
        )
        payload = parse_json_object(result.text, what="查询扩展")
        variants = [str(v) for v in payload.get("variants") or [] if str(v).strip()][:8]
        return variants, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("查询扩展失败，跳过：%s", exc)
        return [], used_llm


async def handle_message(
    user_text: str,
    conversation_id: int | None,
    notify=None,
) -> dict:
    """处理一轮用户消息：拆意图 → 执行 → 回复。全部落库。

    notify 是可选的异步回调，执行过程中每完成一步就推一条人话进度，
    供流式接口实时展示。不传就是旧行为（一次性返回）。
    """
    steps: list[str] = []

    async def step(label: str) -> None:
        steps.append(label)
        if notify is not None:
            try:
                await notify(label)
            except Exception:  # noqa: BLE001 - 进度推送失败不影响主流程
                pass

    user_text = (user_text or "").strip()
    if not user_text:
        return {"error": "消息为空"}

    with get_pool().connection() as conn:
        if conversation_id:
            conv = conversation_repo.get(conn, conversation_id)
            if conv is None:
                conversation_id = None
        if not conversation_id:
            conversation_id = conversation_repo.create(
                conn, user_text[:30] or None, {}
            )
            conn.commit()
        conv = conversation_repo.get(conn, conversation_id)
        slots: dict = dict(conv.get("slots") or {}) if conv else {}
        history = conversation_repo.recent_window(conn, conversation_id, 8)

        # 长期画像：每次都要加载（它是唯一权威，且体量很小）
        try:
            prof = profile_repo.get_flat_profile(conn)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取画像失败（忽略）：%s", exc)
            prof = {}

        # 情节记忆：**只在会话开场召回一次**，结果存进 slots。
        # 每轮重新召回会让 prompt 前缀一直变，服务端 KV Cache 全部失效 ——
        # 而一次会话通常只有一个主题，开场召回一次完全够用
        if not slots.get("recall_block") and not history:
            try:
                episodes = episode_mem.recall(conn, user_text, top_k=3)
                block = episode_mem.build_recall_block(episodes)
                if block:
                    slots["recall_block"] = block
                    conversation_repo.update_slots(conn, conversation_id, slots)
                    conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("情节召回失败（忽略）：%s", exc)

    # ---- PlanAgent：拆意图（一次模型调用） ----
    await step("理解你的需求…")
    plan_ctx = {
        "已有槽位": slots,
        "最近对话": history[-6:],
        "用户消息": user_text,
    }
    try:
        plan = await chat_json(
            PLAN_SYSTEM_PROMPT,
            "会话上下文：\n" + _compact(plan_ctx) + f"\n\n用户消息：{user_text}",
            what="意图规划",
            max_tokens=800,
        )
        plan_data = parse_json_object(plan.text, what="意图规划")
    except Exception as exc:  # noqa: BLE001
        logger.warning("意图规划失败：%s", exc)
        plan_data = {}

    intent = plan_data.get("intent") or slots.get("intent")
    cities = plan_data.get("cities") or slots.get("cities") or []
    clarify = (plan_data.get("clarify") or "").strip()
    chat_reply = (plan_data.get("chat") or "").strip()
    if intent:
        slots["intent"] = intent
    if cities:
        slots["cities"] = cities

    plan_note = "意图：" + (intent or "（沿用之前）")
    if cities:
        plan_note += " · 目标城市：" + "、".join(cities)
    await step(plan_note)

    # ---- 画像写入 ----
    # 与 PlanAgent 合并执行：它已经在读用户原话、已经在输出 cities，
    # 顺带抽偏好等于零额外调用、零额外延迟。
    # 单独再调一次 observe() 会让每轮 +2~3 秒（服务商约 6 tok/s），
    # 而记忆写入最大的风险从来不是"抽得慢"，是"压根没运行"。
    memory_note = ""
    pending_note = ""
    scope_note = ""
    updates = extract.clean_updates(plan_data.get("profile_updates") or [])
    if updates and _is_search_command(user_text):
        kept = [u for u in updates if u.get("key") not in SEARCH_ONLY_KEYS]
        dropped = [u for u in updates if u.get("key") in SEARCH_ONLY_KEYS]
        if dropped:
            # 不写，但要说明白。静默丢弃和写错一样糟：
            # 用户会以为"我说过佛山了，系统应该记得"
            scope_note = "、".join(
                writer.LABELS.get(u.get("key"), u.get("key")) for u in dropped
            )
        updates = kept
    if updates:
        with get_pool().connection() as conn:
            written = writer.apply_actions(conn, updates, conversation_id)
            if written["applied"]:
                # 写完必须重新读一次：本轮的 prefs 和回复都要用新值，
                # 否则用户刚说"只找佛山"，这一轮仍然按旧城市去搜
                prof = profile_repo.get_flat_profile(conn)
        if written["applied"]:
            memory_note = writer.describe(written["applied"])
            await step("已写入画像：" + memory_note)
        elif written["pending"]:
            # pending 原来是个死胡同：裁决说「挂起等用户确认」，但没有任何
            # 地方会去确认，等于「静默丢弃」多绕一步。所以这里把它变成一句
            # 问话 —— 用户回一句「要/不要」就会重新走一遍 L1，
            # 比悄悄丢掉诚实，也比直接写进去安全
            pending_note = "、".join(
                writer.LABELS.get(a.get("key"), a.get("key")) for a in written["pending"]
            )
            await step(f"{len(written['pending'])} 条表述不确定，先挂起")

    # ---- 不需要检索：闲聊/追问 ----
    if chat_reply and not clarify:
        reply = chat_reply
        jobs_payload: list[dict] = []
    # ---- 缺关键信息：先问再做，不猜 ----
    elif clarify:
        reply = clarify
        jobs_payload = []
    else:
        # ---- 执行检索管道 ----
        with get_pool().connection() as conn:
            profiles = {
                int(r["resume_id"]): r
                for r in _all_profiles(conn)
            }
            jobs = match_repo.list_current_job_profiles(conn)
        await step(f"扫描岗位库（{len(jobs)} 个在库岗位）")


        await step("挑选最相关的简历…")
        resume_id, resume_note = _pick_resume(profiles, intent or "")
        if resume_id is None:
            reply = "库里还没有已解析的简历。先在「简历」页上传一份 PDF，我再帮你找岗位。"
            jobs_payload = []
        else:
            profile = profiles[resume_id]
            slots["resume_id"] = resume_id
            slots["resume_note"] = resume_note
            await step(f"选用简历 #{resume_id}（{resume_note.get('reason', '')}）")

            # prefs 从画像装配，不再手写常量。
            # 原来 5 个常量硬编码，其中 accept_internship=False 直接和画像里
            # job_nature="实习" 打架，把整个实习池挡在检索之外 ——
            # 用户说"帮我找佛山的后端实习"，返回的 6 个岗位里一个实习都没有
            prefs = _prefs_from_profile(prof, cities)

            # 第一轮：规则匹配 + 词表扩展
            await step("硬约束前置过滤（城市 / 学历 / 性质 / 技能阈值）")
            queries, used_llm = await _expand_queries(intent or "", profile, used_llm=False)
            if queries:
                await step("扩展检索词：" + "、".join(queries[:6]) + ("…" if len(queries) > 6 else ""))
            else:
                await step("检索词：用原始意图，词表与模型都没有扩展")
            await step("语义判定：向量召回 + 模型逐对裁定…")
            semantic = await _semantic_alignment(profile, jobs)
            await step("匹配打分（学历/城市/性质已过滤，技能 + 年限累加）…")
            results = rank(profile, jobs, prefs, semantic)
            sims = _semantic_order(intent or "", queries, jobs)
            passed = [r for r in results if r.decision is not Decision.REJECT]
            await step(f"{len(passed)} 个岗位通过硬约束")

            # 二次召回：结果太少且还没用过模型扩展，就再扩展一轮重排
            if len(passed) < 5 and not used_llm:
                await step("通过数偏少，二次召回：换一批检索词再排一次…")
                more, used_llm = await _expand_queries(
                    f"{intent or ''} {' '.join(queries)}", profile, used_llm=False
                )
                if more:
                    queries = queries + more
                    sims = _semantic_order(intent or "", queries, jobs)

            # 排序：先按决策组（通过→待确认→否决），组内规则分 + 语义相关度。
            # 这里不能用 decision.value 的字符串排序 —— 字母序 confirm 会排在 pass 前面，
            # 实测导致 6 个中性分的"待确认"全部压过真正通过的岗位
            group_order = {Decision.PASS: 0, Decision.NEED_CONFIRM: 1, Decision.REJECT: 2}

            def _order(r):
                sim = sims.get(r.job_id, 0.0)
                return (group_order[r.decision], -((r.score or 0) + sim * 10))

            passed.sort(key=_order)

            top = passed[:6]
            near_miss = [
                r
                for r in results
                if r.decision is Decision.REJECT and (sims.get(r.job_id, 0) >= 0.45)
            ][:3]

            job_urls = {
                int(j.get("job_id") or 0): j.get("url")
                for j in jobs
                if j.get("url")
            }
            await step("整理结果、生成回复…")
            reply = await _compose(
                user_text, intent or "", resume_id, top, near_miss, sims, job_urls,
                profile_block=_profile_block(prof),
                recall_block=slots.get("recall_block") or "",
            )
            jobs_payload = [
                {
                    "job_id": r.job_id,
                    "title": r.title or r.company or "未命名岗位",
                    "company": r.company,
                    "city": next((j.get("city") for j in jobs if j.get("job_id") == r.job_id), None),
                    "url": next((j.get("url") for j in jobs if j.get("job_id") == r.job_id), None),
                    "score": r.score,
                    "decision": r.decision.value,
                    "semantic_sim": sims.get(r.job_id),
                    "missed_skills": r.missed_skills,
                }
                for r in top
            ]
            slots["last_job_ids"] = [r.job_id for r in top]

    # 写入结果必须说出来。用户被告知"记住了"而库里没变，
    # 比干脆不记更糟 —— 预期被建立起来，之后所有判断都建立在错的前提上。
    # 反过来，真的写了也要说，否则用户不知道系统到底记成了什么
    if memory_note and reply:
        reply = f"{reply}\n\n已写入画像：{memory_note}"
    if scope_note and reply:
        reply = f"{reply}\n\n这次的「{scope_note}」我当成这一轮的搜索条件，没写进长期偏好 —— 要长期按它筛就说一句「以后都这样」"
    if pending_note and reply:
        reply = f"{reply}\n\n你提到的「{pending_note}」我不确定是长期要求还是这次的条件，先没写进去。要按它长期筛吗？"

    # ---- 落库 ----
    with get_pool().connection() as conn:
        conversation_repo.add_message(conn, conversation_id, "user", user_text)
        conversation_repo.add_message(
            conn, conversation_id, "assistant", reply, {"jobs": jobs_payload} if jobs_payload else None
        )
        conversation_repo.update_slots(conn, conversation_id, slots)
        conn.commit()

    return {
        "conversation_id": conversation_id,
        "reply": reply,
        "jobs": jobs_payload,
        "slots": slots,
        "steps": steps,
    }


def _compact(context: dict) -> str:
    import json

    return json.dumps(context, ensure_ascii=False, default=str)[:3000]


def _all_profiles(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT r.id, r.file_name, p.basic, p.skills, p.experiences, p.projects
          FROM resume r
          LEFT JOIN resume_profile p ON p.resume_id = r.id AND p.is_current
         WHERE p.id IS NOT NULL
         ORDER BY r.id DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "resume_id": int(r[0]),
                "file_name": r[1],
                "basic": r[2] or {},
                "skills": r[3] or [],
                "experiences": r[4] or [],
                "projects": r[5] or [],
            }
        )
    return out


async def _compose(
    user_text: str,
    intent: str,
    resume_id: int,
    top: list,
    near_miss: list,
    sims: dict[int, float],
    urls: dict[int, str] | None = None,
    profile_block: str = "",
    recall_block: str = "",
) -> str:
    """把检索结果写成回复。模型失败时退回模板，保证一定有回复。

    profile_block / recall_block 由调用方传入，不在这里从头查 ——
    它们是会话开场时算好的固定前缀，会话中不该再变（变了服务端缓存全失效）。
    """
    lines: list[str] = []
    for r in top:
        url = (urls or {}).get(r.job_id)
        lines.append(
            f"- {r.title or r.company or '未命名岗位'}（{r.company or ''}，{r.score if r.score is not None else '-'} 分）"
            + (f" 链接：{url}" if url else "")
        )
    summary = "\n".join(lines) if lines else "（没有通过硬约束的岗位）"
    near = "\n".join(
        f"- {r.title}：{'、'.join(r.missed_skills[:3]) or '硬约束不符'}" for r in near_miss
    )
    try:
        result = await chat_json(
            REPLY_SYSTEM_PROMPT,
            f"{profile_block}"
            f"{recall_block}\n\n"
            f"用户消息：{user_text}\n意图：{intent}\n使用简历：#{resume_id}\n"
            f"通过硬约束的岗位（已按匹配度排序）：\n{summary}\n\n"
            f"语义相近但被否决的岗位：\n{near or '（无）'}",
            what="对话回复",
            max_tokens=900,
        )
        text = parse_json_object(result.text, what="对话回复")
        if isinstance(text, dict):
            for key in ("reply", "content", "text", "answer"):
                if isinstance(text.get(key), str) and text[key].strip():
                    return text[key]
            return ""
        return str(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("回复生成失败，退回模板：%s", exc)
        head = f"围绕「{intent}」找到 {len(top)} 个通过硬约束的岗位：\n" if intent else ""
        return head + summary + (f"\n\n语义相近但不满足硬约束：\n{near}" if near else "")


def _as_bool(value, default: bool) -> bool:
    """把库里读出来的值转成布尔。

    画像存的是 JSONB，同一个槽位历史上有过 true / "true" / "接受" 几种写法
    （早期是人工通过 PUT 接口写的）。不做归一的话，字符串 "false" 是真值 ——
    最坏的情况是用户明确写了"不接受外包"，系统按接受处理。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "是", "接受", "可以", "愿意"):
            return True
        if v in ("false", "0", "否", "不接受", "不", "不愿意"):
            return False
    return default


def _pairs(value) -> list[str]:
    """槽位值统一成字符串列表。历史上的 cities 有 ["广州"] 也有 "广州"。"""
    if not value:
        return []
    if isinstance(value, str):
        return [p for p in value.replace("，", ",").split(",") if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


INTERNSHIP_WORDS = ("实习", "见习", "日常实习", "暑期实习")
FULLTIME_WORDS = ("全职", "社招", "正式", "校招正职")


# ---- 「这一轮的任务」还是「长期偏好」 ----
#
# 这是把记忆写进对话之后立刻暴露的问题：用户说「帮我找佛山的后端实习岗位」，
# cities 就被写成佛山；下次说「广州的呢」，又变成广州。结果**显式设定不再稳定** ——
# 用户明明说过「以后只找广州」，一次搜索就能把它改掉。
#
# 判据用语气，不用槽位：祈使句（帮我/给我/替我）是让我去做一件事，
# 里面的城市和方向是这次的条件；陈述句（我想找/我只找）才是偏好。
# 带排他标记的祈使句仍然算偏好（「以后只帮我找佛山的」）。
IMPERATIVE_MARKERS = ("帮我", "给我", "替我", "找一下", "搜一下", "看看有没有", "帮忙找")
COMMIT_MARKERS = ("只", "仅", "以后", "记住", "别再", "不想", "不考虑", "排除",
                  "不要", "都别", "改成", "必须", "以后都")
# 只有这两个槽位会被本轮条件污染；其余槽位（外包、学历、届别）祈使句里说的
# 也往往是真偏好，不动
SEARCH_ONLY_KEYS = {"cities", "target_roles"}


def _is_search_command(text: str) -> bool:
    """这句话是不是「让我去搜一下」，而不是在交代长期偏好。"""
    if any(m in text for m in COMMIT_MARKERS):
        return False
    return any(m in text for m in IMPERATIVE_MARKERS)


def _prefs_from_profile(prof: dict, cities: list | None) -> dict:
    """把画像翻译成匹配引擎的开关。

    为什么必须有这一层
        原来这里写的是 5 个常量，其中 accept_internship=False 与画像里
        job_nature="实习" 直接冲突。后果不是"少一个开关"，而是整个实习池
        在检索阶段被清空：用户说"找佛山的后端实习"，返回 6 个岗位里
        一个实习都没有，而库里躺着 38 个实习岗。

    缺省值取向：**未知一律放宽**
        accept_outsourcing 未知 → True，accept_internship 未知 → True。
        理由是判定层还会再拦一次（reject 会写明卡在哪），而召回层如果
        先过滤掉，用户永远不知道有这些岗位存在 —— 前者的代价是多看一条，
        后者的代价是以为"没有"。

    accept_parttime 仍然默认 False：兼职和实习是两回事，混进来会让结果变脏。
    """
    nature = prof.get("job_nature")
    nature_text = " ".join(_pairs(nature)) if isinstance(nature, (list, tuple)) else str(nature or "")

    if any(w in nature_text for w in INTERNSHIP_WORDS):
        accept_internship = True
    elif any(w in nature_text for w in FULLTIME_WORDS):
        accept_internship = False
    else:
        accept_internship = True

    target = [str(c) for c in (cities or []) if c] or _pairs(prof.get("cities"))

    return {
        "accept_relocation": _as_bool(prof.get("accept_relocation"), False),
        "accept_outsourcing": _as_bool(prof.get("accept_outsourcing"), True),
        "accept_internship": accept_internship,
        "accept_parttime": "兼职" in nature_text,
        "target_cities": target,
    }


def _profile_block(prof: dict) -> str:
    """把画像拼成给模型看的一小段（约 300 token）。

    放在 prompt 最前面且内容稳定：画像变化是低频事件，
    所以不会因为这段而反复让服务端缓存失效。
    """
    if not prof:
        return ""
    labels = {
        "cities": "目标城市", "job_nature": "岗位类型", "target_roles": "目标岗位",
        "salary_floor": "薪资底线", "edu_level": "学历", "work_auth": "届别/身份",
        "hard_constraints": "硬性要求", "excluded_companies": "排除的公司",
        "accept_outsourcing": "接受外包", "accept_relocation": "接受异地",
    }
    lines = ["【求职者长期画像】"]
    for key, value in prof.items():
        if value in (None, "", [], {}):
            continue
        label = labels.get(key, key)
        shown = "、".join(value) if isinstance(value, list) else str(value)
        lines.append(f"- {label}：{shown}")
    return "\n".join(lines) + "\n\n" if len(lines) > 1 else ""
