"""worker 循环：领任务 → 执行 handler → 落终态。

宿主
    默认在 FastAPI lifespan 里以 asyncio 后台任务启动（单机无需第二个进程）；
    scripts/worker.py 可选独立进程跑同一循环（headless 定时发现场景）。
    启动时先把上个进程遗留的 running 行重排 pending（崩溃恢复）。

失败与重试
    handler 抛 TaskCancelled → cancelled 终态（用户取消，不算失败）；
    其他异常 → attempts < max_attempts 时回 pending 等下轮重试，否则 failed。
    重试只对"瞬态"判断保守处理：一律先按可重试回 pending，
    由 max_attempts 兜底防止无限循环——简单且不会把真错误吞成重试风暴。
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..config import settings
from . import queue
from .handlers import TASK_HANDLERS
from .queue import TaskCancelled

logger = logging.getLogger(__name__)

_worker_task: asyncio.Task | None = None


async def _run_one(task: dict, conn) -> None:
    task_id = task["id"]
    handler = TASK_HANDLERS.get(task["type"])
    if handler is None:
        queue.fail(conn, task_id, f"未注册的任务类型：{task['type']}", requeue=False)
        return

    async def progress(text: str) -> None:
        # 协作式取消：写进度时顺带检查 cancelled，已取消则抛信号中止 handler
        await asyncio.to_thread(queue.mark_progress, conn, task_id, text)

    try:
        result = await handler(task, progress)
        queue.finish(conn, task_id, result)
        logger.info("任务 #%s [%s] 完成", task_id, task["type"])
    except TaskCancelled as exc:
        conn.execute(
            "UPDATE task_queue SET progress = '已取消', finished_at = now() WHERE id = %s",
            (task_id,),
        )
        conn.commit()
        logger.info("任务 #%s 被用户取消", task_id)
    except Exception as exc:  # noqa: BLE001 - handler 的一切异常都要落终态
        logger.exception("任务 #%s [%s] 失败", task_id, task["type"])
        requeue = task["attempts"] < task["max_attempts"]
        queue.fail(conn, task_id, str(exc)[:500], requeue=requeue)


async def worker_loop() -> None:
    """worker 主循环。由 lifespan 以 asyncio.create_task 启动，cancel 优雅退出。"""
    from ..db.pool import get_pool

    logger.info("任务 worker 启动（poll %.1fs）", settings.tasks_poll_interval_s)
    with get_pool().connection() as conn:
        requeued = queue.requeue_stale(conn, settings.tasks_stale_after_s)
        if requeued:
            logger.warning("恢复了 %d 个上个进程遗留的 running 任务", requeued)

    last_scheduled_check = 0.0
    while True:
        try:
            # 定时发现：间隔到点且没有 pending/running 的 discover 任务时自动入队
            interval_h = settings.tasks_scheduled_discover_h
            now = time.monotonic()
            if interval_h and interval_h > 0 and now - last_scheduled_check > 60:
                last_scheduled_check = now
                _maybe_schedule_discover(interval_h)

            with get_pool().connection() as conn:
                task = queue.claim(conn)
            if task is None:
                await asyncio.sleep(settings.tasks_poll_interval_s)
                continue
            logger.info("领取任务 #%s [%s]（第 %s 次尝试）", task["id"], task["type"], task["attempts"])
            with get_pool().connection() as conn:
                await _run_one(task, conn)
        except asyncio.CancelledError:
            logger.info("任务 worker 收到退出信号")
            raise
        except Exception:
            # 队列表暂时不可用等基础设施故障：记日志退避，不能让 worker 死掉
            logger.exception("worker 循环异常，退避后继续")
            await asyncio.sleep(3.0)


def _maybe_schedule_discover(interval_h: float) -> None:
    from ..db.pool import get_pool

    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                """
                SELECT status, created_at FROM task_queue
                 WHERE type = 'discover_run'
                 ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        enqueue_it = False
        if row is None:
            enqueue_it = True
        else:
            status = row[0]
            age_h = 0.0
            if row[1] is not None:
                from datetime import datetime, timezone

                created = row[1] if row[1].tzinfo else row[1].replace(tzinfo=timezone.utc)
                age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
            if status in ("pending", "running"):
                enqueue_it = False  # 已经有一轮在排队/执行，不重复
            else:
                enqueue_it = age_h >= interval_h
        if enqueue_it:
            with get_pool().connection() as conn:
                queue.enqueue(conn, "discover_run", {"scheduled": True})
            logger.info("定时发现触发：距上次 %.2f 小时（阈值 %.1f）", age_h, interval_h)
    except Exception:  # noqa: BLE001 - 调度失败只影响"这轮没排上"，worker 本体不能死
        logger.exception("定时发现检查失败（忽略）")


def start() -> asyncio.Task:
    """lifespan 调用：启动 worker 并保存引用，供 shutdown 时取消。"""
    global _worker_task
    if not settings.tasks_enabled:
        logger.info("任务层已禁用（TASKS_ENABLED=false），worker 不启动")
        raise RuntimeError("tasks disabled")
    _worker_task = asyncio.create_task(worker_loop())
    return _worker_task


async def stop() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _worker_task = None
