"""匹配接口。

当前偏好（是否接受外包/异地等）尚未持久化，用请求参数临时传入，
默认值取最宽松的合法组合。偏好表属于 M2 后半段，加在这里会让匹配逻辑
先被一个还没定下来的表单结构绑住。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..db import match_repo
from ..db.pool import get_pool
from ..match.engine import Decision, rank, reject_summary
from ..match.relax import compute_relaxations, narrate
from ..match.semantic_judge import align_skills

logger = logging.getLogger(__name__)

# 返回结构一变就 bump，旧缓存自动失效（当前：L0-L3 分层，score 改为按完整度分档）
CACHE_VERSION = "v2"
router = APIRouter(prefix="/api", tags=["match"])


def _filter_items(
    payload: dict, include_rejected: bool, job_id: int | None = None
) -> dict:
    """按开关与岗位过滤视图。缓存里永远是全量，视图在这里生成。

    counts 保持全量 —— 顶部那三个数字本来就是"一共有多少通过/待确认/否决"，
    跟着过滤会变成 0，反而让人以为没有否决项。

    job_id 是工作台用的：只要这一个岗位的结论，其余的都不要。
    """
    out = payload
    if not include_rejected:
        out = {**out, "items": [i for i in out.get("items", []) if i.get("decision") != "reject"]}
    if job_id is not None:
        out = {**out, "items": [i for i in out["items"] if i.get("job_id") == job_id]}
    return out


def _cond_hash(prefs: dict, profile: dict) -> str:
    """缓存键：筛选条件 + 简历画像 + 返回结构版本。

    画像内容进哈希，是为了让"简历重新解析过"自动失效；
    版本号进哈希，是为了让"返回结构改过"也自动失效 —— 否则改完得分逻辑，
    用户还是会看到旧结构（缺 confidence 字段）的缓存，等于没改。
    """
    raw = json.dumps(
        {"v": CACHE_VERSION, "prefs": prefs, "profile": profile},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _semantic_alignment(resume_profile: dict, jobs: list[dict]) -> dict:
    """语义判定：向量召回候选对，模型逐对裁定。

    一次匹配请求只做一次批量调用（约 ¥0.003）。任何失败都退化为纯规则匹配，
    不影响主流程。
    """
    if not settings.enable_semantic:
        return {}
    try:
        return await align_skills(resume_profile, jobs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("语义判定失败，本次仅用规则匹配：%s", exc)
        return {}


def _prefs(
    accept_relocation: bool,
    accept_outsourcing: bool,
    accept_internship: bool,
    accept_parttime: bool,
    target_cities: str,
) -> dict:
    return {
        "accept_relocation": accept_relocation,
        "accept_outsourcing": accept_outsourcing,
        "accept_internship": accept_internship,
        "accept_parttime": accept_parttime,
        # 逗号分隔的期望城市。为空时城市判定回退到简历推断的当前城市
        "target_cities": [c.strip() for c in target_cities.split(",") if c.strip()],
    }


@router.get("/resumes/{resume_id}/matches")
async def list_matches(
    resume_id: int,
    include_rejected: bool = False,
    accept_relocation: bool = False,
    accept_outsourcing: bool = True,
    accept_internship: bool = False,
    accept_parttime: bool = False,
    target_cities: str = "",
    force: bool = False,
    # 工作台只需要单个岗位的结论。全量算完有缓存，这里只做展示层过滤，
    # 不为它单开一条计算路径
    job_id: int | None = None,
) -> dict:
    """返回该简历与全部在库岗位的匹配结果。

    include_rejected 默认关闭：列表页只该看到有意义的候选。
    但否决结果不会丢 —— 打开这个开关就是完整的「为什么不匹配」清单，
    这是求职者最需要的部分之一。

    target_cities 为逗号分隔的期望城市（如「北京,杭州」）。
    不传时城市判定回退到简历推断的当前城市，并在解释里标明这是推断结果。

    force=true 跳过缓存强制重算。缓存键是「简历画像 + 筛选条件 + 岗位库版本」，
    三者任一变化都会自然失效，所以默认走缓存不会拿到过期结论。
    """
    started = time.monotonic()
    prefs = _prefs(
        accept_relocation, accept_outsourcing, accept_internship, accept_parttime, target_cities
    )

    with get_pool().connection() as conn:
        profile = match_repo.get_current_profile(conn, resume_id)
        if profile is None:
            raise HTTPException(
                status_code=409,
                detail="该简历尚无可用画像，请等待解析完成或先调用 reparse",
            )
        jobs = match_repo.list_current_job_profiles(conn)
        job_version = match_repo.get_job_library_version(conn)
        cond_hash = _cond_hash(prefs, profile)
        cached = None if force else match_repo.get_match_cache(
            conn, resume_id, cond_hash, job_version
        )

    if cached is not None:
        payload, created_at = cached
        logger.info("匹配命中缓存 resume=%s 版本=%s", resume_id, job_version)
        return {
            **_filter_items(payload, include_rejected, job_id),
            "cached": True,
            "cached_at": created_at.isoformat() if created_at else None,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    if not jobs:
        return {
            "resume_id": resume_id,
            "total_jobs": 0,
            "counts": {},
            "reject_breakdown": {},
            "items": [],
            "note": "库中还没有岗位，请先通过 /api/jobs/ingest 导入 JD",
        }

    semantic = await _semantic_alignment(profile, jobs)
    results = rank(profile, jobs, prefs, semantic)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.decision.value] = counts.get(result.decision.value, 0) + 1

    # 岗位完整度单独成表：MatchResult 是纯匹配产物，不该再掺进数据来源信息
    job_levels = {j["job_id"]: j.get("detail_level", "unknown") for j in jobs}

    items = []
    for result in results:
        # 全量构造，不在这里按 include_rejected 裁剪。
        # 否则缓存里存的就是"过滤过的视图"，换个开关就得重算一遍 ——
        # 实测就是这个 bug：include_rejected 从 true 切到 false 仍命中了旧结果
        items.append(
            {
                "job_id": result.job_id,
                "title": result.title,
                "company": result.company,
                "decision": result.decision.value,
                # 完整度不足 60% 的岗位不给分：字段缺一半，算出来的数字是噪音，
                # 展示它比不展示更糟——用户会拿它当依据
                "score": result.display_score,
                "confidence": result.confidence,
                "info_missing": result.info_missing,
                "score_note": None if result.confidence >= 0.6 else "岗位信息不完整，未评分",
                "detail_level": job_levels.get(result.job_id, "unknown"),
                "reasons": result.reasons,
                "missed_skills": result.missed_skills,
                "skill_must_count": result.skill_must_count,
                "skill_hit_count": result.skill_hit_count,
                "checks": [
                    {
                        "rule": c.rule,
                        "label": c.label,
                        "decision": c.decision.value,
                        "detail": c.detail,
                        "jd_evidence": c.jd_evidence,
                    }
                    for c in result.checks
                ],
            }
        )

    logger.info(
        "匹配完成 resume=%s 岗位=%d 通过=%d 待确认=%d 否决=%d",
        resume_id,
        len(results),
        counts.get("pass", 0),
        counts.get("confirm", 0),
        counts.get("reject", 0),
    )
    payload = {
        "resume_id": resume_id,
        "total_jobs": len(results),
        "counts": counts,
        "reject_breakdown": reject_summary(results),
        "items": items,
    }
    elapsed_ms = int((time.monotonic() - started) * 1000)
    with get_pool().connection() as conn:
        match_repo.save_match_cache(
            conn, resume_id, cond_hash, job_version, payload, elapsed_ms
        )
    return {
        **_filter_items(payload, include_rejected, job_id),
        "cached": False,
        "elapsed_ms": elapsed_ms,
    }


@router.get("/resumes/{resume_id}/job-scores")
async def list_job_scores(
    resume_id: int,
    accept_relocation: bool = False,
    accept_outsourcing: bool = True,
    accept_internship: bool = False,
    accept_parttime: bool = False,
    target_cities: str = "",
) -> dict:
    """给岗位库排序用的轻量分数表：只要 job_id / 决策 / 分数。

    为什么不复用 /matches：那里每条都带完整 reasons 与 checks，
    几十个岗位乘上十几条解释，对"只想按分数排个序"的场景是浪费。
    这里的计算完全是本地规则，不调用模型，所以可以随选简历即时刷新。
    """
    prefs = _prefs(
        accept_relocation, accept_outsourcing, accept_internship, accept_parttime, target_cities
    )
    with get_pool().connection() as conn:
        profile = match_repo.get_current_profile(conn, resume_id)
        if profile is None:
            raise HTTPException(status_code=409, detail="这份简历还没有可用的画像，先等解析完成")
        jobs = match_repo.list_current_job_profiles(conn)

    results = rank(profile, jobs, prefs)
    return {
        "resume_id": resume_id,
        "total": len(results),
        "items": [
            {
                "job_id": r.job_id,
                "decision": r.decision.value,
                "score": r.score,
                "detail_level": r.detail_level,
            }
            for r in results
        ],
    }


@router.post("/resumes/{resume_id}/relaxations")
async def list_relaxations(
    resume_id: int,
    accept_relocation: bool = False,
    accept_outsourcing: bool = True,
    accept_internship: bool = False,
    accept_parttime: bool = False,
    target_cities: str = "",
) -> dict:
    """放宽探索：硬约束全线不满足时，给出"放宽哪一条能通"的地图。

    放宽后的结果由规则算（可复现），人话建议由模型写。
    这里不改变正式匹配的判定口径 —— 正式匹配仍然按原规则否决。
    """
    prefs = _prefs(
        accept_relocation, accept_outsourcing, accept_internship, accept_parttime, target_cities
    )
    with get_pool().connection() as conn:
        profile = match_repo.get_current_profile(conn, resume_id)
        if profile is None:
            raise HTTPException(status_code=409, detail="这份简历还没有可用的画像，先等解析完成")
        jobs = match_repo.list_current_job_profiles(conn)

    report = compute_relaxations(profile, jobs, prefs)
    report["advice"] = await narrate(report)
    report["resume_id"] = resume_id
    logger.info(
        "放宽探索 resume=%s 通过 %s→各维度 %s",
        resume_id,
        report["base"]["pass"],
        {v["dimension"]: v["pass_count"] for v in report["variants"]},
    )
    return report
