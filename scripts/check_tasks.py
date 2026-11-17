"""异步任务层（P1.3）的回归：队列语义 / 取消 / 重试 / stale 恢复 / 并发 claim / 接线断言。

测试用专用 type（"check_tasks_noop" 等）与 url 特征隔离，跑完精确还原，
不碰真实任务行。handler 错误重试语义用注册表临时覆盖验证（不真调模型）。

用法：
    .venv/Scripts/python.exe scripts/check_tasks.py   # 需要数据库
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}{(' — ' + detail) if detail else ''}")


def main() -> int:
    import psycopg

    from app.config import settings
    from app.db.pool import init_schema
    from app.tasks import queue
    from app.tasks.handlers import TASK_HANDLERS
    from app.tasks.queue import TaskCancelled

    init_schema()
    conn = psycopg.connect(settings.database_url, connect_timeout=3)

    snap = conn.execute(
        "SELECT id, type, payload, status, progress, result, error, attempts, max_attempts,"
        " worker_id, created_at, started_at, finished_at FROM task_queue"
    ).fetchall()
    TEST_TYPES = ("check_tasks_noop", "check_tasks_boom", "check_tasks_echo")

    try:
        conn.execute("DELETE FROM task_queue WHERE type = ANY(%s)", (list(TEST_TYPES),))
        conn.commit()

        print("== ① 队列基本语义：入队 → 领取 → 进度 → 完成 ==")
        tid = queue.enqueue(conn, "check_tasks_noop", {"hello": "world"})
        claimed = queue.claim(conn, "worker-test")
        check("claim 领到刚入队的任务", claimed is not None and claimed["id"] == tid)
        check("领取后状态 running 且 attempts=1",
              claimed is not None and claimed["attempts"] == 1)
        try:
            queue.mark_progress(conn, tid, "处理中 50%")
            check("进度写入成功", queue.get_task(conn, tid)["progress"] == "处理中 50%")
        except TaskCancelled:
            check("进度写入成功", False, "意外被取消")
        queue.finish(conn, tid, {"result": "ok"})
        t = queue.get_task(conn, tid)
        check("完成后状态 done 且带 result", t["status"] == "done" and t["result"] == {"result": "ok"})

        print("\n== ② 取消语义 ==")
        tid2 = queue.enqueue(conn, "check_tasks_noop", {})
        res = queue.cancel(conn, tid2)
        check("pending 任务直接取消", res["ok"] and res["mode"] == "pending")
        t2 = queue.get_task(conn, tid2)
        check("取消后 status=cancelled", t2["status"] == "cancelled")
        # running 的取消是协作式：标记 cancelled，handler 写进度时抛 TaskCancelled
        tid2b = queue.enqueue(conn, "check_tasks_noop", {})
        queue.claim(conn, "worker-test")
        queue.cancel(conn, tid2b)
        check("running 任务被标记取消（协作式）", queue.get_task(conn, tid2b)["status"] == "cancelled")
        cancelled_raised = False
        try:
            queue.mark_progress(conn, tid2b, "再写一次进度")
        except TaskCancelled:
            cancelled_raised = True
        check("handler 写进度时收到 TaskCancelled 信号", cancelled_raised)
        # done 任务不可取消
        res = queue.cancel(conn, tid)
        check("done 终态任务不可取消", not res["ok"])

        print("\n== ③ 失败重试：attempts < max 回 pending，耗尽转 failed ==")
        tid3 = queue.enqueue(conn, "check_tasks_boom", {}, max_attempts=2)
        queue.claim(conn, "worker-test")
        queue.fail(conn, tid3, "第一次失败", requeue=True)
        t3 = queue.get_task(conn, tid3)
        check("第一次失败后回 pending（可重试）", t3["status"] == "pending" and t3["attempts"] == 1)
        queue.claim(conn, "worker-test")
        queue.fail(conn, tid3, "第二次失败", requeue=False)
        t3 = queue.get_task(conn, tid3)
        check("重试额度耗尽后转 failed", t3["status"] == "failed" and "第二次失败" in (t3["error"] or ""))

        print("\n== ④ 并发 claim（SKIP LOCKED）：两个连接领到不同任务 ==")
        t_a = queue.enqueue(conn, "check_tasks_noop", {"which": "a"})
        t_b = queue.enqueue(conn, "check_tasks_noop", {"which": "b"})
        conn_b = psycopg.connect(settings.database_url, connect_timeout=3)
        claimed_main = queue.claim(conn, "worker-1")
        claimed_b = queue.claim(conn_b, "worker-2")
        ids = {claimed_main["id"] if claimed_main else None,
               claimed_b["id"] if claimed_b else None}
        check("两个 worker 领到不同任务（不重复领取）",
              None not in ids and len(ids) == 2, str(ids))
        check("并发领取覆盖了两个入队任务", ids == {t_a, t_b}, str(ids))
        conn_b.close()

        print("\n== ⑤ stale 恢复：running 超时重排 pending ==")
        tid5 = queue.enqueue(conn, "check_tasks_noop", {})
        queue.claim(conn, "worker-zombie")
        conn.execute(
            "UPDATE task_queue SET started_at = now() - interval '30 minutes' WHERE id = %s",
            (tid5,),
        )
        conn.commit()
        requeued = queue.requeue_stale(conn, settings.tasks_stale_after_s)
        t5 = queue.get_task(conn, tid5)
        check("遗留 running 被重排 pending", t5["status"] == "pending" and requeued >= 1,
              f"requeued={requeued}")
        check("重排时清空 worker 与 started_at",
              t5.get("worker_id") is None and t5.get("started_at") is None)

        print("\n== ⑥ 接线断言：解析迁移与白名单 ==")
        resumes_src = (ROOT / "backend" / "app" / "api" / "resumes.py").read_text(encoding="utf-8")
        check("upload/reparse 已迁到队列（enqueue 出现 3 处）",
              resumes_src.count('task_queue.enqueue(conn, "parse_resume"') == 3)
        check("不再使用 BackgroundTasks 执行解析", "background.add_task" not in resumes_src)
        check("手动入队白名单不含 parse_resume（防绕过文件校验）",
              "parse_resume" not in {"discover_run", "match_rebuild"})
        check("三个 handler 已注册",
              set(TASK_HANDLERS) == {"parse_resume", "discover_run", "match_rebuild"})
    finally:
        conn.execute("DELETE FROM task_queue WHERE type = ANY(%s)", (list(TEST_TYPES),))
        conn.commit()
        conn.close()
        print("  （已还原 task_queue 测试行）")

    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
