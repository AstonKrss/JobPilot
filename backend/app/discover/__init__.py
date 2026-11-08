"""主动岗位发现（DiscoverAgent）：agent 主动去公司官网招聘页找新岗位。

设计哲学与全项目一致：一次规划 + 确定性管线，循环可以有、出口必须写死。
    计划 = 确定性遍历 discover_source 里的启用来源（无 LLM）
    抓取 = fetcher.py（限速 + 诚实 UA + 每轮预算硬出口）
    解析 = adapters.py（moka 契约实测校准 / html 列表页 LLM 辅助）
    入库 = ingest/jd_pipeline.py 统一管线（source='careers'，去重复用 job 表唯一键）
    判定 = 前端复用 GET /api/radar/job/{id}（与雷达页同口径，不另写一份分数）

这是全项目唯一真正的"开放式任务"——要查几个站、每个站抓到哪才算够，
事先不知道。所以它是"循环 + 硬出口"原则的正当用武之地。
"""

from . import adapters, agent, fetcher, seed_sources

__all__ = ["adapters", "agent", "fetcher", "seed_sources"]
