"""情节召回打分的离线回归。

背景（docs/agent-eval-diagnosis-2026-10-29.md 4.5）：
    旧权重 0.55/0.25/0.20 有两个结构性偏差 ——
    1) 正反馈：hit_count 只增不减、5 次饱和后频次项是永久加分。
       实测一条 cos=0.764 的记忆排在 cos=0.494 之后（后者 57 次命中）；
    2) 无下限：无关 query 也稳定返回 3 条，最高 sim 只有 0.29。

修法：sim < 0.35 直接不召回；频次乘上「距上次召回」的衰减（60 天半衰期）；
权重重配为 0.70 相似 / 0.20 时间 / 0.10 频次。

这个脚本只测纯函数 _score_one：固定时钟注入，不需要数据库、
不需要向量模型、不需要网络。第二部分把 README 里那条实测翻车案例
（0.764 输给 0.494）做成断言 —— 旧公式必须复现翻车，新公式必须翻正，
两边任一不成立都说明对问题的理解有错。

用法：
    .venv/Scripts/python.exe scripts/check_episode_recall.py
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}{(' — ' + detail) if detail else ''}")


def old_formula(sim: float, days_old: float, hit_count: int) -> float:
    """旧公式（0.55/0.25/0.20，频次无衰减）。只在本脚本里用于对照复现。"""
    return (
        0.55 * sim
        + 0.25 * math.exp(-days_old / 30.0)
        + 0.20 * min(hit_count / 5.0, 1.0)
    )


def main() -> int:
    from app.memory import episode as ep

    # 固定时钟：所有衰减相对它算，测试结果不随日期漂移
    now = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)

    def ago(days: float) -> datetime:
        return now - timedelta(days=days)

    print("== 阈值：无关 query 不再注入 ==")
    for sim in (0.29, 0.31, 0.20):
        score, keep = ep._score_one(sim, ago(1), 0, None, now)
        check(f"sim={sim} 不召回", not keep, f"score={score}")
    _, keep = ep._score_one(ep.MIN_SIM, ago(1), 0, None, now)
    check(f"边界：sim={ep.MIN_SIM} 恰好保留（判据是 < 阈值，不是 <=）", keep)

    print("\n== 正反馈：README 的实测翻车案例必须翻正 ==")
    # 记忆 A：cos=0.494，被召回 57 次，5 天前创建/最后命中（高频复用的旧话题）
    # 记忆 B：cos=0.764，被召回 17 次，45 天前创建、30 天前最后命中
    # README 记录的实测结果：A 第 1，B 排第 3 —— 相关的记忆输给了霸榜的
    a_days, b_days, b_last_hit = 5.0, 45.0, 30.0
    a_old = old_formula(0.494, a_days, 57)
    b_old = old_formula(0.764, b_days, 17)
    check("旧公式复现翻车（0.494 排在 0.764 前面）", a_old > b_old,
          f"旧分 A={a_old:.4f} B={b_old:.4f}，若此处不成立说明对原问题的记录有误")

    a_score, _ = ep._score_one(0.494, ago(a_days), 57, ago(a_days), now)
    b_score, _ = ep._score_one(0.764, ago(b_days), 17, ago(b_last_hit), now)
    check("新公式翻正：0.764 的排在 0.494 的前面", b_score > a_score,
          f"新分 A={a_score:.4f} B={b_score:.4f}")
    print(f"       旧公式：A={a_old:.4f} > B={b_old:.4f}（翻车）")
    print(f"       新公式：A={a_score:.4f} < B={b_score:.4f}（修复）")

    print("\n== 频次衰减：霸榜优势有界且会过期 ==")
    # 同相似度同年龄，57 次命中 vs 0 次：旧公式永久领先 0.20；
    # 新公式在最后命中过去 90 天后只剩 0.1×exp(-1.5)≈0.022
    base, _ = ep._score_one(0.50, ago(10), 0, None, now)
    hot, _ = ep._score_one(0.50, ago(10), 57, ago(90), now)
    gap = hot - base
    check(f"冷置 90 天的霸榜记忆优势 < 0.03（实测 {gap:.4f}）", 0 <= gap < 0.03)
    hot_fresh, _ = ep._score_one(0.50, ago(10), 57, ago(1), now)
    check("最近仍在被用的记忆保留大部分频次加成（不搞一刀切清零）",
          hot_fresh - base > 0.08, f"gap={hot_fresh - base:.4f}")

    print("\n== 边界与回退 ==")
    score, keep = ep._score_one(0.60, ago(10), 10, None, now)
    expected_freq = min(10 / 5.0, 1.0) * math.exp(-10 / 60.0)
    check("last_hit_at 缺失时回退 created_at",
          abs(score - (0.7 * 0.60 + 0.2 * math.exp(-10 / 30.0) + 0.1 * expected_freq)) < 1e-9)
    score, keep = ep._score_one(0.60, ago(10), 0, ago(1), now)
    check("hit_count=0 时频次项为 0（不看 last_hit_at）",
          abs(score - (0.7 * 0.60 + 0.2 * math.exp(-10 / 30.0))) < 1e-9)
    _, keep = ep._score_one(0.60, now + timedelta(days=1), 0, None, now)
    check("未来时间不奖励（衰减按 1.0）", keep)
    _, keep = ep._score_one(0.60, None, 0, None, now)
    check("created_at 缺失按不过期处理", keep)

    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
