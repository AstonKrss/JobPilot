"""验证 LLM 输出快照（stage_snapshot）真的能命中。

只对幂等调用开启（JD 抽取 / 简历抽取 / 放宽建议）。
这里用一次极小的调用跑两遍：第二遍应该 cached=True、cost=0。

用法：.venv/Scripts/python.exe scripts/check_llm_cache.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.pool import get_pool  # noqa: E402
from app.llm.deepseek import chat_json  # noqa: E402

SYSTEM = '只输出 JSON：{"ok": true}'
USER = "这是缓存连通性测试，请原样返回 {\"ok\": true}"


async def main() -> int:
    pool = get_pool()
    try:
        first = await chat_json(
            SYSTEM, USER, what="缓存测试", max_tokens=50, cache_tag="check_cache"
        )
        second = await chat_json(
            SYSTEM, USER, what="缓存测试", max_tokens=50, cache_tag="check_cache"
        )
    finally:
        pool.close()

    print(f"第一次: cached={first.cached} cost=¥{first.cost_cny:.6f} 输出={first.text[:40]!r}")
    print(f"第二次: cached={second.cached} cost=¥{second.cost_cny:.6f} 输出={second.text[:40]!r}")

    ok = (not first.cached) and second.cached and first.text == second.text
    print("结果:", "PASS（第二次未调用模型）" if ok else "FAIL（没有命中快照）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
