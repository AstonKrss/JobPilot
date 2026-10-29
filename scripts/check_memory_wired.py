"""记忆写入链路的回归测试。

这组测试的存在理由，是评测抓到的那条：「助手回答『记住了』，数据库一条没变」。

所以它测的不是"抽取准不准"，而是**这条链路到底通不通**：
    说完话 → 画像变了吗？历史表多了一条吗？回复里说了改了什么吗？

三部分：
    1. 纯函数（免费）：L0 门禁、prefs 装配、合并语义
    2. 静态接线：agent.py / profile.py 必须共用同一个写入函数
    3. 真实对话（花 ~¥0.01，10 秒）：发一句偏好，断言库里真的变了，然后还原

第 3 部分是唯一的硬证据。前两部分都能"通过"而线上仍然不写 ——
这正是原来的状态。

用法：
    .venv/Scripts/python.exe scripts/check_memory_wired.py
    .venv/Scripts/python.exe scripts/check_memory_wired.py --no-live   # 不调模型
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

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


def part_l0() -> None:
    from app.memory.extract import should_extract

    print("== L0 门禁：原来被结构挡住的 5 条 ==")
    # 评测里 5 条漏检，全部是 L0 拦的。这 5 条现在必须过
    for text in ("我是 2028 届本科", "暂时不考虑字节跳动", "一定要双休",
                 "我优先看大厂", "我想找杭州或宁波的"):
        check(f"放行：{text}", should_extract(text))

    print("\n== L0：负例仍要挡住 ==")
    for text in ("你好", "谢谢", "简历要怎么改？", "这个岗位怎么样？",
                 "这次先看看深圳的", "我今天面了两家"):
        check(f"挡住：{text}", not should_extract(text))

    print("\n== L0：边界 ==")
    check("太短不抽", not should_extract("实习"))
    check("空串不抽", not should_extract(""))
    check("超长不抽", not should_extract("佛山" * 300))
    # 放宽后的已知代价：提问式句子也会进来，靠模型返回空来兜
    check("提问会进来（已知代价，靠 L1 兜）", should_extract("为什么广州的岗位这么少？"))


def part_prefs() -> None:
    from app.chat.agent import _as_bool, _prefs_from_profile

    print("\n== prefs 从画像装配 ==")
    p = _prefs_from_profile(
        {"job_nature": "实习", "accept_outsourcing": False, "cities": ["广州", "深圳"]}, []
    )
    check("画像说实习 → accept_internship=True", p["accept_internship"] is True)
    check("画像说不外包 → accept_outsourcing=False", p["accept_outsourcing"] is False)
    check("本轮没给城市时用画像城市", p["target_cities"] == ["广州", "深圳"], str(p["target_cities"]))

    p = _prefs_from_profile({"job_nature": "全职"}, ["佛山"])
    check("画像说全职 → accept_internship=False", p["accept_internship"] is False)
    check("本轮城市优先", p["target_cities"] == ["佛山"])

    p = _prefs_from_profile({}, [])
    check("画像为空时实习放宽（宁可多给）", p["accept_internship"] is True)
    check("画像为空时外包放宽", p["accept_outsourcing"] is True)
    check("画像为空时不按城市过滤", p["target_cities"] == [])
    check("兼职默认不放宽", p["accept_parttime"] is False)

    check("字符串 'false' 不能当真值", _as_bool("false", True) is False)
    check("字符串 '不接受' 读成 False", _as_bool("不接受", True) is False)
    check("None 走缺省", _as_bool(None, True) is True)


def part_merge() -> None:
    from app.memory.profile_repo import _merge

    print("\n== 合并语义（静默丢数据的三个分支）==")
    check("additive + replace 要合并，不覆盖",
          _merge(["广州"], {"modality": "additive", "op": "replace", "value": ["深圳"]})
          == ["广州", "深圳"],
          str(_merge(["广州"], {"modality": "additive", "op": "replace", "value": ["深圳"]})))
    check("additive 缺 op 时也要合并",
          _merge(["广州"], {"modality": "additive", "value": "佛山"}) == ["广州", "佛山"])
    check("op=add 要合并", _merge(["广州"], {"op": "add", "value": "深圳"}) == ["广州", "深圳"])
    check("remove 真的移除，不是只留",
          _merge(["广州", "深圳"], {"op": "remove", "value": "深圳"}) == ["广州"])
    check("exclusive 覆盖", _merge(["广州", "深圳"], {"modality": "exclusive",
                                                "value": ["佛山"]}) == ["佛山"])
    check("重复值不重复加", _merge(["广州"], {"modality": "additive", "value": "广州"}) == ["广州"])


def part_wiring() -> None:
    print("\n== 静态接线：两处必须共用同一个写入函数 ==")
    agent = (ROOT / "backend/app/chat/agent.py").read_text(encoding="utf-8")
    profile = (ROOT / "backend/app/api/profile.py").read_text(encoding="utf-8")
    writer = ROOT / "backend/app/memory/writer.py"

    check("写入模块存在", writer.exists())
    check("对话链路调用 writer.apply_actions",
          "writer.apply_actions(" in agent)
    check("对话链路不再硬编码 prefs",
          '"accept_internship": False' not in agent,
          "还有硬编码的 accept_internship")
    check("对话链路用 _prefs_from_profile",
          "_prefs_from_profile(prof" in agent)
    check("手动接口复用 writer.apply_text",
          "writer.apply_text(" in profile)
    check("PlanAgent 提示词要求输出 profile_updates",
          "profile_updates" in agent)
    check("提示词里写了「只说不写等于骗人」",
          "等于骗人" in agent)


def part_live() -> None:
    """真实对话一次，断言库里真的变了。这是唯一的硬证据。"""
    import psycopg
    from fastapi.testclient import TestClient

    from app.main import app

    print("\n== 真实对话：说完话，库里有变化吗 ==")
    conn = psycopg.connect(DSN)
    try:
        row = conn.execute("SELECT value FROM user_profile WHERE key = 'cities'").fetchone()
        before_cities = row[0] if row else None
        max_hist = conn.execute(
            "SELECT coalesce(max(id), 0) FROM user_profile_history"
        ).fetchone()[0]
        max_conv = conn.execute("SELECT coalesce(max(id), 0) FROM conversation").fetchone()[0]

        client = TestClient(app)
        resp = client.post(
            "/api/chat",
            json={"message": "以后只找佛山的岗位，其他城市都不看了"},
        )
        ok_status = resp.status_code == 200
        check("对话接口返回 200", ok_status, str(resp.status_code))
        if not ok_status:
            return

        data = resp.json()
        reply = data.get("reply") or ""

        after = conn.execute("SELECT value FROM user_profile WHERE key = 'cities'").fetchone()
        after_cities = after[0] if after else None
        new_hist = conn.execute(
            "SELECT count(*) FROM user_profile_history WHERE id > %s", (max_hist,)
        ).fetchone()[0]

        check("画像 cities 真的变成佛山", "佛山" in (after_cities or []),
              f"{before_cities} → {after_cities}")
        check("历史表新增了记录", new_hist >= 1, f"新增 {new_hist} 条")
        check("回复里说明了写入了什么", "已写入画像" in reply, reply[:80])
        check("回复没有骗人（说了记住就得真的记）",
              ("记住" not in reply) or ("佛山" in (after_cities or [])))
    finally:
        # 还原：评测最忌讳的就是留下被自己改过的数据，
        # 下一次评测会以污染后的状态为基线，越跑越离谱
        try:
            if before_cities is None:
                conn.execute("DELETE FROM user_profile WHERE key = 'cities'")
            else:
                from psycopg.types.json import Jsonb

                conn.execute(
                    "UPDATE user_profile SET value = %s WHERE key = 'cities'",
                    (Jsonb(before_cities),),
                )
            conn.execute("DELETE FROM user_profile_history WHERE id > %s", (max_hist,))
            conn.execute(
                "DELETE FROM conversation_message WHERE conversation_id > %s", (max_conv,)
            )
            conn.execute("DELETE FROM conversation WHERE id > %s", (max_conv,))
            conn.commit()
            print("  （已还原画像与会话数据）")
        except Exception as exc:  # noqa: BLE001
            print(f"  !! 还原失败，请手工检查：{exc}")
        finally:
            conn.close()


def main() -> int:
    part_l0()
    part_prefs()
    part_merge()
    part_wiring()

    if "--no-live" in sys.argv:
        print("\n（跳过真实对话：--no-live）")
    else:
        part_live()

    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
