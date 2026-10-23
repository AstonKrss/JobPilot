"""事实核查规则层的回归测试（纯逻辑）。

重点覆盖「改写把中文数字换成阿拉伯数字」这个高频误报场景：
简历原文写「三千万笔」，改写示例写「3000 万笔」，这是同义改写而不是造假。
按字符串比对会误判，按数值比对才对。

用法：.venv/Scripts/python.exe scripts/check_factcheck.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.rewrite.factcheck import check_example, cn_to_number  # noqa: E402

SOURCE = """李强 高级后端工程师
技能 Python Go Kafka Redis Kubernetes
字节跳动 后端工程师 2019.07 - 2023.06 北京
负责支付核心链路，支撑日均三千万笔交易。
主导风控引擎重构，规则生效时间从小时级降到分钟级。
将网关 P99 延迟从 120ms 优化到 45ms。
清华大学 计算机科学与技术 本科 统招
分布式任务调度平台 基于 Kubernetes 与 PostgreSQL 实现任务编排，支撑日均五十万任务。"""


def run(name: str, example: str, should_pass: bool) -> bool:
    result = check_example(example, SOURCE)
    ok = result.passed == should_pass
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name}")
    if not ok:
        print(f"       期望 {'通过' if should_pass else '拦截'}，实际 {result.summary}")
    elif not should_pass:
        print(f"       拦下原因：{result.summary}")
    return ok


def main() -> int:
    cases: list[bool] = []

    # 中文数字 ↔ 阿拉伯数字等价（最容易误报的一类）
    cases.append(run("中文数字等价：三千万 → 3000万", "支撑日均3000万笔交易", True))
    cases.append(run("中文数字等价：五十万 → 500000", "支撑日均500000任务", True))
    cases.append(run("数值被改大：三千万 → 五千万", "支撑日均五千万笔交易", False))
    cases.append(run("数值被改大：120ms → 80ms", "将网关 P99 延迟优化到 80ms", False))

    # 原有数值直接沿用
    cases.append(run("沿用原文数值 120ms", "将 P99 延迟从 120ms 降到 45ms", True))

    # 英文专名
    cases.append(run("沿用原文技术名 Kafka", "熟悉 Kafka 与 Redis", True))
    cases.append(run("新增原文没有的技术名 Flink", "熟悉 Kafka 与 Flink", False))
    cases.append(run("新增原文没有的技术名 Spark", "使用 Spark 处理数据", False))

    # 不涉及数字与专名的纯措辞改写（宽松模式应放行）
    cases.append(run("纯措辞重组", "负责支付核心链路的稳定性与性能优化", True))
    cases.append(run("空示例", "", True))

    # 中文数字解析自身
    parse_cases = [
        ("三千万", 30_000_000),
        ("五十万", 500_000),
        ("一百二十", 120),
        ("十", 10),
        ("两千", 2000),
    ]
    for text, expect in parse_cases:
        got = cn_to_number(text)
        ok = got == expect
        print(f"[{'PASS' if ok else 'FAIL'}] 中文数字解析 {text} = {expect}，实际 {got}")
        cases.append(ok)

    total = sum(cases)
    print(f"\n{total}/{len(cases)} 通过")
    return 0 if total == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
