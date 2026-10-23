"""会话持久化：ChatAgent 的状态与消息。

为什么持久化
    用户拍板"可回看"。而且槽位（意图/城市/简历）跨轮复用，
    关掉浏览器再打开还能接着聊，不用重新说一遍意图。
"""

from __future__ import annotations

import psycopg


def create(conn: psycopg.Connection, title: str | None, slots: dict) -> int:
    row = conn.execute(
        "INSERT INTO conversation (title, slots) VALUES (%s, %s) RETURNING id",
        (title, psycopg.types.json.Jsonb(slots)),
    ).fetchone()
    assert row is not None
    return int(row[0])


def get(conn: psycopg.Connection, conversation_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, title, slots, created_at FROM conversation WHERE id = %s",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "title": row[1],
        "slots": row[2] or {},
        "created_at": row[3].isoformat() if row[3] else None,
    }


def update_slots(conn: psycopg.Connection, conversation_id: int, slots: dict) -> None:
    conn.execute(
        "UPDATE conversation SET slots = %s, updated_at = now() WHERE id = %s",
        (psycopg.types.json.Jsonb(slots), conversation_id),
    )


def list_recent(conn: psycopg.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.id, c.title, c.updated_at,
               (SELECT count(*) FROM conversation_message m WHERE m.conversation_id = c.id) AS messages
          FROM conversation c
         ORDER BY c.updated_at DESC
         LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "id": int(r[0]),
            "title": r[1],
            "updated_at": r[2].isoformat() if r[2] else None,
            "messages": int(r[3]),
        }
        for r in rows
    ]


def add_message(
    conn: psycopg.Connection,
    conversation_id: int,
    role: str,
    content: str,
    meta: dict | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO conversation_message (conversation_id, role, content, meta)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (
            conversation_id,
            role,
            content,
            psycopg.types.json.Jsonb(meta) if meta else None,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def get_messages(conn: psycopg.Connection, conversation_id: int, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT role, content, meta, created_at
          FROM conversation_message
         WHERE conversation_id = %s
         ORDER BY id
         LIMIT %s
        """,
        (conversation_id, limit),
    ).fetchall()
    return [
        {
            "role": r[0],
            "content": r[1],
            "meta": r[2],
            "created_at": r[3].isoformat() if r[3] else None,
        }
        for r in rows
    ]


def recent_window(conn: psycopg.Connection, conversation_id: int, limit: int = 8) -> list[dict]:
    """最近几轮对话，供组装上下文。不做全量回放 —— 会话越长上下文越要克制。"""
    rows = conn.execute(
        """
        SELECT role, content FROM (
            SELECT role, content, id FROM conversation_message
             WHERE conversation_id = %s
             ORDER BY id DESC LIMIT %s
        ) t ORDER BY id
        """,
        (conversation_id, limit),
    ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]
