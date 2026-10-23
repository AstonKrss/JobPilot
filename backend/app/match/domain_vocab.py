"""领域类别词表：查询扩展的第一级（零成本、确定、可解释）。

为什么词表优先
    「博客系统 ↔ ERP 系统」这类等价，靠 embedding 相似度直接判是不可靠的
    （实测小模型正负分布重叠）。但它们同属「业务系统开发」这个**类别**——
    类别关系是人工可枚举、可校对的，用词表表达最稳。
    词表未命中的长尾，才交给模型生成变体（第二级）。

结构：类别名 → 该类别下的常见表述（含中英文、缩写、同义词）。
expand_query() 返回的是**同类别下除命中词之外的所有表述**，
这些词与用户意图拼在一起去做向量召回，能覆盖"换了说法的同类岗位"。
"""

from __future__ import annotations

import re

# 类别 → 表述列表。命中任意一个表述即视为属于该类别
DOMAIN_GROUPS: dict[str, list[str]] = {
    "业务系统开发": ["博客", "cms", "内容管理", "后台管理", "管理系统", "erp", "oa", "crm", "业务系统", "管理系统开发"],
    "AI/Agent": ["agent", "智能体", "llm", "大模型", "rag", "ai应用", "ai工程", "prompt", "chatbot", "对话机器人", "ai智能体", "openai", "gpt"],
    "数据/数据库": ["mysql", "postgresql", "oracle", "sql", "数据库", "dba", "redis", "mongodb", "数据仓库"],
    "后端开发": ["java", "python", "go", "golang", "后端", "服务端", "api", "微服务", "spring", "springboot", "django", "flask", "fastapi"],
    "前端": ["vue", "react", "前端", "h5", "小程序", "typescript", "javascript"],
    "桌面/客户端": ["pyqt", "qt", "桌面应用", "客户端", "winform", "wpf"],
    "测试": ["测试", "测试用例", "qa", "自动化测试", "pytest", "jmeter"],
    "运维/安全": ["运维", "devops", "kubernetes", "k8s", "docker", "linux", "安全", "渗透", "网络安全"],
    "文档/办公自动化": ["文档处理", "docx", "word", "pdf", "excel", "office", "wps", "办公自动化"],
    "爬虫/数据采集": ["爬虫", "数据采集", "scrapy", "requests"],
}

# 拆词用：把中文/英文混排拆成可匹配的短语
_SPLIT = re.compile(r"[,，、;；/\s|]+")


def _normalize(text: str) -> list[str]:
    return [piece.strip().lower() for piece in _SPLIT.split(text or "") if piece.strip()]


def match_groups(text: str) -> list[str]:
    """返回文本命中的类别名列表。"""
    pieces = _normalize(text)
    if not pieces:
        return []
    joined = " ".join(pieces)
    hits: list[str] = []
    for group, words in DOMAIN_GROUPS.items():
        for word in words:
            if word in joined:
                hits.append(group)
                break
    return hits


def expand_query(text: str) -> list[str]:
    """按词表扩展：返回同类别下未出现在原文里的表述。

    这些扩展词与原文意图一起去做向量召回，用来覆盖
    「换了说法的同类岗位」（博客系统 → ERP/管理平台）。
    """
    pieces = _normalize(text)
    if not pieces:
        return []
    joined = " ".join(pieces)
    out: list[str] = []
    for group, words in DOMAIN_GROUPS.items():
        hit = any(word in joined for word in words)
        if not hit:
            continue
        for word in words:
            if word not in joined and len(word) >= 2:
                out.append(word)
    return out
