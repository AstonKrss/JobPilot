"""任务注册表：type → async handler(task, progress) -> result dict。

handler 约定
    async def handler(task: dict, progress) -> dict
    progress(text) 是异步回调：写 task_queue.progress 并做协作式取消检查
    （抛 TaskCancelled 时由 runner 落 cancelled 终态）。
    返回的 dict 会写进 task_queue.result。

parse_resume 的实现留在 api/resumes.py（它和上传/失败文案是同一上下文），
这里延迟 import —— 队列模块不该反向依赖 API 层的其余路由。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], Awaitable[None]]


async def _parse_resume_handler(task: dict, progress: ProgressFn) -> dict:
    """简历解析（含扫描件 OCR，1-2 分钟）。payload: {resume_id, path}"""
    from ..api.resumes import _parse_resume

    resume_id = int(task["payload"].get("resume_id") or 0)
    path = task["payload"].get("path") or ""
    if not resume_id or not path:
        raise ValueError(f"payload 缺少 resume_id/path：{task['payload']}")
    await progress(f"解析简历 #{resume_id}（{Path(path).suffix.lower()}）")
    # _parse_resume 自带完整的状态机（parsing→done/failed + 失败文案落库），
    # 这里只负责把它跑完并回填队列结果
    await _parse_resume(resume_id, Path(path))
    from ..db.pool import get_pool

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT parse_status, parse_error FROM resume WHERE id = %s", (resume_id,)
        ).fetchone()
    status = row[0] if row else "unknown"
    if status == "failed":
        raise RuntimeError(row[1] or "解析失败（原因见 resume.parse_error）")
    return {"resume_id": resume_id, "parse_status": status}


async def _discover_run_handler(task: dict, progress: ProgressFn) -> dict:
    """主动发现一轮：去配置好的官网招聘页抓新岗位（可能 1-2 分钟）。"""
    from ..discover import agent as discover_agent

    async def notify(label: str) -> None:
        await progress(label)

    return await discover_agent.run_round(trigger="queue", notify=notify)


async def _match_rebuild_handler(task: dict, progress: ProgressFn) -> dict:
    """批量匹配重建：force 跳过缓存全量重算并预热缓存。payload: {resume_id}"""
    from ..api import match

    resume_id = int(task["payload"].get("resume_id") or 0)
    if not resume_id:
        raise ValueError(f"payload 缺少 resume_id：{task['payload']}")
    await progress(f"全量重算简历 #{resume_id} 的匹配（跳过缓存）…")
    started_label = "匹配重建（跳过缓存）"
    result = await match.list_matches(
        resume_id=resume_id,
        include_rejected=True,
        accept_relocation=False,
        accept_outsourcing=True,
        accept_internship=False,
        accept_parttime=False,
        target_cities="",
        force=True,
        job_id=None,
    )
    await progress(f"匹配完成：{result.get('counts') or {}}")
    return {
        "resume_id": resume_id,
        "total_jobs": result.get("total_jobs"),
        "counts": result.get("counts") or {},
        "cached": result.get("cached"),
        "label": started_label,
    }


# type → handler。加新任务类型在这里登记；API 手动入队只放行 USER_ENQUEUEABLE 里的类型
TASK_HANDLERS: dict[str, Callable[[dict, ProgressFn], Awaitable[dict]]] = {
    "parse_resume": _parse_resume_handler,
    "discover_run": _discover_run_handler,
    "match_rebuild": _match_rebuild_handler,
}

# 手动入队白名单：parse_resume 由上传/重解析流程自动入队，不开放手动塞
USER_ENQUEUEABLE = {"discover_run", "match_rebuild"}
