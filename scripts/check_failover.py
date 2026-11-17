"""Failover 故障注入自检：切断模型/向量/语义判定，验证系统按设计优雅降级。

这组测试补全了「检查项 10：异常兜底能力」：
    之前代码里写了降级逻辑，但测试套件只验证过正常路径（或者在 gap 里测过单点）。
    这里用 monkeypatch 把关键外部依赖全部弄坏（完全离线，不需真实模型、不写污染数据），
    硬断言四条降级链路都满足：不抛 500、返回结构完整、降级标记进入 trace["degraded"]。

四组注入场景：
    ① PlanAgent 挂掉（网络断开/超时） → 沿用历史意图或退回 clarify，不 500，标 plan_fallback
    ② 语义判定挂掉（429/模型异常）   → 自动退回纯规则匹配，标 semantic_judge_fallback
    ③ 本地向量模型返回 None           → 字面召回照常工作，标 vector_unavailable
    ④ Compose 模型挂掉                → 退回模板拼装回复（带降级提示），标 compose_template_fallback

用法：
    .venv/Scripts/python.exe scripts/check_failover.py
"""

from __future__ import annotations

import asyncio
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


def _fake_jobs() -> list[dict]:
    return [
        {
            "job_id": 101,
            "title": "Java后端实习生",
            "company": "测试A公司",
            "city": "佛山",
            "job_nature": "实习",
            "edu_level": "本科",
            "edu_fulltime_required": None,
            "exp_min": 0,
            "exp_max": 1,
            "must_have_skills": ["Java", "Spring Boot"],
            "nice_to_have_skills": [],
            "detail_level": "full",
            "url": "https://example.com/101",
        },
        {
            "job_id": 102,
            "title": "Python后端开发工程师",
            "company": "测试B公司",
            "city": "佛山",
            "job_nature": "全职",
            "edu_level": "本科",
            "edu_fulltime_required": None,
            "exp_min": 3,
            "exp_max": None,
            "must_have_skills": ["Python", "FastAPI"],
            "nice_to_have_skills": [],
            "detail_level": "full",
            "url": "https://example.com/102",
        },
    ]


def _fake_profile() -> dict:
    return {
        "resume_id": 6,
        "file_name": "test.pdf",
        "basic": {"edu_level": "本科", "edu_fulltime": True, "work_years": 0, "current_city": "佛山"},
        "skills": [{"name": "Java"}, {"name": "Spring Boot"}],
        "experiences": [],
        "projects": [],
    }


class _DummyConn:
    """不连真实 PostgreSQL：只提供 agent.py 里读写的最小 stub，保证测试完全离线且零污染。"""

    def __init__(self, conv_slots: dict | None = None):
        self._slots = dict(conv_slots or {})
        self.messages: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, *args, **kwargs):
        class _Res:
            def fetchone(self):
                return None

            def fetchall(self):
                return []

        return _Res()

    def commit(self):
        pass


class _DummyPool:
    def __init__(self, conn: _DummyConn):
        self._conn = conn

    def connection(self):
        return self._conn


def run_failover_cases() -> None:
    from app.chat import agent as ag
    from app.db import conversation_repo, match_repo, repo
    from app.memory import profile_repo

    # 保存原始引用，finally 里完整恢复
    orig_pool = ag.get_pool
    orig_conv_get = conversation_repo.get
    orig_conv_create = conversation_repo.create
    orig_conv_win = conversation_repo.recent_window
    orig_conv_add = conversation_repo.add_message
    orig_conv_slots = conversation_repo.update_slots
    orig_prof_flat = profile_repo.get_flat_profile
    orig_all_prof = ag._all_profiles
    orig_list_jobs = match_repo.list_current_job_profiles
    orig_def_rid = repo.get_default_resume_id
    orig_chat_json = ag.chat_json
    orig_align = ag.align_skills
    orig_embed = ag.embed

    conv = _DummyConn()
    pool = _DummyPool(conv)

    try:
        ag.get_pool = lambda: pool
        state = {"slots": {"intent": "后端实习", "cities": ["佛山"]}}
        conversation_repo.get = lambda c, cid: {"id": cid, "slots": dict(state["slots"])}
        conversation_repo.create = lambda c, t, s: 1
        conversation_repo.recent_window = lambda c, cid, n: [{"role": "user", "content": "找佛山后端实习"}]
        conversation_repo.add_message = lambda c, cid, role, content, meta=None: conv.messages.append((role, content))
        conversation_repo.update_slots = lambda c, cid, s: state.update({"slots": dict(s)})
        profile_repo.get_flat_profile = lambda c: {"cities": ["佛山"], "job_nature": "实习"}
        ag._all_profiles = lambda c: [_fake_profile()]
        match_repo.list_current_job_profiles = lambda c: _fake_jobs()
        repo.get_default_resume_id = lambda c: 6

        # ①②③④ 同时断网：LLM 全部抛错 + 向量不可用
        async def broken_llm(*args, **kwargs):
            raise RuntimeError("模拟网关 503 不可用")

        async def broken_align(*args, **kwargs):
            raise RuntimeError("模拟语义判定超时")

        ag.chat_json = broken_llm
        ag.align_skills = broken_align
        ag.embed = lambda texts: None

        print("== ① 全链路断网压测（Plan + SemanticJudge + Embed + Compose 全部挂掉）==")
        out = asyncio.run(ag.handle_message("帮我看看佛山后端实习", conversation_id=1))
        degraded = set((out.get("trace") or {}).get("degraded") or [])
        check("全断网下不抛异常且返回正常结构", "reply" in out and "jobs" in out and "trace" in out, str(out.keys()))
        check("Plan 挂掉仍沿用历史意图执行检索", out.get("intent") == "后端实习", f"intent={out.get('intent')}")
        check("trace 记录了 plan_fallback", "plan_fallback" in degraded, str(degraded))
        check("trace 记录了 semantic_judge_fallback", "semantic_judge_fallback" in degraded, str(degraded))
        check("trace 记录了 vector_unavailable", "vector_unavailable" in degraded, str(degraded))
        check("trace 记录了 compose_template_fallback", "compose_template_fallback" in degraded, str(degraded))
        check("字面召回仍然救回了「Java后端实习生」", any(j["job_id"] == 101 for j in out.get("jobs") or []), str(out.get("jobs")))
        check("模板兜底回复包含明确降级提示", "降级" in (out.get("reply") or ""), (out.get("reply") or "")[:80])

        print("\n== ② 截断显式标记（超过 RECALL_MAX=40 时 trace.recall.truncated 必须为 True）==")
        many_jobs = [
            {**_fake_jobs()[0], "job_id": 1000 + i, "title": f"Java后端实习生#{i}"}
            for i in range(45)
        ]
        match_repo.list_current_job_profiles = lambda c: many_jobs
        out2 = asyncio.run(ag.handle_message("找后端实习", conversation_id=1))
        rec = (out2.get("trace") or {}).get("recall") or {}
        check("union_count=45 > 40 时 truncated == True", rec.get("truncated") is True, str(rec))
        check("selected_ids 被截断到 40", len(rec.get("selected_ids") or []) == ag.RECALL_MAX, str(len(rec.get("selected_ids") or [])))
        check("lexical_ids 完整保留截断前的字面命中数 (45)", len(rec.get("lexical_ids") or []) == 45, str(len(rec.get("lexical_ids") or [])))

        print("\n== ③ 无历史槽位且 Plan 挂掉：不得用空意图硬搜全库 ==")
        state["slots"] = {}
        out3 = asyncio.run(ag.handle_message("你好，随便看看", conversation_id=1))
        check("走 clarify 出口而非检索", (out3.get("trace") or {}).get("route") == "clarify", str(out3.get("trace")))
        check("不返回岗位卡片", (out3.get("jobs") or []) == [])
    finally:
        ag.get_pool = orig_pool
        conversation_repo.get = orig_conv_get
        conversation_repo.create = orig_conv_create
        conversation_repo.recent_window = orig_conv_win
        conversation_repo.add_message = orig_conv_add
        conversation_repo.update_slots = orig_conv_slots
        profile_repo.get_flat_profile = orig_prof_flat
        ag._all_profiles = orig_all_prof
        match_repo.list_current_job_profiles = orig_list_jobs
        repo.get_default_resume_id = orig_def_rid
        ag.chat_json = orig_chat_json
        ag.align_skills = orig_align
        ag.embed = orig_embed


def main() -> int:
    run_failover_cases()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
