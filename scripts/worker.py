"""可选的独立 worker 进程。

默认部署不需要它：FastAPI lifespan 已在网页服务内启动同一个 worker 循环。
这个脚本是给"headless 定时发现"场景的——网页服务没开、但希望定时去官网
抓新岗位时，把它挂到 Windows 计划任务或常驻终端：

    python scripts/worker.py

注意：与网页服务同时运行时，两者共享 task_queue（SKIP LOCKED 保证不会
重复领任务）；但主动发现在跨进程层面靠 pg advisory lock 互斥，
同一时刻只会有一个进程真正执行。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.db.pool import init_schema, get_pool  # noqa: E402
from app.tasks import runner  # noqa: E402


def main() -> None:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    init_schema()
    print(
        f"JobPilot worker（独立进程）启动：poll {settings.tasks_poll_interval_s}s，"
        f"定时发现 {settings.tasks_scheduled_discover_h}h（0=关）。Ctrl+C 退出。"
    )
    try:
        import asyncio

        asyncio.run(runner.worker_loop())
    except KeyboardInterrupt:
        print("worker 已退出")
    finally:
        get_pool().close()


if __name__ == "__main__":
    main()
