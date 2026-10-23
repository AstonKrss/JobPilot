"""估算各环节的上下文占用，回答「1M 上下文够不够、什么在吃预算」。

为什么要分「判定视图」和「素材视图」
    匹配只需要学历/年限/城市/技能名这些判定依据（很小），
    改写才需要经历详情和高光（很大）。混成一个整体塞进上下文，
    等于每次都付素材视图的 token，而这些 token 在匹配时毫无作用。

token 估算说明
    这里用「字符数 × 0.7」粗估中文 token（DeepSeek 未公开 tokenizer）。
    绝对值会有偏差，但用于比较不同方案的量级关系足够。

用法：.venv/Scripts/python.exe scripts/estimate_context.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import match_repo  # noqa: E402
from app.db.pool import get_pool  # noqa: E402

CHARS_PER_TOKEN = 0.7
CONTEXT_LIMIT = 1_000_000


def est(obj) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return int(len(text) * CHARS_PER_TOKEN)


def fmt(n: int) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def bar(n: int, total: int = CONTEXT_LIMIT, width: int = 28) -> str:
    filled = min(int(n / total * width), width)
    return "█" * filled + "·" * (width - filled)


def main() -> int:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, file_name FROM resume ORDER BY id LIMIT 1"
        ).fetchone()
        if rows is None:
            print("库里没有简历，先上传一份")
            return 1
        resume_id = int(rows[0])
        profile = match_repo.get_current_profile(conn, resume_id)
        jobs = match_repo.list_current_job_profiles(conn)

    # 简历画像拆两个视图
    judgment_view = {
        "basic": profile["basic"],
        "skills": [s.get("name") for s in profile["skills"]],
    }
    material_view = {
        "experiences": profile["experiences"],
        "projects": profile["projects"],
    }
    t_judge = est(judgment_view)
    t_material = est(material_view)
    t_full = t_judge + t_material

    print("=" * 66)
    print("简历画像的两个视图")
    print("=" * 66)
    print(f"  判定视图（basic + 技能名）      {fmt(t_judge):>7} token   {bar(t_judge)}")
    print(f"  素材视图（经历详情 + 项目）      {fmt(t_material):>7} token   {bar(t_material)}")
    print(f"  合并成一份                     {fmt(t_full):>7} token")
    print(f"  → 匹配阶段只需要判定视图，能省 {fmt(t_material)} token/次")

    # 岗位画像拆两个视图
    job_judge = {
        "title": jobs[0].get("title") if jobs else "",
        "city": jobs[0].get("city") if jobs else "",
        "edu_level": jobs[0].get("edu_level") if jobs else "",
        "exp_min": jobs[0].get("exp_min") if jobs else None,
        "must_have_skills": jobs[0].get("must_have_skills") if jobs else [],
    }
    t_job_judge = est(job_judge)
    t_job_full = est(jobs[0]) if jobs else 0

    print()
    print("=" * 66)
    print("岗位画像的两个视图")
    print("=" * 66)
    print(f"  判定视图（硬约束字段 + 技能名）  {fmt(t_job_judge):>7} token")
    print(f"  完整视图（含 evidence 等全部）    {fmt(t_job_full):>7} token")

    # 不同规模的上下文占用
    print()
    print("=" * 66)
    print("不同做法的上下文占用（按岗位数放大）")
    print("=" * 66)
    scenarios = [
        ("A 全量岗位 + 完整画像（最差）", t_full + t_job_full * 1000),
        ("B 全量岗位 + 判定视图", t_judge + t_job_judge * 1000),
        ("C 硬约束过滤后 50 个 + 判定视图", t_judge + t_job_judge * 50),
        ("D 精选 10 个 + 完整 JD（用于写解释）", t_full + t_job_full * 10),
        ("E 单岗位深聊（画像素材 + 1 个 JD）", t_full + t_job_full),
    ]
    for name, tokens in scenarios:
        pct = tokens / CONTEXT_LIMIT * 100
        print(f"  {name:34s} {fmt(tokens):>7}  {bar(tokens)}  {pct:5.1f}%")
        if tokens > CONTEXT_LIMIT:
            print(f"  {'':34s} ↑ 已超出 1M 上限")

    # 多轮对话的线性增长
    print()
    print("=" * 66)
    print("多轮对话：不做管理会线性增长")
    print("=" * 66)
    per_turn = t_judge + t_job_full + 300  # 画像 + 一个 JD + 用户消息
    for turns in (10, 50, 100, 300):
        total = per_turn * turns
        print(f"  {turns:>3} 轮，每轮带全量历史  {fmt(total):>8}  {bar(total)}  {total / CONTEXT_LIMIT * 100:5.1f}%")
    print(f"  （每轮约 {fmt(per_turn)} token：判定视图 + 一个 JD + 消息）")

    print()
    print("结论：匹配链路靠「在数据库里过滤」把上下文压成 O(1)，不会爆；")
    print("      真正会爆的是「每轮都重放全部历史」的对话，需要滑动窗口 + 摘要。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
