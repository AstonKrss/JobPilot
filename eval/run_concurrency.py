"""并发竞态评测：真实 uvicorn 进程 + 异步并发请求。

为什么不用 TestClient
    Starlette 的 TestClient 把请求穿过同一个 portal，并发线程到不了 ASGI 应用
    的并发路径 —— 它测的是"功能对不对"，测不出"同时来了怎么办"。这里起一个
    真实的 uvicorn 子进程（独立端口，不占用 8000），用 httpx 异步并发打进去，
    连接池（max_size=4）与数据库写入路径都在真实竞争条件下工作。

三个竞态点（都是既有评测从没碰过的）：
    T1  N 个会话同时写**不同**城市 —— apply_update 的 SELECT→合并→UPSERT
        窗口在 READ COMMITTED 下可能互相覆盖（丢失更新）
    T2  N 个会话同时写**同一个**城市 —— 历史表会记几条？最终值对不对？
    T3  同一 URL 的 JD 并发入库 6 次 —— uq_job_source_id 唯一键下，
        要么恰好 1 条 job 且 is_new 只出现一次，要么有请求拿到明确冲突响应；
        最坏结果是"重复入库"（第三轮严格测试抓到过的缺陷，本次复查）

隔离纪律与 run_scenarios 相同：跑前快照、跑后精确还原。

用法：
    .venv/Scripts/python.exe eval/run_concurrency.py   # 需数据库 + LLM key
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

PORT = 8124
BASE = f"http://127.0.0.1:{PORT}"
RESULTS_DIR = ROOT / "eval" / "results"
DSN = None  # 运行时从 settings 取

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


def start_server() -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--app-dir", str(ROOT / "backend"),
            "--port", str(PORT), "--log-level", "warning",
        ],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as resp:
                if resp.status == 200:
                    return proc
        except OSError:
            time.sleep(0.5)
    proc.kill()
    raise SystemExit("uvicorn 60 秒内没有就绪")


# ---------- 快照 / 还原 ----------


def snapshot(conn) -> dict:
    return {
        "profile": {
            r[0]: (r[1], r[2], r[3], r[4], r[5])
            for r in conn.execute(
                "SELECT key, value, evidence, modality, confidence, conversation_id FROM user_profile"
            ).fetchall()
        },
        "hist_max": conn.execute("SELECT coalesce(max(id), 0) FROM user_profile_history").fetchone()[0],
        "conv_max": conn.execute("SELECT coalesce(max(id), 0) FROM conversation").fetchone()[0],
        "job_max": conn.execute("SELECT coalesce(max(id), 0) FROM job").fetchone()[0],
        "episodes": conn.execute("SELECT id, hit_count, last_hit_at FROM episode").fetchall(),
    }


def restore(conn, snap: dict) -> None:
    conn.execute("DELETE FROM job WHERE id > %s", (snap["job_max"],))
    conn.execute("DELETE FROM conversation_message WHERE conversation_id > %s", (snap["conv_max"],))
    conn.execute("DELETE FROM conversation WHERE id > %s", (snap["conv_max"],))
    conn.execute("DELETE FROM user_profile_history WHERE id > %s", (snap["hist_max"],))
    for eid, hit_count, last_hit_at in snap["episodes"]:
        conn.execute("UPDATE episode SET hit_count = %s, last_hit_at = %s WHERE id = %s",
                     (hit_count, last_hit_at, eid))
    conn.execute("DELETE FROM user_profile")
    for key, (value, evidence, modality, confidence, conversation_id) in snap["profile"].items():
        conn.execute(
            "INSERT INTO user_profile (key, value, evidence, modality, confidence, conversation_id)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (key, json.dumps(value, ensure_ascii=False) if value is not None else None,
             evidence, modality, confidence, conversation_id),
        )
    conn.commit()


# ---------- 并发请求 ----------


async def post_chat(client, text: str) -> dict:
    resp = await client.post(f"{BASE}/api/chat", json={"message": text})
    body = resp.json() if resp.status_code == 200 else {"_status": resp.status_code, "_body": resp.text[:200]}
    return body


async def post_ingest(client, payload: dict) -> dict:
    resp = await client.post(f"{BASE}/api/jobs/ingest", json=payload)
    body = resp.json() if resp.status_code == 200 else {"_status": resp.status_code, "_detail": resp.text[:200]}
    return body


JD_TEXT = (
    "高级Java开发工程师\n"
    "职责：负责核心交易系统的设计与开发，参与微服务架构演进，保障系统高可用。\n"
    "要求：3年以上Java开发经验，熟悉Spring Boot、MySQL、Redis，了解分布式事务。\n"
    "薪资：25-35K·14薪。工作地点：深圳市南山区。五险一金，双休，期权激励。"
)


async def t1_different_cities(client, conn, snap) -> None:
    """6 个会话同时写不同城市：全部成功、最终值是其中之一、无 500。"""
    print("\n== T1：6 会话并发写不同城市 ==")
    cities = ["佛山", "广州", "深圳", "东莞", "中山", "珠海"]
    bodies = await asyncio.gather(*[
        post_chat(client, f"以后只找{c}的岗位，其他城市都不考虑") for c in cities
    ])
    bad = [b for b in bodies if "_status" in b]
    check("全部返回 200", not bad, "; ".join(str(b.get('_status')) for b in bad))
    row = conn.execute("SELECT value FROM user_profile WHERE key = 'cities'").fetchone()
    final = row[0] if row else None
    check("最终 cities 是并发写入值之一（不丢成空/脏值）",
          final in [[c] for c in cities] or (isinstance(final, list) and set(final) <= set(cities) and final),
          f"final={final}")
    hist = conn.execute("SELECT count(*) FROM user_profile_history WHERE id > %s",
                        (snap["hist_max"],)).fetchone()[0]
    check("历史表有写入痕迹", hist >= 1, f"{hist} 条")


async def t2_same_city(client, conn, snap) -> None:
    """8 个会话同时写同一个城市：最终值确定、无异常。"""
    print("\n== T2：8 会话并发写同一城市 ==")
    bodies = await asyncio.gather(*[
        post_chat(client, "以后只找佛山的岗位，其他城市都不考虑") for _ in range(8)
    ])
    bad = [b for b in bodies if "_status" in b]
    check("全部返回 200", not bad, "; ".join(str(b.get('_status')) for b in bad))
    row = conn.execute("SELECT value FROM user_profile WHERE key = 'cities'").fetchone()
    check("最终 cities == [佛山]", (row[0] if row else None) == ["佛山"],
          f"final={row[0] if row else None}")
    hist = conn.execute("SELECT count(*) FROM user_profile_history WHERE id > %s",
                        (snap["hist_max"],)).fetchone()[0]
    check(f"历史表写入 {hist} 条（每条成功写入各留一条）", hist >= 1, f"{hist} 条")


async def t3_ingest_dedup(client, conn, snap) -> None:
    """同一 URL 并发入库 6 次：恰好 1 条 job，is_new 只出现一次。"""
    print("\n== T3：同 URL 并发入库 ×6（复查第三轮抓到的去重缺陷）==")
    url = f"https://example.com/job/concurrency-test-{int(time.time())}"
    payload = {"source": "manual", "raw_text": JD_TEXT, "url": url, "detail_level": "full"}
    bodies = await asyncio.gather(*[post_ingest(client, payload) for _ in range(6)])
    statuses = [b.get("_status", 200) for b in bodies]
    ok_count = sum(1 for s in statuses if s == 200)
    check("全部返回 200 或明确的冲突语义（不得 500）",
          all(s in (200, 409) for s in statuses), f"statuses={statuses}")
    job_ids = {b.get("job_id") for b in bodies if b.get("job_id")}
    rows = conn.execute("SELECT id FROM job WHERE url = %s", (url,)).fetchall()
    check("库里该 URL 恰好 1 条岗位", len(rows) == 1, f"库里 {len(rows)} 条，返回 job_ids={job_ids}")
    is_new_count = sum(1 for b in bodies if b.get("is_new") is True)
    check("is_new=True 恰好出现一次（重复抓取不该反复提醒）", is_new_count == 1,
          f"{is_new_count} 次 is_new=True / {ok_count} 个成功请求")


async def amain() -> None:
    import httpx
    import psycopg

    from app.config import settings

    proc = start_server()
    conn = psycopg.connect(settings.database_url)
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            snap = snapshot(conn)
            # T1/T2 都动 cities：各自先重置成已知的空状态，测完由统一 restore 兜底
            conn.execute("DELETE FROM user_profile WHERE key = 'cities'")
            conn.commit()
            try:
                await t1_different_cities(client, conn, snap)
                conn.execute("DELETE FROM user_profile WHERE key = 'cities'")
                conn.execute("DELETE FROM user_profile_history WHERE id > %s", (snap["hist_max"],))
                conn.execute("DELETE FROM conversation WHERE id > %s", (snap["conv_max"],))
                conn.execute("DELETE FROM conversation_message WHERE conversation_id > %s", (snap["conv_max"],))
                conn.commit()
                await t2_same_city(client, conn, snap)
                conn.execute("DELETE FROM user_profile WHERE key = 'cities'")
                conn.execute("DELETE FROM user_profile_history WHERE id > %s", (snap["hist_max"],))
                conn.execute("DELETE FROM conversation WHERE id > %s", (snap["conv_max"],))
                conn.execute("DELETE FROM conversation_message WHERE conversation_id > %s", (snap["conv_max"],))
                conn.commit()
                await t3_ingest_dedup(client, conn, snap)
            finally:
                restore(conn, snap)
    finally:
        conn.close()
        proc.kill()


def main() -> int:
    global DSN
    started = time.monotonic()
    asyncio.run(amain())
    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过，{time.monotonic() - started:.0f}s")
    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "passed": PASS, "failed": FAIL}
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / f"concurrency_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
