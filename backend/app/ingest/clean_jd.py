"""JD 原文清洗：剥掉页面上的界面噪声，只留岗位内容。

Boss 详情页的 innerText 里混着一大堆非正文内容：

    无人驾驶安全员/五险一金/双休
    佛山 / 3-5年 / 中专/中技
    收藏 / 立即沟通 / 举报 / 微信扫码分享     ← 按钮
    职位描述                                  ← 真内容的起点
    ...
    饶丽 / 在线 / 华夏汇能科技 · 人事          ← 招聘者名片
    去App / 与BOSS随时沟通                     ← 下载引导
    工作地址 / 点击查看地图                    ← 地址模块
    求职工具 / 升级VIP / 尊享8大特权
    热门职位 / 热门城市 / 附近城市             ← 整块站内 SEO 链接
    佛山咨询经理招聘佛山理科教师招聘佛山销售助理招聘...

不清洗的后果很直接：这些字会被当成 JD 正文送去抽取，
「司机」「双休」「五险一金」可能被抽成必备技能，城市名变成岗位地点，
而"热门城市"那一段甚至会把一堆招聘岗位名塞进描述里。

设计口径：**只删确定的界面噪声，不做任何语义改写**。
清洗后如果短得离谱（说明规则误伤），直接回退原文 —— 宁可脏，不可丢。
"""

from __future__ import annotations

import re

# 整行等于这些内容时删除。都是 Boss 详情页的固定界面文案
NOISE_LINES = {
    "收藏",
    "立即沟通",
    "继续沟通",
    "举报",
    "微信扫码分享",
    "查看更多信息",
    "点击查看地图",
    "工作地址",
    "求职工具",
    "升级VIP",
    "去升级",
    "去App",
    "前往App",
    "与BOSS随时沟通",
    "与BOSS随时沟通",
    "在线",
    "职位描述",
    "职位要求",
    "公司介绍",
    "展开",
    "收起",
    "查看更多",
    "查看全部",
    "提升求职效率",
    "求职效率",
    "尊享8大特权",
    "分割线",
}

# 形如 "-K"、"−K·薪"、"-元/天" 的薪资占位：Boss 未登录或未渲染时会渲染成这样，
# 留在正文里只会让抽取把薪资格式判错
NOISE_PATTERNS = [
    # 薪资占位：ASCII 连字符、全角减号、各种破折号、真正的减号 U+2212 都要覆盖
    re.compile(r"^[\-\u2212－—–~～\s]*K\s*(·?\s*\d*\s*薪)?$"),
    re.compile(r"^[\-\u2212－—–~～\s]*元/天$"),
    re.compile(r"^尊享\d+大特权.*$"),
    re.compile(r"^\d+\s*人?\s*看过$"),
    re.compile(r"^刚刚活跃$|^今日活跃$|^\d+小时内活跃$"),
]

# 招聘者名片：「公司 · 人事」「公司·HR」这类行，正文里不会这么写。
# 角色后缀大小写不固定（实测出现过「· Hr」），所以整条按忽略大小写匹配
RECRUITER_LINE = re.compile(
    r"^.{2,24}\s*[·•]\s*(人事|HR|招聘者|招聘专员|招聘经理|招聘|经理|总监|主管|CEO|创始人|合伙人|boss)$",
    re.IGNORECASE,
)
# 名片上方那行通常是招聘者姓名（2–4 个汉字、无标点）
PERSON_NAME = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")

# 出现即截断（这些之后的内容都是页面模块，不属于 JD）
STOP_PREFIXES = (
    "工作地址",
    "查看更多信息",
    "求职工具",
    "热门职位",
    "热门城市",
    "热门企业",
    "附近城市",
    "点击查看地图",
    "去App",
    "前往App",
    "与BOSS随时沟通",
    "BOSS直聘招聘",
    "相关搜索",
    "相关推荐",
    "投诉",
)

# 站内 SEO 链接块：一行里塞了一串「XX招聘」，长短不一但没有标点
_SEO_LINK = re.compile(r"(招聘.{0,6}?){3,}")

# Boss 把薪资数字渲染成私有区字形（U+E000–U+F8FF）来防采集。
# 这些字符进入文本后既读不出数值，也会在界面上显示成方块，
# 所以先把它们剔掉，让「−K」「−K·薪」回到可识别的形态再由噪声规则删掉。
_PUA = re.compile(r"[\ue000-\uf8ff]")


# 组合噪声行：「收藏 / 立即沟通 / 举报 / 微信扫码分享」把几个按钮文案塞在一行。
# 插件抓 innerText 时这些按钮通常各占一行，单行规则就能删掉；
# 但用户手动 Ctrl+A 复制网页内容时，它们常被压成一行 —— 整行不等于任何一个
# 噪声词，于是混进正文被送去抽取，实测会让"举报""扫码分享"这类词干扰技能判断。
# 判据从严：必须被分隔符切成 2~6 段，且**每一段**都是已知噪声才删。
# 正文里的「Java / Python」也会被切成多段，但那些词不在表里，不会误伤。
_COMBO_SEP = re.compile(r"\s*[/|·•、,，]\s*")


def _is_combo_noise(line: str) -> bool:
    parts = [p.strip() for p in _COMBO_SEP.split(line) if p.strip()]
    if not 2 <= len(parts) <= 6:
        return False
    return all(p in NOISE_LINES for p in parts)


def _is_noise(line: str) -> bool:
    if line in NOISE_LINES:
        return True
    if _is_combo_noise(line):
        return True
    if any(p.match(line) for p in NOISE_PATTERNS):
        return True
    # 一行里出现 3 次以上「招聘」，是站内链接堆，不是正文
    if len(line) > 24 and len(_SEO_LINK.findall(line)) >= 3:
        return True
    return False


def _is_stop(line: str) -> bool:
    return any(line.startswith(prefix) for prefix in STOP_PREFIXES)


def _strip_recruiter_card(lines: list[str]) -> list[str]:
    """去掉末尾的招聘者名片：一行「公司 · 人事」，上面一行是姓名。

    正文里不会出现「某公司 · 人事」这种行，所以这条规则是安全的；
    姓名只在紧跟名片时才删，不会误伤正文里的短行。
    """
    out: list[str] = []
    for line in lines:
        if RECRUITER_LINE.match(line):
            # 名片上方若是一个 2–4 字的纯姓名，一起收掉
            if out and PERSON_NAME.match(out[-1]):
                out.pop()
            continue
        out.append(line)
    return out


def clean_jd(text: str) -> str:
    """返回清洗后的 JD 文本。无法判断时原样返回，绝不把原文清空。"""
    raw = (text or "").strip()
    if not raw:
        return raw

    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [_PUA.sub("", ln).strip() for ln in lines]
    lines = [ln for ln in lines if ln]

    head: list[str] = []       # 职位描述之前：标题 / 城市 / 年限 / 学历
    body: list[str] = []       # 职位描述之后：真内容
    in_body = False
    seen_head: set[str] = set()

    for line in lines:
        if not in_body:
            # 正文起点。原来只认「职位描述 / 职位要求 / 岗位职责」，
            # 但不少 JD 用的是「任职要求」「工作内容」开头 —— 认不出来就会走
            # fallback（整段当正文），虽然结果差不多，但 head/body 的切分失准，
            # 招聘者名片和 SEO 链接更容易漏网
            if line in ("职位描述", "职位要求", "岗位职责",
                        "任职要求", "工作内容", "任职资格", "工作要求"):
                in_body = True
                continue
            if _is_stop(line) or _is_noise(line):
                continue
            # 标题常常重复两遍（列表卡片 + 详情头部）
            if line in seen_head:
                continue
            seen_head.add(line)
            head.append(line)
            # 头部最多留 5 行，再往后就不是头部信息了
            if len(head) > 5:
                head.pop()
            continue

        if _is_stop(line):
            break
        if _is_noise(line):
            continue
        body.append(line)

    if not body:
        # 没有「职位描述」这类小节标题（手动粘贴的 JD 常见），
        # 就整段按正文处理，同样套用截断与去噪
        body = [ln for ln in lines if not _is_noise(ln) and not _is_stop(ln)]
        head = []

    body = _strip_recruiter_card(body)

    kept = head + body
    # 连续重复行去重（正文里也可能有重复的标题行）
    deduped: list[str] = []
    for line in kept:
        if deduped and deduped[-1] == line:
            continue
        deduped.append(line)

    # 原文本里的空行在清洗时被丢掉了，这里按小节标题（以冒号结尾）补回来。
    # 不补的话整段会挤成一片，读起来非常吃力 —— 而这份文本是要给人看的
    parts: list[str] = []
    for line in deduped:
        if parts and (line.endswith("：") or line.endswith(":")):
            parts.append("")
        parts.append(line)

    cleaned = "\n".join(parts).strip()

    # 安全阀：清洗后几乎没剩下东西，说明规则误伤，宁可保留原文。
    #
    # 这里原本写的是"不足原文 25% 就回退"，实测被它坑了一次：#29 那份 JD
    # 正文只有三行（岗位职责/任职要求/其他详聊），449 字里 380 字是页面噪声，
    # 清洗后剩 90 字，占比 20% —— 明明清对了，却被判成误伤而退回脏文本。
    # 噪声占比高的短 JD 很常见，"占比"不是可靠信号，所以只保留绝对下限。
    if len(cleaned) < 40:
        return raw
    return cleaned
