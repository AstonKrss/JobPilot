"""送 LLM 前的脱敏。

依据项目决策：剔除姓名、手机、邮箱、住址、证件号，只留教育、经历、技能、项目。
判断依据很直接 —— 这些字段对「匹配岗位」没有任何贡献，剔除是纯减风险、不减效果。

姓名处理说明
    姓名无法用正则识别，只能靠版面启发式：简历首块的第一行如果是一个 2~4 字的
    孤立中文串，且不属于章节标题词，就当作姓名剔除。这会带来两类误差：
      · 漏删：姓名与职位写在同一行（「张三 | 后端工程师」）时不会被识别
      · 误删：首行是「个人简历」这类标题时被误判（已用章节词白名单规避）
    因此本模块返回被替换掉的原文，便于在日志里核对，不静默处理。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 手机号：前后不能紧邻数字，避免把长串数字（如 20 位流水号）里的 11 位误判
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# 18 位身份证（末位可为 X）
ID_CARD_RE = re.compile(r"(?<![0-9Xx])\d{17}[0-9Xx](?![0-9Xx])")
# 带省市街道门牌或楼栋房号的地址片段，只在同一行内匹配，避免吞掉整段文字
ADDRESS_RE = re.compile(r"[\u4e00-\u9fa5]{2,10}(?:省|市|区|县)[\u4e00-\u9fa5\d]{0,20}(?:路|街|道|巷)[\d号栋单元室\-]{0,12}")

# 章节标题白名单：这些词出现在首行时不是姓名
_SECTION_WORDS = {
    "个人简历", "求职简历", "简历", "个人信息", "基本信息", "教育背景", "教育经历",
    "工作经历", "工作经验", "项目经历", "项目经验", "专业技能", "技能特长", "自我评价",
    "联系方式", "求职意向", "实习经历", "校园经历", "荣誉奖项",
}
# 姓名行：2~4 个汉字，前后无其他字符
_NAME_LINE_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")


def _strip_name(first_line: str) -> str | None:
    """首行像姓名则返回该姓名，否则返回 None。"""
    candidate = first_line.strip()
    if not _NAME_LINE_RE.match(candidate):
        return None
    if candidate in _SECTION_WORDS:
        return None
    return candidate


def sanitize(text: str, *, detect_name: bool = True) -> tuple[str, list[str]]:
    """返回 (脱敏后文本, 被替换掉的原文列表)。

    detect_name 只对文档首块开启：姓名只可能出现在开头，对每个块都跑姓名启发式
    会把正文里的短语误删（例如一行只有"数据分析"四个字时被当成姓名）。
    """
    removed: list[str] = []
    out = text

    for regex, placeholder, label in (
        (PHONE_RE, "[手机]", "phone"),
        (EMAIL_RE, "[邮箱]", "email"),
        (ID_CARD_RE, "[证件号]", "id_card"),
        (ADDRESS_RE, "[住址]", "address"),
    ):
        for hit in regex.findall(out):
            removed.append(f"{label}:{hit}")
        out = regex.sub(placeholder, out)

    first_line = out.split("\n", 1)[0] if out else ""
    name = _strip_name(first_line) if detect_name else None
    if name:
        removed.append(f"name:{name}")
        out = "[姓名]" + out[len(first_line):]

    if removed:
        # 只记数量与类型，不把原始 PII 写进日志文件
        kinds = sorted({item.split(":", 1)[0] for item in removed})
        logger.info("脱敏完成，剔除 %d 处：%s", len(removed), ",".join(kinds))
    return out, removed
