"""官网抓取客户端：限速、诚实 UA、每轮预算硬出口、两段式 WAF cookie。

三条纪律（每条都有依据）：
    1. 对目标站点保持礼貌 —— 同一域名最小间隔 discover_rate_limit_s（默认 2s）。
       抓官网不是抓平台，被封 IP 得不偿失，慢一点没成本。
    2. 诚实 UA —— 不伪装成浏览器（伪装只能换来一场猫鼠游戏），声明自己是
       本地求职助手。站点拒绝就是拒绝，报原因。
    3. 预算硬出口 —— 每轮总抓取次数 discover_max_fetches（默认 50）。
       "开放式任务必须有硬出口"是这个项目对 agent 的一致要求。

两段式 cookie：实测 app.mokahr.com（阿里云 WAF）首访 302 播种 acw_tc 等Cookie，
带 cookie 的第二次 GET 才返回真实页面。这不是破解，是正常浏览器每次都会走的流程。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from ..config import settings
from ..llm.errors import PermanentError, TransientError

logger = logging.getLogger(__name__)

# 抓取层的临时/永久错误。复用 llm.errors 的三分类语义，但文案必须是抓取语境
# （classify_http_status 的 403 文案会提示"检查 DEEPSEEK_API_KEY"，用在官网抓取是误导）


class FetchError(Exception):
    """抓取失败，message 可直接给用户看。transient=True 表示值得整轮重试。"""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.message = message
        self.transient = transient


# 站点侧的临时状态值得换一批时间重试；403/404 对诚实 UA 来说通常是"拒绝你"，重试无意义
_TRANSIENT_STATUS = {429, 408, 425, 500, 502, 503, 504}


@dataclass
class Fetcher:
    """每轮发现共用一个实例：预算、限速状态、httpx client 的生命周期都在这里。"""

    budget: int = 0
    _domain_last: dict[str, float] = field(default_factory=dict)
    _client: httpx.AsyncClient | None = None
    spent: int = 0

    async def __aenter__(self) -> "Fetcher":
        self._client = httpx.AsyncClient(
            headers={"User-Agent": settings.discover_user_agent},
            timeout=settings.discover_timeout_s,
            follow_redirects=False,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def remaining(self) -> int:
        return self.budget - self.spent

    async def _throttle(self, url: str) -> None:
        """同域限速。串行抓取下这是唯一需要的节流。"""
        domain = urlsplit(url).netloc
        last = self._domain_last.get(domain)
        if last is not None:
            gap = settings.discover_rate_limit_s - (time.monotonic() - last)
            if gap > 0:
                await asyncio.sleep(gap)
        self._domain_last[domain] = time.monotonic()

    def _spend(self, url: str, what: str) -> None:
        if self.remaining <= 0:
            raise FetchError(f"本轮抓取预算已用完（{self.budget} 次），来源「{what}」本轮跳过")
        self.spent += 1

    async def get_html(self, url: str, *, what: str = "") -> str:
        """GET 页面正文。预算按 URL 计（两段式 cookie 不重复计费）。

        失败分类：超时/连接类与 429/5xx → transient（整轮可重试）；
        403/404/其他 4xx → permanent（诚实报因，不重试）。
        """
        what = what or url
        self._spend(url, what)
        last_err: FetchError | None = None
        for attempt in (1, 2):  # 抓取层轻量重试：2 次，间隔 3s；与 LLM 的 5 次退避区分开
            await self._throttle(url)
            try:
                r = await self._client.get(url)
                if r.status_code in (301, 302, 303, 307, 308):
                    # WAF 播种 cookie 后的二次访问。cookie 在 client jar 里，原 URL 再取一次
                    r = await self._client.get(url)
                if r.status_code in _TRANSIENT_STATUS:
                    last_err = FetchError(f"目标站临时不可用（HTTP {r.status_code}）", transient=True)
                elif r.status_code in (401, 403):
                    raise FetchError("目标站拒绝访问（HTTP 403）：站点不允许此客户端抓取，诚实放弃")
                elif r.status_code == 404:
                    raise FetchError("招聘页地址已失效（HTTP 404）：公司可能换了招聘系统或链接写错")
                elif r.status_code != 200:
                    raise FetchError(f"目标站返回异常状态（HTTP {r.status_code}）")
                return r.text
            except httpx.TimeoutException as exc:
                last_err = FetchError(f"抓取超时（{settings.discover_timeout_s}s）", transient=True)
                logger.debug("[%s] timeout: %s", what, exc)
            except httpx.NetworkError as exc:
                last_err = FetchError("网络连接失败", transient=True)
                logger.debug("[%s] network: %s", what, exc)
            if attempt == 1 and last_err is not None and last_err.transient:
                await asyncio.sleep(3.0)
        raise last_err or FetchError("抓取失败（原因未分类）")

    async def post_json(self, url: str, payload: dict, *, what: str = "") -> dict:
        """POST JSON。与 get_html 同样的限速与预算纪律；预算按 URL 计。"""
        what = what or url
        self._spend(url, what)
        await self._throttle(url)
        try:
            r = await self._client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise FetchError(f"请求超时（{settings.discover_timeout_s}s）", transient=True) from exc
        except httpx.NetworkError as exc:
            raise FetchError("网络连接失败", transient=True) from exc
        if r.status_code != 200:
            raise FetchError(f"目标接口返回 HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise FetchError("目标接口返回的不是 JSON") from exc
