"""给没有标题的岗位补 title。

自动入库的 Boss 岗位里，一部分原文以「工作内容：」「岗位职责:」或公司介绍开头，
模型抽取时按"依据原文"的纪律填了 null，导致界面显示「未命名岗位」。
这里用一次批量模型调用给它们各起一个 ≤12 字的岗位名（依据职责内容概括），
并把新规则同步进抽取 prompt（jobs.py 2a 条），以后入库的不会再缺。

用法：.venv/Scripts/python.exe scripts/backfill_titles.py [--dry-run]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.db.pool import get_pool  # noqa: E402
from app.llm.deepseek import chat_json, parse_json_object  # noqa: E402


async def main(dry_run: bool) -> int:
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT j.id, left(j.raw_text, 200)
              FROM job j
              LEFT JOIN job_profile p ON p.job_id = j.id AND p.is_current
             WHERE p.id IS NOT NULL AND (p.title IS NULL OR btrim(p.title) = '')
             ORDER BY j.id
            """
        ).fetchall()
    if not rows:
        print("没有缺标题的岗位")
        return 0
    print(f"{len(rows)} 个岗位缺标题")

    listing = "\n".join(f"{i}. job_id={r[0]}\n   原文开头：{(r[1] or '').replace(chr(10), ' / ')[:150]}" for i, r in enumerate(rows, 1))
    result = await chat_json(
        "你是岗位命名器。给你若干岗位的 JD 原文开头，为每个岗位起一个不超过 12 字的岗位名。"
        "依据职责内容概括（如：后端开发工程师、小程序维护开发、桌面应用开发），"
        "不要照抄公司名，不要加书名号。\n"
        '只输出 JSON 对象：{"titles": {"<序号>": "<岗位名>", ...}}，序号与输入一致，不要解释。',
        listing,
        what="批量补岗位标题",
        max_tokens=1500,
    )
    payload = parse_json_object(result.text, what="批量补岗位标题")
    titles = payload.get("titles") or {}

    updated = 0
    with get_pool().connection() as conn:
        for i, row in enumerate(rows, 1):
            # 模型可能拿序号也可能拿 job_id 当键，两种都认
            title = str(titles.get(str(row[0])) or titles.get(str(i)) or "").strip()
            if not title:
                print(f"  job {row[0]}: 模型没给标题，跳过")
                continue
            title = title[:24]
            print(f"  job {row[0]}: {title}")
            if not dry_run:
                conn.execute(
                    "UPDATE job_profile SET title = %s WHERE job_id = %s AND is_current",
                    (title, row[0]),
                )
                updated += 1
        if not dry_run:
            conn.commit()
    print(f"完成：{'(dry-run) ' if dry_run else ''}{updated}/{len(rows)} 个已更新，成本 ¥{result.cost_cny:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--dry-run" in sys.argv)))
