"""验证向量缓存的持久化。

两级缓存：进程内 dict → embedding_cache 表。
这个脚本验证：编码过的文本确实写进了库，而且换一个进程也能直接取到。

用法：.venv/Scripts/python.exe scripts/check_embed_cache.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.pool import get_pool  # noqa: E402
from app.match import semantic  # noqa: E402

TEXTS = ["分布式系统设计", "高并发交易链路", "Kubernetes 容器编排", "机器学习模型部署"]


def count_rows() -> int:
    with get_pool().connection() as conn:
        return conn.execute("SELECT count(*) FROM embedding_cache").fetchone()[0]


def drop_test_keys() -> None:
    """先清掉这几条，否则第二次跑时"新增 0 条"会让断言误判。"""
    keys = [semantic._embed_key(t) for t in TEXTS]
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM embedding_cache WHERE key = ANY(%s)", (keys,))
        conn.commit()


def main() -> int:
    pool = get_pool()
    try:
        if not semantic.available():
            print("语义模型不可用（未下载或依赖缺失），跳过测试")
            return 2

        drop_test_keys()
        before = count_rows()
        print(f"库中已有向量: {before} 条（已清掉本次要测的 {len(TEXTS)} 条）")

        # 清内存，模拟"刚重启服务"
        semantic._cache.clear()
        v1 = semantic.embed(TEXTS)
        after_first = count_rows()
        print(f"第一次编码后: {after_first} 条（新增 {after_first - before}）")

        # 再清一次内存，这次必须靠库命中
        semantic._cache.clear()
        v2 = semantic.embed(TEXTS)
        print("第二次编码（内存已清空，应靠库命中）")

        if v1 is None or v2 is None:
            print("FAIL: 编码返回空")
            return 1

        same = all(
            abs(a - b) < 1e-6
            for va, vb in zip(v1, v2)
            for a, b in zip(va, vb)
        )
        added = after_first - before
        ok = same and added == len(TEXTS)
        print(f"两次结果一致: {same} | 新增写入: {added}（应为 {len(TEXTS)}）")
        print("结果:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
