"""把已入库的 JD 原文按新规则清洗一遍。

背景：清洗逻辑是后加的，之前入库的岗位 raw_text 里还带着
「收藏 / 立即沟通 / 举报 / 热门职位 / 热门城市 …」这些页面噪声。
这份脏文本既会显示在界面上，也会在重新抽取时被当成 JD 正文。

用法：
    .venv/Scripts/python.exe scripts/clean_existing_jobs.py            # 只清洗，不重新抽取
    .venv/Scripts/python.exe scripts/clean_existing_jobs.py --dry-run  # 只看会改多少

注意：清洗只改原文，不动已抽取的画像。若某条岗位的画像明显是脏文本抽出来的
（典型症状：必备技能里出现「双休」「五险一金」这类词），用 --reingest 重新抽取。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.db.pool import get_pool  # noqa: E402
from app.ingest.clean_jd import clean_jd  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("clean-jobs")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    parser.add_argument(
        "--reingest",
        action="store_true",
        help="清洗后重新抽取画像（会产生 LLM 调用费用）",
    )
    args = parser.parse_args()

    changed = 0
    total = 0
    saved = 0
    targets: list[tuple[int, str]] = []

    with get_pool().connection() as conn:
        rows = conn.execute("SELECT id, raw_text FROM job ORDER BY id").fetchall()
        total = len(rows)
        for job_id, raw in rows:
            cleaned = clean_jd(raw or "")
            if cleaned == (raw or ""):
                continue
            changed += 1
            saved += len(raw or "") - len(cleaned)
            targets.append((job_id, cleaned))
            logger.info("  #%s %d 字 → %d 字", job_id, len(raw or ""), len(cleaned))
            if not args.dry_run:
                conn.execute("UPDATE job SET raw_text = %s WHERE id = %s", (cleaned, job_id))
        if not args.dry_run:
            conn.commit()

    logger.info(
        "共 %d 个岗位，清洗 %d 个，减少 %d 字%s",
        total,
        changed,
        saved,
        "（dry-run，未写库）" if args.dry_run else "",
    )

    if args.reingest and not args.dry_run:
        import httpx

        for job_id, cleaned in targets:
            try:
                resp = httpx.post(
                    "http://127.0.0.1:8000/api/jobs/ingest",
                    json={
                        "source": "boss",
                        "raw_text": cleaned,
                        "detail_level": "full",
                        "url": None,
                        "source_job_id": None,
                    },
                    timeout=180,
                )
                logger.info("  重新抽取 #%s -> http %s", job_id, resp.status_code)
            except Exception as exc:  # noqa: BLE001 - 脚本里逐条容错，不中断
                logger.warning("  重新抽取 #%s 失败：%s", job_id, exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
