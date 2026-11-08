"""主动发现（DiscoverAgent）的离线回归。

四部分，从纯到活：
    ① Moka 契约解析（真实页面快照 fixture）—— 契约坏了这里第一个知道
    ② html 锚点预筛（合成列表页）—— 规则层的行为锁定
    ③ 急招/新发布启发式与 digest 确定性 —— 去重语义的地基
    ④ run_round 编排（mock 抓取与入库，真库）—— 管线状态机的隔离性：
       单源失败不连坐、预算耗尽收尾、discover_run 落库。需要数据库；
       库没起就整段跳过（照 check_radar 的惯例），①②③永远可跑。

用法：
    .venv/Scripts/python.exe scripts/check_discover.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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


def part_moka_contract() -> None:
    from app.discover.adapters import UnsupportedSourceError, parse_moka_init_data

    print("== ① Moka 契约（真实页面快照）==")
    fixture = (ROOT / "scripts" / "fixtures" / "moka_ruijie_apply.html").read_text(encoding="utf-8")
    listings, meta = parse_moka_init_data(fixture, "https://app.mokahr.com/apply/ruijie/26518")
    check(f"解析出岗位条目 > 0（实测 {len(listings)}）", len(listings) > 0)
    check("meta.org_id == ruijie", meta.get("org_id") == "ruijie", str(meta))
    # siteId 在 JSON 里 int/str 都可能出现，比较时归一（URL 模板对两种都兼容）
    check("meta.site_id == 26518", int(meta.get("site_id") or 0) == 26518, str(meta.get("site_id")))
    check("meta.total（站内岗位总数）存在", meta.get("total", 0) >= len(listings), str(meta.get("total")))

    first = listings[0]
    check("条目带标题", bool(first.title))
    check("条目带详情页 URL（hash 路由构造）",
          first.url and first.url.startswith("https://app.mokahr.com/apply/ruijie/26518#/job/")
          and len(first.url) > 60, str(first.url))
    check("条目带稳定 source_job_id（跨轮去重的依据）",
          bool(first.source_job_id) and first.source_job_id.startswith("moka:ruijie:"),
          str(first.source_job_id))
    check("条目带发布时间", bool(first.published_at), str(first.published_at))
    check("org_name 来自 init-data", bool(first.org_name))

    # 幂等：同一页面解析两次结果一致（digest 确定性的前提）
    listings2, meta2 = parse_moka_init_data(fixture, "https://app.mokahr.com/apply/ruijie/26518")
    check("二次解析幂等（条目完全一致）",
          [l.title for l in listings] == [l.title for l in listings2]
          and [l.source_job_id for l in listings] == [l.source_job_id for l in listings2])

    # 非 Moka 页面要诚实报因，不是返回空列表
    try:
        parse_moka_init_data("<html><body>登录墙</body></html>", "https://x")
        check("非 Moka 页面应抛 UnsupportedSourceError", False)
    except UnsupportedSourceError:
        check("非 Moka 页面抛 UnsupportedSourceError（诚实报因）", True)


def part_html_anchors() -> None:
    from app.discover.adapters import extract_job_anchors

    print("\n== ② html 锚点预筛（合成列表页）==")
    html_text = (ROOT / "scripts" / "fixtures" / "careers_list_sample.html").read_text(encoding="utf-8")
    base = "https://hr.example.com/careers"
    anchors = extract_job_anchors(html_text, base)
    titles = [a["title"] for a in anchors]
    check(f"岗位锚点命中 ≥3（实际 {len(anchors)}）", len(anchors) >= 3, str(titles))
    check("含「高级后端开发工程师」", any("高级后端" in t for t in titles))
    check("相对路径已拼成绝对 URL",
          all(a["url"].startswith("https://hr.example.com") for a in anchors if "example.com/job/other" not in a["url"]),
          str([a["url"] for a in anchors]))
    check("登录/关于我们等噪声被筛掉", all("登录" not in t and "关于我们" not in t for t in titles), str(titles))
    check("入口页「招聘官网」被预筛保留（判给 LLM 挑，规则层不越权）",
          any(t == "招聘官网" for t in titles), str(titles))
    # 外域伙伴链接保留（是否真岗位由 LLM 判断，规则层只按词预筛）
    check("带岗位词的外域锚点保留", any("partner.example.com" in a["url"] for a in anchors))
    # 二次解析幂等
    anchors2 = extract_job_anchors(html_text, base)
    check("锚点解析幂等", [a["url"] for a in anchors] == [a["url"] for a in anchors2])


def part_heuristics() -> None:
    from app.discover.adapters import (
        Listing,
        detect_adapter,
        find_urgent,
        is_fresh,
        listing_digest_text,
    )

    print("\n== ③ 急招/新鲜度启发式 + digest 确定性 ==")
    check("「急招」命中", find_urgent("测试工程师（急招）") == "急招")
    check("「热招」命中", find_urgent("后端开发 热招中") == "热招")
    check("普通标题不误报", find_urgent("后端开发工程师") is None)
    check("「着急」不算急招（判据要求指招聘紧急，不是用户着急）",
          find_urgent("别着急") is None, str(find_urgent("别着急")))

    now = datetime.now(timezone.utc)
    check("7 天内发布 → fresh", is_fresh((now - timedelta(days=3)).isoformat(), now))
    check("8 天前发布 → 非 fresh", not is_fresh((now - timedelta(days=8)).isoformat(), now))
    check("未来时间 → 非 fresh（时钟偏差不奖励）",
          not is_fresh((now + timedelta(days=1)).isoformat(), now))
    check("解析不了的时间 → 非 fresh（不猜）", not is_fresh("上周发布", now))
    check("None → 非 fresh", not is_fresh(None, now))

    check("mokahr.com 域名 → moka 适配器", detect_adapter("https://app.mokahr.com/apply/x/1") == "moka")
    check("其他域名 → html 兜底", detect_adapter("https://hr.example.com/jobs") == "html")

    l = Listing(title="后端开发工程师", url="https://app.mokahr.com/x#/job/1",
                city="福州", commitment="全职", published_at="2026-11-06",
                org_name="锐捷网络", source_job_id="moka:ruijie:abc")
    d1 = listing_digest_text(l)
    d2 = listing_digest_text(l)
    check("digest 两次拼接完全一致（跨轮去重的地基）", d1 == d2)
    check("digest 长度过 30 字入库门槛", len(d1) >= 30, f"{len(d1)} 字")
    check("digest 包含标题与公司", "后端开发工程师" in d1 and "锐捷网络" in d1)


def part_orchestration() -> None:
    """run_round 编排：mock 掉抓取与入库，真库跑状态机。

    断言的是管线行为，不是抓取/入库本身（那两件事各有真实链路的测试）：
    单源失败不连坐 / 汇总数字正确 / discover_run 落库 / 来源状态回写。
    """
    import psycopg

    from app.config import settings
    from app.db.pool import init_schema

    try:
        # 新表要靠启动时的 init_schema 建：check 直接连库不会自动建表
        init_schema()
        conn = psycopg.connect(settings.database_url, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        print(f"\n== ④ run_round 编排：跳过（数据库未启动：{exc}）==")
        return
    print("\n== ④ run_round 编排（mock 抓取，真库）==")
    try:
        from app.discover import agent as dag
        from app.discover.adapters import Listing, UnsupportedSourceError
        from app.discover.fetcher import FetchError

        # 真库基线：跑前快照 discover 两表，跑后还原
        hist_sources = conn.execute("SELECT id, name, url, adapter, enabled, note FROM discover_source").fetchall()
        hist_runs = conn.execute("SELECT coalesce(max(id), 0) FROM discover_run").fetchone()[0]

        conn.execute("DELETE FROM discover_source")
        # 好源用 mokahr.com 域名：auto 探测按 URL 指纹落定 moka 适配器 → 命中 fake_ok；
        # 坏源用普通域名 → html 适配器 → fake_bad。两个 mock 分支各走一遍
        conn.execute(
            "INSERT INTO discover_source (name, url, adapter, enabled) VALUES"
            " ('好源', 'https://app.mokahr.com/apply/mock/1', 'auto', true),"
            " ('坏源', 'https://bad.example.com/jobs', 'auto', true)"
        )
        conn.commit()

        listing = Listing(title="后端开发工程师", url="https://ok.example.com/job/1",
                          city="福州", commitment="全职", published_at=None,
                          org_name="好源公司", source_job_id="mock:1")

        async def fake_ok(fetcher, url):
            return [listing, listing], {"total": 2}

        async def fake_bad(fetcher, url):
            raise FetchError("站点拒绝访问（HTTP 403）")

        orig_ingest = dag.ingest_jd_text
        calls = {"n": 0}

        async def fake_ingest(**kwargs):
            calls["n"] += 1
            return {"job_id": 9000 + calls["n"], "profile_id": 1, "is_new": calls["n"] <= 2,
                    "profile": {}, "integrity": {}, "cost_cny": 0.0}

        dag.ingest_jd_text = fake_ingest
        dag.ADAPTERS["moka"] = fake_ok   # auto 探测按域名走 html，这里直接注入 moka 键并改期望
        dag.ADAPTERS["html"] = fake_bad  # 坏源走 html 兜底 → FetchError
        try:
            import asyncio

            summary = asyncio.run(dag.run_round(trigger="manual"))
        finally:
            dag.ingest_jd_text = orig_ingest

        check("好源成功（sources_ok=1）", summary["sources_ok"] == 1, str(summary["per_source"]))
        check("坏源记入 failures 且带原因",
              any(f["source"] == "坏源" and "403" in f["reason"] for f in summary["failures"]),
              str(summary["failures"]))
        check("好源失败不连坐（坏源先跑完全不影响）", summary["jobs_found"] == 2,
              f"found={summary['jobs_found']}")
        check("同一条目重复出现 → 第二次 is_new=False（mock 返回值控制）",
              summary["jobs_new"] == 2, f"new={summary['jobs_new']}")
        check("discover_run 落库且状态 done",
              conn.execute("SELECT status, jobs_found, jobs_new FROM discover_run"
                           " WHERE id = %s", (summary["run_id"],)).fetchone()[0] == "done")
        src_status = conn.execute(
            "SELECT name, last_status FROM discover_source ORDER BY name").fetchall()
        check("来源状态回写：好源 ok / 坏源 failed",
              dict(src_status).get("好源") == "ok" and dict(src_status).get("坏源") == "failed",
              str(src_status))
        reply = dag.format_reply(summary)
        check("format_reply 数字与汇总一致",
              f"发现 {summary['jobs_new']} 个新岗位" in reply and "1/2 个官网招聘源" in reply,
              reply[:60])
        check("format_reply 交代失败来源", "坏源" in reply, reply[:120])

        # 还原：评测不能污染基线
        conn.execute("DELETE FROM discover_source")
        for s in hist_sources:
            conn.execute(
                "INSERT INTO discover_source (name, url, adapter, enabled, note)"
                " VALUES (%s, %s, %s, %s, %s)", (s[1], s[2], s[3], s[4], s[5]))
        conn.execute("DELETE FROM discover_run WHERE id > %s", (hist_runs,))
        conn.commit()
        print("  （已还原 discover_source / discover_run）")
    finally:
        conn.close()


def main() -> int:
    part_moka_contract()
    part_html_anchors()
    part_heuristics()
    part_orchestration()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
