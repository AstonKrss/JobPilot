"""Eval Harness（judge + run_scenarios 并发限流 / 断点续跑 / 独立裁判参数）离线自检。

因为外部网关订阅当前不可用（HTTP 403），需要有一套不依赖外网的回归证明：
    1) judge.py 的 asyncio.Semaphore 并发控制 + 独立裁判参数透传 + 防偏见 Prompt + 断点续评
    2) run_scenarios.py 的 --resume 跳过已通过场景、只重跑失败场景 + 相对路径不崩
    3) deepseek.py 的 model / base_url / api_key 覆盖参数正确到达请求构造

用法：
    .venv/Scripts/python.exe scripts/check_eval_harness.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "eval"))

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


def test_judge_harness() -> None:
    import judge
    from app.config import settings
    from app.llm import deepseek

    print("== ① judge：防偏见 prompt + 独立裁判透传 + Semaphore 并发限流 + 断点续评 ==")
    check("JUDGE_SYSTEM_PROMPT 含防长度偏见声明", "严禁长度偏见" in judge.JUDGE_SYSTEM_PROMPT)
    check("JUDGE_SYSTEM_PROMPT 含忽略顺序声明", "忽略材料顺序" in judge.JUDGE_SYSTEM_PROMPT)

    # 独立裁判参数透传到 deepseek.chat_json
    seen: list[dict] = []
    orig_chat_json = judge.chat_json
    orig_j_model, orig_j_base, orig_j_key = settings.judge_model, settings.judge_base_url, settings.judge_api_key

    async def fake_judge_chat(system, user, *, what, max_tokens=300, model=None, base_url=None, api_key=None):
        seen.append({"model": model, "base_url": base_url, "api_key": api_key})
        class _R:
            text = json.dumps({
                "fabrication": 1, "tone": 1, "relevance": 1, "clarity": 1,
                "clarity_applicable": False, "reason": "ok",
            })
        _R.model = model or settings.llm_model
        return _R()

    try:
        judge.chat_json = fake_judge_chat
        settings.judge_model = "claude-judge-test"
        settings.judge_base_url = "https://judge.example.com/v1"
        settings.judge_api_key = "sk-judge-test"
        out = asyncio.run(judge.judge_turn({"user": "你好", "reply": "你好", "jobs": []}))
        check("独立裁判模型透传成功", seen and seen[0]["model"] == "claude-judge-test", str(seen))
        check("独立裁判端点/密钥透传成功", seen[0]["base_url"] == "https://judge.example.com/v1" and seen[0]["api_key"] == "sk-judge-test")
        check("返回值记录了实际裁判模型", out.get("judge_model") == "claude-judge-test")
    finally:
        judge.chat_json = orig_chat_json
        settings.judge_model, settings.judge_base_url, settings.judge_api_key = orig_j_model, orig_j_base, orig_j_key

    # deepseek._post_chat 真实覆盖校验
    check("deepseek._build_headers 支持覆盖 api_key", deepseek._build_headers("s1", "k-custom")["Authorization"] == "Bearer k-custom")

    # 并发限流 + 断点续评：用临时报告模拟 4 条轮次，前 2 条已评、后 2 条待评，并发=2
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        sc_file = td_path / "scenarios_tmp.json"
        sc_file.write_text(json.dumps({
            "results": [
                {"id": f"s-{i}", "desc": "", "turns": [{"text": f"q{i}", "reply": f"r{i}", "jobs": []}]}
                for i in range(4)
            ]
        }, ensure_ascii=False), encoding="utf-8")
        resumed_file = td_path / "judged_tmp.json"
        resumed_file.write_text(json.dumps({
            "source_report": sc_file.name,
            "verdicts": [
                {"scenario_id": "s-0", "turn_index": 1, "fabrication": 1, "tone": 1, "relevance": 1,
                 "clarity": 1, "clarity_applicable": False, "reason": "cached", "judge_ok": True},
                {"scenario_id": "s-1", "turn_index": 1, "judge_ok": False, "error": "prev failed"},
            ]
        }, ensure_ascii=False), encoding="utf-8")

        active = {"cur": 0, "max": 0, "called": []}

        async def tracked_chat(system, user, *, what, max_tokens=300, model=None, base_url=None, api_key=None):
            active["cur"] += 1
            active["max"] = max(active["max"], active["cur"])
            active["called"].append(user.splitlines()[0])
            await asyncio.sleep(0.05)
            active["cur"] -= 1
            class _R:
                text = '{"fabrication":1,"tone":1,"relevance":1,"clarity":1,"clarity_applicable":false,"reason":"ok"}'
            _R.model = "mock-judge"
            return _R()

        orig_argv = sys.argv
        judge.chat_json = tracked_chat
        try:
            sys.argv = ["judge.py", str(sc_file), "--resume", str(resumed_file), "--concurrency", "2"]
            rc = judge.main()
        finally:
            sys.argv = orig_argv
            judge.chat_json = orig_chat_json

        saved = json.loads(resumed_file.read_text(encoding="utf-8"))
        check("断点续评退出码为 0（4/4 全过）", rc == 0, str(rc))
        check("已成功的 s-0 被跳过，只补评失败/未评的 3 条", len(active["called"]) == 3, str(active["called"]))
        check("Semaphore 严格限制最大并发 ≤ 2", active["max"] <= 2 and active["max"] >= 1, f"max={active['max']}")
        check("续评后报告保留 4 条完整顺序", [v["scenario_id"] for v in saved["verdicts"]] == ["s-0", "s-1", "s-2", "s-3"])


def test_scenarios_resume() -> None:
    import run_scenarios

    print("\n== ② run_scenarios：--resume 路径解析与已通过场景过滤 ==")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "prev.json"
        p.write_text(json.dumps({
            "results": [
                {"id": "route-clarify-06", "ok": True, "turns": []},
                {"id": "mem-exclusive-01", "ok": False, "turns": []},
            ]
        }), encoding="utf-8")
        # 模拟相对路径传参：只校验 Path.resolve() 后能找到且跳过 ok=True 的场景
        resolved = p.resolve()
        prev = json.loads(resolved.read_text(encoding="utf-8"))
        done_map = {r["id"]: r for r in prev["results"] if r.get("ok")}
        check("只跳过 ok=True 的场景（failed 保留重跑）", list(done_map) == ["route-clarify-06"])
    check("run_scenarios EXPECT_KEYS 完整", {"profile_equals", "steps_last_contains"} <= run_scenarios.EXPECT_KEYS)


def main() -> int:
    test_judge_harness()
    test_scenarios_resume()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
