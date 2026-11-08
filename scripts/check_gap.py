"""技术栈补漏（gap）的离线回归。

四部分：
    ① 展开缓存分流（mock 模型调用，真库）：norm_key 归一命中同键、
       并列写法拆分、命中/未命中、失败降级
    ② 焦虑控制：>8 项只展开前 8、tag 越界回退、条目长度截断
    ③ 自评 upsert 幂等 + priority 聚合排序（真库，快照-还原；
       job 级联删除清理，不动真实岗位）
    ④ prompt 静态断言：推断声明、上限常量

用法：
    .venv/Scripts/python.exe scripts/check_gap.py   # ①②④ 离线可跑；③ 需数据库
"""

from __future__ import annotations

import sys
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


def part_cache_flow() -> None:
    """① 缓存分流：mock chat_json，真库表。"""
    import asyncio

    from app.gap import expand as ex

    print("== ① 展开缓存分流（mock 模型，真库）==")

    # 清掉此前崩溃运行可能留下的测试缓存（本测试的 norm 集合），保证从全 miss 起步
    try:
        import psycopg as _psycopg

        from app.config import settings as _settings

        _c = _psycopg.connect(_settings.database_url, connect_timeout=3)
        _c.execute("DELETE FROM skill_expansion_cache WHERE skill_norm = ANY(%s)",
                   (["redis", "mysql", "go", "java", "新技能x"],))
        _c.commit()
        _c.close()
    except Exception:
        pass

    calls = {"n": 0}

    async def fake_chat_json(system, user, what=None, max_tokens=None):
        calls["n"] += 1
        # 按 user 文本里的技能行生成条目
        lines = [ln[2:] for ln in user.splitlines() if ln.startswith("- ")]
        rows = []
        for s in lines:
            rows.append({"skill": s, "items": [
                {"text": f"能讲清{s}的核心概念与典型问题", "tag": "基础"},
                {"text": f"能用{s}实现一个完整的小需求并说明权衡", "tag": "进阶"},
            ]})
        class R:
            text = __import__("json").dumps({"skills": rows}, ensure_ascii=False)
            model = "mock"
        return R()

    orig = ex.chat_json
    ex.chat_json = fake_chat_json
    try:
        # 首次：全 miss，一次调用展开
        r1 = asyncio.run(ex.expand_for_job(["Redis", "MySQL"], "后端工程师", None))
        check("首次展开产生 1 次模型调用", calls["n"] == 1, f"calls={calls['n']}")
        check("展开条目 2 项技能各 2 条", r1["expanded_n"] == 2, f"{r1['expanded_n']}")
        check("条目按原文顺序返回", [s["skill"] for s in r1["skills"]] == ["Redis", "MySQL"])

        # 二次：同技能同语境 → 全部命中缓存，0 次调用
        r2 = asyncio.run(ex.expand_for_job(["Redis", "MySQL"], "另一个岗位", None))
        check("二次调用命中缓存（0 次模型调用）", calls["n"] == 1 and r2["cache_hits"] == 2,
              f"calls={calls['n']} hits={r2['cache_hits']}")

        # norm_key 归一：写法不同命中同一条缓存
        r3 = asyncio.run(ex.expand_for_job(["redis", "REDIS "], "第三个岗位", None))
        check("写法变体命中同一缓存（norm_key 归一，两个变体各自命中）", calls["n"] == 1 and r3["cache_hits"] == 2,
              f"calls={calls['n']} hits={r3['cache_hits']}")

        # 并列写法拆分：「精通 Go 或 Java」→ Go/Java 两个子技能都展开
        r4 = asyncio.run(ex.expand_for_job(["精通 Go 或 Java"], "第四个岗位", None))
        go_items = next((s for s in r4["skills"] if "Go" in s["skill"] or "Java" in s["skill"]), None)
        check("并列写法被拆分展开", go_items is not None and len(go_items["items"]) >= 2,
              str(go_items))

        # 模型挂了 → 优雅降级（不抛异常，条目为空）
        async def broken(*a, **k):
            raise RuntimeError("网关挂了")
        ex.chat_json = broken
        r5 = asyncio.run(ex.expand_for_job(["新技能X"], "第五个岗位", None))
        check("模型调用失败优雅降级（条目空、不抛异常）",
              r5["skills"][0]["expanded"] is False and len(r5["skills"][0]["items"]) == 0)
    finally:
        ex.chat_json = orig


def part_anxiety_control() -> None:
    """② 焦虑控制：上限、tag 回退、长度截断。"""
    from app.gap import expand as ex

    print("\n== ② 焦虑控制 ==")
    check("展开上限常量为 8", ex.MAX_EXPAND_SKILLS == 8)
    check("每技能条目上限为 3", ex.MAX_ITEMS_PER_SKILL == 3)
    check("prompt 声明是推断而非企业标准", "推断" in ex.EXPAND_SYSTEM_PROMPT)
    check("prompt 禁止编造企业内部细节", "不编造" in ex.EXPAND_SYSTEM_PROMPT)

    items = ex._validate_items(
        [{"text": "这条太短", "tag": "基础"},
         {"text": f"这是一条正常长度的能力问句，编号 {'x' * 200}", "tag": "不存在的tag"},
         {"text": "能讲清 X 的原理与权衡", "tag": "深入"},
         "非 dict 项"],
        "X")
    check("过短条目被过滤", len(items) == 2, f"{len(items)}")
    check("越界 tag 回退为 进阶", all(i.tag in ex.ALLOWED_TAGS for i in items))
    check("超长文本截断到 120 字", all(len(i.text) <= 120 for i in items))


def part_assess_priority() -> None:
    """③ 自评 upsert 幂等 + priority 聚合（真库，快照-还原）。"""
    import psycopg

    from app.config import settings

    try:
        conn = psycopg.connect(settings.database_url, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        print(f"\n== ③ 自评与聚合：跳过（数据库未启动：{exc}）==")
        return
    print("\n== ③ 自评 upsert + priority 聚合（真库）==")
    try:
        # 清掉此前运行留下的测试痕迹：测试岗位、mock 指纹条目、本测试专用键
        conn.execute("DELETE FROM job WHERE url LIKE 'https://gap.test/%'")
        conn.execute("DELETE FROM skill_expansion_cache WHERE items::text LIKE '%核心概念与典型问题%'")
        conn.execute("DELETE FROM skill_expansion_cache WHERE cache_key IN ('t_redis','t_sb','t_cold')")
        conn.commit()

        # 基线快照
        cache_snap = conn.execute("SELECT cache_key, skill, skill_norm, context, items, model FROM skill_expansion_cache").fetchall()
        assess_snap = conn.execute("SELECT resume_id, item_hash, item, skill_norm, status FROM skill_self_assess").fetchall()
        job_max = conn.execute("SELECT coalesce(max(id), 0) FROM job").fetchone()[0]

        rid = conn.execute("SELECT coalesce(max(id), 1) FROM resume").fetchone()[0]

        from app.gap import assess as ga
        from psycopg.types.json import Jsonb

        # upsert 幂等：同条目两次保存 → 1 行且状态覆盖
        conn.execute("DELETE FROM skill_self_assess WHERE resume_id = %s AND skill_norm = %s",
                     (rid, "redis"))
        n1 = ga.save_assessments(conn, rid, [
            {"item": "能用 Redis 实现分布式锁并说明续约策略", "skill_norm": "redis", "status": "can_explain"}])
        n2 = ga.save_assessments(conn, rid, [
            {"item": "能讲清缓存穿透和雪崩的区别及应对", "skill_norm": "redis", "status": "never"}])
        row = conn.execute(
            "SELECT status FROM skill_self_assess WHERE resume_id=%s AND item_hash=%s",
            (rid, ga.item_hash("能讲清缓存穿透和雪崩的区别及应对"))).fetchone()
        check(f"upsert 幂等（两次保存共 {n1}+{n2}，同一条目 1 行且状态被覆盖）",
              n1 == 1 and n2 == 1 and row and row[0] == "never",
              f"row={row}")

        # 注入两条展开缓存（Redis / Spring Boot）
        conn.execute("DELETE FROM skill_expansion_cache WHERE skill_norm IN ('redis','spring boot','supercold')")
        conn.execute(
            """INSERT INTO skill_expansion_cache (cache_key, skill, skill_norm, context, items, model)
               VALUES
               ('t_redis','Redis','redis','社招', %s,'mock'),
               ('t_sb','Spring Boot','spring boot','社招', %s,'mock'),
               ('t_cold','SuperCold框架','supercold','社招', %s,'mock')
               ON CONFLICT (cache_key) DO UPDATE SET items = EXCLUDED.items""",
            (Jsonb([{"text": "能讲清缓存穿透和雪崩的区别及应对", "tag": "基础"},
                    {"text": "能用 Redis 实现分布式锁并说明续约策略", "tag": "进阶"}]),
             Jsonb([{"text": "能讲清 Spring Boot 自动装配的原理", "tag": "进阶"}]),
             Jsonb([{"text": "SuperCold 冷门条目", "tag": "基础"}])),
        )
        conn.commit()

        # 造 3 个测试岗位：Redis×3、Spring Boot×1（级联清理只删测试 job）
        for tag in ("gap_t1", "gap_t2", "gap_t3"):
            jid = conn.execute(
                "INSERT INTO job (source, raw_text, url) VALUES ('manual', %s, %s) RETURNING id",
                (f"gap 测试岗位原文 {tag}，超过三十个字符以满足长度检查要求。", f"https://gap.test/{tag}"),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO job_profile (job_id, title, must_have_skills, is_current, extract_version)
                   VALUES (%s, %s, %s, true, 'gap-test')""",
                (jid, f"测试岗位 {tag}", Jsonb(["Redis", "spring boot" if tag == "gap_t1" else "Redis"])) if tag != "gap_t3"
                else (jid, f"测试岗位 {tag}", Jsonb(["Redis"])),
            )
        conn.commit()

        prio = ga.priority(conn, rid)
        check("聚合产出条目", len(prio) >= 2, f"{len(prio)}")
        redis_items = [p for p in prio if p["skill_norm"] == "redis"]
        check("Redis 条目 ≥ 3 个岗位要求（3 个测试岗位 + 真库原有）",
              redis_items and redis_items[0]["jobs_n"] >= 3,
              str([(p["jobs_n"], p["item"][:16]) for p in prio[:4]]))
        check("can_explain 的条目不进聚合（分布式锁已评能讲清）",
              all("分布式锁" not in p["item"] for p in prio),
              str([p["item"][:14] for p in prio]))
        check("按岗位数降序", all(prio[i]["jobs_n"] >= prio[i + 1]["jobs_n"] for i in range(len(prio) - 1)))
        check("无展开缓存的技能不进聚合（SuperCold 无岗位引用不出现）",
              all(p["skill_norm"] != "supercold" for p in prio))

        # 还原
        conn.execute("DELETE FROM job WHERE id > %s", (job_max,))
        conn.execute("DELETE FROM skill_expansion_cache")
        from psycopg.types.json import Jsonb

        for key, skill, norm, ctx, items, model in cache_snap:
            conn.execute(
                "INSERT INTO skill_expansion_cache (cache_key, skill, skill_norm, context, items, model) VALUES (%s,%s,%s,%s,%s,%s)",
                (key, skill, norm, ctx, Jsonb(items), model))
        conn.execute("DELETE FROM skill_self_assess")
        for r_id, ih, item, norm, status in assess_snap:
            conn.execute(
                "INSERT INTO skill_self_assess (resume_id, item_hash, item, skill_norm, status) VALUES (%s,%s,%s,%s,%s)",
                (r_id, ih, item, norm, status))
        conn.commit()
        print("  （已还原 skill_expansion_cache / skill_self_assess / 测试岗位）")
    finally:
        conn.close()


def part_prompt() -> None:
    print("\n== ④ prompt 与常量 ==")
    from app.gap.expand import EXPAND_SYSTEM_PROMPT, _context
    check("校招语境识别（校招/实习 → 校招）",
          _context("校招") == "校招" and _context("实习") == "校招" and _context("社招") == "社招" and _context(None) == "社招")
    check("prompt 要求从基础到深入排列", "从基础到深入" in EXPAND_SYSTEM_PROMPT)
    check("prompt 要求行为化问句", "可被面试验证" in EXPAND_SYSTEM_PROMPT)


def main() -> int:
    part_cache_flow()
    part_anxiety_control()
    part_assess_priority()
    part_prompt()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
