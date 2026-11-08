"""DiscoverAgent 主管线：确定性遍历来源 → 抓取 → 解析 → 入库 → 汇总。

无自主循环、无多轮模型决策：来源清单在 discover_source 表里（用户配置），
每轮就是"逐个来源跑一遍既定流程"，出口是硬的——来源跑完或预算耗尽。
"agent 自主性"体现在它在没有人翻页的情况下覆盖 N 个站点，
不在模型即兴发挥上（这与 chat/agent.py 的架构判断一脉相承）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from ..config import settings
from ..db.pool import get_pool
from ..ingest.jd_pipeline import JdIngestError, ingest_jd_text
from .adapters import (
    ADAPTERS,
    Listing,
    detect_adapter,
    find_urgent,
    is_fresh,
    listing_digest_text,
)
from .fetcher import FetchError, Fetcher

logger = logging.getLogger(__name__)

# 同一时刻只允许一轮发现（本地单用户；锁是防手抖双击，不是并发设计）
_round_lock = asyncio.Lock()

# 单源单轮入库上限：防止某一个大站把整轮名额吃光
_PER_SOURCE_CAP = 12


async def _step(notify, label: str) -> None:
    if notify is not None:
        try:
            await notify(label)
        except Exception:  # noqa: BLE001 - 进度推送失败不影响主流程
            pass


def _digest_for(l: Listing, fallback_org: str | None) -> str:
    """digest 原文。org_name 缺失时用来源名兜底（html 适配器的列表页通常不带公司名）。"""
    if not l.org_name and fallback_org:
        l.org_name = fallback_org
    return listing_digest_text(l)


def _load_sources(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, company_key, name, url, adapter, note FROM discover_source"
        " WHERE enabled ORDER BY id"
    ).fetchall()
    return [
        {"id": int(r[0]), "company_key": r[1], "name": r[2],
         "url": r[3], "adapter": r[4] or "auto", "note": r[5]}
        for r in rows
    ]


async def run_round(trigger: str = "manual", notify=None) -> dict:
    """跑一轮发现。返回汇总 dict，全程落 discover_run 表（含失败原因）。

    失败形态：单来源失败记 failures 继续跑下一个；预算耗尽中途收尾；
    全部来源失败时 status=done 但 failures 说明原因——不抛异常，
    让调用方（SSE/对话）总能拿到一份"发生了什么"的完整交代。
    """
    if _round_lock.locked():
        raise RuntimeError("上一轮发现还在跑，等它结束再试")
    async with _round_lock:
        return await _run_round_inner(trigger, notify)


async def _run_round_inner(trigger: str, notify) -> dict:
    started = time.monotonic()
    await _step(notify, "开始主动搜索官网岗位…")

    with get_pool().connection() as conn:
        sources = _load_sources(conn)
        run_row = conn.execute(
            "INSERT INTO discover_run (trigger) VALUES (%s) RETURNING id",
            (trigger,),
        ).fetchone()
        run_id = int(run_row[0])
        conn.commit()

    if not sources:
        summary = {
            "run_id": run_id, "sources_total": 0, "sources_ok": 0,
            "jobs_found": 0, "jobs_new": 0, "urgent_count": 0,
            "new_jobs": [], "per_source": [], "failures": [
                {"source": "（无来源）", "reason": "还没有配置任何官网来源，先在发现页添加招聘页 URL"}
            ],
            "elapsed_s": round(time.monotonic() - started, 1),
        }
        _finish_run(run_id, "done", summary)
        return summary

    summary: dict = {
        "run_id": run_id, "sources_total": len(sources), "sources_ok": 0,
        "jobs_found": 0, "jobs_new": 0, "urgent_count": 0,
        "new_jobs": [], "per_source": [], "failures": [],
    }
    budget_used_note = None

    async with Fetcher(budget=settings.discover_max_fetches) as fetcher:
        for src in sources:
            if fetcher.remaining <= 0:
                budget_used_note = f"抓取预算（{settings.discover_max_fetches} 次）耗尽，剩余来源本轮未扫"
                summary["failures"].append({"source": "（预算）", "reason": budget_used_note})
                break

            adapter = src["adapter"]
            if adapter == "auto":
                adapter = detect_adapter(src["url"])
                with get_pool().connection() as conn:
                    conn.execute("UPDATE discover_source SET adapter = %s WHERE id = %s",
                                 (adapter, src["id"]))
                    conn.commit()

            await _step(notify, f"扫描 {src['name']}（{adapter} 适配器）…")
            try:
                fetch = ADAPTERS.get(adapter)
                if fetch is None:
                    raise FetchError(f"未知适配器 {adapter}（来源配置有误）")
                listings, meta = await fetch(fetcher, src["url"])
            except FetchError as exc:
                summary["failures"].append({"source": src["name"], "reason": exc.message})
                _mark_source(conn := _conn(), src["id"], "failed", exc.message, 0)
                await _step(notify, f"{src['name']} 失败：{exc.message}")
                continue
            except Exception as exc:  # noqa: BLE001 - 解析层的意外错误不许炸掉整轮
                logger.exception("来源 %s 解析异常", src["name"])
                summary["failures"].append({"source": src["name"], "reason": f"解析异常：{exc}"})
                _mark_source(_conn(), src["id"], "failed", f"解析异常：{exc}", 0)
                continue

            # 预算与单源上限双闸：单源吃不光整轮，整轮也不被单站拖死
            capped = listings[:_PER_SOURCE_CAP]
            src_new = 0
            src_found = 0
            for l in capped:
                if fetcher.remaining <= 0 and l.url:
                    break
                digest = _digest_for(l, src["name"])
                try:
                    result = await ingest_jd_text(
                        source="careers",
                        raw_text=digest,
                        url=l.url,
                        source_job_id=l.source_job_id,
                        detail_level="digest",
                        org_name=l.org_name,
                    )
                except JdIngestError as exc:
                    summary["failures"].append(
                        {"source": src["name"], "listing": l.title, "reason": exc.message})
                    continue
                src_found += 1
                urgent = find_urgent(f"{l.title} {digest}")
                fresh = is_fresh(l.published_at)
                summary["jobs_found"] += 1
                if result["is_new"]:
                    src_new += 1
                    summary["jobs_new"] += 1
                    if urgent:
                        summary["urgent_count"] += 1
                    summary["new_jobs"].append({
                        "job_id": result["job_id"],
                        "title": l.title,
                        "company": l.org_name or src["name"],
                        "city": l.city,
                        "url": l.url,
                        "source": "careers",
                        "urgent": urgent,
                        "fresh": fresh,
                        "published_at": l.published_at,
                    })
            summary["per_source"].append({
                "source": src["name"], "adapter": adapter, "found": src_found,
                "new": src_new, "total_listed": len(listings),
                "listed_capped": len(capped) < len(listings),
            })
            _mark_source(_conn(), src["id"], "ok", None, src_found)
            summary["sources_ok"] += 1
            await _step(
                notify,
                f"{src['name']}：抓到 {src_found} 条（新增 {src_new}）"
                + (f"，站内共 {meta.get('total')} 条，本轮取前 {len(capped)}" if meta.get("total") else ""),
            )

    summary["elapsed_s"] = round(time.monotonic() - started, 1)
    if budget_used_note:
        summary["budget_note"] = budget_used_note
    _finish_run(run_id, "done", summary)
    await _step(notify, f"本轮完成：新增 {summary['jobs_new']} 个岗位，用时 {summary['elapsed_s']}s")
    return summary


def _conn():
    """每个小步骤独立取连接：来源之间的状态落库互相独立，一段失败不连坐。"""
    return get_pool().connection()


def _mark_source(cm, source_id: int, status: str, error: str | None, found: int) -> None:
    """来源状态落库。cm 是 pool.connection() 上下文管理器。"""
    with cm as conn:
        conn.execute(
            "UPDATE discover_source SET last_run_at = now(), last_status = %s,"
            " last_error = %s, last_found = %s WHERE id = %s",
            (status, error, found, source_id),
        )
        conn.commit()


def _finish_run(run_id: int, status: str, summary: dict) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE discover_run SET finished_at = now(), status = %s,"
            " sources_checked = %s, jobs_found = %s, jobs_new = %s,"
            " urgent_count = %s, summary = %s, elapsed_ms = %s"
            " WHERE id = %s",
            (
                status,
                summary.get("sources_total", 0),
                summary.get("jobs_found", 0),
                summary.get("jobs_new", 0),
                summary.get("urgent_count", 0),
                json.dumps(summary, ensure_ascii=False, default=str),
                int(summary.get("elapsed_s", 0) * 1000),
                run_id,
            ),
        )
        conn.commit()


def format_reply(summary: dict) -> str:
    """把汇总写成对话回复。模板，不用模型——数字本身就是内容。"""
    total = summary.get("sources_total", 0)
    ok = summary.get("sources_ok", 0)
    new = summary.get("jobs_new", 0)
    urgent = summary.get("urgent_count", 0)
    if total == 0:
        return "还没有配置官网来源。先到「发现」页的「主动搜索」面板添加公司招聘页地址，再让我去搜。"
    lines = [f"主动搜了 {ok}/{total} 个官网招聘源：发现 {new} 个新岗位"
             + (f"，其中 {urgent} 个带急招信号" if urgent else "") + "。"]
    for j in summary.get("new_jobs", [])[:6]:
        mark = "【急招】" if j.get("urgent") else ("【新】" if j.get("fresh") else "")
        city = j.get("city") or "城市未标注"
        lines.append(f"- {mark}{j['title']}（{j.get('company') or ''}，{city}）")
    more = len(summary.get("new_jobs", [])) - 6
    if more > 0:
        lines.append(f"…另有 {more} 个新岗位")
    fails = summary.get("failures") or []
    if fails:
        lines.append("没扫成的来源：" + "；".join(
            f"{f.get('source')}（{f.get('reason')}）" for f in fails[:3]
        ))
    lines.append("每条的「能不能投」判定和雷达页同口径，到「发现」页看卡片即可。")
    return "\n".join(lines)
