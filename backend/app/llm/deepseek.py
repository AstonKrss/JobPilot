"""LLM 调用封装（OpenCode Go 网关，OpenAI 兼容格式）。

网关的三个硬要求（实测得出，不是文档抄来的）
  1) 必须带自定义 User-Agent，且不能用通用 SDK 名 —— 网关以此识别客户端类型
  2) 必须带 x-opencode-session，同一会话内保持稳定 —— 网关据此做路由与 prompt 缓存
     命中；分段抽取共享同一 session 时，共同的 system prompt 能命中缓存，直接降成本
  3) 端点用 https://opencode.ai/zen/go/v1；文档里写的 inference/go/openai/v1 实测
     返回 429 Endpoint is unavailable

为什么默认关闭思考模式
    这不是性能优化，是正确性要求：
      · 思考模式下 temperature 不生效，而我们靠 temperature=0 保证同一输入同一输出，
        这是 agent 快照可重放的前提
      · 实测开启思考时，200 个 completion token 全部消耗在 reasoning 上、content 为空，
        抽取会直接拿到空字符串；关掉后同样的调用只花 6 个 token 且输出正确
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass

import httpx
import psycopg
from psycopg.types.json import Jsonb

from ..config import settings
from .errors import PermanentError, TransientError, call_with_retry, classify_http_status

logger = logging.getLogger(__name__)

# 输入/输出价格（元/百万 token），按 DeepSeek 官方高峰价估算，仅用于成本监控
PRICE_IN_CNY_PER_M = 2.0
PRICE_OUT_CNY_PER_M = 8.0


@dataclass(slots=True)
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int
    model: str
    reasoning_tokens: int = 0
    finish_reason: str | None = None
    # 命中快照时为 True：这一次没有真的调用模型，tokens 是当时消耗的记录
    cached: bool = False
    # 命中服务端 prompt cache 的输入 token。记忆架构（append-only / 前缀稳定）
    # 的全部收益都体现在这个数字上，所以必须打点
    tokens_cached: int = 0

    @property
    def cost_cny(self) -> float:
        """completion_tokens 已包含 reasoning token，这里不重复计。"""
        if self.cached:
            return 0.0
        return (
            self.tokens_in / 1_000_000 * PRICE_IN_CNY_PER_M
            + self.tokens_out / 1_000_000 * PRICE_OUT_CNY_PER_M
        )


def ensure_llm_ready() -> None:
    """调用前显式检查密钥，给出可执行的指引而不是让用户面对 401。"""
    if not settings.llm_api_key:
        raise PermanentError(
            "未配置 LLM_API_KEY，无法调用模型。请在项目根目录的 .env 中填入密钥",
            detail="settings.llm_api_key is empty",
        )


def _build_headers(session_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
        # 网关要求识别客户端身份；用通用 SDK 名可能被限流
        "User-Agent": settings.llm_user_agent,
        # 会话级标识，用于路由优化与 prompt 缓存
        "x-opencode-session": session_id,
    }


async def _post_chat(payload: dict, session_id: str) -> dict:
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    # 每次建 client：调用频率低（单份简历数次），不值得为此管理长连接生命周期
    async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as client:
        resp = await client.post(url, json=payload, headers=_build_headers(session_id))
        if resp.status_code != 200:
            raise classify_http_status(resp.status_code, resp.text)
        return resp.json()


async def chat_json(
    system: str,
    user: str,
    *,
    what: str,
    max_tokens: int = 8000,
    session_id: str | None = None,
    cache_tag: str | None = None,
) -> LLMResult:
    """要求模型返回 JSON 对象。

    session_id 应由调用方传入并在一次处理内保持不变（如一份简历的分段抽取共用一个），
    这样网关的 prompt 缓存才能命中。不传则生成一个一次性会话，仅作兜底。

    cache_tag 非空时，把本次调用结果按「模型 + system + user」的哈希存进
    stage_snapshot，同样的输入第二次直接返回。只给**幂等**的调用用：
    JD 抽取、简历抽取、放宽建议这类「同样输入就该同样输出」的场景。
    建议生成、简历改写、对话都不能开 —— 用户点「重新生成」时期待的是新结果，
    命中缓存会让它看起来像按了没反应。
    """
    ensure_llm_ready()
    sid = session_id or f"jobagent-{uuid.uuid4().hex[:12]}"

    cache_key = None
    if cache_tag:
        cache_key = _cache_key(settings.llm_model, system, user, max_tokens)
        hit = _cache_load(cache_tag, cache_key)
        if hit is not None:
            logger.info("[%s] 命中快照，跳过调用（%s）", what, cache_key[:12])
            return hit

    payload: dict = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if settings.llm_disable_thinking:
        # 关闭思考。注意：思考模式下 temperature 会被忽略，
        # 所以只在关闭思考时才设置 temperature，避免写了却不生效造成误解
        payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = 0

    started = time.monotonic()
    data = await call_with_retry(lambda: _post_chat(payload, sid), what=what)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError) as exc:
        raise PermanentError(
            "模型返回结构异常", detail=f"{what}: {json.dumps(data, ensure_ascii=False)[:300]}"
        ) from exc

    content = message.get("content") or ""
    reasoning = message.get("reasoning_content") or ""
    usage = data.get("usage") or {}
    reasoning_tokens = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)

    if not content.strip():
        # 分开报两种空输出：预算耗尽 vs 模型什么都没说。
        # 混成一个"输出为空"会让排查方向完全错位
        if choice.get("finish_reason") == "length":
            raise PermanentError(
                "模型输出预算被耗尽且正文为空（通常是思考 token 占满了 max_tokens）",
                detail=(
                    f"{what}: max_tokens={max_tokens}, reasoning_tokens={reasoning_tokens}, "
                    f"thinking_disabled={settings.llm_disable_thinking}"
                ),
            )
        raise PermanentError(
            f"模型返回空正文（finish_reason={choice.get('finish_reason')}）",
            detail=f"{what}: reasoning_len={len(reasoning)}",
        )

    result = LLMResult(
        text=content,
        tokens_in=int(usage.get("prompt_tokens", 0)),
        tokens_out=int(usage.get("completion_tokens", 0)),
        model=data.get("model", settings.llm_model),
        reasoning_tokens=reasoning_tokens,
        finish_reason=choice.get("finish_reason"),
        # 字段名各服务商不统一，命中不了的先记 0，原始 usage 会整段留底
        tokens_cached=int(
            usage.get("prompt_cache_hit_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        ),
    )
    logger.info(
        "[%s] %d+%d tokens (缓存命中 %d, reasoning %d) ≈ ¥%.4f",
        what,
        result.tokens_in,
        result.tokens_out,
        result.tokens_cached,
        reasoning_tokens,
        result.cost_cny,
    )
    _log_call(what, result, elapsed_ms, sid, usage)
    if cache_tag and cache_key:
        _cache_save(cache_tag, cache_key, result)
    return result


# ---------- 调用打点 ----------


def _log_call(what: str, result: LLMResult, elapsed_ms: int, session_id: str, usage: dict) -> None:
    """把每次调用落进 llm_call。

    这些数字不是给监控看的，是给**记忆架构做判断**用的：
    如果带缓存命中的第 2、3 轮耗时和首轮没有差别，说明前缀没稳定或服务端没开缓存，
    那 append-only 那套优化的前提就不成立，必须回头改设计。
    写失败只记 warning，不干扰主流程。
    """
    try:
        from ..db.pool import get_pool

        with get_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_call
                    (what, model, session_id, tokens_in, tokens_cached, tokens_out,
                     elapsed_ms, cost_cny, usage_raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    what,
                    result.model,
                    session_id,
                    result.tokens_in,
                    result.tokens_cached,
                    result.tokens_out,
                    elapsed_ms,
                    result.cost_cny,
                    Jsonb(usage),
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 打点失败不该影响业务
        logger.debug("写入调用打点失败（忽略）：%s", exc)


# ---------- 输出快照 ----------
# stage_snapshot 表建了、input_hash 索引也建了，注释写着"相同输入可直接复用输出"，
# 但一直没接上（0 行）。这里把它用起来：只对幂等调用开放（见 chat_json 的 cache_tag）


def _cache_key(model: str, system: str, user: str, max_tokens: int) -> str:
    """模型名也进哈希：换模型后旧结果不该被当成新模型的输出。"""
    h = hashlib.sha256()
    for part in (model, str(max_tokens), system, user):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _cache_load(tag: str, key: str) -> LLMResult | None:
    try:
        from ..db.pool import get_pool

        with get_pool().connection() as conn:
            row = conn.execute(
                """
                SELECT output_json
                  FROM stage_snapshot
                 WHERE stage = %s AND input_hash = %s AND status = 'ok'
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (tag, key),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - 缓存不可用不该让主流程失败
        logger.warning("读取 LLM 快照失败：%s", exc)
        return None

    if not row or not isinstance(row[0], dict):
        return None
    data = row[0]
    text = data.get("text")
    if not text:
        return None
    return LLMResult(
        text=text,
        tokens_in=int(data.get("tokens_in") or 0),
        tokens_out=int(data.get("tokens_out") or 0),
        model=data.get("model") or settings.llm_model,
        reasoning_tokens=int(data.get("reasoning_tokens") or 0),
        finish_reason=data.get("finish_reason"),
        cached=True,
    )


def _cache_save(tag: str, key: str, result: LLMResult) -> None:
    try:
        from ..db.pool import get_pool

        with get_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO stage_snapshot
                    (trace_id, subject_type, subject_id, stage, agent, input_hash,
                     output_json, model, tokens_in, tokens_out, status)
                VALUES (%s, 'llm', NULL, %s, 'chat_json', %s, %s, %s, %s, %s, 'ok')
                """,
                (
                    uuid.uuid4(),
                    tag,
                    key,
                    Jsonb(
                        {
                            "text": result.text,
                            "tokens_in": result.tokens_in,
                            "tokens_out": result.tokens_out,
                            "output_tokens": result.tokens_out,
                            "reasoning_tokens": result.reasoning_tokens,
                            "model": result.model,
                            "finish_reason": result.finish_reason,
                        }
                    ),
                    result.model,
                    result.tokens_in,
                    result.tokens_out,
                ),
            )
            conn.commit()
    except psycopg.Error as exc:
        # 快照写失败只影响省钱，不影响这次结果，所以不抛
        logger.warning("写入 LLM 快照失败：%s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入 LLM 快照异常：%s", exc)


def parse_json_object(text: str, *, what: str) -> dict:
    """解析模型输出为 dict。

    失败归为 permanent：同样的 prompt 再跑一次大概率还是同样的格式错误，
    盲目重试只是重复烧钱。原文写进 detail 便于定位是模型问题还是 prompt 问题。
    """
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PermanentError(
            "模型输出不是合法 JSON",
            detail=f"{what}: {text[:500]}",
        ) from exc
    if not isinstance(value, dict):
        raise PermanentError("模型输出不是 JSON 对象", detail=f"{what}: {text[:300]}")
    return value


async def estimate_cost_guard(cost: float, what: str) -> None:
    """成本超限只告警不掐断。

    掐断会让用户拿到半成品却不知原因；告警能让问题在开发期就被发现。
    """
    if cost > settings.cost_limit_per_resume:
        logger.warning(
            "[%s] 本次成本 ¥%.3f 已超过单份上限 ¥%.2f，请检查是否分段过细或 prompt 过长",
            what,
            cost,
            settings.cost_limit_per_resume,
        )


__all__ = ["LLMResult", "chat_json", "parse_json_object", "ensure_llm_ready", "TransientError"]
