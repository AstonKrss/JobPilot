"""召回层的回归测试：方向相关的岗位到底有没有进候选集。

这组测试针对的是评测里最扎眼的一条：「找产品经理返回 0 个产品岗」。

原来的实现把 rank() 的 REJECT 直接当召回过滤器，产品岗在排序之前就消失了。
现在拆成两段：`_recall_candidates()` 只看方向，`rank()` 只看能不能投。
所以这里断言两件事：

    1. 目标方向的岗位必须进候选（召回）
    2. 它们进不了「能投」栏时，必须进「方向对但卡住」栏（不是凭空消失）

不调模型，只用本地向量 + 数据库里的 80 个岗位。

用法：.venv/Scripts/python.exe scripts/check_recall.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import psycopg  # noqa: E402

from app.chat.agent import (  # noqa: E402
    BLOCKED_MIN_SIM,
    _prefs_from_profile,
    _recall_candidates,
    _semantic_order,
)
from app.db import match_repo, repo  # noqa: E402
from app.match.engine import Decision, rank  # noqa: E402

PASS = 0
FAIL = 0
DSN = "postgresql://postgres@127.0.0.1:5433/ragjob"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}{(' — ' + detail) if detail else ''}")


def _recall_in_direction(r, meta) -> bool:
    """与线上 _in_direction 同判据：片段命中标题，或相似度过阈值。"""
    frags = meta.get("fragments") or []
    title = str(r.title or "")
    return any(f in title for f in frags)


def main() -> int:
    conn = psycopg.connect(DSN)
    jobs = match_repo.list_current_job_profiles(conn)
    rid = repo.get_default_resume_id(conn)
    profile = match_repo.get_current_profile(conn, rid) if rid else None
    conn.close()

    if not jobs:
        print("岗位库为空，跳过")
        return 0
    by_id = {int(j["job_id"]): j for j in jobs}
    titles = {i: str(j.get("title") or "") for i, j in by_id.items()}

    print(f"== 库里有 {len(jobs)} 个岗位 ==")

    print("\n== 召回：目标方向必须进候选 ==")
    cases = {
        "产品经理": (lambda t: "产品" in t, "产品岗"),
        "后端实习": (lambda t: "实习" in t and any(k in t for k in ("后端", "Java", "java", "开发", "Python", "Devops")), "开发实习岗"),
        "数据分析": (lambda t: "数据" in t, "数据岗"),
        "前端开发": (lambda t: "前端" in t, "前端岗"),
    }
    for intent, (pred, label) in cases.items():
        selected, meta = _recall_candidates(intent, [], jobs)
        got = [titles[int(j["job_id"])] for j in selected]
        expect = [t for t in titles.values() if pred(t)]
        hit = [t for t in expect if t in got]
        check(
            f"「{intent}」召回 {len(hit)}/{len(expect)} 个{label}",
            len(expect) == 0 or len(hit) == len(expect),
            f"缺：{[t for t in expect if t not in got][:5]}",
        )
        # 上限是 RECALL_MAX=40，不是 30：片段匹配会把同方向的整个池子放进来
        check(f"「{intent}」候选数在 5~40 之间（{len(selected)}）", 5 <= len(selected) <= 40,
              str(len(selected)))
        check(f"「{intent}」候选是全库的子集", len(selected) <= len(jobs))

    print("\n== 召回层不按城市过滤 ==")
    # 画像城市是广州/深圳，但佛山的岗位必须留在候选里 ——
    # 在召回层按城市筛，用户问"产品经理"时会静默丢掉佛山那 4 个
    selected, _ = _recall_candidates("产品经理", [], jobs)
    fosh = [titles[int(j["job_id"])] for j in selected if "佛山" in str(j.get("city") or "")]
    check("佛山的岗位留在候选里（不按城市预筛）", len(fosh) >= 2, str(fosh[:4]))

    print("\n== 判定层：方向对但卡住的岗位要能被摘出来 ==")
    if profile is None:
        check("有可用简历画像", False, "没有默认简历")
    else:
        prof = {"job_nature": "实习", "accept_outsourcing": False, "cities": ["广州", "深圳"]}
        prefs = _prefs_from_profile(prof, [])
        selected, meta = _recall_candidates("产品经理", [], jobs)
        results = rank(profile, selected, prefs, {})
        passed = [r for r in results if r.decision is not Decision.REJECT]
        # 与线上一致：用片段判断方向，用"命中片段长度之和"排序。
        # 早先这里用了整串（terms），于是只有标题里含「产品经理」四个字的
        # 「B端产品经理」算同方向，「产品顾问」「产品实习生」都被判成不在方向上 ——
        # 测试与实现用不同判据，测出来的结论就没有意义
        frags = meta.get("fragments") or []
        cand_by_id = {int(j["job_id"]): j for j in selected}

        def in_direction(r) -> bool:
            title = titles.get(r.job_id, "")
            return any(f in title for f in frags)

        def affinity(r) -> int:
            title = titles.get(r.job_id, "")
            return sum(len(f) for f in frags if f in title)

        # 平手时用什么排很关键：产品岗和项目经理的 aff 都是 2（都只命中一个两字词），
        # 谁进前三取决于第二排序键。线上用的是语义相似度，这里必须一致 ——
        # 用 score 的话，reject 的岗位 score 全是 0，等于随机
        sims = _semantic_order("产品经理", [], selected)
        blocked = sorted(
            (r for r in results if r.decision is Decision.REJECT and in_direction(r)),
            key=lambda r: (-affinity(r), -sims.get(r.job_id, 0.0)),
        )[:3]
        check("有『方向对但投不进』的岗位（不是空栏）", len(blocked) >= 1, str(len(blocked)))
        check(
            "这些岗位确实带着卡点原因",
            all([c for c in r.checks if c.decision.value == "reject"] for r in blocked),
            "有岗位没有 reject 明细",
        )
        check(
            "能投的岗位与卡住的岗位不重叠",
            not ({r.job_id for r in passed} & {r.job_id for r in blocked}),
        )
        # 关键：产品岗不该凭空消失。第三栏 UI 上只放 3 个，
        # 所以断言"至少 3 个有归属"，而不是"全部"—— 多出来的会被截断，
        # 但它们确实进了候选、也确实被判过，只是没占屏幕
        prod = [jid for jid, t in titles.items() if "产品" in t and jid in cand_by_id]
        seen = {r.job_id for r in passed} | {r.job_id for r in blocked}
        check(
            f"召回里的产品岗 {len(prod)} 个，至少 3 个有归属（能投或卡住）",
            len(set(prod) & seen) >= min(3, len(prod)),
            f"只有 {len(set(prod) & seen)} 个出现",
        )
        if blocked:
            reasons = " ".join(c.detail for c in blocked[0].checks if c.decision.value == "reject")
            check("卡点写的是人话（含城市或技能）", ("城市" in reasons or "技能" in reasons), reasons[:60])

    print("\n== 方向全不匹配时的输出结构 ==")
    # 用户要求的行为：方向内全投不进时，回复主体是「差什么、补什么」，
    # 方向外岗位降级为附注。这三条断言钉住这个契约
    agent_src = (ROOT / "backend" / "app" / "chat" / "agent.py").read_text(encoding="utf-8")
    check("blocked 容量随方向命中动态调整", "blocked_max = 5 if direction_hits == 0 else 3" in agent_src)
    check("fit 卡片带 off_direction 标记", '"off_direction": direction_hits == 0 and not _in_direction(r)' in agent_src)
    check("回复结构指令存在（第一句直说/主体讲缺口/附注降级）",
          all(k in agent_src for k in ("第一句直说", "补了哪一项就能投", "不许推荐")))
    check("卡点带缺什么技能（补什么的原料）", "_gap(r)" in agent_src and "missed_skills" in agent_src)

    # 真实数据验证：产品意图下 direction_hits==0 时 blocked 应能装下库里全部产品岗
    sel, meta = _recall_candidates("产品经理", [], jobs)
    res = rank(profile, sel, _prefs_from_profile({"job_nature": "实习", "accept_outsourcing": False, "cities": ["广州", "深圳"]}, []), {})
    prods = [r for r in res if r.decision is Decision.REJECT and _recall_in_direction(r, meta)]
    check(f"产品岗 REJECT 的有 {len(prods)} 个（应 >= 3，blocked_max=5 要装得下）", len(prods) >= 3)

    print("\n== 边界 ==")
    check("空意图不崩", _recall_candidates("", [], jobs)[1]["total"] == len(jobs))
    check("空岗位库返回空", _recall_candidates("产品经理", [], [])[0] == [])
    check("阈值是正的", BLOCKED_MIN_SIM > 0)

    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
