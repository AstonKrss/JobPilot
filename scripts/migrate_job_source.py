"""补齐 job.source 的 zhaopin 取值，并修正历史误标。

背景
    job 表的 CHECK 约束最初只有 boss / liepin / manual，漏了 zhaopin。
    扩展在智联页面入库时传 source='zhaopin' 会被约束拒绝；而历史数据里
    有一批智联岗位被标成了 boss（URL 明明是 zhaopin.com），
    界面上就显示成「BOSS直聘」——用户一眼就看出来不对。

本脚本做三件事，可重复执行：
    1. 放宽 CHECK 约束，加入 zhaopin
    2. 按 URL 域名回填错误的 source
    3. 打印仍然缺 source_job_id 的智联岗位（列表页抓的，去重靠不住）

用法：.venv/Scripts/python.exe scripts/migrate_job_source.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db.pool import get_pool  # noqa: E402

CONSTRAINT_SQL = """
ALTER TABLE job DROP CONSTRAINT IF EXISTS job_source_check;
ALTER TABLE job ADD CONSTRAINT job_source_check
    CHECK (source IN ('boss', 'liepin', 'zhaopin', 'manual'));
"""

# 按域名回填。域名判据比"当初传了什么"可靠：URL 是不会撒谎的那一列
BACKFILL = [
    ("https://www.zhaopin.com/%", "zhaopin"),
    ("https://sou.zhaopin.com/%", "zhaopin"),
    ("https://jobs.zhaopin.com/%", "zhaopin"),
    ("http://www.zhaopin.com/%", "zhaopin"),
    ("https://www.liepin.com/%", "liepin"),
    ("https://www.zhipin.com/%", "boss"),
]


def main() -> int:
    pool = get_pool()
    # 脚本跑完要显式关池，否则 psycopg_pool 的 worker 线程卡住，
    # 退出时会刷一屏 "couldn't stop thread" 警告
    try:
        with pool.connection() as conn:
            conn.execute(CONSTRAINT_SQL)
            print("约束已更新：source 允许 boss / liepin / zhaopin / manual")

            total_fixed = 0
            for pattern, source in BACKFILL:
                cur = conn.execute(
                    "UPDATE job SET source = %s WHERE url LIKE %s AND source <> %s",
                    (source, pattern, source),
                )
                if cur.rowcount:
                    total_fixed += cur.rowcount
                    print(f"  回填 {cur.rowcount} 条 → {source}（{pattern}）")

            conn.commit()
            print(f"共修正 {total_fixed} 条来源")

            rows = conn.execute(
                """
                SELECT j.id, p.title, j.source, j.url
                  FROM job j
                  LEFT JOIN job_profile p ON p.job_id = j.id AND p.is_current
                 WHERE j.source = 'zhaopin' AND j.source_job_id IS NULL
                 ORDER BY j.id DESC
                """
            ).fetchall()
            if rows:
                print(f"\n以下 {len(rows)} 条智联岗位没有岗位 ID（列表页抓的，去重不可靠）:")
                for r in rows:
                    print(f"  #{r[0]} {r[1]} ← {r[3]}")
                print("  处理办法：在智联岗位详情页重新抓一次，会自动带上正确 ID")
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
