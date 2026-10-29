"""导出前的可读性校验：这份简历，机器读得出来吗。

为什么需要它
    我们改完简历就把文件交出去，但从没验过"下游能不能读"。招聘系统
    （Workday / Taleo / 北森这类 ATS）不是人，它不看你排得多好看，只看
    能不能从文件里抽出一串有顺序的文本。常见的失败不是"排版丑"，而是：

    - 内容在文本框 / 图片里 → 抽出来是空的，简历直接被判定为不合格
    - 日期用了 en dash（2023–2024）→ 部分系统把整段日期连同年份一起吞掉
    - 一整段职责被合成一个 1200 字的段落 → 系统无法切分成条目，关键词密度
      算出来全是噪音
    - 联系方式被排成两行（邮箱断行）→ 正则匹配不到，系统认为你没留邮箱

    这些都是**我们生成的文件的属性**，不是模型能力问题，所以可以用确定性代码验。

为什么不用 PDF 验
    我们交给用户的产物是 DOCX（原文件是 docx 就直接改，是 pdf 就转成 docx 再改），
    PDF 是用户自己导出后的下一步。验 DOCX 才能在我们还能改的时候发现问题。

为什么不出一个总分
    五项检查里，"文本层空了"和"日期用了 en dash"的严重程度差一个数量级。
    压成一个 85 分只会掩盖这个差别，用户不知道该先修哪个。所以只报
    「几项通过 / 几项注意 / 几项未通过」+ 每项的具体成因。
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from ..ingest.layout import Block

# A4 + 上下 2cm / 左右 2cm 边距 + 五号字（10.5pt）的可容纳量。
# 这两个常数只用于估算页数，误差约 ±0.3 页 —— 够用来判断"还在两页内"
# 还是"明显超了"，不够用来判断"刚好 2.0 页"。所以结论措辞必须带"预计"。
CHARS_PER_LINE = 86
LINES_PER_PAGE = 44

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"1[3-9]\d{9}")
# 列表编号：一段里本该是一条 bullet，出现三个以上编号说明多条被并成了一块
BULLET = re.compile(r"(?:^|[\s；;。])(?:\d{1,2}[.、)]|[•·●▪◆])")

# 破折号区间：en dash / em dash / 减号 / 全角破折号。
# LaTeX 与不少模板会把 "2023-2024" 排成 en dash，而 Workday 的导入会把它
# 连同整段日期一起丢掉。
#
# 判据不能写成「年份 + 破折号 + 年份」：真实简历里最常见的是
# "2026.07–2026.09"，年份后面先跟 .07 才到破折号，那种写法一条都匹配不到
# （第一版就是这么写的，被测试直接打脸）。改成「破折号两侧都是数字」，
# 再用 _spans 过滤出附近确实有四位年份的 —— 这样 "3–5 年经验" 不会被当成日期。
DASH_RANGE = re.compile(r"\d[\d.\-/]*\s*[\u2012\u2013\u2014\u2015\u2212]\s*\d[\d.\-/]*")

# 判断「一段里塞了几条经历」用的是更宽的口径：半角连字符和中文的「至/到/~」
# 也要算。这里的目的是数条目，不是判风险 —— 用半角连字符写日期本身没问题，
# 但一条里出现两个日期区间就说明几条经历被并进了一段。
SPAN_ANY = re.compile(r"\d[\d.\-/]*\s*[-–—−~至到]\s*\d[\d.\-/]*")

YEAR = re.compile(r"(?:19|20)\d{2}")

# 判断日期时，破折号前后各看这么多字符里有没有年份
_YEAR_WINDOW = 12


def _spans(text: str, pattern: re.Pattern[str]) -> list[str]:
    """取出文本里「看起来是日期区间」的数字区间片段。

    只保留附近有四位年份的：连字符和破折号在中文排版里也用于其他场合
    （"3-5 年"、"2-3 个"），单看符号会把正常行文算成日期。
    """
    out: list[str] = []
    for m in pattern.finditer(text):
        window = text[max(0, m.start() - _YEAR_WINDOW): m.end() + _YEAR_WINDOW]
        if YEAR.search(window):
            out.append(m.group(0))
    return out


def _range_spans(text: str) -> list[str]:
    """会被 ATS 吞掉的长横线日期（只认 en/em dash 这类）。"""
    return _spans(text, DASH_RANGE)


def _any_date_spans(text: str) -> list[str]:
    """任意写法的日期区间，只用于数「这一段里有几条经历」。"""
    return _spans(text, SPAN_ANY)

SECTION_WORDS = (
    "教育", "工作经历", "实习经历", "项目经历", "项目经验", "技能", "证书",
    "荣誉", "奖项", "自我评价", "校园经历", "实践经历", "专业技能", "个人信息",
)


def _width(text: str) -> int:
    """显示宽度：中日韩字符按 2 个半角算。用字符数会低估中文段落一倍。"""
    total = 0
    for ch in text:
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def estimate_pages(blocks: list[Block]) -> dict:
    """按显示宽度估算行数与页数。

    这是估算不是测量：真正排版时有段前间距、表格、分页符。所以只在
    "是否明显超过两页" 这个量级上用它，并且界面上写明"预计"。
    """
    chars = sum(len(b.text) for b in blocks)
    lines = sum(max(1, -(-_width(b.text) // CHARS_PER_LINE)) for b in blocks)
    return {
        "chars": chars,
        "lines": lines,
        "paragraphs": len(blocks),
        "pages": round(lines / LINES_PER_PAGE, 1),
    }


def _normalize(text: str) -> str:
    """比较用的归一：全角转半角、去空白、小写。

    简历里的 "MySQL" 和 "mysql"、技能间的全角顿号，都该算命中。
    """
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", "", text).lower()


def keyword_hits(text: str, skills: list[str]) -> tuple[list[str], list[str]]:
    """必备技能在简历正文里的命中情况。

    只做字面包含，不做同义词推断 —— 这是"文件里有没有这个词"的检查，
    不是匹配打分。用同义词表会把"简历里没写 MySQL"说成"写过了"，
    而这条检查的全部价值就在于指出没写的那个词。
    """
    flat = _normalize(text)
    hit, miss = [], []
    for skill in skills:
        key = _normalize(skill)
        if not key:
            continue
        (hit if key in flat else miss).append(skill)
    return hit, miss


def cut_candidates(blocks: list[Block], job: dict, limit: int = 5) -> list[dict]:
    """超出两页时，按"删了损失最小"排序给出候选段落。

    为什么不是"从最旧的经历开始删"
        一条旧经历的某行如果正好命中 JD 的关键词，它比一条新的、却没提到
        任何岗位要求的行更值得留。按板块顺序机械地砍，会把最相关的内容删掉。

    打分口径（越小越该删）
        relevance  与 JD 技能/岗位名的重合度，0~1
        unique     这段的核心词是否在别处也出现过，重复的更容易删
    跳过：联系方式 / 姓名 / 学校（_protected）、板块标题（删了结构就断了）。
    """
    skills = [s for s in (job.get("must_have_skills") or []) + (job.get("nice_to_have_skills") or []) if s]
    title_words = [w for w in re.split(r"[\s/、,，]+", job.get("title") or "") if len(w) >= 2]
    keys = skills + title_words

    protected = {_normalize(x) for x in (
        (job.get("_name"), job.get("_school"), job.get("_email"), job.get("_phone"))
    ) if x}

    norm_all = [_normalize(b.text) for b in blocks]
    out: list[dict] = []
    for i, block in enumerate(blocks):
        text = block.text.strip()
        if _width(text) < 14:
            continue  # 姓名、联系方式、板块标题这类短行，删了只会更糟
        flat = _normalize(text)
        if any(p and p in flat for p in protected):
            continue
        if any(w in text for w in SECTION_WORDS) and _width(text) <= 12:
            continue

        # 用前 8 个字做"是否重复"的指纹，太短会误判、太长会漏判
        fingerprint = flat[:8]
        duplicated = bool(fingerprint) and sum(1 for j, n in enumerate(norm_all)
                                               if j != i and fingerprint in n) > 0
        hits = sum(1 for k in keys if _normalize(k) and _normalize(k) in flat)
        relevance = min(hits / 2.0, 1.0)

        score = relevance * 2 + (0.0 if duplicated else 1.0)
        if duplicated:
            why = "这段和简历别处重复，删了信息不丢"
        elif hits:
            why = f"提到 {hits} 个岗位关键词，属于最后才考虑的"
        else:
            why = "没提到任何岗位要求"
        out.append({"text": text[:80], "score": round(score, 2), "why": why,
                    "width": _width(text)})

    out.sort(key=lambda x: x["score"])
    return out[:limit]


def check_docx(path: Path, blocks: list[Block], *, job: dict | None = None,
               basic: dict | None = None) -> dict:
    """跑全部检查。返回 {level, checks, stats, keywords}。

    level 只由最严重的那一项决定：有任何 fail 就是 fail，否则有 warn 就是
    warn。不做加权 —— 加权会把"文本层空了"稀释成"总分 70，还行"。
    """
    job = job or {}
    basic = basic or {}
    text = "\n".join(b.text for b in blocks)
    stats = estimate_pages(blocks)
    checks: list[dict] = []

    def add(key: str, level: str, title: str, detail: str) -> None:
        checks.append({"key": key, "level": level, "title": title, "detail": detail})

    # 1. 文本层
    if stats["chars"] < 200:
        add("text_layer", "fail", "文本层几乎是空的",
            f"只抽出 {stats['chars']} 个字符。内容可能在做文本框或图片里，"
            "招聘系统会认为这份简历没有内容")
    else:
        add("text_layer", "pass", "文本可抽取", f"{stats['chars']} 个字符、{stats['paragraphs']} 个段落")

    # 2. 联系方式
    want_email = (basic.get("email") or "").strip()
    want_phone = (basic.get("phone") or "").strip()
    emails = EMAIL.findall(text)
    phones = PHONE.findall(text)
    if want_email and want_email not in text:
        add("contact", "fail", "邮箱读不出来",
            f"画像里的邮箱 {want_email} 在导出文件里找不到 —— "
            "常见原因是它被排进了表格或文本框，或断成了两行")
    elif want_phone and want_phone not in text:
        add("contact", "warn", "手机号读不出来",
            f"画像里的手机号 {want_phone} 在导出文件里找不到，可能被分组或加空格拆开了")
    elif emails or phones:
        add("contact", "pass", "联系方式可读", f"识别到 {'邮箱' if emails else ''}{'、手机' if phones else ''}")
    else:
        add("contact", "warn", "没找到邮箱和手机号",
            "如果简历里本来就没写，ATS 无法联系你；如果写了却没读到，说明被排版挡住了")

    # 3. 关键词覆盖
    skills = list(job.get("must_have_skills") or [])
    if skills:
        hit, miss = keyword_hits(text, skills)
        ratio = len(hit) / len(skills)
        if ratio >= 0.99:
            add("keywords", "pass", "必备技能全部出现", f"{len(hit)}/{len(skills)}")
        elif ratio >= 0.5:
            add("keywords", "warn", "部分必备技能没出现",
                f"{len(hit)}/{len(skills)}，缺：{'、'.join(miss[:6])}")
        else:
            add("keywords", "warn", "多数必备技能没出现",
                f"{len(hit)}/{len(skills)}，缺：{'、'.join(miss[:6])}。"
                "如果确实不会，不要硬写；确认会写的要补进技能段")
    else:
        hit, miss = [], []
        add("keywords", "skip", "没有可对照的技能要求", "这个岗位没抽出必备技能，跳过关键词检查")

    # 4. 日期分隔符
    dash_hits = _range_spans(text)
    if dash_hits:
        samples = "、".join(dash_hits)[:60]
        add("date_dash", "warn", "日期用了长横线",
            f"共 {len(dash_hits)} 处（{samples}）。部分招聘系统会把这种横线连同年份一起丢掉，"
            "统一改成半角连字符 2023-2024 最稳")
    else:
        add("date_dash", "pass", "日期格式安全", "没有发现会被吞掉的长横线")

    # 5. 段落是否被合成一整块
    #
    # 最初按「超过两行就算长」判，实测在一份正常的 PDF 转 DOCX 上直接误报 7 处 ——
    # 两到三行的项目描述本来就该这么写。真正会让 ATS 出错的是**多个条目被并进
    # 同一个段落**：它靠段落边界切分经历，一段里塞三段实习，会被读成一句话。
    #
    # 所以判据只认两件有 ATS 后果的事：
    #   这一段里出现多个日期区间（几条经历被并进来）
    #   这一段里出现多个列表编号（几条 bullet 被并成一坨）
    # 不用「句末标点数量」：一段项目描述里出现五个分号是正常写作，
    # 试过那一条，立刻在真实简历上误报了 —— 分号多不代表结构坏。
    merged: list[str] = []
    for b in blocks:
        width = _width(b.text)
        spans = _any_date_spans(b.text)
        # 多个日期区间不受长度限制：条目写得短的时候，两条经历并成一段
        # 可能只有两行，但它照样会让 ATS 读成一句
        if len(spans) >= 2:
            merged.append(b.text[:40])
            continue
        # 2.5 行是下限而不是 3 行：真实简历里"四条编号挤在一段"往往正好
        # 是两行半，卡在 3 行会让最该报的那种漏掉（实测就差 4 个半角宽）
        if width <= CHARS_PER_LINE * 2.5:
            continue
        bullets = len(BULLET.findall(b.text))
        # 编号这一条要在「两行半以上」才判：出现三个以上编号，说明几条本可
        # 独立成段的条目被压在一起。两行内写 "1、x；2、y" 是常见写法，
        # 那种不报 —— 报了也只是让人把一个正常段落拆成两个，没有收益
        if bullets >= 3 or width > CHARS_PER_LINE * 12:
            merged.append(b.text[:40])
    if merged:
        add("structure", "warn", f"{len(merged)} 个段落里塞了多个条目",
            f"例如「{merged[0]}…」。ATS 靠段落切分经历，多个条目并成一段会被"
            "读成一句话，关键词密度也跟着失真。拆成一段一条即可")
    else:
        add("structure", "pass", "段落切分正常",
            f"{len(blocks)} 个段落，没有把多段经历并成一块")

    # 6. 页数预算（学生简历超过 2 页基本没人翻）
    pages = stats["pages"]
    if pages > 2.2:
        add("pages", "warn", "预计超过 2 页",
            f"按字数估算约 {pages} 页。删内容而不是压缩行距 —— 压缩只会让版面更挤")
    else:
        add("pages", "pass", "页数在预算内", f"预计约 {pages} 页")

    levels = {c["level"] for c in checks}
    level = "fail" if "fail" in levels else ("warn" if "warn" in levels else "ok")

    result = {
        "level": level,
        "checks": checks,
        "stats": stats,
        "keywords": {"required": skills, "hit": hit, "miss": miss},
        "file": path.name,
        # 计数而不是比率：3 项通过 / 1 项注意 是事实，"85 分" 是包装
        "counts": {
            "pass": sum(1 for c in checks if c["level"] == "pass"),
            "warn": sum(1 for c in checks if c["level"] == "warn"),
            "fail": sum(1 for c in checks if c["level"] == "fail"),
        },
    }
    if pages > 2.2:
        result["cut_candidates"] = cut_candidates(blocks, job)
    return result
