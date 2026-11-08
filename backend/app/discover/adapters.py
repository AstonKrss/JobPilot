"""招聘页适配器：把不同形态的官网招聘页解析成统一的岗位条目。

契约全部实测得出，不靠文档（2026-11-08，app.mokahr.com/apply/ruijie/26518）：

moka（Moka ATS，国内大量公司使用）
    两段式 GET（WAF 播种 cookie）→ HTML 里 <script id="init-data"> 内嵌完整
    启动 JSON：org.id / siteId / jobs[]（title、id、publishedAt、locations、
    commitment、hireMode、status）/ jobStats.total。**首屏列表零 API 调用。**
    其翻页/详情接口 POST /api/outer/ats-apply/website/jobs 返回 AES-CBC 加密体
    （密钥由前端运行时持有）—— v1 不硬啃：内嵌列表够用，代码里如实记录，
    加密翻页留作 v2。岗位详情页 URL 可直接构造（hash 路由）。

html（兜底）
    服务器渲染的列表页：抽 <a href> → 按岗位词预筛 → 一次 LLM 调用提取条目。
    JS 渲染站点没有锚点可解析 → UnsupportedSourceError，诚实报因
    （"需要浏览器扩展支持"是下一阶段的事，不是静默失败）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape as html_unescape
from urllib.parse import urljoin, urlsplit

from ..llm.deepseek import chat_json, parse_json_object
from .fetcher import FetchError, Fetcher

logger = logging.getLogger(__name__)


class UnsupportedSourceError(Exception):
    """这个来源按当前适配器解析不出岗位。message 说明为什么，给用户看。"""


@dataclass(slots=True)
class Listing:
    """统一的岗位条目。url 指向详情页（给用户点）；source_job_id 用于跨轮稳定去重。"""

    title: str
    url: str | None = None
    city: str | None = None
    commitment: str | None = None
    published_at: str | None = None   # ISO 日期或原始文本
    org_name: str | None = None
    source_job_id: str | None = None


# ---- 「急需」启发式：只认明说的字样，不猜 ----
# 误报成本大于漏报：一个每次都亮"急"灯的标记，用户三天后就学会无视它
URGENT_MARKS = ("急招", "急聘", "急需", "热招", "火速", "名额紧张", "urgent", "asap")
FRESH_DAYS = 7  # 发布 7 天内标「新」，不与「急」混用——急不急是意愿，新不新是事实


def detect_adapter(url: str) -> str:
    """URL 指纹自动探测适配器。探测不了就是 html 兜底。"""
    host = urlsplit(url).netloc.lower()
    return "moka" if "mokahr.com" in host else "html"


def find_urgent(text: str) -> str | None:
    """返回命中的急招字样；None 表示没有。"""
    low = (text or "").lower()
    for mark in URGENT_MARKS:
        if mark in low:
            return mark
    return None


def is_fresh(published_at: str | None, now: datetime | None = None) -> bool:
    """发布时间在 FRESH_DAYS 天内。解析不了返回 False（不猜）。"""
    if not published_at:
        return False
    try:
        dt = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return timedelta(0) <= (now - dt) <= timedelta(days=FRESH_DAYS)


def _date_short(published_at: str | None) -> str:
    """发布时间取日期部分。解析不了就原样截断，保持可读。"""
    if not published_at:
        return "时间未知"
    return str(published_at)[:10]


# ---------------------------------------------------------------------------
# moka 适配器
# ---------------------------------------------------------------------------

_INIT_DATA_RE = re.compile(r'id="init-data"[^>]*value="([^"]+)"', re.S)
_JOB_URL_TEMPLATE = "https://app.mokahr.com/apply/{org}/{site_id}#/job/{job_id}"


def parse_moka_init_data(html_text: str, page_url: str) -> tuple[list[Listing], dict]:
    """从 Moka 页面 HTML 解析 init-data 内嵌岗位列表。纯函数，可离线回归。

    返回 (listings, meta{org_id, site_id, org_name, total})。
    没有 init-data、没有 jobs 时抛 UnsupportedSourceError —— 说明该页不是
    标准 Moka 招聘页（比如是登录墙或改版了），如实报告而不是空列表。
    """
    import json

    m = _INIT_DATA_RE.search(html_text or "")
    if not m:
        raise UnsupportedSourceError("页面里没有 Moka 的 init-data（可能不是招聘页或已改版）")
    data = json.loads(html_unescape(m.group(1)))
    org = data.get("org") or {}
    org_id = org.get("id") or ""
    site_id = data.get("siteId")
    jobs = data.get("jobs") or []
    if not jobs:
        raise UnsupportedSourceError("Moka 页面没有内嵌岗位列表（可能全部岗位已关闭）")

    listings: list[Listing] = []
    for j in jobs:
        if str(j.get("status") or "") != "open":
            continue  # 已关闭岗位不入库，噪声
        title = str(j.get("title") or "").strip()
        if not title:
            continue
        locs = j.get("locations") or []
        city = next((str(l.get("address") or "").strip() for l in locs if l.get("address")), None)
        detail_url = None
        if org_id and site_id:
            detail_url = _JOB_URL_TEMPLATE.format(org=org_id, site_id=site_id, job_id=j.get("id"))
        listings.append(Listing(
            title=title,
            url=detail_url,
            city=city,
            commitment=str(j.get("commitment") or "").strip() or None,
            published_at=j.get("publishedAt") or j.get("openedAt"),
            org_name=org.get("name") or None,
            source_job_id=f"moka:{org_id}:{j.get('id')}" if j.get("id") else None,
        ))

    total = ((data.get("jobStats") or {}).get("total"))
    meta = {"org_id": org_id, "site_id": site_id, "org_name": org.get("name"),
            "total": total, "embedded": len(jobs)}
    return listings, meta


def listing_digest_text(l: Listing) -> str:
    """digest 入库的原文（两个适配器共用）。**确定性拼接**：同一岗位每轮抓到
    同样的文本，才能靠 (source, md5(raw_text)) 唯一键做跨轮去重
    （is_new 语义依赖它）。"""
    return (
        f"岗位：{l.title}\n"
        f"公司：{l.org_name or '未知'}\n"
        f"城市：{l.city or '未标注'}\n"
        f"工作性质：{l.commitment or '未标注'}\n"
        f"发布时间：{_date_short(l.published_at)}\n"
        f"来源：公司官网招聘页（摘要，详情见链接）"
    )


async def fetch_moka_listings(fetcher: Fetcher, url: str) -> tuple[list[Listing], dict]:
    """两段式 GET 拿页面 → 解析内嵌列表。"""
    html_text = await fetcher.get_html(url, what="Moka 招聘页")
    return parse_moka_init_data(html_text, url)


# ---------------------------------------------------------------------------
# html 兜底适配器
# ---------------------------------------------------------------------------

# 锚点预筛：URL 路径或锚文本带岗位词才算候选。这一步用规则省模型调用
_JOB_ANCHOR_HINTS = ("job", "position", "career", "jobs", "recruit", "zhaopin",
                     "岗位", "职位", "招聘", "校招", "社招", "实习")
_NOISE_ANCHOR = ("login", "register", "about", "policy", "协议", "登录", "注册", "关于我们")
_MAX_ANCHORS = 80

_TAG_STRIP_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)
_A_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.S | re.I)


def extract_job_anchors(html_text: str, base_url: str) -> list[dict]:
    """从 HTML 抽同域锚点并按岗位词预筛。纯函数，可离线回归。"""
    cleaned = _TAG_STRIP_RE.sub(" ", html_text or "")
    seen: set[str] = set()
    out: list[dict] = []
    for href, inner in _A_RE.findall(cleaned):
        text = re.sub(r"<[^>]+>", " ", inner)
        text = re.sub(r"\s+", " ", html_unescape(text)).strip()
        if not (2 <= len(text) <= 40):
            continue
        href = html_unescape(href).strip()
        if href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        abs_url = urljoin(base_url, href)
        path = urlsplit(abs_url).path.lower() + " " + text.lower()
        if any(n in path for n in _NOISE_ANCHOR):
            continue
        if not any(h in path for h in _JOB_ANCHOR_HINTS):
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        out.append({"title": text, "url": abs_url})
        if len(out) >= _MAX_ANCHORS:
            break
    return out


LIST_EXTRACT_PROMPT = """你是招聘列表页解析器。下面是网页里预筛出的链接候选（JSON）。
从中挑出**真正的岗位条目**（单个具体岗位，不是"招聘官网""岗位列表"这类入口页），
只输出 JSON：

{"listings": [{"title": "岗位名", "url": "链接", "city": "城市或 null"}]}

规则：
1. 只依据给定的候选，不编造。没有岗位就输出 {"listings": []}。
2. url 必须从候选里原样复制，不要改写。
3. title 保留原文措辞，带部门/城市后缀（如「后端开发工程师（交易方向）」）也保留。"""


async def fetch_html_listings(fetcher: Fetcher, url: str) -> tuple[list[Listing], dict]:
    """抓 SSR 列表页 → 锚点预筛 → 一次 LLM 提取。"""
    html_text = await fetcher.get_html(url, what="招聘列表页")
    anchors = extract_job_anchors(html_text, url)
    if not anchors:
        raise UnsupportedSourceError(
            "页面上检测不到岗位链接（大概率是 JS 渲染的招聘页，需要浏览器扩展支持，"
            "v1 后端直抓不做这件事）"
        )
    result = await chat_json(
        LIST_EXTRACT_PROMPT,
        "候选链接：\n" + "\n".join(
            f"- title: {a['title']} | url: {a['url']}" for a in anchors
        ),
        what="招聘列表解析",
        max_tokens=1500,
    )
    payload = parse_json_object(result.text, what="招聘列表解析")
    listings: list[Listing] = []
    for item in payload.get("listings") or []:
        title = str(item.get("title") or "").strip()
        link = str(item.get("url") or "").strip()
        if not title or not link.startswith("http"):
            continue
        listings.append(Listing(
            title=title,
            url=link,
            city=(str(item.get("city")).strip() or None) if item.get("city") else None,
            org_name=None,
        ))
    if not listings:
        raise UnsupportedSourceError("列表页里没有解析出具体岗位（可能全是入口链接或招聘公告）")
    meta = {"anchors": len(anchors), "picked": len(listings)}
    return listings, meta


ADAPTERS = {
    "moka": fetch_moka_listings,
    "html": fetch_html_listings,
}
