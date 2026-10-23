"""简历画像与岗位画像的读取，供匹配层使用。"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb


def get_current_profile(conn: psycopg.Connection, resume_id: int) -> dict | None:
    """取简历当前画像。无画像（解析未完成或失败）返回 None。"""
    row = conn.execute(
        """
        SELECT basic, skills, experiences, projects
          FROM resume_profile
         WHERE resume_id = %s AND is_current
        """,
        (resume_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "basic": row[0] or {},
        "skills": row[1] or [],
        "experiences": row[2] or [],
        "projects": row[3] or [],
    }


def list_current_job_profiles(conn: psycopg.Connection) -> list[dict]:
    """取所有岗位的当前画像。

    只读 is_current：同一岗位重抽后旧画像会置为 false，若不过滤会匹配到过期数据。
    """
    rows = conn.execute(
        """
        SELECT j.id, p.title, p.company, p.city, p.job_nature, p.edu_level,
               p.edu_fulltime_required, p.exp_min, p.exp_max,
               p.must_have_skills, p.nice_to_have_skills, p.hard_constraint_evidence, j.detail_level,
               j.url
          FROM job j
          JOIN job_profile p ON p.job_id = j.id AND p.is_current
         ORDER BY j.id
        """
    ).fetchall()
    return [
        {
            "job_id": int(r[0]),
            "title": r[1],
            "company": r[2],
            "city": r[3],
            "job_nature": r[4],
            "edu_level": r[5],
            "edu_fulltime_required": r[6],
            "exp_min": r[7],
            "exp_max": r[8],
            "must_have_skills": r[9] or [],
            "nice_to_have_skills": r[10] or [],
            "hard_constraint_evidence": r[11] or {},
            "detail_level": r[12] or "unknown",
            "url": r[13],
        }
        for r in rows
    ]


def get_job_library_version(conn: psycopg.Connection) -> str:
    """岗位库版本指纹，用于判断匹配缓存是否过期。

    用「当前画像条数 + 最大画像 id」：新岗位入库会让条数变；
    已有岗位重新抽取会新增一行 job_profile（旧的置 is_current=false），最大 id 跟着变。
    两种变化都能被捕捉到，不必依赖时间戳比大小。
    """
    row = conn.execute(
        "SELECT count(*), coalesce(max(id), 0) FROM job_profile WHERE is_current"
    ).fetchone()
    return f"{row[0]}-{row[1]}"


def get_match_cache(conn: psycopg.Connection, resume_id: int, cond_hash: str, job_version: str):
    """取一份未过期的匹配结果。返回 (payload, created_at) 或 None。"""
    row = conn.execute(
        """
        SELECT payload, created_at
          FROM match_run
         WHERE resume_id = %s AND cond_hash = %s AND job_version = %s
         ORDER BY created_at DESC
         LIMIT 1
        """,
        (resume_id, cond_hash, job_version),
    ).fetchone()
    return (row[0], row[1]) if row else None


def save_match_cache(
    conn: psycopg.Connection,
    resume_id: int,
    cond_hash: str,
    job_version: str,
    payload: dict,
    elapsed_ms: int,
) -> None:
    """存一份匹配结果。同一组合只留最新一条，避免表无限膨胀。"""
    conn.execute(
        "DELETE FROM match_run WHERE resume_id = %s AND cond_hash = %s AND job_version = %s",
        (resume_id, cond_hash, job_version),
    )
    conn.execute(
        """
        INSERT INTO match_run (resume_id, cond_hash, job_version, payload, elapsed_ms)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (resume_id, cond_hash, job_version, Jsonb(payload), elapsed_ms),
    )
    conn.commit()
