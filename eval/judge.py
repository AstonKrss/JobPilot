"""LLM-as-judge：对场景评测产出的回复做质量评判。

它和 run_scenarios.py 硬断言的分工
    硬断言回答「对不对」：画像写没写、路由走没走对、URL 贴没贴。
    judge 回答「好不好」：有没有编造结果里不存在的岗位、语气有没有把
    投不进的岗位当推荐、说没说清差什么、有没有回应这轮的问题。
    这四件事靠正则断言会误伤（自然语言变体太多），靠人眼看不可回归，
    所以交给模型按固定 rubric 打分。

方法学限制（写在最前面，防止误读数字）
    judge 与被评系统目前共用同一个模型（网关只有这一个），同源自评
    有系统性偏差 —— 自己的错误自己看不见。所以这里的数字用于**回归
    对比**（改 prompt / 改链路前后各跑一遍，看维度通过率的变化），
    不作为绝对质量声明。网关以后有更强的模型时，换 judge 只需在
    .env 里加 JUDGE_MODEL 并同步 deepseek.py 的模型参数。

用法：
    .venv/Scripts/python.exe eval/judge.py                          # 评最新一份 scenarios 报告
    .venv/Scripts/python.exe eval/judge.py eval/results/scenarios_xxx.json
    .venv/Scripts/python.exe eval/judge.py --compare eval/results/judged_旧.json  # 与上次对比
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.llm.deepseek import chat_json, parse_json_object  # noqa: E402

RESULTS_DIR = ROOT / "eval" / "results"

JUDGE_SYSTEM_PROMPT = """你是「求职助手」回复质量的评审。给定一轮真实对话的用户消息、助手回复、
以及助手当时手里的检索结果（事实依据），按四个维度打分。只输出 JSON：

{"fabrication": 1, "tone": 1, "relevance": 1, "clarity": 1, "clarity_applicable": true,
 "reason": "一句话依据"}

维度定义（1 = 通过，0 = 不通过）：

1. fabrication 编造
   回复中出现的岗位名、公司名、分数、城市、技能，必须能在「检索结果」里找到；
   出现结果里没有的具体岗位或数字 → 0。泛指（"有几个岗位""分数不高"）不算编造。
   把结果里的信息换一种说法复述不算编造。

2. tone 语气
   检索结果为空、或明确说"没有能投的"时，回复必须直说，不得用"最值得看""建议优先"
   推销不属于用户方向的岗位；方向内正常推荐 → 1。列出"方向对但投不进"的岗位并
   说明卡点，是正确行为，不算推销。**助手按产品定位把与求职无关的请求（写代码等）
   一句话拉回求职主题，也不算推销** ——「检索为空」指的是用户问了岗位而手里没结果的情形。

3. relevance 贴题
   回复是否回应了用户这一轮的问题。答非所问、自顾自推销别的东西 → 0。
   **例外：用户提出与求职无关的请求时，简短回应并拉回求职主题算贴题**（产品设定的
   正确行为，不执行任务本身）。

4. clarity 说清缺口（clarity_applicable = false 时忽略此项）
   当检索结果里存在"方向对但投不进"的岗位（result 里 decision 为 reject 的条目），
   回复是否说清了卡在哪（城市不符 / 缺哪些技能）。没有这类岗位时 clarity_applicable 填 false。

严格：只依据给出的材料判断，不要脑补助手"可能"拿到了什么。reason 一句话，最长 40 字。"""


def latest_scenarios_report() -> Path:
    files = sorted(RESULTS_DIR.glob("scenarios_*.json"))
    if not files:
        raise SystemExit(f"eval/results/ 下没有 scenarios_*.json，先跑 eval/run_scenarios.py")
    return files[-1]


def turns_from_report(report: dict) -> list[dict]:
    out: list[dict] = []
    for scenario in report.get("results") or []:
        for i, turn in enumerate(scenario.get("turns") or [], 1):
            out.append({
                "scenario_id": scenario.get("id"),
                "desc": scenario.get("desc", ""),
                "turn_index": i,
                "user": turn.get("text", ""),
                "reply": turn.get("reply", ""),
                "jobs": turn.get("jobs") or [],
            })
    return out


def _jobs_block(jobs: list[dict]) -> str:
    if not jobs:
        return "（检索结果为空）"
    lines = []
    for j in jobs:
        blockers = "；".join(j.get("blockers") or [])
        missed = "、".join((j.get("missed_skills") or [])[:4])
        extra = (f" 卡在：{blockers}。" if blockers else "") + (f" 缺技能：{missed}。" if missed else "")
        lines.append(
            f"- {j.get('title')}（{j.get('company') or '公司未知'}，{j.get('city') or '城市未知'}，"
            f"{j.get('score') if j.get('score') is not None else '-'} 分，decision={j.get('decision')}）{extra}"
        )
    return "检索结果：\n" + "\n".join(lines)


async def judge_turn(item: dict) -> dict:
    user_msg = (
        f"用户消息：{item['user']}\n\n助手回复：\n{item['reply']}\n\n"
        f"{_jobs_block(item['jobs'])}"
    )
    result = await chat_json(JUDGE_SYSTEM_PROMPT, user_msg, what="回复评审", max_tokens=300)
    payload = parse_json_object(result.text, what="回复评审")

    def bit(key: str) -> int:
        v = payload.get(key)
        return 1 if v == 1 or v == "1" or v is True else 0

    return {
        "fabrication": bit("fabrication"),
        "tone": bit("tone"),
        "relevance": bit("relevance"),
        "clarity": bit("clarity"),
        "clarity_applicable": payload.get("clarity_applicable") is not False,
        "reason": str(payload.get("reason") or "")[:80],
        "judge_ok": True,
    }


def aggregate(verdicts: list[dict]) -> dict:
    ok = [v for v in verdicts if v.get("judge_ok")]

    def rate(key: str) -> tuple[float | None, int]:
        if key == "clarity":
            applicable = [v for v in ok if v.get("clarity_applicable")]
        else:
            applicable = ok
        if not applicable:
            # n=0 不代表满分：全都没评上（judge 全挂）时必须显示"没有数据"，
            # 否则一次全挂的运行看起来像一次全过的运行
            return (None, 0)
        return (sum(v[key] for v in applicable) / len(applicable), len(applicable))

    fab, n1 = rate("fabrication")
    ton, n2 = rate("tone")
    rel, n3 = rate("relevance")
    cla, n4 = rate("clarity")
    overall = None
    if ok:
        passed = sum(
            1
            for v in ok
            if v["fabrication"] == 1 and v["tone"] == 1 and v["relevance"] == 1
            and (v["clarity"] == 1 or not v["clarity_applicable"])
        )
        overall = passed / len(ok)
    return {
        "fabrication_pass": None if fab is None else round(fab, 3), "fabrication_n": n1,
        "tone_pass": None if ton is None else round(ton, 3), "tone_n": n2,
        "relevance_pass": None if rel is None else round(rel, 3), "relevance_n": n3,
        "clarity_pass": None if cla is None else round(cla, 3), "clarity_n": n4,
        "turn_pass": None if overall is None else round(overall, 3),
        "turns_judged": len(ok),
        "judge_errors": len(verdicts) - len(ok),
    }


def _fmt(value) -> str:
    return "—" if value is None else f"{value:.3f}"


def compare(cur: dict, prev: dict) -> None:
    print("\n== 与上次对比 ==")
    for key, label in (
        ("fabrication_pass", "编造"), ("tone_pass", "语气"),
        ("relevance_pass", "贴题"), ("clarity_pass", "缺口"), ("turn_pass", "整体"),
    ):
        old, new = prev.get(key), cur.get(key)
        if old is None or new is None:
            print(f"  {label}: 旧 {_fmt(old)} / 新 {_fmt(new)}（某一边没有数据）")
            continue
        delta = new - old
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        print(f"  {label}: {old:.3f} → {new:.3f} {arrow} {delta:+.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM-as-judge 回复质量评判")
    parser.add_argument("report", nargs="?", help="scenarios 报告路径；缺省用最新一份")
    parser.add_argument("--compare", help="与某份 judged_*.json 对比维度通过率")
    args = parser.parse_args()

    report_path = Path(args.report) if args.report else latest_scenarios_report()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    items = turns_from_report(report)
    if not items:
        raise SystemExit(f"{report_path.name} 里没有可评的轮次")

    import asyncio

    verdicts: list[dict] = []
    print(f"评审 {len(items)} 轮回复（来源 {report_path.name}）…")

    async def run_all() -> None:
        for item in items:
            try:
                v = await judge_turn(item)
            except Exception as exc:  # noqa: BLE001
                v = {"judge_ok": False, "error": str(exc)[:120]}
                print(f"  [JUDGE-ERR] {item['scenario_id']} 第 {item['turn_index']} 轮：{v['error']}")
            v.update({"scenario_id": item["scenario_id"], "turn_index": item["turn_index"],
                      "user": item["user"], "reply": item["reply"]})
            verdicts.append(v)
            if v.get("judge_ok"):
                flags = "".join(
                    "!" if v[k] == 0 else "."
                    for k in ("fabrication", "tone", "relevance")
                )
                cla = "na" if not v["clarity_applicable"] else ("." if v["clarity"] == 1 else "!")
                print(f"  [{flags}{cla}] {item['scenario_id']} #{item['turn_index']}  {v['reason']}")

    asyncio.run(run_all())

    summary = aggregate(verdicts)
    out = RESULTS_DIR / f"judged_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(
        json.dumps({
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source_report": report_path.name,
            "summary": summary,
            "verdicts": verdicts,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n== 维度通过率（{summary['turns_judged']} 轮，judge 失败 {summary['judge_errors']}）==")
    print(f"  编造: {_fmt(summary['fabrication_pass'])} (n={summary['fabrication_n']})")
    print(f"  语气: {_fmt(summary['tone_pass'])} (n={summary['tone_n']})")
    print(f"  贴题: {_fmt(summary['relevance_pass'])} (n={summary['relevance_n']})")
    print(f"  缺口: {_fmt(summary['clarity_pass'])} (n={summary['clarity_n']})")
    print(f"  整体: {_fmt(summary['turn_pass'])}")
    print(f"报告已写入 {out.relative_to(ROOT)}")

    if args.compare:
        prev = json.loads(Path(args.compare).read_text(encoding="utf-8")).get("summary") or {}
        compare(summary, prev)
    return 0 if summary["judge_errors"] == 0 and summary["turn_pass"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
