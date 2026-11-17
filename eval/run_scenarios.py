"""会话级端到端评测：多轮真实对话，断言「库里变了」「路由走对了」「回复守约」。

它和 eval/run_eval.py 的分工
    run_eval.py 是规则级回归：构造的简历×岗位对，验证 match_one 的判定口径，
    不需要库、不需要模型。
    本脚本是链路级回归：真的发一句话给对话接口（PlanAgent → 记忆写入 →
    检索 → 回复），断言的是用户视角的结果 —— 说完话画像变没变、下一轮
    用没用上、该反问的时候有没有反问。eval 的教训（docs/agent-eval-diagnosis
    -2026-10-29.md R3）：「接口有测试就算过」挡不住「链路压根没接」，
    副作用的验收标准是库里多了什么。

隔离纪律（每条场景独立，跑完精确还原）
    快照 user_profile 全表、user_profile_history / conversation 的 max(id)、
    episode 的命中计数 → 清空并写入 setup 画像 → 逐轮 POST /api/chat →
    断言 → 还原快照。评测最忌讳留下被自己改过的数据：
    下一次评测会以污染后的状态为基线，越跑越离谱。

用法：
    .venv/Scripts/python.exe eval/run_scenarios.py              # 全部跑（需库 + LLM key）
    .venv/Scripts/python.exe eval/run_scenarios.py --dry        # 只校验场景文件结构
    .venv/Scripts/python.exe eval/run_scenarios.py --filter mem-  # 只跑 id 含该串的
    .venv/Scripts/python.exe eval/run_scenarios.py --scenarios eval/scenarios_hard.jsonl
    .venv/Scripts/python.exe eval/run_scenarios.py --resume eval/results/scenarios_xxx.json  # 断点续跑
    结果落 eval/results/scenarios_<时间戳>.json，每跑完一条即时落盘；支持 --resume 跳过已完成场景。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from psycopg.types.json import Jsonb  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

SCENARIOS_PATH = ROOT / "eval" / "scenarios.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"

PASS = 0
FAIL = 0

# expect 支持的断言键。加新键先在这里登记，--dry 才能拦住拼错的断言
EXPECT_KEYS = {
    "profile_equals",        # {key: value} 画像槽位最终值完全相等
    "profile_contains",      # {key: [v...]} 数组槽位包含全部给定值
    "profile_absent",        # [key] 槽位必须不存在（没被写出来）
    "history_rows",          # int 画像历史表精确增量
    "history_rows_min",      # int 画像历史表增量下限
    "reply_contains",        # [str] 任意一轮回复包含
    "reply_last_contains",   # [str] 最后一轮回复包含
    "reply_not_contains",    # [str] 所有轮次都不得包含
    "reply_max_chars",       # int 最后一轮回复长度上限
    "jobs_empty_last",       # bool 最后一轮不带岗位卡片
    "steps_last_contains",   # [str] 最后一轮的执行进度里包含（意图/城市等中间事实）
}
SETUP_KEYS = {"profile", "episodes"}


def load_scenarios(path: Path | None = None) -> list[dict]:
    scenarios_path = path or SCENARIOS_PATH
    cases: list[dict] = []
    with scenarios_path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{scenarios_path.name} 第 {line_no} 行不是合法 JSON：{exc}")
    return cases


def validate(cases: list[dict]) -> list[str]:
    """结构性校验：--dry 模式与正式跑共用。返回问题列表。"""
    problems: list[str] = []
    seen: set[str] = set()
    for c in cases:
        cid = c.get("id") or f"<第 {len(problems) + 1} 条>"
        if cid in seen:
            problems.append(f"{cid}: id 重复")
        seen.add(cid)
        if not c.get("turns"):
            problems.append(f"{cid}: turns 为空")
        for key in (c.get("setup") or {}):
            if key not in SETUP_KEYS:
                problems.append(f"{cid}: 未知的 setup 键 {key}")
        for key in (c.get("expect") or {}):
            if key not in EXPECT_KEYS:
                problems.append(f"{cid}: 未知的 expect 键 {key}（合法：{sorted(EXPECT_KEYS)}）")
    return problems


# ---------- 数据库快照 / 还原 ----------


def snapshot(conn) -> dict:
    profile = {
        r[0]: {"value": r[1], "evidence": r[2], "modality": r[3],
               "confidence": r[4], "conversation_id": r[5]}
        for r in conn.execute(
            "SELECT key, value, evidence, modality, confidence, conversation_id"
            " FROM user_profile"
        ).fetchall()
    }
    return {
        "profile": profile,
        "hist_max": conn.execute("SELECT coalesce(max(id), 0) FROM user_profile_history").fetchone()[0],
        "conv_max": conn.execute("SELECT coalesce(max(id), 0) FROM conversation").fetchone()[0],
        # 情节记忆整表快照：setup 可能预埋/删除 episode，还原必须精确到整行
        "episodes_full": conn.execute(
            "SELECT id, conversation_id, topic, summary, facts, embedding,"
            " created_at, last_hit_at, hit_count FROM episode"
        ).fetchall(),
    }


def apply_setup(conn, setup: dict) -> None:
    """清空画像后写入场景基线。空 dict 也是合法基线（验证「什么都没配」的行为）。

    episodes 预埋用本地向量（ONNX，无 LLM 成本）；conversation_id 置 NULL，
    避免引用被还原流程删除的会话。
    """
    conn.execute("DELETE FROM user_profile")
    for key, value in (setup.get("profile") or {}).items():
        conn.execute(
            "INSERT INTO user_profile (key, value) VALUES (%s, %s)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
    conn.commit()
    for ep in (setup.get("episodes") or []):
        from app.memory.episode import _to_bytes
        from app.match import semantic

        vecs = semantic.embed([f"{ep.get('topic')}｜{ep.get('summary')}"])
        conn.execute(
            """
            INSERT INTO episode (conversation_id, topic, summary, facts, embedding)
            VALUES (NULL, %s, %s, %s, %s)
            """,
            (
                ep.get("topic"),
                ep.get("summary"),
                Jsonb(ep.get("facts") or []),
                _to_bytes(vecs[0]) if vecs else None,
            ),
        )
    conn.commit()


def restore(conn, snap: dict) -> None:
    """精确还原到快照状态。顺序：会话 → 情节整表重建 → 画像。"""
    conn.execute("DELETE FROM conversation_message WHERE conversation_id > %s", (snap["conv_max"],))
    conn.execute("DELETE FROM conversation WHERE id > %s", (snap["conv_max"],))
    conn.execute("DELETE FROM user_profile_history WHERE id > %s", (snap["hist_max"],))
    # 情节整表重建：先删后插。两个坑（都被 ctx 套件抓过）：
    #   ① facts 是 JSONB 列，Python list 必须用 Jsonb 适配——裸 list 会被当成
    #      PG 数组语法，JSONB 解析直接报错；
    #   ② 引用已删会话的行 conversation_id 回填 NULL（外键），否则插不进去。
    conn.execute("DELETE FROM episode")
    for (eid, conv_id, topic, summary, facts, embedding, created_at,
         last_hit_at, hit_count) in snap["episodes_full"]:
        conv_ok = None
        if conv_id is not None:
            conv_ok = conn.execute(
                "SELECT 1 FROM conversation WHERE id = %s", (conv_id,)
            ).fetchone()
        conn.execute(
            """
            INSERT INTO episode (id, conversation_id, topic, summary, facts, embedding,
                                 created_at, last_hit_at, hit_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                eid,
                conv_id if conv_ok else None,
                topic, summary,
                Jsonb(facts) if facts is not None else None,
                embedding,
                created_at, last_hit_at, hit_count,
            ),
        )
    conn.execute("SELECT setval('episode_id_seq', (SELECT coalesce(max(id), 1) FROM episode))")
    conn.execute("DELETE FROM user_profile")
    for key, row in snap["profile"].items():
        conn.execute(
            "INSERT INTO user_profile (key, value, evidence, modality, confidence, conversation_id)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (key, json.dumps(row["value"], ensure_ascii=False) if row["value"] is not None else None,
             row["evidence"], row["modality"], row["confidence"], row["conversation_id"]),
        )
    conn.commit()


# ---------- 断言 ----------


def read_profile(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM user_profile").fetchall()}


def evaluate_expectations(conn, case: dict, turns_out: list[dict], hist_before: int) -> list[str]:
    problems: list[str] = []
    expect = case.get("expect") or {}
    profile = read_profile(conn)
    hist_after = conn.execute("SELECT count(*) FROM user_profile_history WHERE id > %s",
                              (hist_before,)).fetchone()[0]

    def fmt(value) -> str:
        return json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value

    if "profile_equals" in expect:
        for key, want in expect["profile_equals"].items():
            got = profile.get(key)
            if got != want:
                problems.append(f"画像 {key} = {fmt(got)} ≠ 期望 {fmt(want)}")
    if "profile_contains" in expect:
        for key, wants in expect["profile_contains"].items():
            got = profile.get(key) or []
            got_list = got if isinstance(got, list) else [got]
            for w in wants:
                if w not in got_list:
                    problems.append(f"画像 {key} 缺少 {fmt(w)}（实际 {fmt(got_list)}）")
    if "profile_absent" in expect:
        for key in expect["profile_absent"]:
            if key in profile and profile.get(key) not in (None, [], ""):
                problems.append(f"画像 {key} 不该有值，实际 {fmt(profile.get(key))}")
    if "history_rows" in expect and hist_after != expect["history_rows"]:
        problems.append(f"画像历史增量 {hist_after} ≠ 期望 {expect['history_rows']}")
    if "history_rows_min" in expect and hist_after < expect["history_rows_min"]:
        problems.append(f"画像历史增量 {hist_after} < 期望下限 {expect['history_rows_min']}")

    replies = [t["reply"] for t in turns_out]
    all_text = "\n".join(replies)
    last = replies[-1] if replies else ""
    if "reply_contains" in expect:
        for sub in expect["reply_contains"]:
            if sub not in all_text:
                problems.append(f"回复里没有「{sub}」")
    if "reply_last_contains" in expect:
        for sub in expect["reply_last_contains"]:
            if sub not in last:
                problems.append(f"最后一轮回复里没有「{sub}」")
    if "reply_not_contains" in expect:
        for sub in expect["reply_not_contains"]:
            if sub in all_text:
                problems.append(f"回复里出现了「{sub}」")
    if "reply_max_chars" in expect and len(last) > expect["reply_max_chars"]:
        problems.append(f"最后一轮回复 {len(last)} 字 > 上限 {expect['reply_max_chars']}")
    if "jobs_empty_last" in expect:
        empty = not (turns_out[-1]["jobs"] if turns_out else [])
        if empty != expect["jobs_empty_last"]:
            problems.append(f"最后一轮岗位卡片为空 = {empty}，期望 {expect['jobs_empty_last']}")
    if "steps_last_contains" in expect:
        steps_text = "\n".join(turns_out[-1]["steps"]) if turns_out else ""
        for sub in expect["steps_last_contains"]:
            if sub not in steps_text:
                problems.append(f"最后一轮执行进度里没有「{sub}」（进度：{steps_text[:120]}）")
    return problems


# ---------- 执行 ----------


def run_case(client, conn, case: dict) -> dict:
    started = time.monotonic()
    snap = snapshot(conn)
    turns_out: list[dict] = []
    error: str | None = None
    try:
        apply_setup(conn, case.get("setup") or {})
        conversation_id = None
        for text in case["turns"]:
            resp = client.post(
                "/api/chat",
                json={"message": text, "conversation_id": conversation_id},
            )
            if resp.status_code != 200:
                error = f"第 {len(turns_out) + 1} 轮接口 {resp.status_code}：{resp.text[:200]}"
                break
            data = resp.json()
            conversation_id = data.get("conversation_id") or conversation_id
            turns_out.append({
                "text": text,
                "reply": data.get("reply") or "",
                "jobs": data.get("jobs") or [],
                "steps": data.get("steps") or [],
                "direction_hits": data.get("direction_hits"),
                # 结构化 Trace 落盘（多路召回 ID 分离 + 截断标记 + 降级标记）
                "trace": data.get("trace") or {},
            })
        problems = [error] if error else evaluate_expectations(conn, case, turns_out, snap["hist_max"])
    finally:
        try:
            restore(conn, snap)
        except Exception as exc:  # noqa: BLE001
            # 还原失败后事务处于 aborted 状态——必须 rollback，否则
            # 下一个场景的 snapshot 会 InFailedSqlTransaction 整个套件中断
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            print(f"  !! 还原失败，请手工检查 user_profile/episode：{exc}")

    return {
        "id": case["id"],
        "desc": case.get("desc", ""),
        "note": case.get("note", ""),
        "ok": not problems,
        "failures": problems,
        "turns": turns_out,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def _save_scenario_report(out: Path, results: list[dict], elapsed_s: float) -> dict:
    passed = sum(1 for r in results if r.get("ok"))
    failed = len(results) - passed
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "elapsed_s": round(elapsed_s, 1),
        "results": results,
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _shown_path(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def main() -> int:
    parser = argparse.ArgumentParser(description="会话级端到端评测")
    parser.add_argument("--dry", action="store_true", help="只校验场景文件结构，不调模型不连库")
    parser.add_argument("--filter", default="", help="只跑 id 含该子串的场景")
    parser.add_argument("--scenarios", default=None, help="场景文件路径；缺省 eval/scenarios.jsonl")
    parser.add_argument("--resume", default=None, help="从已有报告断点续跑，跳过其中已通过的场景")
    args = parser.parse_args()

    cases = load_scenarios(Path(args.scenarios) if args.scenarios else None)
    problems = validate(cases)
    if problems:
        for p in problems:
            print(f"[SCHEMA] {p}")
        return 1
    if args.dry:
        print(f"场景文件结构 OK：{len(cases)} 条，断言键全部合法")
        return 0

    if args.filter:
        cases = [c for c in cases if args.filter in c["id"]]
    if not cases:
        print(f"没有匹配 --filter {args.filter!r} 的场景")
        return 1

    from fastapi.testclient import TestClient
    import psycopg

    from app.config import settings
    from app.main import app

    RESULTS_DIR.mkdir(exist_ok=True)
    done_map: dict[str, dict] = {}
    if args.resume:
        out = Path(args.resume).resolve()
        prev = json.loads(out.read_text(encoding="utf-8"))
        # 只跳过已通过的场景；失败或中断未录完的都在本次重跑
        done_map = {r["id"]: r for r in (prev.get("results") or []) if r.get("ok")}
    else:
        out = RESULTS_DIR / f"scenarios_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    results_by_id: dict[str, dict] = dict(done_map)
    started = time.monotonic()

    with psycopg.connect(settings.database_url) as conn, TestClient(app) as client:
        for case in cases:
            cid = case["id"]
            if cid in done_map:
                print(f"\n== {cid}：已通过，续跑跳过")
                continue
            print(f"\n== {cid}：{case.get('desc', '')}")
            result = run_case(client, conn, case)
            results_by_id[cid] = result
            if result["ok"]:
                print(f"[PASS] ({result['elapsed_s']}s)")
            else:
                for f in result["failures"]:
                    print(f"[FAIL] {f}")
            for t in result["turns"]:
                shown = t["reply"][:60].replace("\n", " ")
                print(f"    > {t['text']}  ⇒  {shown}{'…' if len(t['reply']) > 60 else ''}")
            # 每跑完一条即时落盘（Checkpointing），中断不丢前面成果
            ordered = [results_by_id[c["id"]] for c in cases if c["id"] in results_by_id]
            _save_scenario_report(out, ordered, time.monotonic() - started)

    ordered = [results_by_id[c["id"]] for c in cases if c["id"] in results_by_id]
    report = _save_scenario_report(out, ordered, time.monotonic() - started)

    print(f"\n{report['passed']}/{len(ordered)} 通过，{report['elapsed_s']}s，报告已写入 {_shown_path(out)}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
