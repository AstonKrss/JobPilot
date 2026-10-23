"""技能匹配的共用逻辑。

被两处使用：硬约束层（判断必备技能是否低到该否决）与打分层（计算命中率得分）。
两处必须用同一套判定，否则会出现「否决层认为没命中、打分层认为命中」的自相矛盾。

并列结构的处理
    JD 常写「Go 或 Java」「PyTorch 或 TensorFlow」。若把整串当一个技能比较，
    只会 Go 的候选人永远命中不了。所以比较前按「或 / 、 / ,」等切分成候选集，
    命中任一项即算满足。切分只发生在匹配时，不改动存储的原文 ——
    解释里要给用户看 JD 的实际措辞。

两字符技能名的特殊处理
    「Go」是常见技能但只有两个字符，若允许子串匹配会命中「django」。
    因此子串匹配要求长度 >= 3，短名必须精确相等。
"""

from __future__ import annotations

import re

from ..normalize.fuzzy import norm_key
from ..normalize.skill_alias import expand

_SPLIT_RE = re.compile(r"[或/、,，;；|]+|以及|或者")

# 子串匹配的最小长度。低于此长度只允许精确相等，否则 "go" 会命中 "django"
MIN_SUBSTRING_LEN = 3


def skill_tokens(skill: str) -> set[str]:
    """把一条技能要求拆成检索键集合，并做别名展开。

    两步缺一不可：切分解决「Go 或 Java」的并列写法，
    别名展开解决「K8s / Kubernetes」的同义写法 —— 后者字面完全不同，
    任何字符串相似度都命中不了。
    """
    tokens: set[str] = set()
    for piece in _SPLIT_RE.split(skill or ""):
        key = norm_key(piece)
        if key:
            tokens |= expand(key)
    return tokens


def resume_token_set(resume_skills: list) -> set[str]:
    """合并简历里所有技能的检索键。兼容 [{name:...}] 与纯字符串两种形态。"""
    tokens: set[str] = set()
    for item in resume_skills or []:
        name = item.get("name") if isinstance(item, dict) else str(item)
        tokens |= skill_tokens(name or "")
    return tokens


def skill_hit(resume_tokens: set[str], skill: str) -> bool:
    """简历是否覆盖该条要求。任一候选 token 命中即算满足。"""
    for jd_token in skill_tokens(skill):
        for resume_token in resume_tokens:
            if jd_token == resume_token:
                return True
            if len(jd_token) >= MIN_SUBSTRING_LEN and jd_token in resume_token:
                return True
            if len(resume_token) >= MIN_SUBSTRING_LEN and resume_token in jd_token:
                return True
    return False


def split_hits(resume_tokens: set[str], skills: list[str]) -> tuple[list[str], list[str]]:
    """返回 (命中列表, 未命中列表)。"""
    hit = [s for s in skills if skill_hit(resume_tokens, s)]
    miss = [s for s in skills if s not in hit]
    return hit, miss
