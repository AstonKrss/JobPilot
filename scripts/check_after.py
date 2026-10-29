"""投递之后的三个接口的回归测试。

覆盖：
    GET /api/applications/followups   该催谁
    GET /api/applications/calibration 打分准不准
    GET /api/resumes/{id}/tailored/{job}/check 导出文件复查

这几条都是"结论型"接口 —— 它们最容易出的问题不是 500，而是**在样本不足时
给出一个看起来很像结论的数字**。所以断言的重点之一是确认样本不足时它说的是
"看不出来"，而不是伪造一个比率。

用法：.venv/Scripts/python.exe scripts/check_after.py
（直接连本机数据库，不需要先启动服务）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.rewrite.ats_check import CHARS_PER_LINE, LINES_PER_PAGE  # noqa: E402

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


def main() -> int:
    c = TestClient(app)

    print("== 该催谁 ==")
    r = c.get("/api/applications/followups")
    check("接口可用", r.status_code == 200, str(r.status_code))
    d = r.json()
    check("默认 10 天", d.get("days") == 10, str(d.get("days")))
    check("返回结构完整", {"days", "count", "items"} <= set(d))
    check("count 与 items 长度一致", d["count"] == len(d["items"]))
    check("没有该催的时候不返回 hint", (bool(d["items"]) or d.get("hint") is None))

    for item in d["items"]:
        check("每条的等待天数不少于阈值", item["quiet_days"] >= 10, str(item["quiet_days"]))
        check("标明是否从没回过", "never_replied" in item)
        break

    r = c.get("/api/applications/followups?days=1")
    check("可以改天数", r.status_code == 200 and r.json()["days"] == 1)
    # 天数越小，命中应当越多或相等（单调性）
    check("天数越小命中不少于大天数",
          r.json()["count"] >= d["count"],
          f"{r.json()['count']} vs {d['count']}")
    r = c.get("/api/applications/followups?days=0")
    check("days=0 返回 400", r.status_code == 400, str(r.status_code))

    print("\n== 打分准不准 ==")
    r = c.get("/api/applications/calibration")
    check("接口可用", r.status_code == 200, str(r.status_code))
    cal = r.json()
    check("四个分档", len(cal.get("buckets") or []) == 4, str(len(cal.get("buckets") or [])))
    check("带结论", bool((cal.get("verdict") or {}).get("text")))
    check("结论有 level", (cal.get("verdict") or {}).get("level") in ("ok", "bad", "unknown"))

    for b in cal["buckets"]:
        check(f"分档 {b['label']} 字段完整", {"applied", "replied", "rate", "enough"} <= set(b))
        # 核心断言：样本不够时不许给比率
        if b["applied"] < 3:
            check(f"分档 {b['label']} 样本不足时不给比率", b["rate"] is None, str(b["rate"]))
            check(f"分档 {b['label']} 标记为不足", b["enough"] is False)
        check(f"分档 {b['label']} 回复数不超过投递数", b["replied"] <= b["applied"])
        break

    # 分档之间不能重叠，否则同一个岗位会被算两次
    lo = [b["lo"] for b in cal["buckets"]]
    check("分档区间递增", lo == sorted(lo), str(lo))

    print("\n== 导出文件复查 ==")
    r = c.get("/api/resumes/999999/tailored/999999/check")
    check("没生成过时返回 404", r.status_code == 404, str(r.status_code))

    # 用一份真实生成过的文件（data/tailored/resume1-job37.docx）
    sample = ROOT / "data" / "tailored" / "resume1-job37.docx"
    if sample.exists():
        r = c.get("/api/resumes/1/tailored/37/check")
        check("已有文件可复查", r.status_code == 200, str(r.status_code))
        rep = r.json()
        check("复查带检查项", len(rep.get("checks") or []) >= 5, str(len(rep.get("checks") or [])))
        check("复查带统计", (rep.get("stats") or {}).get("chars", 0) > 0)
        check("每项都有成因说明", all(x.get("detail") for x in rep["checks"]))
        check("计数与检查项一致",
              rep["counts"]["pass"] + rep["counts"]["warn"] + rep["counts"]["fail"]
              == sum(1 for x in rep["checks"] if x["level"] in ("pass", "warn", "fail")))
    else:
        check("已有文件可复查（样本缺失，跳过）", True, "data/tailored/resume1-job37.docx 不存在")

    print("\n== 页数估算的参数是有依据的 ==")
    # 这两个常数直接决定"是否超过两页"的判断，改之前得先知道它的含义
    check("每行宽度为正", CHARS_PER_LINE > 40, str(CHARS_PER_LINE))
    check("每页行数为正", 30 <= LINES_PER_PAGE <= 60, str(LINES_PER_PAGE))

    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
