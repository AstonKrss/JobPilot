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

    规则只有三条：
        op=remove          → 从数组里移除（不再用"覆盖"糊弄，那会把别的值一起清掉）
        additive 或 op=add → 并进数组，已有的保留
        其余               → 覆盖

    第一条和第二条都是 bug 修出来的：

    - 原来只判 `modality=="additive" and op=="add"`。模型只要少给一个 op
      （提示词里并没有强制二者同现），"深圳也可以" 就会走覆盖分支，
      把已有的广州整条丢掉。历史数据里有一次是正确合并的 —— 那是运气，不是约束。
    - op=remove 原来落到覆盖分支，等于"要删 X"变成"只留 X"。提示词里写了
      允许 remove，代码却没实现，这类不一致最难发现：模型照做了，数据反了。
    """
    op = action.get("op")
    new = action.get("value")
    base = current if isinstance(current, list) else ([current] if current else [])

    if op == "remove":
        drop = new if isinstance(new, list) else [new]
        return [item for item in base if item not in drop]

    if action.get("modality") == "additive" or op == "add":
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
