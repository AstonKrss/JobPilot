"""岗位雷达：主动发现"值得投而且进得去"的岗位。

和「匹配」页的分工
    匹配页回答：给定这个岗位，你和它有多匹配。
    雷达回答：在库里这些岗位中，哪些值得你花时间看，以及为什么。
    前者是逐条评价，后者是批量筛选 + 排序。所以雷达会先按可达性整批摘掉
    不可投的岗位，再在公司质量、技能匹配、通勤这三轴上排。这也是为什么
    雷达页第一批结果通常很少 —— 那是真实情况，不是功能坏了。

为什么雷达分不是匹配分
    用户的诉求是"帮我发现优质岗位"，不是"帮我找最像我的岗位"。一个 88 分
    的匹配如果是外包，去了简历贬值；一个 62 分的匹配如果是美的系制造业 IT，
    三年后回看是对的。所以公司质量权重最高（0.45），匹配次之（0.35）。
    三个权重都存在 radar_pref 里，随时可调。
"""

from __future__ import annotations

import logging
from collections import Counter

import psycopg
from fastapi import APIRouter, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from ..db import match_repo, repo
from ..db.pool import get_pool
from ..match.engine import rank
from ..radar import company as company_mod
from ..radar import gate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/radar", tags=["radar"])


class PrefUpdate(BaseModel):
    cities: list[str] | None = None
    nearby_cities: list[str] | None = None
    graduation_year: int | None = None
    edu_level: str | None = None
    stage: str | None = None
    accept_outsourcing: bool | None = None
    weight_company: float | None = None
    weight_match: float | None = None
    weight_distance: float | None = None


class CompanyUpdate(BaseModel):
    parent: str | None = None
    hq_city: str | None = None
    industry: str | None = None
    nature: str | None = None
    is_big_tech: bool | None = None
    is_outsourcing: bool | None = None
    campus_channel: bool | None = None
    note: str | None = None


def _load_pref(conn: psycopg.Connection) -> dict:
    row = conn.execute(
        """
        SELECT cities, nearby_cities, graduation_year, edu_level, stage,
               accept_outsourcing, weight_company, weight_match, weight_distance
          FROM radar_pref WHERE id = 1
        """
    ).fetchone()
    if row is None:
        conn.execute("INSERT INTO radar_pref (id) VALUES (1) ON CONFLICT DO NOTHING")
        conn.commit()
        return _load_pref(conn)
    return {
        "cities": row[0] or [],
        "nearby_cities": row[1] or [],
        "graduation_year": row[2],
        "edu_level": row[3],
        "stage": row[4],
        "accept_outsourcing": row[5],
        "weight_company": row[6],
        "weight_match": row[7],
        "weight_distance": row[8],
    }


def _job_rows(conn: psycopg.Connection) -> list[dict]:
    """雷达用的岗位行。比 list_current_job_profiles 多带届别与校招批次。

    单独写一条 SQL 而不是复用匹配层那个函数：雷达需要字段集不一样，
    硬塞进去会让匹配层的返回结构跟着变，影响范围反而大。
    """
    rows = conn.execute(
        """
        SELECT j.id, p.title, p.company, p.city, p.salary_raw, p.job_nature,
               p.edu_level, p.edu_fulltime_required, p.exp_min, p.exp_max,
               p.must_have_skills, p.nice_to_have_skills, p.hard_constraint_evidence,
               p.graduate_years, p.campus_type, p.company_key,
               j.detail_level, j.url, j.source, p.description
          FROM job j
          JOIN job_profile p ON p.job_id = j.id AND p.is_current
         ORDER BY j.id DESC
        """
    ).fetchall()
    return [
        {
            "job_id": int(r[0]),
            "title": r[1],
            "company": r[2],
            "city": r[3],
            "salary_raw": r[4],
            "job_nature": r[5],
            "edu_level": r[6],
            "edu_fulltime_required": r[7],
            "exp_min": r[8],
            "exp_max": r[9],
            "must_have_skills": r[10] or [],
            "nice_to_have_skills": r[11] or [],
            "hard_constraint_evidence": r[12] or {},
            "graduate_years": r[13] or [],
            "campus_type": r[14],
            "company_key": r[15],
            "detail_level": r[16] or "unknown",
            "url": r[17],
            "source": r[18],
            "description": r[19],
        }
        for r in rows
    ]


def _company_stats(conn: psycopg.Connection) -> dict[str, dict]:
    """按公司统计岗位数与硕士要求比例。

    硕士比例是唯一一个能从数据里算出来的"卡不卡学历"证据 ——
    它不需要预置、不会过时，样本越多越准。样本少于 3 条时 company.score
    会忽略它（见 company.score 里的门限）。
    """
    rows = conn.execute(
        """
        SELECT p.company_key,
               count(*) AS jobs,
               count(*) FILTER (WHERE p.edu_level IN ('硕士','博士')) AS master_jobs,
               avg(p.exp_min) FILTER (WHERE p.exp_min IS NOT NULL) AS avg_exp
          FROM job_profile p
         WHERE p.is_current AND p.company_key IS NOT NULL
         GROUP BY p.company_key
        """
    ).fetchall()
    out: dict[str, dict] = {}
    for key, jobs, master_jobs, avg_exp in rows:
        out[key] = {
            "jobs": int(jobs),
            "master_ratio": (master_jobs / jobs) if jobs else 0.0,
            "avg_exp": float(avg_exp) if avg_exp is not None else None,
        }
    return out


def _radar_score(pref: dict, company_score: float, match_score: float | None, distance: float) -> float:
    wc = pref.get("weight_company") or 0.45
    wm = pref.get("weight_match") or 0.35
    wd = pref.get("weight_distance") or 0.20
    parts = [(wc, company_score), (wd, distance * 100)]
    # 没有简历时匹配轴缺席，权重要重新归一，否则 35 分的空缺会把所有岗位一起压低，
    # 排序看起来"都很差"，而这其实只是"还没上传简历"
    if match_score is not None:
        parts.append((wm, match_score))
    total_w = sum(w for w, _ in parts) or 1.0
    return round(sum(w * v for w, v in parts) / total_w, 1)


def _enrich_pref(conn: psycopg.Connection, pref: dict, resume_id: int | None) -> dict:
    """届别与学历留空时从主简历补。这两条是判断的基础，让用户手填纯属找麻烦。"""
    if pref.get("graduation_year") is not None and pref.get("edu_level"):
        return pref
    rid = resume_id or repo.get_default_resume_id(conn)
    if not rid:
        return pref
    prof = match_repo.get_current_profile(conn, rid)
    basic = (prof or {}).get("basic") or {}
    if pref.get("graduation_year") is None and basic.get("graduation_year"):
        pref = {**pref, "graduation_year": int(basic["graduation_year"])}
    if not pref.get("edu_level") and basic.get("edu_level"):
        pref = {**pref, "edu_level": basic["edu_level"]}
    return pref


def _engine_view(job: dict) -> dict:
    """雷达行 → 匹配引擎要的字段。过滤器两处都要用，抽出来避免字段漏传。"""
    keys = (
        "job_id", "title", "company", "city", "job_nature", "edu_level",
        "edu_fulltime_required", "exp_min", "exp_max", "must_have_skills",
        "nice_to_have_skills", "hard_constraint_evidence", "detail_level", "url",
    )
    return {k: job.get(k) for k in keys}


def _build_item(
    job: dict, pref: dict, index: dict, stats: dict, match: dict
) -> dict:
    """把一条岗位组装成雷达条目。

    列表接口与单岗位接口共用同一个函数。分开写两份的后果很具体：
    插件里显示的判定和雷达页上的判定不一致，而用户会同时看到这两个地方。
    """
    g = gate.evaluate(job, pref)
    comp = index.get(job["company_key"]) if job["company_key"] else None
    cscore = company_mod.score(comp, stats.get(job["company_key"] or ""))
    return {
        "job_id": job["job_id"],
        "title": job["title"],
        "company": job["company"],
        "city": job["city"],
        "salary_raw": job["salary_raw"],
        "job_nature": job["job_nature"],
        "edu_level": job["edu_level"],
        "exp_min": job["exp_min"],
        "exp_max": job["exp_max"],
        "graduate_years": job["graduate_years"],
        "campus_type": job["campus_type"],
        "detail_level": job["detail_level"],
        "url": job["url"],
        "source": job["source"],
        "must_have_skills": job["must_have_skills"][:6],
        "company_profile": None
        if comp is None
        else {
            "name": comp["name"],
            "parent": comp["parent"],
            "industry": comp["industry"],
            "nature": comp["nature"],
            "tags": comp["tags"],
            "note": comp["note"],
            "source": comp["source"],
        },
        "company_score": cscore,
        "gate": g,
        "match": match,
        "radar_score": _radar_score(pref, cscore["score"], match.get("score"), g["distance"]),
    }


@router.get("")
def get_radar(
    resume_id: int | None = None,
    include_blocked: bool = False,
    # 负数 limit 会让下游 SQL 直接报错
    limit: int = Query(default=80, ge=1, le=500),
) -> dict:
    """雷达结果。include_blocked=true 时连不可达岗位一起返回（用于解释"为什么这么少"）。"""
    with get_pool().connection() as conn:
        company_mod.ensure_seed(conn)
        pref = _load_pref(conn)

        pref = _enrich_pref(conn, pref, resume_id)

        jobs = _job_rows(conn)
        companies = company_mod.load_all(conn)
        index = company_mod.build_index(companies)
        stats = _company_stats(conn)

        # 没认出来的公司就地补一条 inferred 记录，只存名字。
        # 写操作放在 GET 里不优雅，但它让"第一次打开就有公司维度"成立，
        # 否则用户要先手动点一次"识别公司"才能看到效果
        unknown_keys = {j["company_key"] for j in jobs if j["company_key"] and j["company_key"] not in index}
        if unknown_keys:
            for key in unknown_keys:
                name = next(j["company"] for j in jobs if j["company_key"] == key)
                company_mod.learn(conn, index, name)

        # 匹配轴：走同一套引擎，保证雷达里的"技能重合"和匹配页的数字口径一致
        match_by_job: dict[int, dict] = {}
        rid = resume_id or repo.get_default_resume_id(conn)
        if rid:
            profile = match_repo.get_current_profile(conn, rid)
            if profile:
                engine_jobs = [_engine_view(j) for j in jobs]
                for r in rank(profile, engine_jobs, {"accept_relocation": True,
                                                    "accept_outsourcing": pref["accept_outsourcing"],
                                                    "accept_internship": pref["stage"] != "campus",
                                                    "accept_parttime": False,
                                                    "target_cities": pref["cities"]}):
                    # 引擎对被否决的岗位不给分（score=None）。但雷达已经用
                    # 更宽的口径判过可达性，这里是"排序信号"而不是"资格结论"，
                    # 所以拿技能命中率兜一层，避免这些岗位在排序里变得没有信息
                    mscore = r.score
                    if mscore is None and r.skill_must_count:
                        mscore = 100.0 * r.skill_hit_count / r.skill_must_count
                    match_by_job[r.job_id or 0] = {
                        # 排序用原始分，展示用 display_score。
                        # 两者刻意分开：完整度不足的岗位不给分是对的（用户不该拿它当依据），
                        # 但排序需要信号 —— 若按 None 处理，这些岗位会退化成"只有公司分
                        # 和距离"，实测把安全员排到了 Java 岗位前面
                        "score": mscore,
                        "display_score": r.display_score,
                        "reliable": r.confidence >= 0.6,
                        "decision": r.decision.value,
                        "hit": r.skill_hit_count,
                        "must": r.skill_must_count,
                        "missed": r.missed_skills[:3],
                    }

        items: list[dict] = []
        grade_counter: Counter[str] = Counter()
        offtopic_count = 0
        for job in jobs:
            item = _build_item(job, pref, index, stats, match_by_job.get(job["job_id"]) or {})
            grade_counter[item["gate"]["grade"]] += 1
            if item["gate"].get("offtopic"):
                offtopic_count += 1
            if item["gate"]["grade"] == "blocked" and not include_blocked:
                continue
            items.append(item)

        # 排序和匹配引擎同口径：先按结论（可达 > 勉强 > 待确认 > 不可达），
        # 再按雷达分。只按分排会把"字段缺失、结论不可信"的岗位顶到前面，
        # 而它们恰恰是最不该先看的
        order = {"reachable": 0, "stretch": 1, "unknown": 2, "blocked": 3}
        items.sort(key=lambda x: (order.get(x["gate"]["grade"], 4), -x["radar_score"]))
        keep = items[:limit]
        verdict = _verdict(items, grade_counter, offtopic_count)

    return {
        "pref": pref,
        "resume_id": rid,
        "total_jobs": len(jobs),
        "returned": len(keep),
        # 分档计数用全量算，不受 limit 影响，否则"通过 3 个"会因为截断变成错的
        "grade_counts": dict(grade_counter),
        "offtopic": offtopic_count,
        "blocked_hidden": 0 if include_blocked else grade_counter.get("blocked", 0),
        "verdict": verdict,
        # 数据缺口单独报出来：公司名抽不到的岗位无法评价公司质量，
        # 只抓了列表卡片的岗位无法判断技能。这两类混在结果里会让排序失真，
        # 而用户从列表上看不出区别
        "data_gaps": {
            "no_company": sum(1 for j in jobs if not j["company"]),
            "digest_only": sum(1 for j in jobs if j["detail_level"] != "full"),
        },
        "items": keep,
    }


def _verdict(items: list[dict], counts: Counter, offtopic: int) -> dict:
    """给一句诚实的结论。

    为什么要有这一段：用户看到"库里 45 个岗位，只推 3 个"时，第一反应会是
    "功能坏了"。真实原因通常是两条中的一条 —— 方向上混进了大量非技术岗，
    或者采集时搜的就是社招岗。这两种情况需要的动作完全不同（前者改搜索词，
    后者改经验筛选），所以必须分开说，而不是只丢一个空列表。
    """
    good = [i for i in items if i["gate"]["grade"] in ("reachable", "stretch")]
    strong = [i for i in good if i["company_score"]["tier"] in ("T0", "T1")]
    blocked = counts.get("blocked", 0)

    if not items:
        return {
            "level": "empty",
            "text": "库里还没有可评的岗位。先按下面的搜索计划抓一批，雷达才有东西可筛。",
        }
    if not good:
        return {
            "level": "warn",
            "text": (
                f"当前 {sum(counts.values())} 个岗位里，没有一个是你现在能投的技术岗。"
                f"{offtopic} 个方向不对口做不了参考，其余卡在届别、经验或城市上。"
                "这说明问题在采集方向，不在筛选 —— 换个搜索词比放宽条件有用。"
            ),
        }
    if not strong and len(good) <= 3:
        return {
            "level": "warn",
            "text": (
                f"只有 {len(good)} 个能投，而且都不在你的首选公司里。"
                f"剩下 {blocked} 个岗位卡在硬条件上（其中 {offtopic} 个方向不对口）。"
                "继续按现有关键词搜增量有限，建议改按公司名直搜。"
            ),
        }
    if not strong:
        return {
            "level": "warn",
            "text": (
                f"可投 {len(good)} 个，但没有一个来自你设为优先梯队的公司。"
                f"另有 {blocked} 个已排除（{offtopic} 个方向不对口，其余卡在届别、经验或城市上）。"
            ),
        }
    return {
        "level": "ok",
        "text": (
            f"可投 {len(good)} 个，其中 {len(strong)} 个来自你设为优先梯队的公司。"
            f"另有 {blocked} 个已排除（{offtopic} 个方向不对口），不会占用你的注意力。"
        ),
    }


@router.get("/job/{job_id}")
def job_verdict(job_id: int, resume_id: int | None = None) -> dict:
    """单个岗位的雷达判定。

    谁在用：插件把一条岗位入库之后立刻调它，在悬浮面板上显示
    「能不能投 / 为什么」。这件事必须在抓取现场发生 ——
    等用户想起来去「发现」页翻，大概率已经投完或者关掉页面了。

    口径与雷达列表完全一致（共用 _build_item），否则同一个岗位在两个地方
    显示不同结论，用户只会更困惑。
    """
    with get_pool().connection() as conn:
        company_mod.ensure_seed(conn)
        pref = _enrich_pref(conn, _load_pref(conn), resume_id)
        jobs = [j for j in _job_rows(conn) if j["job_id"] == job_id]
        if not jobs:
            raise HTTPException(status_code=404, detail="岗位不存在或还没有画像")
        job = jobs[0]

        index = company_mod.build_index(company_mod.load_all(conn))
        stats = _company_stats(conn)
        if job["company_key"] and job["company_key"] not in index:
            company_mod.learn(conn, index, job["company"])

        match: dict = {}
        rid = resume_id or repo.get_default_resume_id(conn)
        if rid:
            profile = match_repo.get_current_profile(conn, rid)
            if profile:
                r = rank(
                    profile,
                    [_engine_view(job)],
                    {
                        "accept_relocation": True,
                        "accept_outsourcing": pref["accept_outsourcing"],
                        "accept_internship": pref["stage"] != "campus",
                        "accept_parttime": False,
                        "target_cities": pref["cities"],
                    },
                )[0]
                mscore = r.score
                if mscore is None and r.skill_must_count:
                    mscore = 100.0 * r.skill_hit_count / r.skill_must_count
                match = {
                    "score": mscore,
                    "display_score": r.display_score,
                    "reliable": r.confidence >= 0.6,
                    "decision": r.decision.value,
                    "hit": r.skill_hit_count,
                    "must": r.skill_must_count,
                    "missed": r.missed_skills[:3],
                }

        item = _build_item(job, pref, index, stats, match)

    item["advice"] = _advice_line(item)
    return item


def _advice_line(item: dict) -> str:
    """一句话结论，插件直接贴在面板上。

    只输出一条。给三条等于没给，而悬浮面板那一行本来就只能放一句话。
    """
    g = item["gate"]
    tier = item["company_score"]
    if g.get("offtopic"):
        return f"方向不对口：{g['blockers'][0] if g['blockers'] else '不是技术岗'}"
    if g["grade"] == "blocked":
        reason = g["blockers"][0] if g["blockers"] else "硬条件不满足"
        return f"别投：{reason}"
    if g["grade"] == "unknown":
        miss = "、".join(g["missing"][:2]) or "关键字段"
        return f"待确认：{miss}没抓到，先点开原站核实"
    best = f"{tier['label']} {tier['score']:.0f} 分"
    if item["company_profile"] and item["company_profile"].get("parent"):
        best = f"{item['company_profile']['parent']}旗下 · {best}"
    if g["grade"] == "stretch":
        caution = g["cautions"][0] if g["cautions"] else "有条件"
        return f"可以试 · {best} · 注意：{caution}"
    return f"可以投 · {best} · {g['distance_label']}"


@router.put("/pref")
def update_pref(payload: PrefUpdate) -> dict:
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="没有要更新的字段")
    if "stage" in fields and fields["stage"] not in ("internship", "campus", "both"):
        raise HTTPException(status_code=400, detail="stage 只能是 internship / campus / both")
    cols = []
    params: list = []
    for key, value in fields.items():
        cols.append(f"{key} = %s")
        params.append(Jsonb(value) if isinstance(value, list) else value)
    with get_pool().connection() as conn:
        conn.execute(
            f"UPDATE radar_pref SET {', '.join(cols)}, updated_at = now() WHERE id = 1",
            tuple(params),
        )
        conn.commit()
        return {"pref": _load_pref(conn)}


@router.get("/companies")
def list_companies() -> dict:
    with get_pool().connection() as conn:
        company_mod.ensure_seed(conn)
        companies = company_mod.load_all(conn)
        stats = _company_stats(conn)
        job_counts = {
            r[0]: int(r[1])
            for r in conn.execute(
                "SELECT company_key, count(*) FROM job_profile WHERE is_current GROUP BY 1"
            ).fetchall()
        }
    out = []
    for c in companies:
        s = stats.get(c["norm_key"]) or {}
        scored = company_mod.score(c, s)
        out.append(
            {
                **c,
                "jobs": job_counts.get(c["norm_key"], 0),
                "master_ratio": round(s.get("master_ratio") or 0, 3),
                **scored,
            }
        )
    out.sort(key=lambda x: (-x["score"], -x["jobs"]))
    return {"count": len(out), "items": out}


@router.put("/companies/{company_id}")
def update_company(company_id: int, payload: CompanyUpdate) -> dict:
    """用户改公司信息。改过的字段锁定，之后自动学习不会覆盖。"""
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="没有要更新的字段")
    cols = []
    params: list = []
    for key, value in fields.items():
        cols.append(f"{key} = %s")
        params.append(value)
    params.append(company_id)
    with get_pool().connection() as conn:
        row = conn.execute(
            f"""
            UPDATE company SET {', '.join(cols)},
                   source = 'user',
                   locked_fields = locked_fields || %s::jsonb,
                   updated_at = now()
             WHERE id = %s
            RETURNING norm_key
            """,
            tuple(params[:-1]) + (Jsonb(list(fields.keys())), company_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="公司不存在")
        conn.commit()
        return {"ok": True, "norm_key": row[0]}


@router.post("/link")
def link_companies() -> dict:
    """回填 job_profile.company_key。老数据没有这个字段，补上之后雷达才有公司维度。"""
    with get_pool().connection() as conn:
        company_mod.ensure_seed(conn)
        rows = conn.execute(
            "SELECT DISTINCT company FROM job_profile WHERE is_current AND company IS NOT NULL"
        ).fetchall()
        updated = 0
        for (name,) in rows:
            key = company_mod.company_key(name)
            if not key:
                continue
            updated += conn.execute(
                """
                UPDATE job_profile SET company_key = %s
                 WHERE is_current AND company = %s
                   AND (company_key IS NULL OR company_key <> %s)
                """,
                (key, name, key),
            ).rowcount
        conn.commit()
    return {"ok": True, "updated": updated, "companies_seen": len(rows)}


# ---------------------------------------------------------------------------
# 搜索计划：告诉用户"该输入什么关键词"，而不是替他猜 URL
#
# 为什么不让后端直接拼搜索结果 URL
#   各平台的搜索参数（city code、筛选位）改版频繁，硬编码一份迟早全错，
#   且错了之后表现是"翻页抓到空白"，很难定位。更稳的做法是把
#   "搜什么词、筛什么条件"算准，URL 由扩展在真实页面上操作 ——
#   扩展看到的就是浏览器看到的，平台改版时坏的是选择器，不是整条链路。
# ---------------------------------------------------------------------------

# 技能 → 搜索用词。平台搜索结果对岗位名敏感、对框架名不敏感：
# 搜「Spring Boot」几乎没有结果，搜「Java」一大堆。所以这里做的是
# "技能 → 岗位名"的映射，而不是把技能原样当关键词
SKILL_TO_QUERY: dict[str, str] = {
    "java": "Java",
    "python": "Python",
    "go": "Go",
    "golang": "Go",
    "c++": "C++",
    "前端": "前端",
    "react": "前端",
    "vue": "前端",
    "spring": "后端",
    "spring boot": "后端",
    "flask": "后端",
    "django": "后端",
    "mysql": "后端",
    "redis": "后端",
    "docker": "后端",
    "kubernetes": "后端",
    "k8s": "后端",
    "linux": "运维",
}

# 方向兜底词。简历技能一个都没命中映射表时用，保证计划不会为空
FALLBACK_QUERIES = ["后端开发", "软件开发"]


@router.get("/plan")
def search_plan(resume_id: int | None = None) -> dict:
    """按当前档位与简历技能，给出"下一步该搜什么"。

    只给关键词和筛选条件，不给 URL。理由见本段顶部注释。
    """
    with get_pool().connection() as conn:
        company_mod.ensure_seed(conn)
        pref = _load_pref(conn)
        rid = resume_id or repo.get_default_resume_id(conn)
        profile = match_repo.get_current_profile(conn, rid) if rid else None
        companies = company_mod.load_all(conn)
        stats = _company_stats(conn)
        in_lib = {
            (r[0] or ""): int(r[1])
            for r in conn.execute(
                "SELECT company, count(*) FROM job_profile WHERE is_current GROUP BY 1"
            ).fetchall()
        }

    stage = pref.get("stage") or "internship"
    cities = pref.get("cities") or []

    # 从简历技能推出方向词，按出现顺序去重（顺序即简历里的重要度）
    directions: list[str] = []
    for item in ((profile or {}).get("skills") or []):
        name = (item.get("name") or "").strip().lower()
        mapped = SKILL_TO_QUERY.get(name)
        if mapped and mapped not in directions:
            directions.append(mapped)
    if not directions:
        directions = list(FALLBACK_QUERIES)

    keywords: list[dict] = []
    seen: set[str] = set()

    def add(q: str, why: str, prio: int) -> None:
        q = q.strip()
        if not q or q in seen:
            return
        seen.add(q)
        keywords.append({"q": q, "why": why, "priority": prio})

    if stage in ("internship", "both"):
        for d in directions[:2]:
            add(f"{d} 实习生", f"简历方向是 {d}，实习岗标题多写「{d} 实习生」", 1)
        add("后端开发实习生", "方向词比框架名命中多，且不限定语言", 2)
        add("软件开发实习生", "制造业公司常用这个写法，不写具体技术栈", 3)
        add("软件测试实习生", "门槛通常低于开发岗，可作为保底通道", 4)

    if stage in ("campus", "both"):
        for d in directions[:2]:
            add(f"{d} 校招", f"{d} 方向的校招批次", 1)
        add("IT 管培生", "制造业/国企的校招常以管培生命名，值得单独搜", 2)
        add("管理培训生 技术", "同上，换个写法的命中差异很大", 3)

    # 公司直搜：已经判定为 T0/T1 且在通勤范围内的公司，直接搜公司名命中率最高，
    # 而且能看到该公司当期在招的全部岗位
    targets: list[dict] = []
    for c in companies:
        if c["source"] == "inferred":
            continue
        scored = company_mod.score(c, stats.get(c["norm_key"]))
        if scored["tier"] not in ("T0", "T1"):
            continue
        if c["hq_city"] and c["hq_city"] not in cities and c["hq_city"] not in (pref.get("nearby_cities") or []):
            continue
        targets.append(
            {
                "company": c["name"],
                "city": c["hq_city"],
                "tier": scored["tier"],
                "score": scored["score"],
                "in_library": in_lib.get(c["name"], 0),
                "why": (c["note"] or "；".join(c["tags"]) or "已判定为适合你的公司"),
            }
        )
    targets.sort(key=lambda x: (-x["score"], -x["in_library"]))
    targets = targets[:12]

    return {
        "stage": stage,
        "cities": cities,
        "nearby_cities": pref.get("nearby_cities") or [],
        "keywords": keywords,
        "company_targets": targets,
        "filters": {
            "degree": pref.get("edu_level") or "本科",
            "experience": "应届生 / 在校 / 经验不限",
            "graduate_year": pref.get("graduation_year"),
            "avoid": ["1 年以上经验", "社招", "外包驻场"],
        },
        "tips": _plan_tips(stage, pref),
    }


def _plan_tips(stage: str, pref: dict) -> list[str]:
    """按档位给的提醒。都是"会被忽略但会影响结果"的操作细节。"""
    tips: list[str] = []
    gy = pref.get("graduation_year")
    if stage in ("internship", "both"):
        tips.append("平台默认按「推荐」排序，先切成「最新」——新发的岗位回复率高得多")
        tips.append("经验筛选勾「在校/应届」或「经验不限」，能直接滤掉大量社招岗")
    if gy:
        tips.append(f"JD 里明写「{gy} 届」的岗位优先级最高：这是平台替你做过的资格筛选")
    tips.append("同一关键词只翻前 3 页：第 4 页之后基本是重复或长期招不满的岗位")
    return tips
