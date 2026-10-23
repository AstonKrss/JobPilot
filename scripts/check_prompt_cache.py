"""验证 prompt cache 到底会不会命中。

为什么必须先测这个：
    记忆架构的核心优化是「append-only + 前缀稳定」，收益全部来自服务端 prompt cache。
    如果实测发现带相同前缀的第 2、3 次调用根本不命中缓存，那套设计的前提就不成立，
    得先修前缀对齐（或被服务商限制），而不是继续往上盖。

做法：
    1. 准备一段足够长的 system（超过缓存最小粒度，通常 1024 token）
    2. 连续发三次，system 逐字相同、user 不同
    3. 看第 2、3 次的 tokens_cached 是否明显大于 0

用法：.venv/Scripts/python.exe scripts/check_prompt_cache.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.pool import get_pool  # noqa: E402
from app.llm.deepseek import chat_json  # noqa: E402

# 刻意写长：太短的前缀根本进不了缓存门槛，会得出「缓存无效」的错误结论
SYSTEM = (
    "你是一个求职助理。下面是一段需要反复引用的长期背景资料，请把它当作稳定上下文。\n\n"
    + "\n".join(
        f"第 {i} 条背景：用户在佛山读本科，计算机专业，2028 届，"
        f"熟悉 Java、Spring Boot、MySQL、Redis，做过带权限与流程追溯的后台系统。"
        for i in range(1, 61)
    )
    + "\n\n回答要求：只输出 JSON，不要解释。"
)

SESSION = "cache-probe-stable-prefix"


async def main() -> int:
    pool = get_pool()
    try:
        rows = []
        for i, q in enumerate(["第一个问题：我能投什么岗位？", "第二个问题：我还缺什么技能？", "第三个问题：简历怎么改？"], start=1):
            r = await chat_json(
                SYSTEM,
                q,
                what=f"缓存探针 第{i}次",
                max_tokens=300,
                session_id=SESSION,   # 固定会话，让服务端有机会复用缓存
            )
            rows.append((i, r.tokens_in, r.tokens_cached, r.tokens_out))
            print(
                f"第 {i} 次：输入 {r.tokens_in} token，"
                f"缓存命中 {r.tokens_cached}，输出 {r.tokens_out}"
            )

        pool2 = get_pool()
        with pool2.connection() as conn:
            db_rows = conn.execute(
                """
                SELECT what, tokens_in, tokens_cached, tokens_out, elapsed_ms
                  FROM llm_call
                 WHERE session_id = %s
                 ORDER BY id DESC LIMIT 3
                """,
                (SESSION,),
            ).fetchall()
        print("\n落库记录（最新 3 条）:")
        for r in db_rows:
            print(f"  {r[0]}：in={r[1]} cached={r[2]} out={r[3]} {r[4]}ms")

        # 判定：第 2 或第 3 次应该有缓存命中
        hit = any(r[2] > 0 for r in rows[1:])
        print("\n结论:", "PASS —— 前缀稳定时缓存可命中" if hit else "FAIL —— 未观测到缓存命中")
        if not hit:
            print("可能原因：服务端未开缓存 / 前缀被网关改写 / 会话标识没生效")
        return 0 if hit else 1
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
