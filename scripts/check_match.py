"""匹配规则的回归测试（纯逻辑，不依赖服务与数据库）。

这些用例覆盖的是「判错一次就静默淘汰一批人」的分支。规则改动后必须重跑，
否则很容易出现「修了 A 分支出错、B 分支悄悄变了行为」的情况。

用法：.venv/Scripts/python.exe scripts/check_match.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.match.engine import match_one  # noqa: E402
from app.match.rules import Decision  # noqa: E402

RESUME = {
    "basic": {
        "edu_level": "本科",
        "edu_fulltime": True,
        "work_years": 6.0,
        "current_city": "杭州",
    },
    "skills": [{"name": "Go"}, {"name": "PostgreSQL"}, {"name": "Redis"}, {"name": "Kafka"}],
    "experiences": [],
    "projects": [],
}

JOB = {
    "job_id": 1,
    "title": "后端工程师",
    "company": None,
    "city": "北京",
    "job_nature": "全职",
    "edu_level": "本科",
    "edu_fulltime_required": None,
    "exp_min": 3,
    "exp_max": 5,
    "must_have_skills": ["Go 或 Java", "Redis"],
    "nice_to_have_skills": [],
    "hard_constraint_evidence": {},
}

PREFS_CITY_BJ = {"target_cities": ["北京"]}


def run(
    name: str,
    *,
    resume: dict | None = None,
    job: dict | None = None,
    prefs: dict | None = None,
    expect: Decision,
    expect_in_reason: str | None = None,
) -> bool:
    # 注意不能用 `prefs or 默认值`：空 dict 是合法的「明确没有任何偏好」，
    # 用 or 会把它当成「没传参」而套上默认值，测试就会验错对象
    result = match_one(
        RESUME if resume is None else resume,
        JOB if job is None else job,
        PREFS_CITY_BJ if prefs is None else prefs,
    )
    ok = result.decision is expect
    if ok and expect_in_reason:
        joined = " | ".join(result.reasons)
        ok = expect_in_reason in joined
    if not ok:
        print(f"[FAIL] {name}")
        print(f"       期望 {expect.value}，实际 {result.decision.value}")
        print(f"       理由：{result.reasons}")
    else:
        print(f"[PASS] {name}")
    return ok


def main() -> int:
    cases: list[bool] = []

    # ---- 学历 ----
    cases.append(
        run(
            "学历不足（要求硕士，简历本科）→ 否决",
            job={**JOB, "edu_level": "硕士"},
            expect=Decision.REJECT,
            expect_in_reason="岗位要求硕士",
        )
    )
    cases.append(
        run(
            "学历不限 → 通过",
            job={**JOB, "edu_level": "不限"},
            expect=Decision.PASS,
        )
    )
    cases.append(
        run(
            "要求统招，简历未注明 → 待确认（不得否决）",
            resume={**RESUME, "basic": {**RESUME["basic"], "edu_fulltime": None}},
            job={**JOB, "edu_fulltime_required": True},
            expect=Decision.NEED_CONFIRM,
            expect_in_reason="未注明是否统招",
        )
    )
    cases.append(
        run(
            "要求统招，简历为非全日制 → 否决",
            resume={**RESUME, "basic": {**RESUME["basic"], "edu_fulltime": False}},
            job={**JOB, "edu_fulltime_required": True},
            expect=Decision.REJECT,
            expect_in_reason="非全日制",
        )
    )
    cases.append(
        run(
            "JD 未写统招（null），简历非全日制 → 通过",
            resume={**RESUME, "basic": {**RESUME["basic"], "edu_fulltime": False}},
            job={**JOB, "edu_fulltime_required": None},
            expect=Decision.PASS,
        )
    )

    # ---- 城市 ----
    cases.append(
        run(
            "城市不符且不接受异地 → 否决",
            prefs={"target_cities": ["上海"]},
            expect=Decision.REJECT,
            expect_in_reason="期望城市是上海",
        )
    )
    cases.append(
        run(
            "城市不符但接受异地 → 通过",
            prefs={"target_cities": ["上海"], "accept_relocation": True},
            expect=Decision.PASS,
        )
    )
    cases.append(
        run(
            "未设期望城市，回退当前城市（杭州 vs 北京）→ 否决",
            prefs={},
            expect=Decision.REJECT,
            expect_in_reason="你当前在杭州",
        )
    )
    cases.append(
        run(
            "期望城市含多个，命中其一 → 通过",
            prefs={"target_cities": ["上海", "北京"]},
            expect=Decision.PASS,
        )
    )
    cases.append(
        run(
            "岗位为远程 → 通过（不受城市限制）",
            job={**JOB, "city": "远程"},
            prefs={"target_cities": ["上海"]},
            expect=Decision.PASS,
            expect_in_reason="远程",
        )
    )
    cases.append(
        run(
            "「北京市」与「北京」视为同城 → 通过",
            job={**JOB, "city": "北京市"},
            expect=Decision.PASS,
        )
    )

    # ---- 工作性质 ----
    cases.append(
        run(
            "外包岗 + 不接受外包 → 否决",
            job={**JOB, "job_nature": "外包"},
            prefs={**PREFS_CITY_BJ, "accept_outsourcing": False},
            expect=Decision.REJECT,
            expect_in_reason="外包",
        )
    )
    cases.append(
        run(
            "外包岗 + 接受外包 → 通过",
            job={**JOB, "job_nature": "外包"},
            prefs={**PREFS_CITY_BJ, "accept_outsourcing": True},
            expect=Decision.PASS,
        )
    )
    cases.append(
        run(
            "实习岗（默认只找全职）→ 否决",
            job={**JOB, "job_nature": "实习"},
            expect=Decision.REJECT,
            expect_in_reason="实习",
        )
    )

    # ---- 必备技能 ----
    cases.append(
        run(
            "必备技能 3/4（75%）→ 通过",
            job={**JOB, "must_have_skills": ["Go 或 Java", "Redis", "Kafka", "MySQL"]},
            expect=Decision.PASS,
        )
    )
    cases.append(
        run(
            "必备技能 0/3 → 否决",
            job={**JOB, "must_have_skills": ["Java", "Spring Boot", "MySQL"]},
            expect=Decision.REJECT,
            expect_in_reason="低于 50%",
        )
    )
    cases.append(
        run(
            "「Go 或 Java」能被「Go」命中（并列结构）→ 通过",
            job={**JOB, "must_have_skills": ["Go 或 Java"]},
            expect=Decision.PASS,
        )
    )
    cases.append(
        run(
            "两字符技能不做子串匹配：简历 Go 不应命中 Django 岗",
            resume={**RESUME, "skills": [{"name": "Go"}]},
            job={**JOB, "must_have_skills": ["Django"]},
            expect=Decision.REJECT,
        )
    )

    # ---- 年限阶梯 ----
    def score_of(resume_years: float, exp_min: int) -> float:
        result = match_one(
            {**RESUME, "basic": {**RESUME["basic"], "work_years": resume_years}},
            {**JOB, "exp_min": exp_min},
            PREFS_CITY_BJ,
        )
        return result.score or 0.0

    near = score_of(6.0, 5)   # 差 1 年内
    mid = score_of(2.0, 5)    # 差 3 年
    far = score_of(0.0, 6)    # 差 6 年
    ladder_ok = near > mid > far
    print(f"[{'PASS' if ladder_ok else 'FAIL'}] 年限阶梯递减：{near} > {mid} > {far}")
    cases.append(ladder_ok)

    senior = score_of(10.0, 3)  # 超出要求不扣分
    print(f"[{'PASS' if senior == near else 'FAIL'}] 年限超出不扣分：{senior} == {near}")
    cases.append(senior == near)

    total = sum(cases)
    print(f"\n{total}/{len(cases)} 通过")
    return 0 if total == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
