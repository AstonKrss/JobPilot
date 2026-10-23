"""放宽探索：当硬约束全线不满足时，回答"差在哪、放宽哪一条能通"。

为什么需要这一层
    匹配给出一堆"否决"，对求职者是不够的。真正有用的是
    「你被卡在城市上；如果接受杭州/深圳，有 12 个岗位能投，其中 3 个匹配度最高」。
    只说"全部否决"等于把问题原样丢回给用户。

分工
    **放宽后的结果由规则算**（零成本、可复现、可解释）：把某条硬约束临时放开，
    重跑一遍匹配，看能多放进来几个、都是哪些。
    **人话结论由模型写**：把上面的数字变成三到五条可执行建议。

放宽的四个维度
    - 城市：接受异地
    - 年限：把岗位的年限要求视作没有（注意：年限本来是软约束，
      这里放开只是不因"经验不足"扣分，不是把门槛抹掉）
    - 工作性质：接受实习 / 外包 / 兼职
    - 学历：忽略学历层次（只作探索，不改硬约束本身）

注意这里**不改变产品的判定口径**：正式匹配仍然按原规则否决；
放宽只是给用户一张"如果……会怎样"的地图。
"""

from __future__ import annotations

import logging

from ..llm.deepseek import chat_json, parse_json_object
from .engine import Decision, rank

logger = logging.getLogger(__name__)

ADVICE_SYSTEM_PROMPT = """你是求职顾问。给你一份"放宽约束后"的匹配统计，
请写出 3 到 5 条对求职者最有用的建议。

要求：
1. 每条一行，以「- 」开头，说清"放宽什么、能多看几个岗位、其中最值得看的是哪个"。
2. 不要编造数据。只用给出的数字与岗位信息。
3. 如果放宽某个维度几乎不改变结果，就直接说它不是瓶颈，不要凑数。
4. 语气直接，不说套话。控制在 200 字以内。
5. 只输出一个 JSON 对象：{"advice": "这几行建议，用换行分隔"}，不要 Markdown 代码块。"""


def _relaxed_jobs(jobs: list[dict], dimension: str) -> list[dict]:
    """按维度构造放宽后的岗位副本。"""
    out: list[dict] = []
    for job in jobs:
        item = dict(job)
        if dimension == "edu":
            item["edu_level"] = None
            item["edu_fulltime_required"] = None
        elif dimension == "exp":
            item["exp_min"] = None
            item["exp_max"] = None
        out.append(item)
    return out


def _relaxed_prefs(prefs: dict, dimension: str) -> dict:
    out = dict(prefs)
    if dimension == "city":
        out["accept_relocation"] = True
    elif dimension == "nature":
        out["accept_internship"] = True
        out["accept_outsourcing"] = True
        out["accept_parttime"] = True
    return out


DIMENSIONS = [
    ("city", "接受异地"),
    ("exp", "放宽年限要求"),
    ("nature", "接受实习/外包/兼职"),
    ("edu", "忽略学历层次"),
]


def compute_relaxations(
    resume_profile: dict, jobs: list[dict], prefs: dict
) -> dict:
    """算出四个放宽维度各自能多放进来哪些岗位。纯规则，无模型调用。"""
    base_results = rank(resume_profile, jobs, prefs)
    base_by_job = {r.job_id: r for r in base_results}
    base_pass = {r.job_id for r in base_results if r.decision is not Decision.REJECT}

    variants: list[dict] = []
    for dimension, label in DIMENSIONS:
        results = rank(
            resume_profile,
            _relaxed_jobs(jobs, dimension),
            _relaxed_prefs(prefs, dimension),
        )
        opened: list[dict] = []
        for result in results:
            if result.decision is Decision.REJECT:
                continue
            if result.job_id in base_pass:
                continue
            base = base_by_job.get(result.job_id)
            blockers = [
                c.label + "：" + c.detail
                for c in (base.checks if base else [])
                if c.decision is Decision.REJECT
            ]
            opened.append(
                {
                    "job_id": result.job_id,
                    "title": result.title,
                    "company": result.company,
                    "city": next(
                        (j.get("city") for j in jobs if j.get("job_id") == result.job_id),
                        None,
                    ),
                    "score": result.score,
                    "decision": result.decision.value,
                    "was_blocked_by": blockers,
                    "missed_skills": result.missed_skills,
                }
            )

        opened.sort(key=lambda item: (item["score"] or 0), reverse=True)
        pass_count = sum(1 for r in results if r.decision is Decision.PASS)
        confirm_count = sum(1 for r in results if r.decision is Decision.NEED_CONFIRM)
        variants.append(
            {
                "dimension": dimension,
                "label": label,
                "pass_count": pass_count,
                "confirm_count": confirm_count,
                "base_pass_count": sum(
                    1 for r in base_results if r.decision is Decision.PASS
                ),
                "opened": opened[:6],
                "opened_total": len(opened),
            }
        )

    return {
        "base": {
            "total": len(base_results),
            "pass": sum(1 for r in base_results if r.decision is Decision.PASS),
            "confirm": sum(1 for r in base_results if r.decision is Decision.NEED_CONFIRM),
            "reject": sum(1 for r in base_results if r.decision is Decision.REJECT),
        },
        "variants": variants,
    }


def _advice_prompt(report: dict) -> str:
    base = report["base"]
    lines = [
        f"当前状态：{base['total']} 个岗位中，通过 {base['pass']}、待确认 {base['confirm']}、否决 {base['reject']}。",
        "",
    ]
    for variant in report["variants"]:
        lines.append(
            f"【{variant['label']}】通过数 {base['pass']} → {variant['pass_count']}，"
            f"新增可投 {variant['opened_total']} 个"
        )
        for item in variant["opened"][:3]:
            lines.append(
                f"   - {item['title']}（{item['city'] or '城市未标'}，{item['score']} 分）"
                f"，原先卡在：{'、'.join(item['was_blocked_by'][:2]) or '未记录'}"
            )
    return "\n".join(lines)


async def narrate(report: dict) -> str:
    """把统计变成三到五条人话建议。失败时返回空串，不影响报告本身。"""
    try:
        result = await chat_json(
            ADVICE_SYSTEM_PROMPT,
            _advice_prompt(report),
            what="放宽建议",
            max_tokens=1200,
            # 同样的统计报告就该给出同样的建议，重复请求不必再花钱
            cache_tag="match_relax",
        )
        text = parse_json_object(result.text, what="放宽建议")
        if isinstance(text, dict):
            for key in ("advice", "result", "content", "text"):
                if isinstance(text.get(key), str):
                    return text[key]
            return ""
        return str(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("放宽建议生成失败，仅返回统计：%s", exc)
        return ""
