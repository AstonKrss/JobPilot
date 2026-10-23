"""画像读写：当前值 + 变更历史。"""

from __future__ import annotations

import logging

import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)


def get_profile(conn: psycopg.Connection) -> dict:
    """返回 {key: {value, evidence, modality, updated_at}}。"""
    rows = conn.execute(
        """
        SELECT key, value, evidence, modality, updated_at
          FROM user_profile ORDER BY key
        """
    ).fetchall()
    return {
        r[0]: {
            "value": r[1],
            "evidence": r[2],
            "modality": r[3],
            "updated_at": r[4].isoformat() if r[4] else None,
        }
        for r in rows
    }


def get_flat_profile(conn: psycopg.Connection) -> dict:
    """只要当前值，拼 prompt 用。"""
    return {k: v["value"] for k, v in get_profile(conn).items()}


def get_history(conn: psycopg.Connection, key: str | None = None, limit: int = 50) -> list[dict]:
    sql = """
        SELECT key, old_value, new_value, evidence, modality, changed_at
          FROM user_profile_history
    """
    params: list = []
    if key:
        sql += " WHERE key = %s"
        params.append(key)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)
    return [
        {
            "key": r[0],
            "old_value": r[1],
            "new_value": r[2],
            "evidence": r[3],
            "modality": r[4],
            "changed_at": r[5].isoformat() if r[5] else None,
        }
        for r in conn.execute(sql, params).fetchall()
    ]


def _merge(current, action: dict):
    """按 op 与 modality 算新值。

    合并规则刻意简单：additive + add 时往数组里并（已有的保留），
    其余情况一律以新值为准 —— 复杂规则在数据量小时只会带来误判。
    """
    new = action.get("value")
    if action.get("modality") == "additive" and action.get("op") == "add":
        base = current if isinstance(current, list) else ([current] if current else [])
        items = new if isinstance(new, list) else [new]
        merged = list(base)
        for item in items:
            if item and item not in merged:
                merged.append(item)
        return merged
    return new


def apply_update(
    conn: psycopg.Connection,
    action: dict,
    conversation_id: int | None = None,
) -> dict:
    """写入一个槽位变更，并留一条历史。返回写入后的值。"""
    key = action["key"]
    row = conn.execute("SELECT value FROM user_profile WHERE key = %s", (key,)).fetchone()
    old = row[0] if row else None
    value = _merge(old, action)

    conn.execute(
        """
        INSERT INTO user_profile (key, value, evidence, modality, conversation_id, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (key) DO UPDATE
           SET value = EXCLUDED.value,
               evidence = EXCLUDED.evidence,
               modality = EXCLUDED.modality,
               conversation_id = EXCLUDED.conversation_id,
               updated_at = now()
        """,
        (key, Jsonb(value), action.get("evidence"), action.get("modality"), conversation_id),
    )
    conn.execute(
        """
        INSERT INTO user_profile_history (key, old_value, new_value, evidence, modality)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            key,
            Jsonb(old) if old is not None else None,
            Jsonb(value),
            action.get("evidence"),
            action.get("modality"),
        ),
    )
    conn.commit()
    logger.info("画像更新 %s: %s -> %s（%s）", key, old, value, action.get("modality"))
    return {"key": key, "old_value": old, "new_value": value}
