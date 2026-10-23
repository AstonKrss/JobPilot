"""岗位入库与 JD 结构化抽取。

为什么抽取放在后端而不是插件里
    插件只负责把 JD 容器的原文搬出来。字段选择器一旦写在插件里，平台改版就得重新
    打包发布；放在后端则只需重启进程。爆掉的半径从"用户要更新扩展"缩小到"改一个
    prompt"。

三态字段（这是匹配正确性的关键）
    edu_level / edu_fulltime_required / job_nature 都允许 null，表示"原文没提"。
    没提 ≠ 不要求，也 ≠ 要求。把它压成布尔会直接制造误杀或漏杀，
    因此 prompt 里明确要求：没写就填 null，并给出原文依据。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..db import repo
from ..db.pool import get_pool
from ..ingest.clean_jd import clean_jd
from ..llm.deepseek import chat_json, parse_json_object
from ..llm.errors import AppError, PermanentError, TransientError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# 这些标题说明抓到的不是岗位页，而是公司介绍 / 招聘专题之类的页面。
# 它们照样能被 LLM 抽出字段，但抽出来的"岗位"是虚构的，进匹配池只是噪声。
NON_JOB_TITLE_MARKS = ("公司介绍", "公司简介", "企业介绍", "非招聘", "招聘简章", "关于我们")


def _reject_non_job(title: str, raw_text: str) -> str | None:
    """返回拒绝理由；None 表示通过。

    只看标题：正文里出现"公司简介"是正常的（很多 JD 末尾带一段公司介绍），
    拿正文判断会误杀。
    """
    for mark in NON_JOB_TITLE_MARKS:
        if mark in title:
            return f"这不是岗位页面（标题含「{mark}」）"
    if not title.strip():
        return "未能识别出岗位名称，可能不是岗位页面"
    return None

JD_SYSTEM_PROMPT = """你是招聘 JD 信息抽取引擎。从 JD 原文中抽取结构化字段，严格遵守：

1. 只能依据原文。原文没写的字段填 null，禁止按行业惯例推测。
2. 每个硬约束字段必须在 hard_constraint_evidence 里给出对应的原文片段（逐字引用，不超过 30 字）。
2a. title：优先用原文里的岗位名。原文只有职责描述没有岗位名时，根据职责概括一个
   不超过 12 字的岗位名（如"后端开发工程师""小程序维护开发"），**禁止填 null** ——
   前端和检索都以 title 为准，null 会让岗位在列表里显示"未命名"。
3. edu_level 只能是：大专 / 本科 / 硕士 / 博士 / 不限 / null。
   原文写"学历不限"填 "不限"；原文完全没提学历填 null。
4. edu_fulltime_required 必须三态：原文明确要求"统招/全日制"填 true；
   原文明确说"不限统招/非全日制亦可"填 false；没提填 null。
   "本科及以上"这种写法不含统招含义，一律 null。
5. experience 写成 "3-5年" 时 exp_min=3、exp_max=5；写"5年以上"时 exp_min=5、exp_max=null；
   写"经验不限"时 exp_min=0、exp_max=null；写"应届生"时 exp_min=0、exp_max=0；没提则均为 null。
6. job_nature 只能是：全职 / 实习 / 外包 / 兼职 / null。原文没提填 null，不要默认全职。
7. 技能必须分两组：
   must_have_skills —— 硬性要求：原文用「必须/要求/精通/熟练掌握/熟悉」表述的技能。
   nice_to_have_skills —— 加分项：原文含「优先/加分/更佳/了解/有…经验者优先」的技能。
   「熟悉」按硬性要求处理；「了解」按加分项处理。
   技能名保留原文措辞（如「Go 或 Java」原样保留，不要自行拆分或改写）。
8. 只输出 JSON 对象，不要任何解释文字或 Markdown 代码块。"""

JD_USER_TEMPLATE = """JD 原文：

<<<
{text}
>>>

请输出如下结构的 JSON（字段名不可变）：
{{
  "title": "岗位名称",
  "company": "公司名",
  "city": "工作城市",
  "salary_raw": "薪资原文，如 20-30K·14薪",
  "job_nature": "全职|实习|外包|兼职|null",
  "edu_level": "大专|本科|硕士|博士|不限|null",
  "edu_fulltime_required": true|false|null,
  "exp_min": 3,
  "exp_max": 5,
  "must_have_skills": ["硬性技能要求，逐条，保留原文措辞"],
  "nice_to_have_skills": ["加分技能，逐条"],
  "description": "岗位职责与要求的要点摘要，不超过 300 字",
  "hard_constraint_evidence": {{
    "edu_level": "原文片段",
    "edu_fulltime_required": "原文片段或 null",
    "job_nature": "原文片段或 null",
    "experience": "原文片段或 null"
  }}
}}"""


class JobExtract(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    company: str | None = None
    city: str | None = None
    salary_raw: str | None = None
    job_nature: str | None = None
    edu_level: str | None = None
    edu_fulltime_required: bool | None = None
    exp_min: int | None = None
    exp_max: int | None = None
    must_have_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    description: str | None = None
    hard_constraint_evidence: dict = Field(default_factory=dict)


class JobIngestRequest(BaseModel):
    """插件上报 / 手动粘贴的统一入参。

    raw_html 只做存档：用于日后回溯"当时页面到底写了什么"，不参与抽取。
    """

    source: str = "manual"
    raw_text: str
    raw_html: str | None = None
    url: str | None = None
    source_job_id: str | None = None
    # full = 详情页完整 JD；digest = 列表页卡片摘要（无技能要求）
    detail_level: str = "unknown"


@router.post("/ingest")
async def ingest_job(payload: JobIngestRequest) -> dict:
    """存入岗位原文并就地抽取结构化字段。

    JD 通常一到两千字，一次 LLM 调用即可，不需要像简历那样拆后台任务。
    """
    if payload.source not in ("boss", "liepin", "zhaopin", "manual"):
        raise HTTPException(status_code=400, detail=f"未知来源：{payload.source}")
    if payload.detail_level not in ("full", "digest", "unknown"):
        raise HTTPException(status_code=400, detail=f"未知完整度：{payload.detail_level}")
    text = payload.raw_text.strip()
    if len(text) < 30:
        raise HTTPException(status_code=400, detail="JD 内容过短，至少要 30 个字符")

    # 清洗必须在抽取之前：插件抓来的是页面 innerText，混着「收藏 / 立即沟通 / 举报」
    # 和整块站内推荐链接，不清掉会被当成 JD 正文，技能与城市都可能抽错
    cleaned = clean_jd(text)
    if cleaned != text:
        logger.info("JD 清洗：%d 字 → %d 字", len(text), len(cleaned))
    text = cleaned

    try:
        result = await chat_json(
            JD_SYSTEM_PROMPT,
            JD_USER_TEMPLATE.format(text=text),
            what="JD 抽取",
            max_tokens=4000,
            # 单个岗位的抽取是一个独立会话；同一岗位反复抽取会命中缓存
            session_id=f"job-{uuid.uuid4().hex[:12]}",
            # 同一份 JD 文本重复入库时直接复用上次的抽取结果，不再付费重抽
            cache_tag="jd_extract",
        )
        profile = JobExtract.model_validate(parse_json_object(result.text, what="JD 抽取"))
    except ValidationError as exc:
        raise HTTPException(status_code=502, detail=f"JD 抽取结果结构异常：{exc.errors()[:2]}")
    except TransientError as exc:
        # 上游临时故障：告诉调用方可以稍后重试，而不是 500 让人以为是代码坏了
        raise HTTPException(status_code=503, detail=str(exc.user_message)) from exc
    except AppError as exc:
        raise HTTPException(status_code=422, detail=str(exc.user_message)) from exc

    data = profile.model_dump()

    # L0 数据闸门：拒绝非岗位页面。
    # 实测有「公司介绍（非招聘岗位）」被抓进来、还拿了 56 分进匹配池 ——
    # 这类数据即使能抽取出字段，也不是岗位，混进结果里纯属噪声
    bad_title = _reject_non_job(data.get("title") or "", text)
    if bad_title:
        raise HTTPException(status_code=422, detail=bad_title)

    with get_pool().connection() as conn:
        job_id = repo.create_job(
            conn,
            source=payload.source,
            raw_text=text,
            raw_html=payload.raw_html,
            url=payload.url,
            source_job_id=payload.source_job_id,
            detail_level=payload.detail_level,
        )
        profile_id = repo.save_job_profile(conn, job_id, data)
        conn.commit()

    logger.info(
        "JD 入库 job=%s profile=%s 费用 ¥%.4f", job_id, profile_id, result.cost_cny
    )
    return {
        "job_id": job_id,
        "profile_id": profile_id,
        "profile": data,
        "cost_cny": round(result.cost_cny, 4),
    }


@router.get("")
async def list_jobs(limit: int = 200, source: str | None = None) -> dict:
    """岗位列表。source 非空时只返回该平台的岗位（boss / liepin / zhaopin / manual）。"""
    with get_pool().connection() as conn:
        items = repo.list_jobs(conn, limit, source)
        counts = repo.count_jobs_by_source(conn)
    return {"count": len(items), "items": items, "sources": counts}


@router.get("/{job_id}")
async def get_job(job_id: int) -> dict:
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT j.id, j.source, j.url, j.raw_text, p.title, p.company, p.city,
                   p.edu_level, p.job_nature, p.exp_min, p.exp_max,
                   p.hard_constraint_evidence, j.detail_level,
                   p.must_have_skills, p.nice_to_have_skills, p.description
              FROM job j
              LEFT JOIN job_profile p ON p.job_id = j.id AND p.is_current
             WHERE j.id = %s
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="岗位不存在")
    return {
        "job_id": int(row[0]),
        "source": row[1],
        "url": row[2],
        "raw_text": row[3],
            "profile": None
        if row[4] is None
        else {
            "title": row[4],
            "company": row[5],
            "city": row[6],
            "edu_level": row[7],
            "job_nature": row[8],
            "exp_min": row[9],
            "exp_max": row[10],
            "hard_constraint_evidence": row[11],
            "must_have_skills": row[13] or [],
            "nice_to_have_skills": row[14] or [],
            "description": row[15],
        },
        # 完整度放在顶层：它描述的是"原文抓得全不全"，不是画像字段
        "detail_level": row[12] or "unknown",
    }
