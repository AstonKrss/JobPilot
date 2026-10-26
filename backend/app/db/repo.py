"""数据访问。

刻意不引入 ORM：本项目的查询都是明确的单表读写，没有跨表关系图需要维护。
为几行 SQL 引入 ORM 只会让"这条 SQL 到底发了什么"变得需要跳三层才能看清。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import psycopg

from ..ingest.layout import Block
from ..radar.company import company_key as company_key_of

logger = logging.getLogger(__name__)


def file_sha256(path: Path) -> str:
    """流式计算，避免把大文件读进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_resume_by_hash(
    conn: psycopg.Connection, file_hash: str
) -> tuple[int, str, str, str] | None:
    """返回 (id, parse_status, source_kind, file_path)。

    带上 file_path 是为了让重复上传能直接命中已落盘的文件，不必重新拼路径。
    """
    row = conn.execute(
        "SELECT id, parse_status, source_kind, file_path FROM resume WHERE file_hash = %s",
        (file_hash,),
    ).fetchone()
    return (int(row[0]), str(row[1]), str(row[2]), str(row[3])) if row else None


def create_resume(
    conn: psycopg.Connection,
    *,
    file_hash: str,
    file_path: str,
    file_name: str,
    page_count: int | None,
    source_kind: str,
) -> int:
    row = conn.execute(
        """
        INSERT INTO resume (file_hash, file_path, file_name, page_count, source_kind, parse_status)
        VALUES (%s, %s, %s, %s, %s, 'pending')
        RETURNING id
        """,
        (file_hash, file_path, file_name, page_count, source_kind),
    ).fetchone()
    assert row is not None  # RETURNING 一定返回一行，否则是数据库异常
    return int(row[0])


def set_resume_status(
    conn: psycopg.Connection,
    resume_id: int,
    status: str,
    *,
    error: str | None = None,
    source_kind: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE resume
           SET parse_status = %s,
               parse_error  = %s,
               source_kind  = COALESCE(%s, source_kind)
         WHERE id = %s
        """,
        (status, error, source_kind, resume_id),
    )


def get_resume_file_path(conn: psycopg.Connection, resume_id: int) -> str | None:
    row = conn.execute("SELECT file_path FROM resume WHERE id = %s", (resume_id,)).fetchone()
    return str(row[0]) if row else None


def set_page_count(conn: psycopg.Connection, resume_id: int, page_count: int) -> None:
    conn.execute("UPDATE resume SET page_count = %s WHERE id = %s", (page_count, resume_id))


def replace_blocks(conn: psycopg.Connection, resume_id: int, blocks: list[Block]) -> None:
    """重解析时先删后插，避免新旧块混在一起导致阅读顺序错乱。"""
    conn.execute("DELETE FROM resume_block WHERE resume_id = %s", (resume_id,))
    if not blocks:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO resume_block
                (resume_id, page_no, block_index, x0, y0, x1, y1, text, block_type, read_order)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    resume_id,
                    b.page_no,
                    b.block_index,
                    b.x0,
                    b.y0,
                    b.x1,
                    b.y1,
                    b.text,
                    b.block_type,
                    b.read_order,
                )
                for b in blocks
            ],
        )


def save_profile(
    conn: psycopg.Connection,
    resume_id: int,
    profile: dict,
    *,
    raw_extract: dict | None = None,
    extract_version: str = "m1",
) -> int:
    """写入画像并把旧的 current 置为 false（部分唯一索引保证只有一份 current）。"""
    conn.execute(
        "UPDATE resume_profile SET is_current = FALSE WHERE resume_id = %s AND is_current",
        (resume_id,),
    )
    row = conn.execute(
        """
        INSERT INTO resume_profile
            (resume_id, extract_version, is_current, basic, skills, experiences, projects, raw_extract)
        VALUES (%s, %s, TRUE, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            resume_id,
            extract_version,
            psycopg.types.json.Jsonb(profile.get("basic", {})),
            psycopg.types.json.Jsonb(profile.get("skills", [])),
            psycopg.types.json.Jsonb(profile.get("experiences", [])),
            psycopg.types.json.Jsonb(profile.get("projects", [])),
            psycopg.types.json.Jsonb(raw_extract) if raw_extract is not None else None,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def get_resume_detail(conn: psycopg.Connection, resume_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT r.id, r.file_name, r.page_count, r.source_kind, r.parse_status, r.parse_error,
               r.created_at, r.file_path,
               p.basic, p.skills, p.experiences, p.projects, p.raw_extract
          FROM resume r
          LEFT JOIN resume_profile p ON p.resume_id = r.id AND p.is_current
         WHERE r.id = %s
        """,
        (resume_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "file_name": row[1],
        "page_count": row[2],
        "source_kind": row[3],
        "parse_status": row[4],
        "parse_error": row[5],
        "created_at": row[6].isoformat() if row[6] else None,
        # 原始文件路径：补技能要改文件，不能只改画像
        "file_path": row[7],
        # 列序：0 id, 1 file_name, 2 page_count, 3 source_kind, 4 parse_status,
        #       5 parse_error, 6 created_at, 7 file_path, 8 basic, 9 skills,
        #       10 experiences, 11 projects, 12 raw_extract
        "profile": None
        if row[8] is None
        else {
            "basic": row[8],
            "skills": row[9],
            "experiences": row[10],
            "projects": row[11],
        },
        # 抽取元信息：用了哪个策略、质量自评结果、换策略重试记录
        "extract_meta": row[12] if row[8] is not None else None,
    }


def get_resume_ordered_text(conn: psycopg.Connection, resume_id: int) -> str:
    """按还原后的阅读顺序拼出简历全文。

    改写建议要引用原文措辞，事实核查也要拿它做比对基准，
    所以必须用 read_order 而不是块插入顺序 —— 前者是版面还原的结果。
    """
    rows = conn.execute(
        """
        SELECT text FROM resume_block
         WHERE resume_id = %s
         ORDER BY read_order NULLS LAST, page_no, block_index
        """,
        (resume_id,),
    ).fetchall()
    return "\n".join(r[0] for r in rows)


def get_job_profile(conn: psycopg.Connection, job_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT j.id, p.title, p.company, p.city, p.job_nature, p.edu_level,
               p.edu_fulltime_required, p.exp_min, p.exp_max,
               p.must_have_skills, p.nice_to_have_skills, p.hard_constraint_evidence,
               p.description, j.raw_text, p.salary_raw
          FROM job j
          JOIN job_profile p ON p.job_id = j.id AND p.is_current
         WHERE j.id = %s
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "job_id": int(row[0]),
        "title": row[1],
        "company": row[2],
        "city": row[3],
        "job_nature": row[4],
        "edu_level": row[5],
        "edu_fulltime_required": row[6],
        "exp_min": row[7],
        "exp_max": row[8],
        "must_have_skills": row[9] or [],
        "nice_to_have_skills": row[10] or [],
        "hard_constraint_evidence": row[11] or {},
        # JD 正文：和 HR 对话必须能用上岗位实际关心什么，否则回复会变成拿简历技能硬凑
        "description": row[12] or "",
        "raw_text": row[13] or "",
        "salary_raw": row[14] or "",
    }


def save_suggestions(
    conn: psycopg.Connection,
    resume_id: int,
    job_id: int | None,
    trace_id,
    suggestions: list,
) -> int:
    """落库建议。suggestions 元素需带 item / fact_status / fact_issue / attempts。"""
    if not suggestions:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO rewrite_suggestion
                (resume_id, job_id, trace_id, category, severity, anchor, problem,
                 direction, example, jd_requirement, fact_status, fact_issue, attempts)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    resume_id,
                    job_id,
                    trace_id,
                    s.item.category,
                    s.item.severity,
                    s.item.anchor,
                    s.item.problem,
                    s.item.direction,
                    s.item.example,
                    s.item.jd_requirement,
                    s.fact_status,
                    s.fact_issue,
                    s.attempts,
                )
                for s in suggestions
            ],
        )
    return len(suggestions)


def get_latest_suggestion_batch(
    conn: psycopg.Connection, resume_id: int, job_id: int | None
) -> dict | None:
    """取最近一批建议（同一次生成的整批）。

    这张表之前只写不读：用户每点一次「生成建议」就重新调模型 40 秒、¥0.03，
    库里却已经躺着一模一样的上一批。现在打开页面先看有没有现成的。

    用 trace_id 圈定批次，不能按 created_at 逐条取 ——
    同一批的几条时间戳可能相同，跨批混在一起就分不清哪条属于哪次生成。
    """
    row = conn.execute(
        """
        SELECT trace_id, created_at
          FROM rewrite_suggestion
         WHERE resume_id = %s AND job_id IS NOT DISTINCT FROM %s
         ORDER BY created_at DESC, id DESC
         LIMIT 1
        """,
        (resume_id, job_id),
    ).fetchone()
    if row is None:
        return None
    trace_id, created_at = row

    items = conn.execute(
        """
        SELECT category, severity, anchor, problem, direction, example,
               jd_requirement, fact_status, fact_issue
          FROM rewrite_suggestion
         WHERE trace_id = %s
         ORDER BY id
        """,
        (trace_id,),
    ).fetchall()

    return {
        "trace_id": str(trace_id) if trace_id else None,
        "created_at": created_at,
        "items": [
            {
                "category": r[0],
                "severity": r[1],
                "anchor": r[2],
                "problem": r[3],
                "direction": r[4],
                "example": r[5],
                "jd_requirement": r[6],
                "fact_status": r[7],
                "fact_issue": r[8],
            }
            for r in items
        ],
    }


def get_profile_created_at(conn: psycopg.Connection, resume_id: int):
    """当前画像的生成时间。建议比它旧说明简历重解析过了，缓存应当作废。"""
    row = conn.execute(
        "SELECT created_at FROM resume_profile WHERE resume_id = %s AND is_current",
        (resume_id,),
    ).fetchone()
    return row[0] if row else None


def list_resumes(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    """简历列表。has_profile 用于界面判断"能不能做匹配"，避免用户点了才报 409。

    is_default 供所有需要"默认用哪份简历"的功能（匹配/建议/定制/HR 回复）取值。
    """
    rows = conn.execute(
        """
        SELECT r.id, r.file_name, r.parse_status, r.source_kind, r.page_count,
               r.parse_error, r.created_at, r.is_default,
               EXISTS (
                   SELECT 1 FROM resume_profile p
                    WHERE p.resume_id = r.id AND p.is_current
               ) AS has_profile
          FROM resume r
         ORDER BY r.is_default DESC, r.id DESC
         LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "id": int(r[0]),
            "file_name": r[1],
            "parse_status": r[2],
            "source_kind": r[3],
            "page_count": r[4],
            "parse_error": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
            "is_default": bool(r[7]),
            "has_profile": bool(r[8]),
        }
        for r in rows
    ]


def list_jobs(
    conn: psycopg.Connection,
    limit: int = 100,
    source: str | None = None,
    with_profile: bool = True,
) -> list[dict]:
    """岗位列表。source 用于按平台筛选（boss / liepin / zhaopin / manual）。

    with_profile=True 时只返回已抽出画像的岗位。
    聊天同步会自动给没入库的岗位建"骨架档案"（只有标题、没画像），
    它们能把聊天记录挂住，但作为岗位列出来只是噪音 —— 点进去什么都看不到。
    """
    sql = """
        SELECT j.id, j.source, j.url, p.title, p.company, p.city, p.job_nature,
               p.edu_level, p.exp_min, p.exp_max, p.must_have_skills,
               p.nice_to_have_skills, j.created_at, j.detail_level,
               p.graduate_years
          FROM job j
          LEFT JOIN job_profile p ON p.job_id = j.id AND p.is_current
    """
    where: list[str] = []
    params: list = []
    if source:
        where.append("j.source = %s")
        params.append(source)
    if with_profile:
        where.append("p.id IS NOT NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY j.id DESC LIMIT %s"
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        {
            "id": int(r[0]),
            "source": r[1],
            "url": r[2],
            "title": r[3],
            "company": r[4],
            "city": r[5],
            "job_nature": r[6],
            "edu_level": r[7],
            "exp_min": r[8],
            "exp_max": r[9],
            "must_have_skills": r[10] or [],
            "nice_to_have_skills": r[11] or [],
            "created_at": r[12].isoformat() if r[12] else None,
            "detail_level": r[13] or "unknown",
            "graduate_years": r[14] or [],
            "has_profile": r[3] is not None,
        }
        for r in rows
    ]


def count_jobs_by_source(conn: psycopg.Connection) -> dict[str, int]:
    """各平台岗位数，用于界面上的分类标签。"""
    rows = conn.execute(
        "SELECT source, count(*) FROM job GROUP BY source ORDER BY count(*) DESC"
    ).fetchall()
    return {str(r[0] or "unknown"): int(r[1]) for r in rows}


def create_job(
    conn: psycopg.Connection,
    *,
    source: str,
    raw_text: str,
    raw_text_original: str | None = None,
    raw_html: str | None = None,
    url: str | None = None,
    source_job_id: str | None = None,
    detail_level: str = "unknown",
) -> tuple[int, bool]:
    """同源同 ID 已存在则更新原文，返回 (id, 是否新增)。

    第二个返回值是给「自动发现新岗位」用的：定时扫描时只有真的新增才该提醒，
    老岗位被重新抓到不算新岗位 —— 没有这个标记，每 2 小时都会推一遍同样的岗位。

    raw_text 存的是**清洗后**的文本（抽取用的是它），raw_text_original 存清洗前。
    两份都留是有代价的（约多一倍字符），但只留一份的代价更大：
    清洗规则改坏之后，既没法复现也没法回归。
    """
    if source_job_id:
        row = conn.execute(
            "SELECT id FROM job WHERE source = %s AND source_job_id = %s",
            (source, source_job_id),
        ).fetchone()
        if row:
            # 完整度只升不降：先用详情页抓过，之后又被列表页覆盖时不能退回 digest
            conn.execute(
                """
                UPDATE job SET raw_text = %s,
                               raw_text_original = COALESCE(%s, raw_text_original),
                               raw_html = COALESCE(%s, raw_html),
                               url = COALESCE(%s, url),
                               detail_level = CASE
                                   WHEN %s = 'full' THEN 'full'
                                   ELSE detail_level
                               END,
                               fetched_at = now()
                 WHERE id = %s
                """,
                (raw_text, raw_text_original, raw_html, url, detail_level, row[0]),
            )
            return int(row[0]), False

    row = conn.execute(
        """
        INSERT INTO job (source, source_job_id, url, raw_html, raw_text,
                         raw_text_original, fetched_at, detail_level)
        VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
        RETURNING id
        """,
        (source, source_job_id, url, raw_html, raw_text, raw_text_original, detail_level),
    ).fetchone()
    assert row is not None
    return int(row[0]), True


def save_job_profile(
    conn: psycopg.Connection, job_id: int, profile: dict, *, extract_version: str = "m1"
) -> int:
    conn.execute(
        "UPDATE job_profile SET is_current = FALSE WHERE job_id = %s AND is_current", (job_id,)
    )
    must = profile.get("must_have_skills", []) or []
    nice = profile.get("nice_to_have_skills", []) or []
    # 届别可能是字符串（"2028届"），统一成 int 再存。抽错的脏值直接丢掉 ——
    # 一个 "2028届" 里混进年份 2082 会让筛选结果莫名其妙
    years: list[int] = []
    for raw in profile.get("graduate_years") or []:
        try:
            year = int(str(raw).strip().rstrip("届"))
        except (TypeError, ValueError):
            continue
        if 2000 <= year <= 2100 and year not in years:
            years.append(year)
    company_key = company_key_of(profile.get("company"))
    row = conn.execute(
        """
        INSERT INTO job_profile
            (job_id, extract_version, is_current, title, company, city, salary_raw, job_nature,
             edu_level, edu_fulltime_required, exp_min, exp_max, skills, description,
             hard_constraint_evidence, must_have_skills, nice_to_have_skills, raw_extract,
             company_key, graduate_years, campus_type)
        VALUES (%s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            job_id,
            extract_version,
            profile.get("title"),
            profile.get("company"),
            profile.get("city"),
            profile.get("salary_raw"),
            profile.get("job_nature"),
            profile.get("edu_level"),
            profile.get("edu_fulltime_required"),
            profile.get("exp_min"),
            profile.get("exp_max"),
            # skills 列保留为 must+nice 的合并视图，方便列表页展示；
            # 匹配逻辑只读下面两个分列，不读这个合并值
            psycopg.types.json.Jsonb(must + nice),
            profile.get("description"),
            psycopg.types.json.Jsonb(profile.get("hard_constraint_evidence", {})),
            psycopg.types.json.Jsonb(must),
            psycopg.types.json.Jsonb(nice),
            psycopg.types.json.Jsonb(profile),
            company_key,
            psycopg.types.json.Jsonb(years),
            profile.get("campus_type"),
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


# ---------- 主简历 ----------

def set_default_resume(conn: psycopg.Connection, resume_id: int) -> bool:
    """把某份简历设为主简历。简历不存在返回 False。

    先清掉旧的默认再设新的：唯一索引（is_default WHERE true）保证同一时刻
    最多一份，两条 UPDATE 的顺序无所谓，包在同一事务里即可。
    """
    row = conn.execute("SELECT 1 FROM resume WHERE id = %s", (resume_id,)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE resume SET is_default = FALSE WHERE is_default")
    conn.execute("UPDATE resume SET is_default = TRUE WHERE id = %s", (resume_id,))
    conn.commit()
    return True


def get_default_resume_id(conn: psycopg.Connection) -> int | None:
    """取主简历 id。没有显式设置过时回退到「最新一份有画像的简历」。"""
    row = conn.execute(
        "SELECT id FROM resume WHERE is_default ORDER BY id LIMIT 1"
    ).fetchone()
    if row:
        return int(row[0])
    row = conn.execute(
        """
        SELECT r.id FROM resume r
         WHERE EXISTS (SELECT 1 FROM resume_profile p
                        WHERE p.resume_id = r.id AND p.is_current)
         ORDER BY r.id DESC LIMIT 1
        """
    ).fetchone()
    return int(row[0]) if row else None


# ---------- 投递跟踪 ----------

# 只有两个可写入状态：投了没。六态是历史设计，已废弃（见 schema.sql 的迁移注释）
APPLICATION_STATUSES = ("candidate", "applied")


def create_application(
    conn: psycopg.Connection, job_id: int, status: str, note: str | None
) -> int:
    """为岗位建一条跟踪记录。同岗位重复创建返回已有 id（幂等，前端可放心重复点）。"""
    row = conn.execute("SELECT id FROM application WHERE job_id = %s", (job_id,)).fetchone()
    if row:
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO application (job_id, status, note) VALUES (%s, %s, %s) RETURNING id",
        (job_id, status, note),
    )
    app_id = int(cur.fetchone()[0])
    conn.execute(
        "INSERT INTO application_event (application_id, status, note) VALUES (%s, %s, %s)",
        (app_id, status, note),
    )
    conn.commit()
    return app_id


def update_application_status(
    conn: psycopg.Connection, app_id: int, status: str, note: str | None
) -> bool:
    """推进状态并记时间线。记录不存在返回 False。"""
    row = conn.execute("SELECT id FROM application WHERE id = %s", (app_id,)).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE application SET status = %s, note = COALESCE(%s, note), updated_at = now() WHERE id = %s",
        (status, note, app_id),
    )
    conn.execute(
        "INSERT INTO application_event (application_id, status, note) VALUES (%s, %s, %s)",
        (app_id, status, note),
    )
    conn.commit()
    return True


def delete_application(conn: psycopg.Connection, app_id: int) -> bool:
    row = conn.execute("SELECT id FROM application WHERE id = %s", (app_id,)).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM application WHERE id = %s", (app_id,))
    conn.commit()
    return True


def get_application_with_timeline(conn: psycopg.Connection, app_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT a.id, a.job_id, a.status, a.note, a.created_at, a.updated_at,
               p.title, p.company, p.city, p.salary_raw, j.source, j.url
          FROM application a
          JOIN job j ON j.id = a.job_id
          LEFT JOIN job_profile p ON p.job_id = j.id AND p.is_current
         WHERE a.id = %s
        """,
        (app_id,),
    ).fetchone()
    if row is None:
        return None
    events = conn.execute(
        """
        SELECT status, note, created_at FROM application_event
         WHERE application_id = %s ORDER BY created_at, id
        """,
        (app_id,),
    ).fetchall()
    return {
        "id": row[0],
        "job_id": row[1],
        "status": row[2],
        "note": row[3],
        "created_at": row[4],
        "updated_at": row[5],
        "title": row[6],
        "company": row[7],
        "city": row[8],
        "salary_raw": row[9],
        "source": row[10],
        "url": row[11],
        "timeline": [
            {"status": e[0], "note": e[1], "created_at": e[2]} for e in events
        ],
    }


def list_applications(
    conn: psycopg.Connection, status: str | None = None, limit: int = 100
) -> list[dict]:
    sql = """
        SELECT a.id, a.job_id, a.status, a.note, a.created_at, a.updated_at,
               p.title, p.company, p.city, p.salary_raw, j.source, j.url
          FROM application a
          JOIN job j ON j.id = a.job_id
          LEFT JOIN job_profile p ON p.job_id = j.id AND p.is_current
    """
    params: list = []
    if status:
        sql += " WHERE a.status = %s"
        params.append(status)
    sql += " ORDER BY a.updated_at DESC LIMIT %s"
    params.append(limit)
    return [
        {
            "id": r[0], "job_id": r[1], "status": r[2], "note": r[3],
            "created_at": r[4], "updated_at": r[5], "title": r[6], "company": r[7],
            "city": r[8], "salary_raw": r[9], "source": r[10], "url": r[11],
        }
        for r in conn.execute(sql, params).fetchall()
    ]


def get_application_by_job(conn: psycopg.Connection, job_id: int) -> dict | None:
    """工作台用：看这个岗位当前跟踪到哪一步了（没有就是 None）。"""
    row = conn.execute(
        "SELECT id, status, note, updated_at FROM application WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "status": row[1], "note": row[2], "updated_at": row[3]}


def get_application_stats(conn: psycopg.Connection) -> dict:
    """投递进展聚合：已投数 / 有回应数 / 按平台分组。

    只给绝对数，不返回「回复率」。
    原因：一轮求职通常只投十几家，样本不够时比率会剧烈波动，
    而「投了 3 家、1 家有回应」是事实，「33% 回复率」是把噪音包装成结论。
    界面上的数字会成为决策依据，那就不能给它一个不稳定的形态。

    「有回应」的判据：该岗位关联的聊天记录里存在 HR 的消息。
    一个已知噪音：猎聘的系统提示（"您可以修改打招呼语"）会被扩展读成 HR 消息，
    可能轻微抬高这个数字。
    """
    status_rows = conn.execute(
        "SELECT status, count(*) FROM application GROUP BY status"
    ).fetchall()
    status_counts = {s: int(n) for s, n in status_rows}

    tracked = sum(status_counts.values())
    applied = status_counts.get("applied", 0)

    # 有回应 = 记录过的岗位里，聊天记录出现过 HR 的消息
    replied = conn.execute(
        """
        SELECT count(DISTINCT a.job_id)
          FROM application a
         WHERE EXISTS (SELECT 1 FROM chat_log cl
                        WHERE cl.job_id = a.job_id AND cl.role = 'HR')
        """
    ).fetchone()[0]

    by_source = [
        {
            "source": r[0],
            "total": int(r[1]),
            "replied": int(r[2]),
            "label": {"boss": "BOSS直聘", "liepin": "猎聘", "zhaopin": "智联招聘",
                      "manual": "手动添加"}.get(r[0], r[0]),
        }
        for r in conn.execute(
            """
            SELECT j.source, count(*) FILTER (WHERE a.status = 'applied'),
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM chat_log cl
                        WHERE cl.job_id = a.job_id AND cl.role = 'HR'))
              FROM application a
              JOIN job j ON j.id = a.job_id
             GROUP BY j.source
             ORDER BY count(*) DESC
            """
        ).fetchall()
    ]

    return {
        "tracked": tracked,
        "applied": applied,
        "replied": int(replied),
        "status_counts": status_counts,
        "by_source": by_source,
    }


def add_resume_skill(
    conn: psycopg.Connection, resume_id: int, name: str, evidence: str
) -> dict | None:
    """给简历画像追加一条技能（用户自述）。

    source=user_declared 与抽取出来的技能区分开：
    抽取出的一定有原文依据，自述的没有，界面上要提示"面试要能讲清楚"。
    已经存在同名技能时不重复添加，只把来源标成自述（说明用户主动确认过）。
    """
    row = conn.execute(
        "SELECT id, skills FROM resume_profile WHERE resume_id = %s AND is_current",
        (resume_id,),
    ).fetchone()
    if row is None:
        return None
    profile_id, skills = row[0], list(row[1] or [])
    for item in skills:
        if isinstance(item, dict) and (item.get("name") or "").lower() == name.lower():
            item["source"] = "user_declared"
            item["evidence"] = item.get("evidence") or evidence
            conn.execute(
                "UPDATE resume_profile SET skills = %s WHERE id = %s",
                (psycopg.types.json.Jsonb(skills), profile_id),
            )
            conn.commit()
            return {"added": False, "skills": skills}
    skills.append({"name": name, "level": None, "evidence": evidence, "source": "user_declared"})
    conn.execute(
        "UPDATE resume_profile SET skills = %s WHERE id = %s",
        (psycopg.types.json.Jsonb(skills), profile_id),
    )
    conn.commit()
    return {"added": True, "skills": skills}
