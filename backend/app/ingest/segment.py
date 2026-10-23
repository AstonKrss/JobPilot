"""把还原后的文本切成适合单次调用的分段。

依据项目决策：长简历不截断，按段抽取再合并。
    截断会丢掉最近的工作经历（通常写在简历后半），而那恰恰是匹配最看重的部分；
    先摘要再匹配则会丢掉硬约束需要的精确信息（学历层次、具体年限）。

切分点选在块边界而非字符位置：块边界是版面给出的天然语义单元，
从块中间切开会把一条工作描述劈成两半，抽取时两边都变成残缺句子。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .layout import Block

# 单段目标长度。DeepSeek 上下文 1M，这个值不是为了塞得下，
# 而是为了让单次输出保持在结构化字段可控的范围内（输出过长容易漏字段）
MAX_CHUNK_CHARS = 1800


@dataclass(slots=True)
class Chunk:
    index: int
    text: str
    # 记录来源块，抽取结果出问题时可回到具体版面位置核查
    source_block_ids: list[int] = field(default_factory=list)


def chunk_blocks(blocks: list[Block], max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    """按块边界累加切段。blocks 需已按阅读顺序排好。"""
    chunks: list[Chunk] = []
    buf: list[str] = []
    size = 0
    ids: list[int] = []

    def flush() -> None:
        nonlocal buf, size, ids
        if not buf:
            return
        chunks.append(
            Chunk(index=len(chunks), text="\n".join(buf).strip(), source_block_ids=list(ids))
        )
        buf, size, ids = [], 0, []

    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        # 单块超长时不硬切：这个块本身是一个完整语义单元（如一大段项目描述），
        # 切开反而破坏它。直接独占一段
        if len(text) >= max_chars:
            flush()
            chunks.append(
                Chunk(index=len(chunks), text=text, source_block_ids=[block.block_index])
            )
            continue

        if size + len(text) > max_chars:
            flush()
        buf.append(text)
        size += len(text)
        ids.append(block.block_index)

    flush()
    return chunks
