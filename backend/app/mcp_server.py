"""JobPilot MCP server：把求职助手的能力暴露给外部 agent（stdio 协议）。

方向与常见误解相反：这里不是"用 MCP 去访问别人"，而是把本项目已有的
检索 / 匹配 / 发现 / 技能自查能力**作为工具面暴露出去**——Claude Desktop、
Claude Code 等客户端可以像调用它的内置工具一样查询你的岗位库。

形态（v1 的三个刻意取舍）
    stdio 传输：客户端以子进程拉起本文件（scripts/mcp_server.py），
    本地单机不需要端口与鉴权，与网页服务的信任模型一致；两者可同时运行
    （各自持独立的连接池）。
    直接 import 复用：不开 HTTP 中转，业务函数（radar.job_verdict /
    match.list_matches / discover.run_round…）已核实可脱离 FastAPI 上下文
    直调——check 脚本就是这个模式。协议正确性交给官方 SDK。
    只读 + 一个受控写口：不暴露 chat_ask（chat 本身是 agent，嵌套 agent
    不可控且慢）；不暴露画像/简历写入（外部 agent 改画像危险）。
    唯一的写是 discover_run——幂等去重，风险可控。

注册方法（README 同步）
    Claude Code： claude mcp add jobpilot -- python scripts/mcp_server.py
    Claude Desktop： json 配置 mcpServers.serverCommand = python，
    args = scripts/mcp_server.py 的绝对路径。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from fastapi import Query as FastApiQuery  # 仅用于类型说明，不在此处构造
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .config import settings
from .db import repo
from .db.pool import get_pool

logger = logging.getLogger(__name__)

server = MCPServer(
    name="jobpilot",
    title="JobPilot 求职助手",
    description=(
        "本地求职助手的岗位库与判定能力：雷达概览、单岗位判定、匹配结论、"
        "画像、技能缺口展开与学习优先级、主动官网发现。数据在本机 PostgreSQL，"
        "读取不需要网络；discover_run 与 resume_matches 会产生对外请求或模型调用。"
    ),
    version="0.1.0",
)


def _clean_error(exc: Exception) -> str:
    """业务异常翻译成人话。fastapi.HTTPException 的 404/409 语义保留。"""
    if isinstance(exc, HTTPException):
        return f"[{exc.status_code}] {exc.detail}"
    return f"{type(exc).__name__}: {exc}"


def _fail(exc: Exception) -> ToolError:
    """预期内的工具失败必须抛 ToolError：SDK 只把它的消息放进 content 给模型
    读（mcpserver/exceptions.py 的约定）；其他异常一律被吞成
    「Error executing tool <name>」，模型看不到原因。check_mcp 抓到的。"""
    return ToolError(_clean_error(exc))


def _default_resume_id() -> int | None:
    """所有可选 resume_id 参数的缺省解析：主简历。"""
    with get_pool().connection() as conn:
        return repo.get_default_resume_id(conn)


# ---------------------------------------------------------------------------
# 读取类：纯 DB，同步函数（FastMCP 自动丢线程池，不阻塞事件循环）
# ---------------------------------------------------------------------------


@server.tool(
    name="radar_overview",
    description=(
        "岗位雷达概览：先按方向/可达性把进不去的岗位整批摘掉，"
        "再按公司质量、匹配、距离排序。返回总判定（可投几个、方向分布）"
        "与岗位条目。对应界面「发现」页。"
    ),
)
def radar_overview(limit: int = 80, include_blocked: bool = False) -> dict[str, Any]:
    from .api import radar

    try:
        # 直调必须显式传 limit：路由函数的默认值是 fastapi.Query 对象
        return radar.get_radar(resume_id=None, include_blocked=include_blocked, limit=max(1, min(int(limit), 500)))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@server.tool(
    name="job_verdict",
    description=(
        "单个岗位的完整判定：方向/可达性闸门（卡在哪一条、缺什么字段）、"
        "公司质量分与梯队、与主简历的匹配结论、下一步建议。"
        "对应界面「发现」页点开单个岗位。"
    ),
)
def job_verdict(job_id: int, resume_id: int | None = None) -> dict[str, Any]:
    from .api import radar

    try:
        rid = resume_id or _default_resume_id()
        return radar.job_verdict(job_id=job_id, resume_id=rid)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@server.tool(
    name="list_jobs",
    description="岗位库清单（原文 + 已抽取字段摘要）。source 可选：boss/liepin/zhaopin/manual/careers。",
)
def list_jobs(source: str | None = None, limit: int = 50) -> dict[str, Any]:
    try:
        with get_pool().connection() as conn:
            items = repo.list_jobs(conn, limit=max(1, min(int(limit), 200)), source=source or None)
        # 摘要瘦身：JD 原文不出 MCP（大且无判定价值），留画像字段与链接
        slim = [
            {k: it.get(k) for k in (
                "id", "source", "url", "title", "company", "city",
                "job_nature", "edu_level", "exp_min", "detail_level",
                "must_have_skills", "graduate_years",
            ) if it.get(k) is not None}
            for it in items
        ]
        return {"count": len(slim), "items": slim}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@server.tool(
    name="resume_matches",
    description=(
        "给定简历 × 岗位库的匹配结论列表（pass/confirm/reject、分数、"
        "缺口、被否决原因）。resume_id 缺省用主简历；job_id 指定时只看单岗位。"
        "注意：首次匹配可能触发一次语义判定模型调用（有缓存，第二次免费）。"
    ),
)
async def resume_matches(
    resume_id: int | None = None,
    job_id: int | None = None,
    include_rejected: bool = False,
) -> dict[str, Any]:
    from .api import match

    try:
        rid = resume_id or _default_resume_id()
        if not rid:
            return {"error": "还没有可用的简历：先在网页界面上传并解析一份简历"}
        return await match.list_matches(
            resume_id=rid,
            include_rejected=include_rejected,
            accept_relocation=False,
            accept_outsourcing=True,
            accept_internship=False,
            accept_parttime=False,
            target_cities="",
            force=False,
            job_id=job_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@server.tool(
    name="profile_summary",
    description="求职者长期画像概要：目标城市、届别、学历、外包/异地态度等（对话链路自动维护的结构化偏好）。",
)
def profile_summary() -> dict[str, Any]:
    from .memory import profile_repo

    try:
        with get_pool().connection() as conn:
            return profile_repo.get_flat_profile(conn)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# ---------------------------------------------------------------------------
# 技能自查类
# ---------------------------------------------------------------------------


@server.tool(
    name="skill_gap_expand",
    description=(
        "把一个岗位的必备技能名词展开为可自查的具体能力问句"
        "（如 Redis → 「能讲清缓存穿透和雪崩的区别及应对」）。"
        "注意：未缓存的技能会触发一次模型调用（结果跨岗位复用）。"
    ),
)
async def skill_gap_expand(job_id: int, resume_id: int | None = None) -> dict[str, Any]:
    from .gap import assess, expand

    try:
        rid = resume_id or _default_resume_id()
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT title, campus_type, must_have_skills FROM job_profile"
                " WHERE job_id = %s AND is_current",
                (job_id,),
            ).fetchone()
        if row is None:
            raise ToolError(f"[404] 岗位 {job_id} 不存在或未解析")
        title, campus_type, must = row[0], row[1], row[2] or []
        result = await expand.expand_for_job([str(s) for s in must], title or "", campus_type)
        existing = None
        with get_pool().connection() as conn:
            existing = assess.load_by_resume(conn, rid) if rid else {}
        for s in result["skills"]:
            for item in s["items"]:
                info = existing.get(assess.item_hash(item["text"]))
                item["status"] = info["status"] if info else None
        return {"job_id": job_id, "title": title, **result}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@server.tool(
    name="skill_gap_priority",
    description=(
        "跨岗位学习优先级：被多个在架岗位同时要求、而自评还没掌握的具体能力，"
        "按岗位数排序——回答「我先学什么」。"
    ),
)
def skill_gap_priority(limit: int = 20) -> dict[str, Any]:
    from .gap import assess

    try:
        rid = _default_resume_id()
        if not rid:
            return {"count": 0, "items": [], "note": "还没有可用的简历"}
        with get_pool().connection() as conn:
            items = assess.priority(conn, rid, max(1, min(int(limit), 60)))
        return {
            "resume_id": rid,
            "count": len(items),
            "items": items,
            "note": "按要求的在架岗位数排序；自评只存在本机。",
        }
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


# ---------------------------------------------------------------------------
# 受控写口
# ---------------------------------------------------------------------------


@server.tool(
    name="discover_run",
    description=(
        "触发一轮主动发现：去配置好的公司官网招聘页抓新岗位"
        "（限速礼貌抓取、每轮 ≤50 次请求预算）。长任务，可能需要 1-2 分钟；"
        "返回新增岗位与每个来源的成败原因。同一时刻只允许一轮。"
    ),
)
async def discover_run() -> dict[str, Any]:
    from .discover import agent as discover_agent

    try:
        return await discover_agent.run_round(trigger="mcp", notify=None)
    except RuntimeError as exc:
        # 模块级锁：上一轮未结束时如实相告（409 语义）
        raise ToolError(f"[409] {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logger.info("JobPilot MCP server 启动（stdio）")
    server.run(transport="stdio")
