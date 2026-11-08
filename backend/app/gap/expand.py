"""技能展开：名词 → 可自查的具体能力问句，带跨岗位缓存。

焦虑控制（写死在常量里，不是 prompt 恳求）
    必备技能超过 MAX_EXPAND_SKILLS 项时只展开前 8 项，其余原样保留
    （truncated=True 让前端如实说明）。50 条缺口的清单没人会看，
    那是焦虑生成器不是学习工具。

缓存键不含 job_id
    sha256(norm_key(技能) + 0x00 + 校招/社招)。norm_key 让
    "SpringBoot"/"Spring Boot"/"springboot" 命中同一条缓存。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from ..llm.deepseek import chat_json, parse_json_object
from ..normalize.fuzzy import norm_key

logger = logging.getLogger(__name__)

MAX_EXPAND_SKILLS = 8
MAX_ITEMS_PER_SKILL = 3
ALLOWED_TAGS = ("基础", "进阶", "深入")

EXPAND_SYSTEM_PROMPT = f"""你是技术招聘考察点分析器。把岗位的必备技能逐项展开为可自查的具体能力问句
——回答「会这个技能」到底意味着能做什么。只输出 JSON：

{{"skills": [{{"skill": "原词", "items": [{{"text": "问句", "tag": "基础|进阶|深入"}}]}}]}}

要求：
1. 问句必须是可被面试验证的具体行为：「能讲清缓存穿透和雪崩的区别及应对」「能用 Redis 实现分布式锁
   并说明过期与续约策略」，而不是「了解 Redis」这种名词复述。
2. 每条 tag：基础 = 上岗前必须会；进阶 = 面试常问的原理与权衡；深入 = 区分初级与熟练的标志。
   同一技能内从基础到深入排列。
3. 基于同类岗位的常见考察点**推断**，不要声称是企业内部标准——没人知道那是什么。
4. 每个技能最多 {MAX_ITEMS_PER_SKILL} 条。没有把握的技能给通用考察点，不编造具体框架版本或内部细节。
5. 只输出 JSON，不要解释。"""


@dataclass(slots=True)
class ExpandItem:
    text: str
    tag: str


def _context(campus_type: str | None) -> str:
    """展开语境：校招/实习与社招的考察深度预期不同，仅此一个维度参与缓存键。"""
    return "校招" if (campus_type or "") in ("校招", "实习") else "社招"


def _cache_key(skill_norm: str, context: str) -> str:
    return hashlib.sha256(f"{skill_norm}\x00{context}".encode("utf-8")).hexdigest()


def _split_parallel(skill: str) -> list[str]:
    """并列写法拆分（「精通 Go 或 Java」→ Go / Java）。与 match/skills.py 同一套分隔词。"""
    parts = re.split(r"或|、|,|，|;|；|\||或者", skill or "")
    return [p.strip() for p in parts if p.strip()]


def _validate_items(raw_items, skill: str) -> list[ExpandItem]:
    out: list[ExpandItem] = []
    for it in raw_items or []:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if len(text) < 6:
            continue
        tag = str(it.get("tag") or "进阶").strip()
        out.append(ExpandItem(text=text[:120], tag=tag if tag in ALLOWED_TAGS else "进阶"))
        if len(out) >= MAX_ITEMS_PER_SKILL:
            break
    return out


def _load_cached(keys: dict[str, str]) -> dict[str, list[ExpandItem]]:
    """批量查展开缓存。key = cache_key，value = skill_norm。DB 挂了等价全 miss。"""
    if not keys:
        return {}
    try:
        from ..db.pool import get_pool

        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT cache_key, items FROM skill_expansion_cache WHERE cache_key = ANY(%s)",
                (list(keys),),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - 缓存挂了就当没有
        logger.debug("读取展开缓存失败（忽略）：%s", exc)
        return {}
    out: dict[str, list[ExpandItem]] = {}
    for key, items in rows:
        norm = keys.get(key)
        if norm is None:
            continue
        out[norm] = [
            ExpandItem(text=str(i.get("text") or ""), tag=str(i.get("tag") or "进阶"))
            for i in (items or [])
            if isinstance(i, dict) and i.get("text")
        ]
    if out:
        logger.info("技能展开缓存命中 %d/%d 项", len(out), len(keys))
    return out


def _save_cached(payload: list[tuple[str, str, str, str, list[ExpandItem], str]], context: str) -> None:
    """写失败只影响下次是否重算，不抛异常。"""
    if not payload:
        return
    try:
        from psycopg.types.json import Jsonb

        from ..db.pool import get_pool

        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO skill_expansion_cache
                        (cache_key, skill, skill_norm, context, items, model)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO NOTHING
                    """,
                    [
                        (key, skill, norm, context,
                         # items 是 JSONB 列：必须用 Jsonb 适配，裸 list 会被当作
                         # PG 数组导致插入失败（check_gap 抓到的静默 bug）
                         Jsonb([{"text": i.text, "tag": i.tag} for i in items]), model)
                        for key, skill, norm, _ctx, items, model in payload
                        if _ctx == context
                    ],
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("写入展开缓存失败（忽略）：%s", exc)


async def expand_for_job(
    skills: list[str], title: str, campus_type: str | None
) -> dict:
    """把必备技能清单展开为自查条目。返回：

    {skills: [{skill, norm, items: [{text, tag, cached}]}],
     expanded_n, cache_hits, truncated, context}

    展开结果按原文顺序返回；超过 MAX_EXPAND_SKILLS 的技能原样保留不展开
    （truncated=True，前端如实说明）。并列写法先拆分再展开，结果挂在原词条下。
    """
    context = _context(campus_type)
    skills = [str(s).strip() for s in (skills or []) if str(s).strip()]
    if not skills:
        return {"skills": [], "expanded_n": 0, "cache_hits": 0, "truncated": False, "context": context}

    to_expand = skills[:MAX_EXPAND_SKILLS]
    truncated = len(skills) > MAX_EXPAND_SKILLS

    # 归一去重：并列写法拆出的子技能共用原词条的展开结果
    norm_of: dict[str, str] = {}          # 子技能 norm -> 首个出现的原词条
    for original in to_expand:
        for piece in _split_parallel(original) or [original]:
            n = norm_key(piece)
            norm_of.setdefault(n, original)
    keys = {(_cache_key(n, context)): n for n in norm_of}

    cached = _load_cached(keys)
    pending_norms = [n for n in norm_of if n not in cached]

    expanded_n = 0
    model_used = ""
    if pending_norms:
        pending_display = []
        for n in pending_norms:
            original = norm_of[n]
            piece = next((p for p in _split_parallel(original) if norm_key(p) == n), original)
            pending_display.append(piece)
        listing = "\n".join(f"- {s}" for s in pending_display)
        try:
            result = await chat_json(
                EXPAND_SYSTEM_PROMPT,
                f"岗位：{title or '未知'}（{context}）\n必备技能：\n{listing}",
                what="技能展开",
                max_tokens=2500,
            )
            model_used = result.model
            data = parse_json_object(result.text, what="技能展开")
        except Exception as exc:  # noqa: BLE001 - 展开失败降级为"只有名词"，不阻塞工作台
            logger.warning("技能展开调用失败，本技能降级为原词展示：%s", exc)
            data = {}

        by_piece: dict[str, list[ExpandItem]] = {}
        for row in data.get("skills") or []:
            if not isinstance(row, dict):
                continue
            piece = str(row.get("skill") or "").strip()
            items = _validate_items(row.get("items"), piece)
            if piece and items:
                by_piece[piece] = items

        save_payload: list[tuple[str, str, str, str, list[ExpandItem], str]] = []
        for piece, items in by_piece.items():
            n = norm_key(piece)
            if n not in pending_norms:
                # 模型可能微调了写法；按归一键对不上的忽略，不让噪音进缓存
                continue
            cached[n] = items
            expanded_n += 1
            save_payload.append((_cache_key(n, context), piece, n, context, items, model_used))
        _save_cached(save_payload, context)

    out: list[dict] = []
    for original in skills:
        if original in to_expand:
            pieces = _split_parallel(original) or [original]
            items: list[ExpandItem] = []
            hits = 0
            for piece in pieces:
                got = cached.get(norm_key(piece))
                if got:
                    items.extend(got)
                    hits += 1
            out.append({
                "skill": original,
                "norm": norm_key(original),
                "items": [{"text": i.text, "tag": i.tag} for i in items],
                "expanded": bool(items),
                "cache_hit": hits == len(pieces) and bool(pieces),
            })
        else:
            out.append({"skill": original, "norm": norm_key(original),
                        "items": [], "expanded": False, "cache_hit": False})

    return {
        "skills": out,
        "expanded_n": expanded_n,
        "cache_hits": sum(1 for s in out if s["cache_hit"]),
        "truncated": truncated,
        "context": context,
    }
