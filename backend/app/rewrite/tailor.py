"""针对某个岗位定制简历：最小改动 + 事实核查 + 落成 DOCX。

流程（每一步都是确定的，模型只在第二步做判断）
    1. 打开可编辑文档：DOCX 直接用原文件；PDF 先按画像重建一份 DOCX
    2. 模型给出改动清单（改哪个 id、改成什么、依据哪句原文）
    3. 逐条校验：段落是否存在 / 是否动了不该动的地方 / 是否新增事实
    4. 应用通过的改动，另存为新文件（原文件不动），返回逐条 diff

为什么坚持"最小改动"
    整篇重写会让简历读起来像另一个人的，而且模型会顺手补一些没发生过的事。
    只改 3-8 处、每处必须有原文依据，既能对上岗位要求，又不会改变事实。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..config import settings
from ..db import repo
from ..db.pool import get_pool
from ..llm.deepseek import chat_json, parse_json_object
from ..match.engine import match_one
from .docx_editor import DocxEditor
from .factcheck import check_example
from .pdf_convert import convert_pdf_to_docx
from .rebuild import build_docx_from_profile, read_header_text

logger = logging.getLogger(__name__)

TAILOR_DIR = settings.upload_dir.parent / "tailored"
TAILOR_DIR.mkdir(parents=True, exist_ok=True)

# 不允许改动的区域：姓名/联系方式在最前面，教育信息按内容识别
HEADER_PARAGRAPHS = 3
MAX_OPS = 8

TAILOR_SYSTEM_PROMPT = """你是简历定制编辑器。给你一份简历的段落清单（每段有 id）和一个目标岗位，
输出一份「最小改动」清单。

硬性约束（违反任何一条，这条改动会被程序拒绝）：
1. 只能引用清单里出现的 id，不得发明。
2. 不得新增数字、英文技术名、公司名、学校名、项目名 —— 这些是事实，不是措辞。
3. 不得改动姓名、联系方式、学校、专业、学历、起止时间。
4. 不得**提高**职责程度：「参与」不能写成「主导」或「负责」，「负责」不能写成「主导」。
   **降低是允许且鼓励的**：学生/实习生简历写「我主导后端架构，负责整体设计」明显偏高，
   改成「我负责后端部分模块开发」属于修正，不算改事实，必须改。
   这类改动的 evidence 写被替换掉的那几个词（如「主导」「架构设计」）。
5. 每处改动必须给 evidence：从简历原文里逐字摘出的依据片段（不超过 40 字）。
   找不到依据，就不要改这一条。
6. 只做 0-5 处。**宁可少改，甚至一处都不改，也不要凑数。**
7. op 只有两种：
   replace —— 改写某一段的措辞
   insert  —— 在某段之后补一条（仅当该事实在简历别处已存在、只是这段没写）

什么是好的改写（必须满足 A 或 B，否则不要动这一段）：
A. **补上原文别处已有的具体信息**：把散落在其他段的技术栈、规模、结果，写进对应的经历里。
   例：原文项目段写了「基于 Kubernetes 编排」，工作段只写「负责调度平台」→ 把 Kubernetes 补进工作段。
B. **把笼统动词换成具体动作**：原文「负责交易链路稳定性」→「负责交易链路的故障定位与容量评估」，
   前提是原文别处确有「故障定位」「容量」相关事实。

**绝对禁止的写法（会被程序拒绝）**：
- 给每句话追加身份词或后缀：「负责 X」→「负责 X 的后端开发与维护」「X 相关工作」「X 的能力」
- 把同一句话换个同义词就说改过了（「负责」→「承担」、「推动」→「推进」）
- 通篇均匀地加同一个短语。这会让简历看起来像机器改的，反而减分。

输出 JSON 对象：
{"ops":[{"op":"replace","id":"p12","text":"改写后的整段文字","reason":"为什么改","evidence":"原文依据"}],
 "summary":"一句话说明这次定制的重点；如果没必要改，写清楚原因并给空 ops"}"""


def _protected_pids(editor: DocxEditor, basic: dict) -> set[str]:
    """姓名/联系方式（文档开头几段）与教育信息不允许改。"""
    outline = editor.outline(max_chars=1000)
    # 文档开头几段通常是姓名与联系方式
    protected = {item["id"] for item in outline[:HEADER_PARAGRAPHS]}
    marks = [str(m) for m in (basic.get("school"), basic.get("major")) if m]
    for item in outline:
        if any(m in item["text"] for m in marks):
            protected.add(item["id"])
    return protected



# 空话短语：去掉它们之后，如果句子和原文几乎一样，说明这次改写什么都没加
_GENERIC_PATTERNS = (
    re.compile(r"的?(后端|前端|服务端|全栈|客户端)?(开发|研发)(与维护|和维护|工作|角色|相关)?"),
    re.compile(r"(相关|等)?(工作内容|工作|职责|能力)"),
    re.compile(r"(相关|等)(经验|经历)"),
)
# 同义动词：只换这两组词里的一个，句子其余不变
_SYNONYM_SWAPS = (
    ("负责", "承担"), ("推动", "推进"), ("参与", "协同"), ("实现", "完成"),
)

# 抬高职责的词。原文有、改后没有 → 这是「降级措辞」，属于安全改动。
# 空话拦截的目的是防止「用废话把自己说得更厉害」，
# 而把「主导」改成「负责部分模块」是减少承诺，必须放行 ——
# 否则建议里最有价值的那条（职责表述偏高）永远落不了地
_SENIORITY_WORDS = (
    "主导", "牵头", "独立设计", "独立完成", "独立开发", "全权", "负责整体",
    "从零搭建", "架构设计", "带队", "统筹",
)

# 剥离泛化短语后与原文的相似度超过这个值，就认为"没有新增信息"
PADDING_SIMILARITY = 0.75


def _normalize(text: str) -> str:
    return re.sub(r"[\s，,。；;、：:（）()]+", "", text or "")


def _strip_generic(text: str) -> str:
    for pattern in _GENERIC_PATTERNS:
        text = pattern.sub("", text)
    return text


def _is_empty_padding(before: str, after: str) -> str:
    """判断这次改动是不是"加了等于没加"。返回拒绝原因，空串表示通过。

    判据：去掉「后端开发」「相关工作」「的能力」这类泛化短语后，
    如果新句与原文仍有 82% 以上重合，且没有引入任何新的数字/英文技术名，
    那这次改写就没有增加信息 —— 只是把句子说长了一点。
    """
    if not before or not after:
        return ""

    # 先判「降级」：原文有抬高职责的词、改后没有了 → 这不是注水，是收敛表述。
    # 相似度必然很高（只换了一个词），但那恰恰是我们要的改法
    if any(w in before for w in _SENIORITY_WORDS) and not any(
        w in after for w in _SENIORITY_WORDS
    ):
        return ""

    # 只换同义动词：直接判死
    for old_w, new_w in _SYNONYM_SWAPS:
        if old_w in before and new_w in after:
            if _normalize(after.replace(new_w, old_w)) == _normalize(before):
                return f"只把「{old_w}」换成了「{new_w}」，属于同义词替换"

    core_before = _normalize(before)
    core_after = _normalize(_strip_generic(after))
    if not core_after:
        return ""

    # 新增了数字或英文技术名 → 确实补了具体信息，放行
    new_latin = set(re.findall(r"[A-Za-z][A-Za-z0-9+#._-]*", after)) - set(
        re.findall(r"[A-Za-z][A-Za-z0-9+#._-]*", before)
    )
    new_digits = set(re.findall(r"\d+", after)) - set(re.findall(r"\d+", before))
    if new_latin or new_digits:
        return ""

    from difflib import SequenceMatcher

    ratio = SequenceMatcher(None, core_before, core_after).ratio()
    if ratio >= PADDING_SIMILARITY:
        return f"改写后与原文 {ratio:.0%} 重合，只是把句子说长了一点，没有新增信息"
    return ""


def _job_brief(job: dict) -> str:
    parts = [
        f"岗位：{job.get('title') or '未命名'}",
        f"城市：{job.get('city') or '未标注'}",
        f"学历要求：{job.get('edu_level') or '未标注'}",
        "年限要求：" + (
            f"{job.get('exp_min')}-{job.get('exp_max') or '不限'} 年"
            if job.get("exp_min") is not None
            else "未标注"
        ),
        "必备技能：" + ("、".join(job.get("must_have_skills") or []) or "未列出"),
        "加分技能：" + ("、".join(job.get("nice_to_have_skills") or []) or "未列出"),
    ]
    return "\n".join(parts)



def _zero_note(gaps: list[str], rejected: list[dict]) -> str:
    """0 处改动时给用户一句准确的解释。

    三种情况不能混为一谈：
      1. 岗位要求都覆盖了 → 不用改
      2. 岗位要的技能简历里确实没有 → 不能凭空补（补了面试会露馅）
      3. 模型改了但都被校验拦下 → 说明改法不合规，看 rejected
    """
    if rejected:
        return f"{len(rejected)} 处改动未通过校验（多为编造事实或空话），文件按原文输出；原因见下方"
    if gaps:
        return (
            "岗位要求的这些技能在简历里找不到对应事实，不能凭空补写"
            "（写进去面试一追问就露馅）；已按原文输出，你可以在下载的文件上自己补"
        )
    return "简历已覆盖该岗位要求，无需改动；已按原文输出一份 Word"


async def tailor_for_job(
    resume_id: int, job_id: int, hints: list[str] | None = None
) -> dict:
    """生成一份针对该岗位的定制简历。返回 diff、摘要、下载信息与成本。

    hints 是建议面板生成的诊断条目（problem + direction）。
    带上它才能让"看的建议"和"改的东西"是同一件事 ——
    否则用户在建议页看到 7 条问题，点生成却只按技能缺口改，
    两边对不上。
    """
    with get_pool().connection() as conn:
        detail = repo.get_resume_detail(conn, resume_id)
        if detail is None:
            return {"error": "简历不存在"}
        profile = detail.get("profile")
        if not profile:
            return {"error": "这份简历还没有画像，先等解析完成"}
        file_path = repo.get_resume_file_path(conn, resume_id)
        job = repo.get_job_profile(conn, job_id) or {}
        if not job:
            return {"error": "岗位不存在或还没有抽取结果"}

    source = Path(file_path) if file_path else None
    if source is None or not source.exists():
        return {"error": "找不到原始简历文件"}

    # ---- 1. 准备可编辑文档 ----
    # 原则：能在用户自己的文件上改，就绝不重排。
    # Word 直接改 run 文字，版式一分不动；PDF 先做保版式转换（pdf2docx 按坐标还原），
    # 转不动才回退到按画像重建 —— 那是最后手段，因为版式会变，用户基本不接受。
    basic = profile.get("basic") or {}
    work_path = TAILOR_DIR / f"resume{resume_id}-job{job_id}.docx"
    if source.suffix.lower() == ".docx":
        editor = DocxEditor(source)
        origin_kind = "docx"
    else:
        converted = TAILOR_DIR / f"resume{resume_id}-frompdf.docx"
        if convert_pdf_to_docx(source, converted):
            editor = DocxEditor(converted)
            origin_kind = "pdf_converted"
        else:
            # 姓名与联系方式从原文件读：画像里的文本是脱敏过的，直接用会变成 [姓名]
            name, contact = read_header_text(source)
            rebuilt = build_docx_from_profile(
                profile,
                TAILOR_DIR / f"resume{resume_id}-rebuilt.docx",
                name=name,
                contact=contact,
            )
            editor = DocxEditor(rebuilt)
            origin_kind = "rebuilt"

    # 全文交给模型。简历单段很少超过千字，整份也就一两千字，
    # 截断省不了多少 token，但会让模型照着残缺的句子改写，
    # 写回时直接丢掉后半段（实测把个人优势那段砍成了「具备基础的 …」）
    outline = editor.outline(max_chars=2000)
    if not outline:
        return {"error": "这份文档没有可编辑的文字段落"}

    # 匹配缺口复用已有引擎，不重算一套
    match = match_one(profile, job)
    gaps = list(match.missed_skills or [])

    hints_block = ""
    if hints:
        # 只把「表达层面」的问题交给模型参考。事实矛盾（时间线、项目名不一致）
        # 必须用户自己确认，写进 prompt 只会诱导模型去猜一个"正确"的年份
        hints_block = (
            "\n\n用户已生成的诊断（供参考，不要为了凑数去改事实）：\n"
            + "\n".join(f"- {h}" for h in hints[:8])
            + "\n\n其中：\n"
            "· 涉及时间、学历、项目名称、专业方向的矛盾 → 一律不得自行修改，交回用户确认；\n"
            "· 涉及职责程度偏高的措辞（主导/独立/架构）→ **属于可改范围**，按约束 4 降低表述；\n"
            "· 涉及期望城市/到岗意愿 → 若与原简历其他信息不冲突，可以按诊断调整措辞。"
        )

    user = (
        f"{_job_brief(job)}\n\n"
        f"简历没覆盖的必备技能：{'、'.join(gaps) or '（无）'}"
        f"{hints_block}\n\n"
        f"简历段落清单（JSON）：\n{_outline_json(outline)}\n\n"
        "请给出改动清单。"
    )

    result = await chat_json(
        TAILOR_SYSTEM_PROMPT, user, what="简历定制", max_tokens=3000
    )
    payload = parse_json_object(result.text, what="简历定制")
    ops = payload.get("ops") or []
    summary = str(payload.get("summary") or "").strip()

    # ---- 3. 逐条校验并应用 ----
    protected = _protected_pids(editor, basic)
    full_text = editor.full_text()
    accepted = 0
    rejected: list[dict] = []

    for op in ops[:MAX_OPS]:
        kind = str(op.get("op") or "").strip()
        pid = str(op.get("id") or "").strip()
        text = str(op.get("text") or "").strip()
        reason = str(op.get("reason") or "").strip()
        evidence = str(op.get("evidence") or "").strip()

        if not editor.exists(pid):
            rejected.append({"pid": pid, "why": "段落 id 不存在", "text": text})
            continue
        if pid in protected:
            rejected.append({"pid": pid, "why": "该段落属于不得改动的区域（姓名/联系方式/教育）", "text": text})
            continue
        if not evidence:
            rejected.append({"pid": pid, "why": "缺少原文依据", "text": text})
            continue

        # 空话拦截：加了等于没加的改写一律拒绝，避免整篇被同一个短语污染
        padding = _is_empty_padding(editor.get_text(pid), text)
        if padding:
            rejected.append({"pid": pid, "why": padding, "text": text})
            continue

        # 事实下限：数字与英文专名必须能在原文找到（宽松模式也保留这条）
        check = check_example(text, full_text)
        if not check.passed:
            rejected.append({"pid": pid, "why": "事实核查未通过：" + check.summary, "text": text})
            continue

        if kind == "insert":
            change = editor.insert_after(pid, text, reason, evidence)
        elif kind == "replace":
            change = editor.replace(pid, text, reason, evidence)
        else:
            rejected.append({"pid": pid, "why": f"未知操作 {kind}", "text": text})
            continue

        if change.accepted:
            accepted += 1
        else:
            rejected.append({"pid": pid, "why": change.rejected_why, "text": text})

    if accepted == 0:
        # 一处都没采纳也要给文件。
        # 之前的做法是删掉产物、不返回 download_url —— 用户等了 40 秒，
        # 结论是"没有可改的地方"，却连一份能自己动手的文件都拿不到。
        # 现在照常保存：内容等同原文，但用户可以在 Word 里自己补那段缺的经历
        saved = editor.save(work_path)
        logger.info(
            "简历 %s 定制完成但无采纳改动：%d 条被拒，来源 %s，已按原文输出 %s",
            resume_id, len(rejected), origin_kind, saved.name,
        )
        return {
            "resume_id": resume_id,
            "job_id": job_id,
            "job_title": job.get("title"),
            "origin": origin_kind,
            "accepted": 0,
            "rejected": rejected,
            "diff": editor.diff(),
            "summary": summary,
            "file_name": saved.name,
            "download_url": f"/api/resumes/{resume_id}/tailored/{job_id}/file",
            # 这不是错误：模型判断这份简历对该岗位无需改动，或所有改动都没通过校验。
            # 两种都是有效结论，前端按"没有可改的地方"展示，不弹红字。
            "note": _zero_note(gaps, rejected),
            "gaps": gaps,
            "cost_cny": round(result.cost_cny, 4),
        }

    saved = editor.save(work_path)
    logger.info(
        "简历 %s 定制完成：%d 条采纳 / %d 条拒绝，来源 %s，¥%.4f",
        resume_id, accepted, len(rejected), origin_kind, result.cost_cny,
    )

    return {
        "resume_id": resume_id,
        "job_id": job_id,
        "job_title": job.get("title"),
        "origin": origin_kind,
        "accepted": accepted,
        "rejected": rejected,
        "diff": editor.diff(),
        "summary": summary,
        "file_name": saved.name,
        "download_url": f"/api/resumes/{resume_id}/tailored/{job_id}/file",
        "cost_cny": round(result.cost_cny, 4),
        "gaps": gaps,
    }


def _outline_json(outline: list[dict]) -> str:
    import json

    # 只给模型 id 与文本，样式名对它没用，反而占上下文
    slim = [{"id": o["id"], "text": o["text"]} for o in outline]
    return json.dumps(slim, ensure_ascii=False)
