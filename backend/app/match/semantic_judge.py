"""语义判定：向量召回候选对，由模型逐对裁定"到底有没有这项能力"。

分工（这是整套语义层的关键）
    向量（本地 bge-small-zh-v1.5）只负责**召回**：从简历证据里挑出最像的几条，
    交给模型。实测小模型的余弦相似度在"是否具备某技能"上没有区分度
    （正例 0.504 / 负例 0.513，负例还更高），所以**绝不能拿相似度直接拍板**；
    但它的**相对排序是对的**（真等价证据总能进 top-k）。
    判定交给模型：中文语义理解正是模型的强项，与"简历解析是 IE 不是 RAG"
    是同一个道理。

成本
    一次匹配请求只做**一次**批量判定调用（把全部候选对塞进一个 prompt），
    约 ¥0.003，而不是每个 (岗位, 技能) 组合调一次。

口径
    只有模型判定为"确实具备"的配对，才把该必备技能从"未命中"升级为"命中"。
    判定为否的保持未命中。解释里会写明依据是哪句简历证据，
    这样这条升级仍然是可核查的，而不是黑箱。
"""

from __future__ import annotations

import logging
import re

from ..config import settings
from ..llm.deepseek import chat_json, parse_json_object
from .skills import skill_hit, skill_tokens
from .semantic import build_evidence_items, recall_pairs

# 英文技术词（Kafka、Spring、SQLAlchemy…）
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]*")


def _tech_tokens(text: str) -> set[str]:
    return {t.lower() for t in _LATIN.findall(text or "") if len(t) >= 2}


def _both_latin_disjoint(a: str, b: str) -> bool:
    """两边都含英文技术词、且这些词一个都不重合。

    这类配对是"同领域不同技术"（Oracle vs MySQL、SpringCloud vs Spring Boot），
    判定结果几乎必然是 false，送给模型只是浪费 token 和稀释注意力。
    反过来，只要有一边不含英文词（例如要求写"高并发系统设计经验"，
    证据写"支撑日均三千万笔交易"），就保留给模型判 —— 那才是真正需要理解的场景。
    """
    ta, tb = _tech_tokens(a), _tech_tokens(b)
    return bool(ta) and bool(tb) and not (ta & tb)

logger = logging.getLogger(__name__)

# 每条 JD 要求最多召回几条证据（送去给模型裁定）
TOP_K = 2
# 相似度下限：低于这个值的配对连送给模型都不值得（纯 token 成本）
RECALL_FLOOR = 0.40
# 单次判定最多处理多少个配对，超出则按分数从高到低截断
MAX_PAIRS = 40

SYSTEM_PROMPT = """你是技能匹配裁定器。给定若干条「岗位要求」与「候选人简历证据」的配对，
逐对判断：这条证据是否证明候选人具备该岗位要求的能力。

判定口径（必须严格）：
1. 证据必须真的体现了该能力。仅仅相关、仅仅同一领域，不算具备。
2. 证据里明确出现的技能名/工具名，若与要求指的是同一个技术，算具备。
3. 证据体现的是更弱的东西（只是用过、只是了解、只是参与），不算具备。
4. 拿不准一律判 false，宁可漏判不可误判。

输出 JSON 对象，不要任何解释文字或 Markdown 代码块：
{"results": [{"job_id": 数字, "skill": "岗位要求原文", "evidence": "证据原文",
  "equivalent": true 或 false, "reason": "一句话理由"}]}"""


def _candidate_pairs(resume_profile: dict, jobs: list[dict]) -> list[dict]:
    """找出"规则判为未命中"的必备技能，并为每条召回候选证据。"""
    evidence_items = build_evidence_items(resume_profile)
    if not evidence_items:
        return []

    resume_tokens: set[str] = set()
    for skill in resume_profile.get("skills") or []:
        name = skill if isinstance(skill, str) else skill.get("name")
        if name:
            resume_tokens |= skill_tokens(name)

    # 先按岗位去重，避免同一个要求在多个岗位里重复送进模型
    missed_by_skill: dict[str, list[int]] = {}
    for job in jobs:
        for skill in job.get("must_have_skills") or []:
            if skill_hit(resume_tokens, skill):
                continue
            missed_by_skill.setdefault(skill, []).append(int(job.get("job_id") or 0))

    if not missed_by_skill:
        return []

    recalled = recall_pairs(
        evidence_items, list(missed_by_skill.keys()), top_k=TOP_K, floor=RECALL_FLOOR
    )

    pairs: list[dict] = []
    skipped = 0
    for skill, job_ids in missed_by_skill.items():
        for evidence, score in recalled.get(skill, []):
            if _both_latin_disjoint(skill, evidence):
                skipped += 1
                continue
            for job_id in job_ids:
                pairs.append(
                    {
                        "job_id": job_id,
                        "skill": skill,
                        "evidence": evidence,
                        "score": score,
                    }
                )

    if skipped:
        logger.info("语义判定：跳过 %d 个同领域不同技术的配对", skipped)

    # 按相似度降序：剩下的是最有可能真的等价的配对。
    # （之前的版本把"Oracle vs MySQL"这类必然为假的配对排在前面，
    #  挤掉了真正需要理解的"高并发 ≈ 日均三千万笔"，导致模型全判 false。）
    pairs.sort(key=lambda p: p["score"], reverse=True)
    return pairs[:MAX_PAIRS]


# ---------- 判定结果缓存 ----------
# 判定只取决于「岗位要求文本 × 简历证据文本」，与是哪个岗位无关，
# 所以可以跨岗位、跨匹配复用。第二次匹配同一批岗位时一次模型调用都不用花。

import hashlib


def _pair_key(skill: str, evidence: str) -> str:
    return hashlib.sha256(f"{skill}\x00{evidence}".encode("utf-8")).hexdigest()


def _load_cached(pairs: list[dict]) -> dict[tuple[str, str], dict]:
    """批量查缓存。DB 不可用时返回空，等价于全 miss，不影响正确性。"""
    if not pairs:
        return {}
    keys = {_pair_key(p["skill"], p["evidence"]): p for p in pairs}
    try:
        from ..db.pool import get_pool

        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                SELECT cache_key, equivalent, reason
                  FROM skill_judge_cache WHERE cache_key = ANY(%s)
                """,
                (list(keys),),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - 缓存挂了就当没有
        logger.debug("读取判定缓存失败（忽略）：%s", exc)
        return {}

    out: dict[tuple[str, str], dict] = {}
    for key, equivalent, reason in rows:
        pair = keys.get(key)
        if pair is None:
            continue
        out[(pair["skill"], pair["evidence"])] = {
            "equivalent": bool(equivalent),
            "reason": reason or "",
            "evidence": pair["evidence"],
        }
    if out:
        logger.info("语义判定缓存命中 %d/%d 对", len(out), len(pairs))
    return out


def _save_cached(results: dict[tuple[str, str], dict], model: str) -> None:
    if not results:
        return
    try:
        from ..db.pool import get_pool

        payload = [
            (_pair_key(skill, evidence), skill, evidence, bool(data["equivalent"]),
             data.get("reason") or "", model)
            for (skill, evidence), data in results.items()
        ]
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO skill_judge_cache
                        (cache_key, skill, evidence, equivalent, reason, model)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO NOTHING
                    """,
                    payload,
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 写失败只影响下次是否重算
        logger.debug("写入判定缓存失败（忽略）：%s", exc)


async def align_skills(resume_profile: dict, jobs: list[dict]) -> dict[tuple[int, str], dict]:
    """返回 {(job_id, 技能): {"evidence": ..., "reason": ...}}。

    模型不可用、调用失败或没有候选配对时返回空 dict —— 语义层缺席，
    主流程照常按纯规则走完。
    """
    try:
        pairs = _candidate_pairs(resume_profile, jobs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("语义召回失败，跳过语义判定：%s", exc)
        return {}

    if not pairs:
        return {}

    # 先查缓存：同一句简历证据 × 同一个岗位要求的判定结果与岗位无关，可以复用。
    # 实测这一步能把重复匹配的模型调用降到 0，而精度完全不受影响
    cached = _load_cached(pairs)
    pending = [p for p in pairs if (p["skill"], p["evidence"]) not in cached]

    judged: dict[tuple[str, str], dict] = {}
    if pending:
        lines: list[str] = []
        for index, pair in enumerate(pending, start=1):
            lines.append(
                f"{index}. job_id={pair['job_id']}\n"
                f"   岗位要求：{pair['skill']}\n"
                f"   简历证据：{pair['evidence']}"
            )
        user = "请逐对判断是否具备：\n" + "\n".join(lines)
        try:
            result = await chat_json(
                SYSTEM_PROMPT, user, what="语义技能判定", max_tokens=4000
            )
            payload = parse_json_object(result.text, what="语义技能判定")
        except Exception as exc:  # noqa: BLE001
            logger.warning("语义判定调用失败，降级为纯规则匹配：%s", exc)
            payload = {}

        for item in payload.get("results") or []:
            skill = str(item.get("skill") or "").strip()
            evidence = str(item.get("evidence") or "").strip()
            if not skill:
                continue
            judged[(skill, evidence)] = {
                "equivalent": bool(item.get("equivalent")),
                "reason": str(item.get("reason") or "").strip(),
                "evidence": evidence,
            }
        _save_cached(judged, settings.llm_model)

    aligned: dict[tuple[int, str], dict] = {}
    for pair in pairs:
        key = (pair["skill"], pair["evidence"])
        data = cached.get(key) or judged.get(key)
        if not data or not data.get("equivalent"):
            continue
        aligned[(pair["job_id"], pair["skill"])] = {
            "evidence": data.get("evidence") or pair["evidence"],
            "reason": data.get("reason") or "",
        }

    logger.info("语义判定完成：%d 个配对 → %d 条升级", len(pairs), len(aligned))
    return aligned
