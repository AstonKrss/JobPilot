"""数据库连接池与建表。

连接池刻意开得很小（1~4）：本地单用户工具，并发上限就是「一个上传请求 + 一个后台任务」。
开大只会占用 Postgres 的连接数上限而不带来任何收益。
"""

from __future__ import annotations

import logging
from pathlib import Path

from psycopg_pool import ConnectionPool

from ..config import settings

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # open=True 立即建连，配置错时当场报错，而不是等到第一个请求才炸
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=4,
            open=True,
            kwargs={"autocommit": False},
        )
        logger.info("数据库连接池已建立: %s", _masked_dsn(settings.database_url))
    return _pool


def _masked_dsn(dsn: str) -> str:
    """日志里不打印口令。"""
    if "@" not in dsn:
        return dsn
    head, tail = dsn.split("@", 1)
    if ":" in head:
        head = head.rsplit(":", 1)[0] + ":***"
    return f"{head}@{tail}"


def init_schema() -> None:
    """建表。幂等（schema.sql 全部 IF NOT EXISTS），可重复执行。"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_pool().connection() as conn:
        conn.execute(sql)
        conn.commit()
    logger.info("表结构已就绪")
