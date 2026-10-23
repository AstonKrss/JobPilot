"""情节记忆：把一次会话压成一条摘要，下次开场按相关度召回。

为什么需要它
    1M 窗口也装不下三个月的对话（每天 30 轮 × 90 天 ≈ 2700 轮）。更要紧的是
    新会话从零开始——昨天聊过"正浩那岗要 2028 届硕士，我不符合"，今天开新对话
    模型不知道，用户得重讲一遍。

为什么召回只在会话开场做一次
    每轮重新召回会让 prompt 前缀一直变，服务端的 KV Cache 全部失效
    （V4.1 把缓存压到 1/4 HBM 就是为了长上下文，白白浪费掉很可惜）。
    一次会话通常只有一个主题，开场召回一次完全够用。

打分三项缺一不可
    只用向量：会把三个月前那次闲聊召回；
    只用时间衰减：会漏掉"不接受外包"这种长期有效的信息；
    只用频次：热门记忆永远霸榜。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from ..llm.deepseek import chat_json, parse_json_object
from ..match import semantic

logger = logging.getLogger(__name__)

# 召回打分权重。0.55 相似 + 0.25 新鲜度 + 0.20 复用得分为初始值，
# 需要后续用真实数据校准（这是拍出来的，不是测出来的）
W_SIM = 0.55
W_TIME = 0.25
W_FREQ = 0.20
HALF_LIFE_DAYS = 30.0

SUMMARIZE_PROMPT = """把这段求职咨询对话压成一条记忆。只输出 JSON：

{"topic": "20 字以内的主题", "summary": "200 字以内的摘要", 
 "facts": ["关键结论1", "关键结论2"]}

要求：
1. topic 要具体（「评估正浩 EcoFirst 实习岗」而不是「聊岗位」）
2. summary 只写结论与依据，不要复述寒暄
3. facts 最多 3 条，每条是可复用的判断（如「该岗要求 2028 届硕士在读，用户本科不符」）
4. 没有实质结论时 facts 可以是空数组"""


def _key_time(created_at: datetime | None) -> float:
    """时间衰减：30 天半衰期。未来时间按 1.0 处理（时钟偏差不该被惩罚）。"""
    if created_at is None:
        return 1.0
    now = datetime.now(timezone.utc)
    dt = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    days = max((now - dt).total_seconds() / 86400.0, 0.0)
    return math.exp(-days / HALF_LIFE_DAYS)


def _to_bytes(vec: list[float]) -> bytes | None:
    try:
        import numpy as np

        return np.asarray(vec, dtype=np.float32).tobytes()
    except Exception:  # noqa: BLE001
        return None


def _from_bytes(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    try:
        import numpy as np

        return [float(x) for x in np.frombuffer(blob, dtype=np.float32)]
    except Exception:  # noqa: BLE001
        return []


async def summarize_conversation(
    conn: psycopg.Connection, conversation_id: int
) -> dict | None:
    """把一次会话压成一条情节记忆。消息太少或模型没给出结论时返回 None。"""
    rows = conn.execute(
        """
        SELECT role, content FROM conversation_message
         WHERE conversation_id = %s ORDER BY id
        """,
        (conversation_id,),
    ).fetchall()
    if len(rows) < 4:  # 少于两轮没什么可记的
        return None

    # 只喂用户说的话 + 助手回答的前半段：摘要要的是结论，不是完整回放
    lines = []
    for role, content in rows:
        text = (content or "").strip()
        if not text:
            continue
        lines.append(f"{'用户' if role == 'user' else '助手'}：{text[:400]}")
    transcript = "\n".join(lines)[-6000:]

    result = await chat_json(
        SUMMARIZE_PROMPT, transcript, what="情节摘要", max_tokens=600
    )
    data = parse_json_object(result.text, what="情节摘要")
    topic = (data.get("topic") or "").strip()
    summary = (data.get("summary") or "").strip()
    if not topic or not summary:
        return None

    facts = [str(f).strip() for f in (data.get("facts") or []) if str(f).strip()][:3]
    vectors = semantic.embed([f"{topic}｜{summary}"])
    blob = _to_bytes(vectors[0]) if vectors else None

    row = conn.execute(
        """
        INSERT INTO episode (conversation_id, topic, summary, facts, embedding)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (conversation_id, topic, summary, Jsonb(facts), blob),
    ).fetchone()
    conn.commit()
    logger.info("情节记忆已生成 #%s：%s", row[0], topic)
    return {"id": int(row[0]), "topic": topic, "summary": summary, "facts": facts}


def recall(conn: psycopg.Connection, query: str, top_k: int = 3) -> list[dict]:
    """按「相似 + 新鲜度 + 复用次数」召回，返回最多 top_k 条。"""
    rows = conn.execute(
        """
        SELECT id, topic, summary, facts, embedding, created_at, hit_count
          FROM episode ORDER BY id DESC LIMIT 200
        """
    ).fetchall()
    if not rows:
        return []

    qvec = (semantic.embed([query]) or [None])[0]
    scored: list[dict] = []
    for r in rows:
        evec = _from_bytes(r[4])
        sim = semantic.cosine(qvec, evec) if qvec and evec else 0.0
        score = (
            W_SIM * sim
            + W_TIME * _key_time(r[5])
            + W_FREQ * min((r[6] or 0) / 5.0, 1.0)
        )
        scored.append({
            "id": r[0], "topic": r[1], "summary": r[2], "facts": r[3] or [],
            "created_at": r[5], "score": round(score, 4), "sim": round(sim, 3),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:top_k]

    if top:
        conn.execute(
            "UPDATE episode SET hit_count = hit_count + 1, last_hit_at = now()"
            " WHERE id = ANY(%s)",
            ([e["id"] for e in top],),
        )
        conn.commit()
    return top


def build_recall_block(episodes: list[dict]) -> str:
    """拼成给模型看的一段背景（约 800 字）。没有记忆时返回空串。"""
    if not episodes:
        return ""
    lines = ["【过往结论，按相关度从上到下】"]
    for i, e in enumerate(episodes, start=1):
        when = e["created_at"].strftime("%m-%d") if e.get("created_at") else "—"
        lines.append(f"{i}. [{when}] {e['topic']}")
        lines.append(f"   结论：{e['summary']}")
        if e.get("facts"):
            lines.append(f"   要点：{'；'.join(e['facts'])}")
    lines.append(
        "注意：以上为历史记忆，可能已过时。与当前对话或画像冲突时，"
        "一律以用户最新说法为准，并主动指出哪里不一致。"
    )
    return "\n".join(lines)
