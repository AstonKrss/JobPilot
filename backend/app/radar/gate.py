"""可达性闸门：先判断「这个岗位你能不能进」，再谈「值不值得去」。

顺序为什么不能反
    一个 85 分的岗位，如果要求硕士或者 4 年经验，对这个用户就是 0 价值。
    把它排进前十，用户点开三次之后就不再相信这个列表了 —— 排序的可信度
    比排序的精确度重要得多。所以先把不可达的整批摘出去，剩下的再比质量。

口径按「双非本科在读 + 0.7 年实习」定
    这是本项目的实际用户画像。换成工作三年的人，EXPERIENCE 的分档要整体右移，
    所以分档表集中放在模块顶部，改口径只改一处。

    特别注意「经验」这一条：佛山大量 Java 岗写的是 3-5 年，那是社招岗，
    校招/实习投过去基本没有回音。把它们判为 blocked 不是为了少给结果，
    而是为了让"你库里 19 个佛山岗位只有 4 个能投"这个事实浮出来 ——
    这个事实本身比多推 15 条噪声有用。
"""

from __future__ import annotations

import re

# 学历序。null（原文没提）不参与比较，见 _check_edu
EDU_RANK: dict[str, int] = {"不限": 0, "大专": 1, "本科": 2, "硕士": 3, "博士": 4}

# 经验分档（年）。用户 0.7 年实习，取 0-1 为可达
EXP_OK_MAX = 1
EXP_STRETCH_MAX = 2
# 超过这个值判为社招岗，校招/实习不投
EXP_BLOCK_MIN = 3

GRADE_LABEL = {"reachable": "可达", "stretch": "勉强", "blocked": "不可达", "unknown": "待确认"}

# ---------------------------------------------------------------------------
# 先判方向，再判门槛
#
# 起因很具体：雷达第一版把「秩序类管培生（物业安全管理方向）」排到了第一。
# 它公司分高（美的系）、城市对（佛山）、学历不限、经验不限 —— 每一项都合格，
# 唯一的问题是它跟"Java 后端"没有任何关系。硬约束全是"没违反"，
# 于是所有闸门都放行了。
#
# 这说明：可达性判定之外，必须先有一道"这是不是一个技术岗"的问题。
# 它不该压缩成一个低分 —— 方向不对是"不在射程内"，不是"分数低一点"。
#
# 判定用标题 + 技能，不用向量：标题是岗位意图最凝练的表达，
# 而向量在这里会把「测试安全员」判得离「测试工程师」很近。
# ---------------------------------------------------------------------------
TECH_MARKS: tuple[str, ...] = (
    "开发", "研发", "工程师", "程序员", "后端", "前端", "全栈", "算法",
    "数据", "测试", "运维", "架构", "嵌入式", "爬虫", "实施", "软件开发",
    "java", "python", "golang", "go", "c++", "c#", "php", "node", "react",
    "vue", "sql", "devops", "sre", "it", "mis", "erp", "sap",
)

# 只看标题。技能要求里出现"运营"往往是「运营系统开发」这种正经岗位，
# 拿它做否决会误杀；标题里出现这些词则基本可以确定岗位性质
NON_TECH_MARKS: tuple[str, ...] = (
    "安全员", "注册安全", "安防", "销售", "客服", "剪辑", "拍摄", "主播",
    "物业", "秩序", "保安", "文员", "会计", "出纳", "人事", "行政",
    "地推", "招商", "配送", "司机", "采购", "助教", "店长", "导购",
)


def judge_direction(job: dict) -> tuple[bool, str]:
    """返回 (是否技术岗, 理由)。理由为空表示通过。"""
    title = (job.get("title") or "").lower()
    skills = " ".join((job.get("must_have_skills") or []) + (job.get("nice_to_have_skills") or []))
    both = f"{title} {skills}".lower()

    for mark in NON_TECH_MARKS:
        if mark in title:
            return False, f"标题含「{mark}」，不是技术开发岗"
    for mark in TECH_MARKS:
        if mark in both:
            return True, ""
    return False, "标题与技能要求里都没有技术开发相关词"



def _check_city(job: dict, pref: dict) -> tuple[float, str | None, list, list]:
    """返回 (距离分 0-1, 距离文案, positives, blockers)。"""
    city = (job.get("city") or "").strip()
    positives: list[str] = []
    blockers: list[str] = []
    cities = [c for c in (pref.get("cities") or []) if c]
    nearby = [c for c in (pref.get("nearby_cities") or []) if c]

    if not city:
        return 0.5, "城市未标注", positives, blockers
    # 城市字段偶尔带区县（"佛山·顺德"），用包含判断而不是相等。
    #
    # 先判 nearby 再判 cities：广州同时出现在两个列表里（可接受 + 需跨城），
    # 顺序反了会让广佛通勤被打成"同城"，距离这一维就失去区分度，
    # 佛山本地岗和广州岗的排名变得一样
    if any(c in city for c in nearby):
        positives.append(f"{city} 属邻市，广佛通勤可行")
        return 0.6, "邻市", positives, blockers
    if any(c in city for c in cities):
        positives.append(f"{city} 在通勤范围内")
        return 1.0, "同城", positives, blockers
    blockers.append(f"{city} 超出你设定的通勤范围（{'/'.join(cities)}）")
    return 0.0, "超出范围", positives, blockers


def _check_edu(job: dict, pref: dict) -> tuple[list, list, list]:
    positives: list[str] = []
    cautions: list[str] = []
    blockers: list[str] = []
    need = job.get("edu_level")
    mine = pref.get("edu_level") or "本科"
    if not need:
        return positives, cautions, blockers

    need_rank = EDU_RANK.get(need)
    my_rank = EDU_RANK.get(mine, 2)
    if need == "不限":
        positives.append("学历不限")
    elif need_rank is not None and need_rank > my_rank:
        blockers.append(f"要求{need}，你是{mine}")
    elif need_rank is not None and need_rank <= my_rank:
        positives.append(f"要求{need}，你满足")

    if job.get("edu_fulltime_required") is True:
        # 统招要求只有在明确写了才算数（"本科及以上"不含统招含义，抽取层已按三态处理）。
        # 它属于"要核实"而不是"已满足"，所以进 cautions —— 挂成绿色对勾会让用户
        # 以为这条已经过了，而实际还要回去确认自己的学籍口径
        cautions.append("原文要求统招，需确认你的学籍口径")
    return positives, cautions, blockers


def _check_graduate_year(job: dict, pref: dict) -> tuple[list, list]:
    """届别。校招场景下这是最先看的一个信号，也是最容易一票否决的。"""
    positives: list[str] = []
    blockers: list[str] = []
    years = list(job.get("graduate_years") or [])
    mine = pref.get("graduation_year")

    # 标题兜底：抽取层加 graduate_years 之前入库的岗位没有这个字段，
    # 但「27届管培生」这种标题本身就写着届别。正则比重新抽一遍便宜得多，
    # 而且精度足够 —— 标题里出现「XX届」时几乎不会指别的东西
    if not years:
        title = job.get("title") or ""
        found = re.findall(r"(20\d{2})\s*届", title)
        if not found:
            short = re.findall(r"(?<!\d)(\d{2})\s*届", title)
            found = [f"20{s}" for s in short if 10 <= int(s) <= 40]
        years = [int(y) for y in found]

    if not years:
        return positives, blockers

    years_int = [int(y) for y in years if str(y).isdigit()]
    if not years_int:
        return positives, blockers

    if mine and int(mine) in years_int:
        positives.append(f"明确招 {mine} 届")
    elif mine:
        shown = "、".join(f"{y} 届" for y in years_int)
        blockers.append(f"只招 {shown}，你是 {mine} 届")
    return positives, blockers


def _check_experience(job: dict) -> tuple[list, list, list]:
    """返回 positives / cautions / blockers。"""
    positives: list[str] = []
    cautions: list[str] = []
    blockers: list[str] = []
    exp_min = job.get("exp_min")
    if exp_min is None:
        return positives, cautions, blockers

    if exp_min <= EXP_OK_MAX:
        positives.append("经验门槛在学生范围内")
    elif exp_min <= EXP_STRETCH_MAX:
        cautions.append(f"要求 {exp_min} 年经验，你的实习时长偏短")
    elif exp_min >= EXP_BLOCK_MIN:
        blockers.append(f"要求 {exp_min} 年以上，是社招岗")
    return positives, cautions, blockers


def _check_nature(job: dict, pref: dict) -> tuple[list, list]:
    positives: list[str] = []
    cautions: list[str] = []
    nature = job.get("job_nature")
    stage = pref.get("stage") or "internship"

    if nature == "外包" and not pref.get("accept_outsourcing"):
        cautions.append("外包岗，按你的设定不优先")
    if nature == "兼职":
        cautions.append("兼职岗，不是实习")
    if nature == "实习":
        positives.append("实习岗")
        if stage == "campus":
            cautions.append("你当前档位是校招优先")
    if nature == "全职":
        if stage == "internship":
            cautions.append("全职岗，现阶段以实习为主")
        else:
            positives.append("全职岗")
    return positives, cautions


def evaluate(job: dict, pref: dict) -> dict:
    """对单个岗位做可达性判定。

    job 只需带 job_profile 的字段，缺字段一律当成"没提"处理，不猜。
    """
    # 方向不对口的岗位直接出局，不再走后面几项判定。
    # 理由单独成类（offtopic），因为它和"门槛不够"是两回事：
    # 前者是岗位性质，后者是候选人资格，混在一起用户分不清该补什么
    tech_ok, tech_reason = judge_direction(job)
    if not tech_ok:
        return {
            "grade": "blocked",
            "label": "方向不对口",
            "offtopic": True,
            "distance": 0.0,
            "distance_label": "—",
            "positives": [],
            "cautions": [],
            "blockers": [tech_reason],
            "missing": [],
        }

    distance, distance_label, pos, blockers = _check_city(job, pref)

    p, c, b = _check_edu(job, pref)
    pos += p
    blockers += b
    cautions_edu = list(c)

    p, b = _check_graduate_year(job, pref)
    pos += p
    blockers += b

    p, c, b = _check_experience(job)
    pos += p
    cautions = cautions_edu + list(c)
    blockers += b

    p, c2 = _check_nature(job, pref)
    pos += p
    cautions += c2

    # 缺哪些关键字段。它不影响可达性判定，但影响"这个结论有多可信"，
    # 界面上必须显示出来，否则用户会把"没抓到"当成"不要求"
    missing: list[str] = []
    if not (job.get("city") or "").strip():
        missing.append("城市")
    if not job.get("edu_level"):
        missing.append("学历")
    if job.get("exp_min") is None:
        missing.append("经验")
    if not job.get("job_nature"):
        missing.append("岗位性质")
    if job.get("detail_level") == "digest":
        missing.append("JD 详情")

    if blockers:
        grade = "blocked"
    elif cautions:
        grade = "stretch"
    elif len(missing) >= 3:
        # 字段缺一半还判"可达"，是在给一个没有依据的结论
        grade = "unknown"
    else:
        grade = "reachable"

    return {
        "grade": grade,
        "label": GRADE_LABEL[grade],
        "offtopic": False,
        "distance": round(distance, 2),
        "distance_label": distance_label,
        "positives": pos,
        "cautions": cautions,
        "blockers": blockers,
        "missing": missing,
    }
