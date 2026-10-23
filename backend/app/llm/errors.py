"""统一的错误分类与重试策略。

设计依据（项目决策）：
    可恢复(transient) → 退避重试 5 次，间隔 1/2/4/8/16s（累计约 31s），全败才算失败
    永久(permanent)   → 一次都不重试，直接降级并说明真实原因
    自身bug           → 快速失败，不重试不降级（重试只是把同一个 bug 重放五次）

为什么 raise 而不是返回错误码：调用方必须在编译期就被迫处理分类，
返回错误码时「忘了判断」会静默走成 happy path。
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Awaitable, Callable, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 决策已定：5 次重试，指数退避。改这里等于改项目决策，需同步更新 docs/design-decisions.md
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0)


class ErrorKind(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    BUG = "bug"


class AppError(Exception):
    """带分类的应用异常。

    user_message 是给用户看的中文，必须说清"发生了什么"，不能是"操作失败请重试"。
    detail 是给日志和排查用的技术细节。
    """

    kind: ErrorKind = ErrorKind.BUG

    def __init__(self, user_message: str, *, detail: str | None = None) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail

    def __str__(self) -> str:  # 日志里同时看到用户话术与技术细节
        return f"{self.user_message}" + (f" | {self.detail}" if self.detail else "")


class TransientError(AppError):
    """值得重试：限流、超时、上游 5xx。"""

    kind = ErrorKind.TRANSIENT


class PermanentError(AppError):
    """重试无意义：加密 PDF、格式不支持、密钥失效、请求参数本身错误。"""

    kind = ErrorKind.PERMANENT


class BugError(AppError):
    """代码缺陷：参数类型错、状态非法。要暴露而不是掩盖。"""

    kind = ErrorKind.BUG


def classify_http_status(status: int, body: str = "") -> AppError:
    """按 HTTP 状态码给出错误类别。

    关键判断：
      429 / 5xx / 408 是服务端临时状态 → transient
      401 / 403 是密钥或权限问题 → permanent（重试五次也是同样结果，纯烧时间）
      400 / 404 / 422 是请求本身不合法 → permanent
    """
    snippet = (body or "")[:300]
    if status == 429:
        return TransientError("模型服务限流，正在重试", detail=f"HTTP 429: {snippet}")
    if status in (408, 425) or 500 <= status < 600:
        return TransientError(
            "模型服务临时不可用，正在重试", detail=f"HTTP {status}: {snippet}"
        )
    if status in (401, 403):
        return PermanentError(
            "调用凭证无效或无权访问，请检查 DEEPSEEK_API_KEY 配置",
            detail=f"HTTP {status}: {snippet}",
        )
    if 400 <= status < 500:
        return PermanentError(
            "请求被模型服务拒绝，需检查请求内容", detail=f"HTTP {status}: {snippet}"
        )
    return BugError("未预期的响应状态", detail=f"HTTP {status}: {snippet}")


def classify_exception(exc: Exception) -> AppError:
    """把第三方库异常收敛成三分类。

    只认识 httpx 的异常族；其他一律视为 bug。宁可暴露一个未分类异常，
    也不要猜成 transient 然后重试五次掩盖真实问题。
    """
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return TransientError(
            "网络连接不稳定，正在重试", detail=f"{type(exc).__name__}: {exc}"
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return classify_http_status(exc.response.status_code, exc.response.text)
    return BugError("内部处理异常", detail=f"{type(exc).__name__}: {exc}")


async def call_with_retry(
    func: Callable[[], Awaitable[T]],
    *,
    what: str,
) -> T:
    """仅对 transient 错误重试；permanent 与 bug 立即抛出。

    what 用于日志定位是哪一步失败（"抽取第 2 段经历"而非"LLM 调用失败"）。
    """
    last: AppError | None = None
    total = len(RETRY_DELAYS) + 1
    for attempt in range(1, total + 1):
        try:
            return await func()
        except Exception as exc:  # 收敛所有异常为三分类，再按类别决定是否重试
            err = classify_exception(exc)
            if err.kind is not ErrorKind.TRANSIENT:
                logger.error("[%s] 不可重试错误(%s) 第%d次: %s", what, err.kind.value, attempt, err)
                raise err from exc
            last = err
            if attempt >= total:
                break
            delay = RETRY_DELAYS[attempt - 1]
            logger.warning(
                "[%s] 可恢复错误，%.0fs 后重试 (%d/%d): %s", what, delay, attempt, total, err
            )
            await asyncio.sleep(delay)

    # 走到这里说明五次全是 transient
    logger.error("[%s] 重试耗尽: %s", what, last)
    raise TransientError(
        f"{what}重试 {total} 次仍然失败，请稍后再试",
        detail=str(last) if last else None,
    )
