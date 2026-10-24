"""FastAPI 入口。

启动时做两件事：建表（幂等）与配置检查。
DEEPSEEK_API_KEY 缺失不阻止启动（理由见 config.py），但会在日志里明确警告，
并在调用模型的接口上返回可读错误，而不是让请求以 401 的形式莫名其妙失败。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api import (
    advice,
    workbench,
    memory,
    skills,
    applications,
    chat,
    chat_logs,
    jobs,
    match,
    profile,
    reply,
    resumes,
    review,
    tailor,
)
from .config import settings
from .db.pool import get_pool, init_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_schema()
    if not settings.llm_api_key:
        logger.warning(
            "DEEPSEEK_API_KEY 未配置：PDF 解析与库入库可以正常进行，"
            "但字段抽取会在调用模型时返回明确错误。请在 .env 中填入密钥"
        )
    yield
    get_pool().close()


app = FastAPI(title="求职 Agent", version="0.1.0", lifespan=lifespan)

# 浏览器扩展的请求理论上经 service worker 走 host_permissions，不受 CORS 限制；
# 但浏览器行为与配置都可能变化，这里放开本地与扩展来源作为保险。
# 服务只监听 127.0.0.1，外部访问不到，因此不存在开放公网的风险。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"(chrome-extension://.*|moz-extension://.*|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?)",
    allow_methods=["*"],
    allow_headers=["*"],
)

# 本地开发工具：界面文件每次都要拿到最新版。
# 默认 StaticFiles 不带 Cache-Control，浏览器会用启发式缓存（按 Last-Modified 估算有效期），
# 结果就是"改了代码、刷新页面没变化" —— 这一条踩过不止一次。
@app.middleware("http")
async def no_cache_for_ui(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


app.include_router(resumes.router)
app.include_router(jobs.router)
app.include_router(match.router)
app.include_router(review.router)
app.include_router(chat.router)
app.include_router(tailor.router)
app.include_router(reply.router)
app.include_router(applications.router)
app.include_router(chat_logs.router)
app.include_router(profile.router)
app.include_router(skills.router)
app.include_router(memory.router)
app.include_router(workbench.router)
app.include_router(advice.router)

# 静态界面必须挂载在所有路由之后：mount("/") 会匹配一切路径，
# 先挂就会把 /api/* 也吞掉
@app.get("/health")
async def health() -> dict:
    """健康检查。

    分开展示 db 与 llm 状态：数据库挂了是阻断性问题，模型没配只影响抽取，
    两者混在一个 ok 字段里会让人误判严重程度。
    """
    db_ok = True
    try:
        with get_pool().connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # 健康检查本身不能抛异常，否则探活永远是 500 而看不到原因
        logger.error("健康检查：数据库不可用 %s", exc)
        db_ok = False
    return {
        "ok": db_ok,
        "db": db_ok,
        "llm_configured": bool(settings.llm_api_key),
    }


# 静态界面必须挂载在所有路由之后：mount("/") 会匹配一切路径，
# 先挂就会把 /api/* 甚至 /health 也吞掉
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
