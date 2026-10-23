"""评测集跑分。

用途说明（重要，避免误读结果）
    这是**规则级回归**，不是统计意义上的质量验证。样本是依据已确认的决策手工构造的，
    所以「跑分 100%」只说明实现与决策一致、且行为被锁住了，不代表对真实简历-岗位对的
    判断准确率。真正的验证需要拿真实数据人工标注，那是下一阶段的事。

    它的核心价值是：任何一次规则改动，都能立刻看出有没有把别的分支改坏。

用法：
    .venv/Scripts/python.exe eval/run_eval.py
    .venv/Scripts/python.exe eval/run_eval.py --sweep   # 扫描必备技能阈值
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.match.engine import match_one  # noqa: E402

CASES_PATH = ROOT / "eval" / "cases.jsonl"


def load_cases() -> list[dict]:
    cases = []
    with CASES_PATH.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"cases.jsonl 第 {line_no} 行不是合法 JSON：{exc}") from exc
    return cases


def check_case(case: dict, extra_prefs: dict | None = None) -> tuple[bool, str]:
    """跑一条用例，返回 (是否通过, 问题描述)。"""
    prefs = dict(case.get("prefs") or {})
    if extra_prefs:
        prefs.update(extra_prefs)

    result = match_one(case["resume"], case["job"], prefs)
    problems: list[str] = []

    actual = result.decision.value
    if actual != case["expect"]:
        problems.append(f"决策 {actual} ≠ 期望 {case['expect']}")

    expected_rule = case.get("expect_rule")
    blocked = [c.rule for c in result.checks if c.blocked]
    if expected_rule and expected_rule not in blocked:
        problems.append(f"否决规则 {blocked or '无'} 未包含 {expected_rule}")

    if "expect_rules_count" in case and len(blocked) != case["expect_rules_count"]:
        problems.append(f"否决条数 {len(blocked)} ≠ {case['expect_rules_count']}")

    score = result.score
    if score is not None:
        if "min_score" in case and score < case["min_score"]:
            problems.append(f"分数 {score} < {case['min_score']}")
        if "max_score" in case and score > case["max_score"]:
            problems.append(f"分数 {score} > {case['max_score']}")

    return (not problems), "；".join(problems)


def summarize(cases: list[dict]) -> int:
    by_cat: dict[str, list[bool]] = defaultdict(list)
    failures: list[tuple[dict, str]] = []
    known_gap: list[tuple[dict, str]] = []
    matrix: Counter[tuple[str, str]] = Counter()

    for case in cases:
        ok, problem = check_case(case)
        result = match_one(case["resume"], case["job"], case.get("prefs") or {})
        matrix[(case["expect"], result.decision.value)] += 1

        if case.get("known_gap"):
            if not ok:
                known_gap.append((case, problem))
            continue

        by_cat[case.get("cat", "未分类")].append(ok)
        if not ok:
            failures.append((case, problem))

    total = sum(len(v) for v in by_cat.values())
    passed = sum(sum(v) for v in by_cat.values())

    print("=== 按类别 ===")
    for cat in sorted(by_cat):
        ok = sum(by_cat[cat])
        flag = "OK  " if ok == len(by_cat[cat]) else "FAIL"
        print(f"  [{flag}] {cat:6s} {ok}/{len(by_cat[cat])}")

    print(f"\n=== 汇总 ===\n  样本 {len(cases)} | 主评测 {total} | 通过 {passed} | 失败 {len(failures)}")
    if total:
        print(f"  主准确率 {passed}/{total} = {passed / total:.1%}")
    if known_gap:
        print(f"  已知缺陷 {len(known_gap)} 条（不计入主准确率）")

    print("\n=== 混淆矩阵（期望 → 实际）===")
    for (expect, actual), count in sorted(matrix.items()):
        mark = "" if expect == actual else "  ← 不一致"
        print(f"  {expect:8s} → {actual:8s} {count}{mark}")

    if failures:
        print("\n=== 失败明细 ===")
        for case, problem in failures:
            print(f"  [{case['id']}] {case.get('note', '')}")
            print(f"        {problem}")
            result = match_one(case["resume"], case["job"], case.get("prefs") or {})
            print(f"        实际理由：{result.reasons}")

    if known_gap:
        print("\n=== 已知缺陷（不是实现 bug，需要补别名表才能解决）===")
        for case, problem in known_gap:
            print(f"  [{case['id']}] {case.get('note', '')}")

    return 0 if not failures else 1


def sweep(cases: list[dict]) -> None:
    """扫描必备技能阈值，看准确率是否对取值敏感。

    注意：阈值只影响 must_skills 一条规则的否决线，所以扫描结果只反映这一条的松紧，
    不能据此断言其他权重也选对了。
    """
    print("=== 必备技能阈值扫描 ===")
    print("阈值   通过/总数   准确率   失败用例")
    results: list[tuple[float, float, list[str]]] = []
    for ratio in [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
        passed = 0
        total = 0
        failed_ids: list[str] = []
        for case in cases:
            if case.get("known_gap"):
                continue
            total += 1
            ok, _ = check_case(case, {"min_must_skill_ratio": ratio})
            if ok:
                passed += 1
            else:
                failed_ids.append(case["id"])
        rate = passed / total if total else 0
        results.append((ratio, rate, failed_ids))
        print(f"{ratio:<6.1f} {passed:>3}/{total:<5} {rate:>6.1%}   {','.join(failed_ids) or '-'}")

    best_rate = max(r[1] for r in results)
    plateau = [r[0] for r in results if r[1] == best_rate]
    if plateau:
        print(f"\n平台区间 [{min(plateau):.1f}, {max(plateau):.1f}] 均达到 {best_rate:.1%}")
        # 刻意不建议「取扫描最优点」：单点最优很可能是标注样本的偶然，
        # 取平台中点、或取区间内有业务语义的值（如 0.5 = 过半命中）更稳
        print("建议取平台内、且有业务语义的值（0.5 = 过半命中），不要取单点最优。")
        print("根本限制：本评测集是按规则手工构造的，扫描只能验证一致性，不能证明参数正确。")


def main() -> int:
    parser = argparse.ArgumentParser(description="匹配规则评测")
    parser.add_argument("--sweep", action="store_true", help="扫描必备技能否决阈值")
    args = parser.parse_args()

    cases = load_cases()
    if args.sweep:
        sweep(cases)
        print()
    return summarize(cases)


if __name__ == "__main__":
    raise SystemExit(main())
