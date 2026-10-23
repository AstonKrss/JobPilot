"""API 端到端冒烟测试。

为什么需要：项目里已有的测试都是"某个算法对不对"（匹配、事实核查、扩展选择器），
**接口层一直没有自动验证** —— 改完代码全靠手动 curl，忘了测就带病上线。
这份脚本把每个 GET 接口和几个不花钱的 POST 过一遍，检查状态码和关键字段。

原则：
1. 只测**不花钱**的路径。需要调模型的接口（匹配重建、建议生成）一律走缓存或跳过。
2. 断言"字段在不在"，不断言"值是多少"——值会变，结构不该变。
3. 任何一项失败就返回非零，方便接 CI。

用法：.venv/Scripts/python.exe scripts/check_api.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

pass_count = 0
fail_count = 0


def ok(msg: str) -> None:
    global pass_count
    pass_count += 1
    print(f"[PASS] {msg}")


def bad(msg: str, detail: str = "") -> None:
    global fail_count
    fail_count += 1
    print(f"[FAIL] {msg}" + (f" — {detail}" if detail else ""))


def call(method: str, path: str, body: dict | None = None, timeout: int = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.load(resp)


def check(method: str, path: str, keys: list[str], body: dict | None = None, timeout: int = 120) -> dict | None:
    """请求并检查返回里是否包含指定字段。"""
    try:
        status, data = call(method, path, body, timeout)
    except urllib.error.HTTPError as exc:
        bad(f"{method} {path}", f"HTTP {exc.code}")
        return None
    except Exception as exc:  # noqa: BLE001
        bad(f"{method} {path}", str(exc)[:80])
        return None

    if status != 200:
        bad(f"{method} {path}", f"HTTP {status}")
        return None
    missing = [k for k in keys if k not in data]
    if missing:
        bad(f"{method} {path}", f"缺少字段 {missing}")
        return None
    ok(f"{method} {path}")
    return data


def main() -> int:
    # 先确认服务活着
    health = check("GET", "/health", ["ok", "db"])
    if health is None:
        print("\n后端没起来，先启动服务再跑这个脚本")
        return 1
    if not health.get("db"):
        bad("数据库连接", "health 里 db=false")
        return 1

    # 简历
    resumes = check("GET", "/api/resumes", ["count", "items"])
    resume_id = None
    if resumes and resumes.get("items"):
        with_profile = [r for r in resumes["items"] if r.get("has_profile")]
        if with_profile:
            resume_id = with_profile[0]["id"]
            item = with_profile[0]
            if "is_default" in item:
                ok("简历条目带 is_default 字段")
            else:
                bad("简历条目带 is_default 字段")
    if resume_id:
        check("GET", f"/api/resumes/{resume_id}", ["profile"])

    # 岗位
    jobs = check("GET", "/api/jobs", ["count", "items"])
    job_id = jobs["items"][0]["id"] if jobs and jobs.get("items") else None
    if job_id:
        check("GET", f"/api/jobs/{job_id}", ["profile"])

    # 匹配（走缓存：第一次可能慢，之后快）
    if resume_id:
        m = check(
            "GET",
            f"/api/resumes/{resume_id}/matches?include_rejected=true&job_id={job_id or 1}",
            ["items", "counts", "cached"],
            timeout=300,
        )
        if m and m.get("items"):
            item = m["items"][0]
            # 分层改造后的关键字段，缺了说明返回结构回退了
            for key in ("confidence", "info_missing", "score_note"):
                if key in item:
                    ok(f"匹配条目含 {key}")
                else:
                    bad(f"匹配条目含 {key}")

    # 投递跟踪
    check("GET", "/api/applications", ["count", "items"])
    check("GET", "/api/applications/stats", ["tracked", "status_counts"])
    if job_id:
        check("GET", f"/api/applications/by-job/{job_id}", ["exists"])

    # 聊天记录、建议、画像、记忆
    if job_id:
        check("GET", f"/api/chat-logs?job_id={job_id}", ["count", "items"])
    if resume_id:
        check("GET", f"/api/resumes/{resume_id}/review/latest", ["exists"])
    check("GET", "/api/profile", ["profile", "history"])
    check("GET", "/api/memory/overview", ["profile_count", "episode_total"])

    q = urllib.parse.quote("我想找后端岗位")
    check("GET", f"/api/memory/recall?q={q}&top_k=1", ["items"])

    # 下一步建议：只走规则，不花钱
    if job_id:
        step = check(
            "POST", "/api/workbench/next-step",
            ["action", "reason", "urgency"],
            {"job_id": job_id, "match_decision": "pass", "app_status": "applied", "chat_count": 0},
        )
        if step and step.get("action"):
            ok(f"下一步建议内容：{step['action']}")
        # 状态机边界：reject 应该劝退
        rejected = call("POST", "/api/workbench/next-step",
                        {"job_id": job_id, "match_decision": "reject"})[1]
        if "放弃" in rejected.get("action", ""):
            ok("reject 状态给出劝退建议")
        else:
            bad("reject 状态给出劝退建议", rejected.get("action", ""))

    # 画像观察：只跑规则预筛（故意用闲聊，不触发模型）
    obs = check(
        "POST", "/api/profile/observe", ["triggered"],
        {"text": "今天天气不错"},
    )
    if obs is not None:
        if obs.get("triggered") is False:
            ok("闲聊被规则预筛挡住（没有调用模型）")
        else:
            bad("闲聊被规则预筛挡住", "居然触发了模型调用")

    # 错误处理：不存在的资源要 404，不能 500
    try:
        call("GET", "/api/resumes/999999")
        bad("不存在的简历返回 404", "居然返回 200")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            ok("不存在的简历返回 404")
        else:
            bad("不存在的简历返回 404", f"HTTP {exc.code}")

    print(f"\n{pass_count}/{pass_count + fail_count} 通过")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
