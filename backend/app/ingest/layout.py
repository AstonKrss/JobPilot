"""版面还原：把带坐标的文本块还原成正确的阅读顺序。

要解决的问题
    两栏排版的简历，按 PDF 原生顺序或单纯按 y 排序会读串：左栏第 3 行与右栏第 1 行
    y 坐标相近，会被排到一起，句子直接崩掉。这不是模型能力问题，是几何问题。

做法（纯几何，不引入版面分析模型）
    1. 找「栏间空白」：存在一段 x 区间，页面中没有任何文本块横跨它，且左右两侧都有
       足够多的内容、且两者的 y 范围显著重叠（重叠才说明是并列两栏，而不是上下结构）
    2. 按这条空白把整页切成若干栏，栏与栏之间按从左到右的顺序读
    3. 每栏内部按 y 聚成行，行内按 x 排序 —— 这样「公司名    2020.03-2023.06」
       这类左名右时间的写法才会并回同一行

为什么扫描件也走这里
    OCR 同样输出「文本 + 四边形坐标」，转成 Block 后复用同一套算法。两条输入路径
    共用一套还原逻辑，比维护两套版面规则更不容易出现行为分叉。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 判定为栏间空白的宽度下限（占内容宽度的比例）。太小的间隙是正常的字间距
MIN_GAP_RATIO = 0.03
# 每一侧至少要有这么多块，才承认是分栏，否则会把单栏页面的缩进误判成栏
MIN_BLOCKS_PER_COLUMN = 2
# 每一侧的块数占比下限，避免把「正文 + 右侧一条边注」切成对等的两栏
MIN_COLUMN_SHARE = 0.2
# 左右两栏 y 范围的最小重叠比例，用来排除上下结构
MIN_VERTICAL_OVERLAP = 0.3
# 两侧块的中位文本长度下限。
# 这条判据解决真实简历里最常见的陷阱：「左侧职位名 + 右侧日期」会形成贯穿页面的横向
# 空白，仅按几何间隙判定就会把单栏切成两栏，读成「职位A、职位B、日期A、日期B」。
# 起初用「两侧文本量比例」区分，实测无效 —— 右侧的 "2020.03 - 2023.06" 比左侧的
# "ByteDance Ltd" 还长，比例高达 0.8。改用块级中位长度：真两栏两侧都是成段文字
# （几十字符一块），标题/时间结构两侧都是短语（十几字符一块）。
MIN_MEDIAN_BLOCK_CHARS = 30.0

# 「侧边栏 + 正文」布局的识别阈值。
#
# 为什么需要这个特例：中文两栏简历常把技能标签、联系方式放在左侧窄栏，
# 左栏全是 2~5 字的短标签，中位块长远低于 MIN_MEDIAN_BLOCK_CHARS。
# 只用上面那条判据会把整页判成单栏，导致左栏标签与右栏正文逐行交错
# （实测读成「Python / 工作经历段 / Go / 项目经历段」），语义完全错乱。
#
# 区分依据是栏宽悬殊：
#   侧边栏：窄侧 < 35% 内容宽度，宽侧 > 45%，且宽侧是成段正文
#   左标题右时间：两侧栏宽接近（都在 35%~60%），且两侧都是短语 → 仍按行读
SIDEBAR_MAX_WIDTH_RATIO = 0.40
MAIN_MIN_WIDTH_RATIO = 0.45

# 尝试剥离的顶部横跨块上限。
# 真实两栏简历几乎都有横跨整页的姓名/标题行，它会把栏间空白堵死，
# 使整页被判成单栏、左右逐行交错。剥离顶部若干"独占行"后重新检测分栏，
# 剥离上限取 4 —— 再多就不像页眉，而像单栏正文了
MAX_HEADER_BLOCKS = 4


@dataclass(slots=True)
class Block:
    """一个带坐标的文本块。坐标单位为 PDF point，原点在页面左上角。

    direction 是文字基线方向向量（PyMuPDF 的 line["dir"]）：
    横排为 (1, 0)，旋转 90° 为 (0, -1) 或 (0, 1)。
    记录它是为了把旋转文本（设计类简历的竖排侧标）与正文区分开 ——
    仅凭 bbox 无法知道这个块是不是横着读的。
    """

    page_no: int
    block_index: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block_type: str = "text"
    read_order: int = -1
    direction: tuple[float, float] = (1.0, 0.0)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 0.0)

    @property
    def is_horizontal(self) -> bool:
        """基线是否水平。y 分量接近 0 即视为横排。"""
        return abs(self.direction[1]) < 0.01


def _median_text_len(blocks: list[Block]) -> float:
    """中位文本长度。用中位数而非均值，避免个别长段落把整体拉高。"""
    if not blocks:
        return 0.0
    lengths = sorted(len(b.text) for b in blocks)
    return float(lengths[len(lengths) // 2])


def _column_width(blocks: list[Block]) -> float:
    if not blocks:
        return 0.0
    return max(b.x1 for b in blocks) - min(b.x0 for b in blocks)


def _looks_like_sidebar(left: list[Block], right: list[Block], content_width: float) -> bool:
    """判断是否为「窄侧边栏 + 宽正文」布局。

    窄侧可以是左侧（常见：技能/联系方式）也可以是右侧（少见，如时间线列）。

    宽侧的判据用「是否存在成段文字」而不是中位长度：正文里常混着
    「个人简介」「工作经历」这类 4 字小标题，会把中位数拉低到 20 出头，
    用中位数会把真实的两栏简历判成单栏（实测踩到过）。
    """
    if content_width <= 0:
        return False
    left_ratio = _column_width(left) / content_width
    right_ratio = _column_width(right) / content_width
    narrow_ratio = min(left_ratio, right_ratio)
    wide_ratio = max(left_ratio, right_ratio)
    if narrow_ratio >= SIDEBAR_MAX_WIDTH_RATIO or wide_ratio <= MAIN_MIN_WIDTH_RATIO:
        return False
    wide_blocks = left if left_ratio > right_ratio else right
    return any(len(b.text) >= MIN_MEDIAN_BLOCK_CHARS for b in wide_blocks)


def _vertical_overlap_ratio(a: list[Block], b: list[Block]) -> float:
    """两组块的 y 跨度重叠比例，用于区分「并列两栏」与「上下两段」。"""
    a_top, a_bottom = min(x.y0 for x in a), max(x.y1 for x in a)
    b_top, b_bottom = min(x.y0 for x in b), max(x.y1 for x in b)
    inter = min(a_bottom, b_bottom) - max(a_top, b_top)
    span = max(a_bottom - a_top, b_bottom - b_top, 1e-6)
    return max(inter, 0.0) / span


def _find_column_gap(blocks: list[Block]) -> tuple[float, float] | None:
    """在页面横向找一条没有任何块横跨的空白带，返回 (起点, 终点)。

    用「覆盖数」而不是「块的 x 中心分布」：只要有一行长文本横跨中间，覆盖数就不为 0，
    分栏判定自动失效 —— 这正是单栏页面需要的否定条件。
    """
    if len(blocks) < MIN_BLOCKS_PER_COLUMN * 2:
        return None

    min_x = min(b.x0 for b in blocks)
    max_x = max(b.x1 for b in blocks)
    width = max_x - min_x
    if width <= 0:
        return None

    # 采样步长取宽度的 1/400，兼顾精度与开销
    step = width / 400
    runs: list[tuple[float, float]] = []
    run_start: float | None = None
    x = min_x
    while x <= max_x:
        covered = any(b.x0 <= x <= b.x1 for b in blocks)
        if not covered:
            if run_start is None:
                run_start = x
        else:
            if run_start is not None:
                runs.append((run_start, x))
                run_start = None
        x += step
    if run_start is not None:
        runs.append((run_start, max_x))

    if not runs:
        return None

    gap_start, gap_end = max(runs, key=lambda r: r[1] - r[0])
    if (gap_end - gap_start) < width * MIN_GAP_RATIO:
        return None

    left = [b for b in blocks if b.cx < gap_start]
    right = [b for b in blocks if b.cx > gap_end]
    rest = len(blocks) - len(left) - len(right)

    if len(left) < MIN_BLOCKS_PER_COLUMN or len(right) < MIN_BLOCKS_PER_COLUMN:
        return None
    if min(len(left), len(right)) / len(blocks) < MIN_COLUMN_SHARE:
        return None
    # 分栏后若仍有大量块横跨中线，说明这条"空白"只是偶然的行尾空隙
    if rest > len(blocks) * 0.2:
        return None
    # 上下结构（上半页一段、下半页一段）不该被切成左右栏
    if _vertical_overlap_ratio(left, right) < MIN_VERTICAL_OVERLAP:
        return None
    # 短语型布局（职位 + 时间、技能 + 等级）按行读才对，切成两栏会把它读成
    # 「所有职位、所有时间」两个不相干的块。宁可漏判两栏退回按行读，
    # 也不要把单栏切错导致正文整体串行。
    #
    # 但「一侧短标签 + 一侧成段正文」是例外：那是侧边栏布局，必须按栏读，
    # 否则会读成「标签 / 正文段 / 标签 / 正文段」逐行交错。
    both_are_prose = (
        _median_text_len(left) >= MIN_MEDIAN_BLOCK_CHARS
        and _median_text_len(right) >= MIN_MEDIAN_BLOCK_CHARS
    )
    if not both_are_prose and not _looks_like_sidebar(left, right, width):
        return None

    return gap_start, gap_end


def _apply_gap(blocks: list[Block], gap: tuple[float, float]) -> list[list[Block]]:
    gap_start, gap_end = gap
    mid = (gap_start + gap_end) / 2
    # 跨越中线的少量块按中心位置归栏，保证不丢内容
    left = [b for b in blocks if b.cx < mid]
    right = [b for b in blocks if b.cx >= mid]
    if not left or not right:
        return [blocks]
    return [left, right]


def _split_columns(blocks: list[Block]) -> tuple[list[Block], list[list[Block]]]:
    """切成 (顶部横跨块, 各栏)。

    顶部横跨块单独返回的原因：它是横跨整页的姓名/标题行，本身不属于任何一栏。
    按中心 x 塞进某一栏，会让姓名出现在侧边栏中间或正文中间。

    分栏检测失败时会尝试剥离顶部若干块再试 —— 页眉正是最常见的失败原因：
    它横跨栏间空白，把整条空白堵死，导致整页被判为单栏。
    """
    gap = _find_column_gap(blocks)
    if gap is not None:
        return [], _apply_gap(blocks, gap)

    ordered = sorted(blocks, key=lambda b: (b.y0, b.x0))
    limit = min(MAX_HEADER_BLOCKS, len(ordered) - 2)
    for cut in range(1, max(limit, 0) + 1):
        header = ordered[:cut]
        body = ordered[cut:]
        body_gap = _find_column_gap(body)
        if body_gap is not None:
            columns = _apply_gap(body, body_gap)
            if len(columns) > 1:
                logger.debug("剥离顶部 %d 个横跨块后检测到分栏", cut)
                return header, columns

    return [], [blocks]


def _order_within_column(blocks: list[Block]) -> list[Block]:
    """栏内排序：先聚成行，行按 y、行内按 x。"""
    if not blocks:
        return []

    heights = sorted(b.height for b in blocks)
    median_h = heights[len(heights) // 2] or 1.0
    # 行容差取中位行高的 60%：够把同一行里字号不同的块并到一起，
    # 又不至于把相邻两行（间距通常 > 行高）压成一行
    tol = max(median_h * 0.6, 1.0)

    rows: list[list[Block]] = []
    for block in sorted(blocks, key=lambda b: (b.cy, b.x0)):
        target = None
        for row in rows:
            row_cy = sum(b.cy for b in row) / len(row)
            if abs(block.cy - row_cy) <= tol:
                target = row
                break
        if target is None:
            rows.append([block])
        else:
            target.append(block)

    rows.sort(key=lambda row: sum(b.cy for b in row) / len(row))
    ordered: list[Block] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda b: b.x0))
    return ordered


def restore_reading_order(blocks: list[Block]) -> list[Block]:
    """还原整份文档的阅读顺序，并回填 read_order。

    按页处理而非全文档统一处理：跨页分栏不一致（首页单栏、次页两栏）很常见，
    统一处理会把两页的坐标混在一起算。

    旋转文本（非水平基线）整体排到最后，不参与分栏与行聚类。
    理由：这类块在设计类简历里通常是竖排侧标、装饰性大字，本身不承载需按位置
    阅读的正文；若混入正常排序，它会插进正文中间把句子切断。
    代价也明确：如果整栏正文被旋转排版，它的位置信息会丢失 —— 这类简历当前不支持，
    但至少不会把可读的部分也拖坏。
    """
    upright = [b for b in blocks if b.is_horizontal]
    rotated = [b for b in blocks if not b.is_horizontal]
    if rotated:
        logger.info(
            "检测到 %d 个非水平文本块（旋转/竖排），已排到阅读顺序末尾：%s",
            len(rotated),
            "、".join(b.text[:12] for b in rotated[:3]),
        )

    pages: dict[int, list[Block]] = {}
    for block in upright:
        pages.setdefault(block.page_no, []).append(block)

    ordered: list[Block] = []
    cursor = 0
    for page_no in sorted(pages):
        header, columns = _split_columns(pages[page_no])
        # 顶部横跨块（姓名/标题）先读，再读各栏
        for block in header:
            block.read_order = cursor
            cursor += 1
            ordered.append(block)
        for column in columns:
            for block in _order_within_column(column):
                block.read_order = cursor
                cursor += 1
                ordered.append(block)

    for block in sorted(rotated, key=lambda b: (b.page_no, b.y0, b.x0)):
        block.read_order = cursor
        cursor += 1
        ordered.append(block)

    return ordered
