"""技能别名归一（归一化 L1 的最小实现）。

为什么必须有这一层
    「K8s」与「Kubernetes」、「PG」与「PostgreSQL」字面毫无交集，
    检索式匹配（n-gram / 子串 / 拼音）对它们完全无能为力。
    这类问题调阈值救不了，只能靠别名映射。

当前范围
    内置一张常用技能缩写表。这不是最终形态 —— 完整方案是从 entity_alias 表
    （entity_type='skill'）加载，并让用户在低置信度确认队列里补充后回写，
    形成「越用越准」的闭环。表结构与队列表已就绪（见 db/schema.sql），
    接上来属于 M3。
"""

from __future__ import annotations

# 键为归一化后的写法（小写、去空格标点），值为标准名。
#
# 只收确定无歧义的缩写。像 "es"（Elasticsearch？）、"ml"（Machine Learning？）
# 这类多义缩写宁可不管 —— 猜错会把不相干的岗位判成匹配，比漏判更糟。
_SYNONYMS: dict[str, str] = {
    "k8s": "kubernetes",
    "kube": "kubernetes",
    "pg": "postgresql",
    "postgres": "postgresql",
    "psql": "postgresql",
    "js": "javascript",
    "ts": "typescript",
    "golang": "go",
    "vuejs": "vue",
    "reactjs": "react",
    "nodejs": "node",
}


def expand(token: str) -> set[str]:
    """展开为「原写法 + 标准名」。

    保留原写法是必要的：有的 JD 写缩写、有的写全称，两个方向都要能命中。
    """
    canonical = _SYNONYMS.get(token)
    if canonical and canonical != token:
        return {token, canonical}
    return {token}
