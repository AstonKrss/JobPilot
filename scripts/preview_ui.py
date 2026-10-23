"""生成界面预览页，用无头浏览器截图检查渲染。

为什么要这么绕：这个界面没有前端框架，也没有可编程的浏览器环境，
但"渲染对不对"必须看真实截图，靠读代码判断会漏掉溢出、错位、截断这类问题。

做法：以真实的 index.html 为底（而不是复制一份），注入一段 load 后自动
点击/渲染的脚本，制造出平时要点几下才有的状态，再截图。

用法：
    .venv/Scripts/python.exe scripts/preview_ui.py            # 生成全部场景
    .venv/Scripts/python.exe scripts/preview_ui.py --clean    # 删除预览页
"""

from __future__ import annotations

import argparse
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "backend" / "static"
INDEX = STATIC / "index.html"

MOCK_REVIEW = {
    "mode": "targeted",
    "job_id": 19,
    "rounds": 2,
    "total": 3,
    "flagged": 1,
    "elapsed_s": 21.5,
    "cost_cny": 0.011,
    "items": [
        {
            "category": "jd_gap",
            "severity": "high",
            "anchor": "技能列表",
            "problem": "岗位必备项里写了 MySQL / Oracle 基本使用，你的技能列表没有对应证据。",
            "jd_requirement": "熟悉MySQL/Oracle基本使用，能编写常见SQL",
            "direction": "确认自己是否真的用过；若有，在项目要点里补一条真实的使用场景与数据量。",
            "example": "用 MySQL 承担证书数据存储，单表约 20 万行，按批次与签发状态建联合索引。",
            "fact_status": "passed",
        },
        {
            "category": "resume_defect",
            "severity": "medium",
            "anchor": "自我评价",
            "problem": "自我评价写「两年开发经验」，但工作经历从 2025-11 至今只有 0.2 年，两处口径不一致。",
            "jd_requirement": None,
            "direction": "统一口径，按实际经历写，避免面试时被追问。",
            "example": None,
            "fact_status": "passed",
        },
        {
            "category": "jd_gap",
            "severity": "medium",
            "anchor": "项目经历",
            "problem": "岗位要求 AI 智能体对接与数据监控集成，简历里没有体现。",
            "jd_requirement": "了解AI智能体对接系统数据的集成开发",
            "direction": "如果做过对接工作，把接口类型、数据同步方式写清楚。",
            "example": "对接 GPT 接口完成电子印章识别，日均调用 3000 次。",
            "fact_status": "flagged",
            "fact_issue": "新增数字「3000」，原文中未出现",
        },
    ],
}

SCENES = {
    "resume-open": "activateView('resumes'); (document.querySelector('[data-view-resume]')||{}).click?.();",
    "resumes": "activateView('resumes');",
    "jobs": "activateView('jobs');",
    "match-base": "activateView('match');",
    "job-open": "activateView('jobs'); (document.querySelector('[data-view-job]')||{}).click?.();",
    "job-sorted": (
        "activateView('jobs'); const s=document.getElementById('job-sort-resume'); "
        "s.value='4'; s.dispatchEvent(new Event('change'));"
    ),
    "job-sorted-decision": (
        "activateView('jobs'); document.getElementById('job-sort-mode').value='decision'; "
        "const s=document.getElementById('job-sort-resume'); "
        "s.value='4'; s.dispatchEvent(new Event('change'));"
    ),
    "match-all": "const d = await api('/api/resumes/4/matches?include_rejected=true&accept_internship=true&accept_relocation=true'); activateView('match'); renderMatches(d);",
    "match-jd": "const d = await api('/api/resumes/4/matches?include_rejected=true&accept_internship=true&accept_relocation=true'); activateView('match'); renderMatches(d); await new Promise(r=>setTimeout(r,300)); (document.querySelector('[data-jd]')||{}).click?.();",
    "review": f"activateView('review'); renderReview({json.dumps(MOCK_REVIEW, ensure_ascii=False)});",
    "relax": (
        "const d = await api('/api/resumes/4/relaxations', {method:'POST'}); "
        "activateView('match'); renderRelaxations(d);"
    ),
    "empty": "state.resumes=[]; state.jobs=[]; renderResumes(); renderJobs(); document.getElementById('match-result').innerHTML='<div class=\"panel\"><div class=\"empty\">没有可显示的结果。若开启了「显示被否决」仍为空，可能是岗位库还没有数据。</div></div>'; activateView('match');",
    # 定位横向溢出：列出所有右边界超过视口的元素。
    # 窄屏"内容被切掉"很难靠肉眼找出是哪个元素撑开的
    "debug-width": "activateView('resumes'); const btn=document.querySelector('[data-view-resume]'); if(btn) btn.click(); await new Promise(r=>setTimeout(r,600)); const w=window.innerWidth; const bad=[...document.querySelectorAll('body *')].filter(el=>{const r=el.getBoundingClientRect(); return r.right>w+1 && r.width>0;}).slice(0,15).map(el=>`${el.tagName.toLowerCase()}.${(el.className||'').toString().split(' ').filter(Boolean).join('.')} right=${Math.round(el.getBoundingClientRect().right)} w=${Math.round(el.getBoundingClientRect().width)}`); document.title='VIEWPORT='+w; document.body.insertAdjacentHTML('afterbegin','<pre id=dbg>'+bad.join('\\n')+'</pre>');",
}

HOOK = """
<script>
window.addEventListener('load', async () => {
  await new Promise((r) => setTimeout(r, 900));
  try {
    __SCENE__
  } catch (e) {
    document.body.insertAdjacentHTML('beforeend', '<pre style="color:#a52a2a">' + e.message + '</pre>');
  }
  // 切换视图带 CSS 过渡，截图若正好卡在过渡中会看到"上一个 tab 还亮着"的假象
  await new Promise((r) => setTimeout(r, 800));
});
</script>
"""

# 截图用的是虚拟时间，CSS 过渡不会推进：切到匹配页时旧 tab 会一直保留高亮，
# 看起来像"点了没生效"。截图时禁掉过渡，避免把工具限制当成渲染 bug。
NO_TRANSITION = (
    "<style>*, *::before, *::after { transition: none !important; animation: none !important; }</style>"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    files = sorted(STATIC.glob("_preview*.html"))
    if args.clean:
        for path in files:
            path.unlink()
        print(f"已删除 {len(files)} 个预览页")
        return 0

    index = INDEX.read_text(encoding="utf-8")
    index = index.replace("<link rel=\"stylesheet\" href=\"./style.css\">",
                          "<link rel=\"stylesheet\" href=\"./style.css\">" + NO_TRANSITION)

    for name, scene in SCENES.items():
        path = STATIC / f"_preview_{name}.html"
        path.write_text(index.replace("</body>", HOOK.replace("__SCENE__", scene) + "</body>"),
                        encoding="utf-8")
        print("生成", path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
