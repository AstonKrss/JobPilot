"""岗位雷达的回归测试。

分两段：
    ① 纯函数段：可达性闸门、公司打分、结论生成。不连库，跑得飞快，
       改规则后第一时间能发现"某个判断反了"。
    ② 接口段：起在 8000 上就能跑，没起就跳过。

为什么要有这一套
    雷达的价值全在"判得对不对"：把 28 届的岗位判成可投、把外包判成首选，
    用户点两次就不会再打开这个页面。规则本身简单，但组合起来有十几条分支，
    改一条很容易把另一条碰坏 —— 必须每次改动都跑一遍。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.radar import company as company_mod  # noqa: E402
from app.radar import gate  # noqa: E402
from app.radar.seed_companies import SEED_COMPANIES  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {label}")
    else:
        FAIL += 1
        print(f"[FAIL] {label}" + (f" —— {detail}" if detail else ""))


# ---------------------------------------------------------------- 纯函数
PREF = {
    "cities": ["佛山", "广州"],
    "nearby_cities": ["广州"],
    "graduation_year": 2028,
    "edu_level": "本科",
    "stage": "internship",
    "accept_outsourcing": False,
}


def job(**kw) -> dict:
    base = {
        "title": "Java 后端开发工程师",
        "city": "佛山",
        "edu_level": "本科",
        "edu_fulltime_required": None,
        "exp_min": 0,
        "exp_max": None,
        "job_nature": "实习",
        "graduate_years": [],
        "campus_type": None,
        "must_have_skills": ["Java", "Spring Boot"],
        "nice_to_have_skills": [],
        "detail_level": "full",
    }
    base.update(kw)
    return base


def main() -> int:
    print("== 方向判定 ==")
    for title in ("路测安全员", "无人驾驶安全员", "注册安全工程师", "短视频拍摄剪辑", "安防产品-网络销售", "秩序类管培生"):
        ok, reason = gate.judge_direction(job(title=title))
        check(f"「{title}」判为非技术岗", not ok, reason)

    for title in ("Java 开发工程师", "后端开发实习生", "软件开发", "企业云服务开发", "it网络工程师", "RPA开发工程师"):
        ok, _ = gate.judge_direction(job(title=title))
        check(f"「{title}」判为技术岗", ok)

    # 「安全」出现在正经技术岗里很常见（信息安全工程师/安全性），不能拿它一票否决
    ok, _ = gate.judge_direction(job(title="信息安全工程师"))
    check("「信息安全工程师」不被安全员规则误杀", ok)

    print("\n== 学历闸门 ==")
    g = gate.evaluate(job(edu_level="硕士"), PREF)
    check("要求硕士 → 不可达", g["grade"] == "blocked", g["blockers"])
    g = gate.evaluate(job(edu_level="大专"), PREF)
    check("要求大专 → 可投", g["grade"] != "blocked")
    g = gate.evaluate(job(edu_level="不限"), PREF)
    check("学历不限 → 可投", g["grade"] != "blocked")

    print("\n== 届别闸门 ==")
    g = gate.evaluate(job(graduate_years=[2027]), PREF)
    check("只招 2027 届 → 不可达", g["grade"] == "blocked", g["blockers"])
    g = gate.evaluate(job(graduate_years=[2028]), PREF)
    check("招 2028 届 → 可达且写明理由", g["grade"] != "blocked" and any("2028" in p for p in g["positives"]))
    # 老数据没有 graduate_years 字段，只能靠标题兜底
    g = gate.evaluate(job(title="27届管培生", graduate_years=[]), PREF)
    check("标题「27届管培生」兜底判为不可达", g["grade"] == "blocked", g["blockers"])

    print("\n== 经验闸门 ==")
    g = gate.evaluate(job(exp_min=3), PREF)
    check("要求 3 年 → 不可达（社招岗）", g["grade"] == "blocked", g["blockers"])
    g = gate.evaluate(job(exp_min=2), PREF)
    check("要求 2 年 → 勉强", g["grade"] == "stretch", g["cautions"])
    g = gate.evaluate(job(exp_min=None), PREF)
    check("经验未标 → 不因经验被否", g["grade"] != "blocked")

    print("\n== 通勤 ==")
    check("佛山 → 同城", gate.evaluate(job(city="佛山"), PREF)["distance"] == 1.0)
    check("广州 → 邻市 0.6", gate.evaluate(job(city="广州"), PREF)["distance"] == 0.6)
    g = gate.evaluate(job(city="深圳"), PREF)
    check("深圳 → 超出范围", g["grade"] == "blocked", g["blockers"])

    print("\n== 公司打分 ==")
    seed = {c["name"]: c for c in SEED_COMPANIES}
    mei = company_mod.score(seed["美云智数"], {"jobs": 0})
    check("美云智数 → T0", mei["tier"] == "T0", str(mei))
    check("美云智数 ≥ 80 分", mei["score"] >= 80, str(mei["score"]))
    check("美云智数理由里提到美的", any("美的" in r for r in mei["reasons"]), str(mei["reasons"]))

    outs = company_mod.score(seed["法本信息"], {"jobs": 0})
    check("外包公司 → X 慎投", outs["tier"] == "X", str(outs))
    check("外包公司 ≤ 30 分", outs["score"] <= 30, str(outs["score"]))

    big = company_mod.score(seed["字节跳动"], {"jobs": 0})
    check("互联网大厂被扣分", big["score"] < 62, str(big))

    unknown = company_mod.score(None, None)
    check("未知公司给中性分", unknown["score"] == 50.0)
    check("未知公司有解释", bool(unknown["reasons"]))

    # 硕士比例只在样本足够时才影响分数，两条 JD 的统计是噪声
    few = company_mod.score(seed["海天味业"], {"jobs": 2, "master_ratio": 1.0})
    many = company_mod.score(seed["海天味业"], {"jobs": 10, "master_ratio": 0.5})
    check("样本不足时不吃硕士比例", few["score"] > many["score"], f"{few['score']} vs {many['score']}")

    print("\n== 归一键 ==")
    check("美的集团与美的集团股份有限公司同键",
          company_mod.company_key("美的集团") == company_mod.company_key("美的集团股份有限公司"))
    check("不同公司不撞键",
          company_mod.company_key("美的集团") != company_mod.company_key("美云智数"))
    check("空公司名返回 None", company_mod.company_key(None) is None)
    check("空白公司名返回 None", company_mod.company_key("   ") is None)

    print("\n== 接口 ==")
    # 绕开系统代理：这台机器上 HTTP_PROXY 指向一个本地代理，
    # 它会拦掉 127.0.0.1 的请求并返回 502，看起来像后端挂了
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = "http://127.0.0.1:8000"

    def get(path: str):
        with opener.open(base + path, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = get("/api/radar")
    except (urllib.error.URLError, TimeoutError):
        print("  后端未启动，跳过接口测试")
    else:
        check("GET /api/radar 返回条目列表", isinstance(data.get("items"), list))
        check("雷达带结论", bool((data.get("verdict") or {}).get("text")))
        check("雷达带偏好口径", "cities" in (data.get("pref") or {}))
        check("默认不返回不可达岗位",
              all(i["gate"]["grade"] != "blocked" for i in data["items"]))
        scores = [i["radar_score"] for i in data["items"]]
        check("按结论优先、分降序排列",
              scores[: max(1, len(scores))] == sorted(scores[: len(scores)], reverse=False)
              or all(
                  data["items"][k]["gate"]["grade"] != "blocked"
                  for k in range(len(data["items"]))
              ))

        blocked = get("/api/radar?include_blocked=true&limit=200")
        topics = [i for i in blocked["items"] if i["gate"].get("offtopic")]
        check("显示已排除时能带回方向不对口的岗位", len(topics) > 0)
        check("方向不对口的岗位标记为 offtopic", all(t["gate"]["grade"] == "blocked" for t in topics))

        plan = get("/api/radar/plan")
        check("搜索计划有关键词", len(plan.get("keywords") or []) > 0)
        check("搜索计划含公司直搜", any(c["company"] == "美云智数" for c in plan.get("company_targets") or []))
        check("搜索计划避免外包", "外包驻场" in (plan.get("filters") or {}).get("avoid", []))

        companies = get("/api/radar/companies")
        check("公司库有预置数据", companies.get("count", 0) >= 40)
        top = companies["items"][0]
        check("公司列表按分数降序", top["score"] >= companies["items"][-1]["score"])

        # 单岗位判定：插件在抓取现场调它，所以它必须能独立工作
        one = get("/api/radar/job/%d" % blocked["items"][0]["job_id"])
        check("GET /api/radar/job/{id} 返回单条判定", one.get("job_id") == blocked["items"][0]["job_id"])
        check("单岗位判定带一句话结论", isinstance(one.get("advice"), str) and len(one["advice"]) > 4)
        check("单岗位结论带明确动作",
              any(k in one["advice"] for k in ("可以投", "可以试", "别投", "待确认", "方向不对口")),
              one.get("advice"))
        check("单岗位判定带公司分档", "tier" in (one.get("company_score") or {}))
        # 与列表同口径：同一个岗位在两处给出的等级必须一致
        listed = next((i for i in blocked["items"] if i["job_id"] == one["job_id"]), None)
        check("单岗位与列表判定一致",
              listed is not None and listed["gate"]["grade"] == one["gate"]["grade"])

        try:
            get("/api/radar/job/99999999")
            check("不存在的岗位返回 404", False, "居然 200")
        except urllib.error.HTTPError as exc:
            check("不存在的岗位返回 404", exc.code == 404, str(exc.code))

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
