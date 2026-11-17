"""task_queue 的队列原语。

领取用 Postgres 的标准队列姿势：
    UPDATE task_queue SET status='running', …
     WHERE id = (SELECT id FROM task_queue
                  WHERE status = 'pending'
                  ORDER BY created_at
                  FOR UPDATE SKIP LOCKED LIMIT 1)
    RETURNING …
两个 worker 并发领取会拿到不同的行（第二个在子查询里跳过被锁的行），
这是不引入 MQ 也能横向扩容的关键。

取消是协作式的：pending 直接改状态；running 由 handler 在写进度时
调 `is_cancelled` 自查，发现 cancelled 就自行收尾（不粗暴杀协程）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

WORKER_ID = "worker-main"


def enqueue(conn, task_type: str, payload: dict | None = None, *, max_attempts: int = 2) -> int:
    row = conn.execute(
        """
        INSERT INTO task_queue (type, payload, max_attempts)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (task_type, Jsonb(payload or {}), max_attempts),
    ).fetchone()
    conn.commit()
    logger.info("任务入队 #%s %s", row[0], task_type)
    return int(row[0])


def claim(conn, worker_id: str = WORKER_ID) -> dict | None:
    """领取一个 pending 任务。没有可领的返回 None。"""
    row = conn.execute(
        """
        UPDATE task_queue SET
            status = 'running',
            worker_id = %s,
            attempts = attempts + 1,
            started_at = now(),
            progress = NULL
         WHERE id = (
            SELECT id FROM task_queue
             WHERE status = 'pending'
             ORDER BY created_at
             FOR UPDATE SKIP LOCKED LIMIT 1
         )
        RETURNING id, type, payload, attempts, max_attempts
        """,
        (worker_id,),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "type": row[1],
        "payload": row[2] or {},
        "attempts": int(row[3]),
        "max_attempts": int(row[4]),
    }


def finish(conn, task_id: int, result: dict | None) -> None:
    conn.execute(
        """
        UPDATE task_queue SET status = 'done', result = %s, progress = '完成',
               finished_at = now()
         WHERE id = %s
        """,
        (Jsonb(result or {}), task_id),
    )
    conn.commit()


def fail(conn, task_id: int, message: str, *, requeue: bool) -> None:
    """失败落库。requeue=True（瞬态失败且还有重试额度）回 pending 等下轮，
    否则终态 failed。"""
    if requeue:
        conn.execute(
            """
            UPDATE task_queue SET status = 'pending', error = %s,
                   progress = '等待重试', worker_id = NULL, started_at = NULL
             WHERE id = %s
            """,
            (message[:500], task_id),
        )
    else:
        conn.execute(
            """
            UPDATE task_queue SET status = 'failed', error = %s,
                   progress = '失败', finished_at = now()
             WHERE id = %s
            """,
            (message[:500], task_id),
        )
    conn.commit()


def mark_progress(conn, task_id: int, text: str) -> None:
    """写进度。同一事务顺带检查协作式取消：已取消则抛 TaskCancelled。"""
    conn.execute("UPDATE task_queue SET progress = %s WHERE id = %s", (text[:300], task_id))
    row = conn.execute("SELECT status FROM task_queue WHERE id = %s", (task_id,)).fetchone()
    conn.commit()
    if row and row[0] == "cancelled":
        raise TaskCancelled(f"任务 #{task_id} 已被用户取消")


class TaskCancelled(Exception):
    """协作式取消信号：handler 捕获后干净收尾，worker 落 cancelled 终态。"""


def cancel(conn, task_id: int) -> dict:
    row = conn.execute(
        "SELECT status FROM task_queue WHERE id = %s", (task_id,)
    ).fetchone()
    if row is None:
        return {"ok": False, "reason": "任务不存在"}
    status = row[0]
    if status == "pending":
        conn.execute(
            "UPDATE task_queue SET status = 'cancelled', finished_at = now(),"
            " progress = '已取消' WHERE id = %s",
            (task_id,),
        )
        conn.commit()
        return {"ok": True, "mode": "pending"}
    if status == "running":
        # running 不硬杀：标记后由 handler 在下一个进度检查点自行退出
        conn.execute(
            "UPDATE task_queue SET status = 'cancelled' WHERE id = %s",
            (task_id,),
        )
        conn.commit()
        return {"ok": True, "mode": "cooperative", "note": "已标记取消，worker 将在下一个进度检查点退出"}
    return {"ok": False, "reason": f"任务已是终态 {status}，无法取消"}


def requeue_stale(conn, stale_after_s: float) -> int:
    """把上个进程遗留的 running 行重排 pending。

    在 worker 启动时调用（单机部署下，任何 running 行都意味着上个进程
    没有正常收尾）。返回重排数量。
    """
    row = conn.execute(
        """
        WITH moved AS (
            UPDATE task_queue SET status = 'pending', worker_id = NULL,
                   started_at = NULL, progress = '服务重启后重新排队',
                   error = '上个进程未完成，已自动恢复'
             WHERE status = 'running'
               AND started_at < now() - (%s || ' seconds')::interval
            RETURNING id
        ) SELECT count(*) FROM moved
        """,
        (str(int(stale_after_s)),),
    ).fetchone()
    conn.commit()
    return int(row[0])


def list_tasks(conn, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, type, status, progress, error, attempts, max_attempts,
               created_at, started_at, finished_at, payload
          FROM task_queue ORDER BY id DESC LIMIT %s
        """,
        (max(1, min(int(limit), 100)),),
    ).fetchall()
    return [
        {
            "id": int(r[0]), "type": r[1], "status": r[2], "progress": r[3],
            "error": r[4], "attempts": r[5], "max_attempts": r[6],
            "created_at": str(r[7]), "started_at": str(r[8]) if r[8] else None,
            "finished_at": str(r[9]) if r[9] else None,
            "payload": r[10] or {},
        }
        for r in rows
    ]


def get_task(conn, task_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, type, status, progress, error, result, attempts, max_attempts,
               created_at, started_at, finished_at, payload, worker_id
          FROM task_queue WHERE id = %s
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]), "type": row[1], "status": row[2], "progress": row[3],
        "error": row[4], "result": row[5] or {}, "attempts": row[6],
        "max_attempts": row[7], "created_at": str(row[8]),
        "started_at": str(row[9]) if row[9] else None,
        "finished_at": str(row[10]) if row[10] else None,
        "payload": row[11] or {},
        "worker_id": row[12],
    }
