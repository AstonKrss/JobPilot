"""主动岗位发现的接口层：来源管理 + 触发一轮（SSE 进度）+ 历史轮次。

SSE 结构照抄 chat.py 的 Queue+notify+哨兵模式（项目里唯一的进度推送范式，
不发明第二种）。来源种子在首次 GET 时幂等写入（照 radar 的 ensure_seed 惯例）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db.pool import get_pool
from ..discover import agent as discover_agent
from ..discover import seed_sources
from ..discover.adapters import detect_adapter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/discover", tags=["discover"])


class SourceCreate(BaseModel):
    name: str
    url: str
    note: str | None = None
    company_key: str | None = None


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    note: str | None = None


@router.get("/sources")
async def list_sources() -> dict:
    with get_pool().connection() as conn:
        seed_sources.ensure_seed_sources(conn)
        rows = conn.execute(
            "SELECT id, company_key, name, url, adapter, enabled, last_run_at,"
            " last_status, last_error, last_found, note"
            " FROM discover_source ORDER BY enabled DESC, id"
        ).fetchall()
    items = [
        {
            "id": int(r[0]), "company_key": r[1], "name": r[2], "url": r[3],
            "adapter": r[4] or "auto", "enabled": bool(r[5]),
            "last_run_at": str(r[6]) if r[6] else None,
            "last_status": r[7], "last_error": r[8],
            "last_found": r[9], "note": r[10],
        }
        for r in rows
    ]
    return {"count": len(items), "items": items}


@router.post("/sources")
async def create_source(payload: SourceCreate) -> dict:
    name = payload.name.strip()
    url = payload.url.strip()
    if not name:
        raise HTTPException(status_code=400, detail="来源名称不能为空")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(status_code=400, detail="URL 不合法：要 http(s) 开头的完整地址")
    adapter = detect_adapter(url)
    with get_pool().connection() as conn:
        dup = conn.execute("SELECT id FROM discover_source WHERE url = %s", (url,)).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail="这个招聘页地址已经在来源列表里了")
        row = conn.execute(
            "INSERT INTO discover_source (company_key, name, url, adapter, note)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (payload.company_key, name, url, adapter, payload.note),
        ).fetchone()
        conn.commit()
    return {"id": int(row[0]), "name": name, "url": url, "adapter": adapter,
            "adapter_note": "auto 来源首抓时按指纹落定" if adapter == "auto" else None}


@router.put("/sources/{source_id}")
async def update_source(source_id: int, payload: SourceUpdate) -> dict:
    sets, args = [], []
    if payload.enabled is not None:
        sets.append("enabled = %s")
        args.append(bool(payload.enabled))
    if payload.note is not None:
        sets.append("note = %s")
        args.append(payload.note)
    if not sets:
        raise HTTPException(status_code=400, detail="没有要更新的字段")
    with get_pool().connection() as conn:
        row = conn.execute(
            f"UPDATE discover_source SET {', '.join(sets)} WHERE id = %s RETURNING id",
            (*args, source_id),
        ).fetchone()
        conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="来源不存在")
    return {"id": source_id, "updated": True}


@router.get("/runs")
async def list_runs(limit: int = 10) -> dict:
    limit = max(1, min(int(limit), 50))
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, started_at, finished_at, status, trigger, sources_checked,"
            " jobs_found, jobs_new, urgent_count, elapsed_ms, error, summary"
            " FROM discover_run ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    items = [
        {
            "id": int(r[0]), "started_at": str(r[1]), "finished_at": str(r[2]) if r[2] else None,
            "status": r[3], "trigger": r[4], "sources_checked": r[5],
            "jobs_found": r[6], "jobs_new": r[7], "urgent_count": r[8],
            "elapsed_ms": r[9], "error": r[10], "summary": r[11],
        }
        for r in rows
    ]
    return {"count": len(items), "items": items}


@router.post("/run")
async def run_discover() -> StreamingResponse:
    """跑一轮主动发现，SSE 推中文进度，最后一条 done 事件带完整汇总。

    上一轮还在跑时返回 409 —— 发现轮是串行语义，并排两轮只会互相拖慢。
    """
    if discover_agent._round_lock.locked():
        raise HTTPException(status_code=409, detail="上一轮发现还在跑，等它结束再试")

    queue: asyncio.Queue = asyncio.Queue()

    async def notify(label: str) -> None:
        await queue.put({"type": "step", "label": label})

    async def run() -> None:
        try:
            summary = await discover_agent.run_round(trigger="manual", notify=notify)
            await queue.put({"type": "done", "summary": summary})
        except Exception as exc:  # noqa: BLE001 - 异常必须变成一条可读事件而不是断流
            logger.exception("发现轮失败")
            await queue.put({"type": "error", "message": f"发现轮失败：{exc}"})
        finally:
            await queue.put(None)

    async def gen():
        task = asyncio.create_task(run())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
