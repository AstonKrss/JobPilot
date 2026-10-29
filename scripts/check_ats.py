"""导出文件自检（ATS）的回归测试。

每个用例都用真实生成的 .docx 跑，而不是构造假的 Block 列表 ——
这一层的价值全在"文件里实际写进去的是什么"，绕开文件就等于绕开了它要测的东西。

重点同样是两个方向：
    记得住问题（邮箱读不到、日期用了长横线、条目被并成一段）
    以及不在正常简历上误报（两行的项目描述是正常的）

用法：.venv/Scripts/python.exe scripts/check_ats.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from docx import Document  # noqa: E402

from app.ingest.docx_text import extract_docx_blocks  # noqa: E402
from app.rewrite.ats_check import check_docx, cut_candidates, estimate_pages  # noqa: E402

PASS = 0
FAIL = 0
TMP = Path(tempfile.mkdtemp(prefix="ats-"))


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}{(' — ' + detail) if detail else ''}")


def make_doc(name: str, paragraphs: list[str]) -> Path:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    path = TMP / name
    doc.save(path)
    return path


def ats_of(path: Path, job: dict | None = None, basic: dict | None = None) -> dict:
    blocks, _ = extract_docx_blocks(path)
    return check_docx(path, blocks, job=job, basic=basic)


def by_key(report: dict, key: str) -> dict:
    return next((c for c in report["checks"] if c["key"] == key), {})


GOOD = [
    "张三",
    "手机：13800138000　邮箱：zhangsan@example.com",
    "教育经历",
    "佛山大学　计算机科学与技术　本科　2024.09-2028.06",
    "技能",
    "Java、Spring Boot、MySQL、Redis、Docker、Git",
    "实习经历",
    "辰粤科技　后端开发实习生　2026.07-2026.09",
    "参与内部工单系统的接口开发，使用 Spring Boot 完成 12 个接口，并用 Redis 缓存热点查询。",
    "项目经历",
    "实验室设备管理平台",
    "基于 Java 与 MySQL 实现设备借还登记，前端用 Vue，部署在 Docker 中。",
]


def main() -> int:
    print("== 正常简历不应误报 ==")
    good = make_doc("good.docx", GOOD)
    r = ats_of(good, job={"must_have_skills": ["Java", "MySQL"]},
               basic={"email": "zhangsan@example.com", "phone": "13800138000"})
    check("正常简历判 ok", r["level"] == "ok", str(r["counts"]))
    check("文本层通过", by_key(r, "text_layer")["level"] == "pass")
    check("联系方式通过", by_key(r, "contact")["level"] == "pass")
    check("关键词全部命中", by_key(r, "keywords")["level"] == "pass", str(by_key(r, "keywords")))
    check("日期格式通过", by_key(r, "date_dash")["level"] == "pass")
    check("段落结构通过", by_key(r, "structure")["level"] == "pass")
    check("两行左右的段不误报", len([b for b in GOOD if len(b) > 40]) >= 2)
    check("页数通过", by_key(r, "pages")["level"] == "pass")

    print("\n== 文本层为空 ==")
    empty = make_doc("empty.docx", [" "])
    r = ats_of(empty)
    check("空文档判 fail", r["level"] == "fail", str(r["counts"]))
    check("空文档命中 text_layer", by_key(r, "text_layer")["level"] == "fail")

    print("\n== 联系方式读不出 ==")
    # 邮箱被拆成两行（真实场景：列宽不够，Word 自动换行）
    # 注意不能直接用 GOOD 的前两段：那段里已经有一个完整的邮箱，
    # 测试会因此"通过"而实际什么都没验证到 —— 第一版就是这么写的
    broken = make_doc(
        "broken-mail.docx",
        ["张三", "手机：13800138000", "邮箱：zhangsan@", "example.com"] + GOOD[2:],
    )
    r = ats_of(broken, basic={"email": "zhangsan@example.com", "phone": "13800138000"})
    check("邮箱断开 → contact 判 fail", by_key(r, "contact")["level"] == "fail",
          by_key(r, "contact").get("detail", ""))
    check("邮箱断开 → 整体 fail", r["level"] == "fail")

    # 手机号分组写 138 0013 8000，正则匹配不到
    grouped = make_doc("grouped.docx", ["张三", "电话 138 0013 8000", "邮箱 a@b.com"] + GOOD[3:])
    r = ats_of(grouped, basic={"phone": "13800138000"})
    check("手机号分组 → contact 判 warn", by_key(r, "contact")["level"] == "warn",
          by_key(r, "contact").get("detail", ""))

    print("\n== 日期分隔符 ==")
    # en dash（U+2013）是 LaTeX 与许多模板的产物，部分 ATS 会连年份一起丢掉
    dash = make_doc("dash.docx", ["张三", "实习 2026.07\u20132026.09　公司", "邮箱 a@b.com"] + GOOD[4:])
    r = ats_of(dash)
    check("en dash 日期判 warn", by_key(r, "date_dash")["level"] == "warn",
          by_key(r, "date_dash").get("detail", ""))
    dash2 = make_doc("dash2.docx", ["张三", "实习 2026.07-2026.09　公司", "邮箱 a@b.com"] + GOOD[4:])
    check("半角连字符不误报", by_key(ats_of(dash2), "date_dash")["level"] == "pass")

    print("\n== 段落被并成一块 ==")
    merged_text = (
        "实习经历：辰粤科技 2026.07-2026.09 负责接口开发；"
        "弘趣信息 2026.01-2026.03 负责数据清洗；"
        "煜晨科技 2025.07-2025.09 负责前端页面。"
    )
    merged = make_doc("merged.docx", ["张三", "邮箱 a@b.com", merged_text] + GOOD[4:])
    r = ats_of(merged)
    check("一段里多个日期区间 → structure warn",
          by_key(r, "structure")["level"] == "warn", by_key(r, "structure").get("detail", ""))

    # 编号那条必须够长才算：一行之内写 "1、x 2、y" 的人很多，
    # 全报出来会让这个提示变成噪音。所以把每条写长，让它真的跨了多行
    bullets = (
        "项目经历 1、设备管理平台，负责后端接口与权限模型，使用 Java 与 MySQL 完成；"
        "2、工单系统，负责状态机与消息通知，接入钉钉机器人；"
        "3、报表导出，把月度统计从手工整理改成定时任务，节省每周四小时；"
        "4、权限中心，统一三个子系统的登录与角色，接入公司统一的目录服务。"
    )
    bl = make_doc("bullets.docx", ["张三", "邮箱 a@b.com", bullets])
    r = ats_of(bl)
    check("一段里多个编号 → structure warn",
          by_key(r, "structure")["level"] == "warn", by_key(r, "structure").get("detail", ""))

    print("\n== 关键词覆盖 ==")
    r = ats_of(good, job={"must_have_skills": ["Java", "MySQL", "Kubernetes", "Kafka"]})
    kw = by_key(r, "keywords")
    check("缺两项时判 warn", kw["level"] == "warn", kw.get("detail", ""))
    check("缺失项列表正确", set(r["keywords"]["miss"]) == {"Kubernetes", "Kafka"},
          str(r["keywords"]["miss"]))
    check("命中项列表正确", set(r["keywords"]["hit"]) == {"Java", "MySQL"})
    check("大小写不敏感", "java" in {x.lower() for x in r["keywords"]["hit"]})

    noreq = ats_of(good, job={"must_have_skills": []})
    check("岗位没技能要求时跳过", by_key(noreq, "keywords")["level"] == "skip")

    print("\n== 页数预算与裁剪建议 ==")
    # 每段约 190 个汉字 ≈ 4.5 行，30 段约 3 页
    long_paras = ["张三", "邮箱 a@b.com", "工作经历"]
    for i in range(30):
        long_paras.append(
            f"第 {i} 段：负责公司内部系统的后端开发与维护，使用 Java 与 Spring Boot 完成后端接口设计，"
            "参与数据库表结构评审与慢查询优化，配合前端完成联调，并编写接口文档。"
            "期间主导了两次版本升级，处理线上故障十余起，整理了完整的部署与回滚手册，"
            "并把重复的巡检工作写成脚本，把每周的人工核对从两小时压到十分钟。"
            "同时承担新同事的入门指导，维护团队内部的技术笔记与常见问题清单。"
        )
    long_doc = make_doc("long.docx", long_paras)
    r = ats_of(long_doc, job={"must_have_skills": ["Java"]})
    check("长简历页数超预算", by_key(r, "pages")["level"] == "warn",
          by_key(r, "pages").get("detail", ""))
    check("超预算时给出裁剪候选", bool(r.get("cut_candidates")), str(len(r.get("cut_candidates") or [])))

    blocks, _ = extract_docx_blocks(long_doc)
    stats = estimate_pages(blocks)
    check("页数估算随文本量增长", stats["pages"] > 2.2, str(stats))
    check("行数不少于段落数", stats["lines"] >= stats["paragraphs"])

    print("\n== 裁剪排序：按信号而不是板块顺序 ==")
    cand = cut_candidates(
        blocks,
        {"must_have_skills": ["Java"], "title": "Java 开发工程师"},
    )
    check("候选按分数升序", all(cand[i]["score"] <= cand[i + 1]["score"] for i in range(len(cand) - 1)),
          str([c["score"] for c in cand]))
    check("候选不超过 5 条", len(cand) <= 5)

    # 同一段重复三次 + 一段命中关键词：重复的最该被删
    dup_doc = make_doc(
        "dup.docx",
        ["张三", "邮箱 a@b.com"] + ["负责日常行政事务与会议室预定。"] * 3
        + ["使用 Java 完成订单系统的接口开发与压测。"],
    )
    dup_blocks, _ = extract_docx_blocks(dup_doc)
    cand = cut_candidates(dup_blocks, {"must_have_skills": ["Java"]})
    check("有候选返回", bool(cand))
    if cand:
        check("最该删的是没提到关键词的重复段",
              "行政" in cand[0]["text"], cand[0]["text"])
        check("理由写明了为什么", any(k in cand[0]["why"] for k in ("重复", "没提到")),
              cand[0]["why"])
        check("命中关键词的段不会排第一", "Java" not in cand[0]["text"], cand[0]["text"])

    print("\n== 交付不受影响 ==")
    r = ats_of(good)
    check("报告带文件名", r["file"] == "good.docx")
    check("报告带统计", "chars" in r["stats"] and r["stats"]["chars"] > 0)
    check("计数三项齐全", set(r["counts"]) == {"pass", "warn", "fail"})

    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
