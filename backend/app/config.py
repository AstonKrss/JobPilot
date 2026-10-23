"""运行配置。

一处刻意的偏离：按编码纪律「配置缺失应启动即失败」，但这里 LLM_API_KEY 允许为空
启动。理由是解析链路（PDF 坐标还原、OCR、归一化检索键）不依赖 LLM，可以独立测试；
若因缺 key 连服务都起不来，会把能验证的部分一起堵死。代价是调用 LLM 处必须显式
检查 key，因此提供 ensure_llm_ready()，缺失时抛出明确的配置错误而非 401。

供应商为 OpenCode Go（订阅制网关），注意两点与直连 DeepSeek 不同的地方：
  1) 必须带自定义 User-Agent 与 x-opencode-session（见 llm/deepseek.py）
  2) 端点 https://opencode.ai/zen/go/v1 可用，而文档里的
     inference/go/openai/v1 实测返回 429 Endpoint is unavailable
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 本地 conda 环境的 Postgres，端口 5433 以避开系统上可能存在的 5432
    database_url: str = "postgresql://postgres@127.0.0.1:5433/ragjob"

    # OpenCode Go 网关
    llm_api_key: str = ""
    llm_base_url: str = "https://opencode.ai/zen/go/v1"
    llm_model: str = "deepseek-v4.1-flash"
    # 网关要求客户端自报身份，禁止使用通用 SDK 名（实测必需，否则可能被限流）
    llm_user_agent: str = "job-agent/0.1.0"

    # 关闭思考模式。不是性能优化，是正确性要求：
    #   思考模式下 temperature 参数不生效，而我们依赖 temperature=0 保证同一输入
    #   同一输出（agent 快照重放的前提）。实测开启思考时 200 个 completion token
    #   全部消耗在 reasoning 上、content 为空，抽取会拿到空字符串。
    llm_disable_thinking: bool = True

    # 语义匹配层：本地向量只负责召回，判定交给模型。
    # 关掉后整条链路退回纯规则匹配（不报错，只是少召回一些换了个说法的技能）
    enable_semantic: bool = True

    # 单次调用超时。上下文 1M 的模型在长简历上单次可能跑几十秒，给足余量
    llm_timeout_s: float = 120.0

    upload_dir: Path = PROJECT_ROOT / "data" / "uploads"

    # 决策已定：单份简历成本上限 0.5 元。用于超限告警，不用于掐断调用
    cost_limit_per_resume: float = 0.5


settings = Settings()
settings.upload_dir.mkdir(parents=True, exist_ok=True)
