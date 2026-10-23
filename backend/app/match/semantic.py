"""语义等价层：用本地 embedding 补检索式匹配覆盖不到的长尾。

为什么需要这一层
    规则层（别名表 + 字符 n-gram + 拼音 + 编辑距离）能处理
    「K8s = Kubernetes」「SpringBoot = Spring Boot」这类**字面**关系，
    但处理不了「分布式经验 ≈ 微服务架构」「做过高并发 ≈ 支撑日均三千万笔」
    这类**语义**等价。招聘 JD 里这类表述极常见，也是纯规则最容易漏的地方
    —— 漏判的代价是本来合适的岗位被误杀。

为什么用本地模型
    DeepSeek 没有 embedding 接口；接云端向量服务要多一套 key 与计费，
    而且模型升级后旧向量与新查询不在同一空间，必须全量重算。
    本地 bge-small-zh-v1.5（ONNX，512 维，CPU 单条约 10ms）：
    零 API 成本、不出本机、向量永远可复现。

口径（重要，别改错）
    语义层**只能把命中升级，不能把未命中变成否决**：
    规则判为未命中、但语义上其实具备的技能 → 升级为命中；
    规则命中的一律保留；两者都不满足的仍然未命中。
    阈值保守（默认 0.82），宁可漏判不可误判 —— 误判会把不相干的岗位判成匹配。

模型不可用时（没下载 / 依赖缺失），整层静默降级为纯规则匹配，不影响主流程。
模型文件由 `scripts/fetch_embed_model.py` 下载到 `data/models/bge-small-zh-v1.5/`。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# 本文件在 backend/app/match/ 下，往上三层才是项目根目录
ROOT = Path(__file__).resolve().parents[3]
MODEL_DIR = ROOT / "data" / "models" / "bge-small-zh-v1.5"
MODEL_FILE = MODEL_DIR / "model_optimized.onnx"
TOKENIZER_FILE = MODEL_DIR / "tokenizer.json"

MAX_LENGTH = 512
# 低于这个相似度一律按"未命中"处理。0.82 在中文技术词上偏保守：
# 无关技术词（Kafka vs Redis）通常落在 0.5–0.65，真正同义/近义表达才到 0.8 以上
DEFAULT_THRESHOLD = 0.82

_lock = threading.Lock()
_session = None
_tokenizer = None
_load_failed = False
_cache: dict[str, list[float]] = {}


def available() -> bool:
    """模型是否可用。不可用时不会抛异常，只会让语义匹配这一层缺席。"""
    return _load() is not None


def _load():
    """加载 ONNX 推理会话与分词器。失败只降级，不抛异常。"""
    global _session, _tokenizer, _load_failed
    if _session is not None or _load_failed:
        return _session
    with _lock:
        if _session is not None or _load_failed:
            return _session
        if not MODEL_FILE.exists() or not TOKENIZER_FILE.exists():
            _load_failed = True
            logger.warning(
                "语义模型文件缺失，跳过语义匹配：%s（运行 scripts/fetch_embed_model.py 下载）",
                MODEL_DIR,
            )
            return None
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(str(TOKENIZER_FILE))
            # 必须自己设 lowercase=True。仓库里的 tokenizer.json 是给 fastembed 用的，
            # 它的归一化器 lowercase=False，于是 "Python"/"Java"/"Kafka" 全被切成 [UNK]，
            # 所有短英文词会得到完全相同的向量（相似度 1.0），整个语义层直接失效。
            # 这个坑很隐蔽：编码不报错，只是结果毫无意义。
            try:
                from tokenizers.normalizers import BertNormalizer

                tokenizer.normalizer = BertNormalizer(
                    clean_text=True,
                    handle_chinese_chars=True,
                    strip_accents=True,
                    lowercase=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("分词器归一化设置失败，可能影响语义质量：%s", exc)
            tokenizer.enable_truncation(max_length=MAX_LENGTH)
            tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            session = ort.InferenceSession(
                str(MODEL_FILE), providers=["CPUExecutionProvider"]
            )
            _session, _tokenizer = session, tokenizer
            logger.info("语义模型已就绪：%s", MODEL_FILE)
        except Exception as exc:  # noqa: BLE001 - 任何失败都只降级，不阻断
            _load_failed = True
            logger.warning("语义模型加载失败，本次仅用规则匹配：%s", exc)
    return _session


def embed(texts: list[str]) -> list[list[float]] | None:
    """批量编码（CLS pooling + L2 归一化）。返回 None 表示语义层不可用。

    三级查找：内存 → embedding_cache 表 → 真的跑模型。
    重启服务后内存清空，但库里还在，所以不会有"每次重启第一枪都慢"的问题。
    """
    if not texts:
        return []
    session = _load()
    if session is None:
        return None

    # 空文本不送模型（分词器对空串的行为不可控）。
    # 空串占位用零向量补齐，保持与输入等长 —— 调用方拿到 None 才表示"语义层不可用"
    texts = list(texts)
    unique = [t for t in dict.fromkeys(texts) if t]
    missing = [t for t in unique if t not in _cache]

    # 第二级：持久化缓存
    if missing:
        restored = _load_persisted(missing)
        if restored:
            _cache.update(restored)
            missing = [t for t in missing if t not in _cache]

    if missing:
        try:
            import numpy as np

            encodings = _tokenizer.encode_batch(missing)
            input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
            token_type_ids = np.array([e.type_ids for e in encodings], dtype=np.int64)
            output = session.run(
                None,
                {
                    "input_ids": input_ids,
                    "attention_mask": attention_mask,
                    "token_type_ids": token_type_ids,
                },
            )[0]
            # bge 系列用 CLS 向量，再做 L2 归一化
            cls = output[:, 0, :]
            norms = np.linalg.norm(cls, axis=1, keepdims=True)
            cls = cls / np.maximum(norms, 1e-9)
            for text, vector in zip(missing, cls):
                _cache[text] = [float(x) for x in vector]
            # 第三级写回：编码成功才落库，失败的文本下次自然重算
            _save_persisted({t: _cache[t] for t in missing if t in _cache})
        except Exception as exc:  # noqa: BLE001
            logger.warning("语义编码失败，降级为规则匹配：%s", exc)
            return None

    known = next((v for v in _cache.values() if v), None)
    if known is None and missing:
        return None
    out: list[list[float]] = []
    for t in texts:
        if not t:
            out.append([0.0] * len(known or []))
        elif t in _cache:
            out.append(_cache[t])
        else:
            return None
    return out


# 模型换了（或归一化设置变了）必须改这里，否则旧向量会被当成新模型的输出复用，
# 相似度会莫名其妙地漂移
EMBED_MODEL_TAG = "bge-small-zh-v1.5/lowercase/cls-normalized"


def _embed_key(text: str) -> str:
    return hashlib.sha256(f"{EMBED_MODEL_TAG}\x00{text}".encode("utf-8")).hexdigest()


def _load_persisted(texts: list[str]) -> dict[str, list[float]]:
    """按哈希批量取回向量。DB 不可用时静默返回空，语义层照常走内存+模型。"""
    keys = {_embed_key(t): t for t in texts}
    try:
        import numpy as np

        from ..db.pool import get_pool

        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT key, vector FROM embedding_cache WHERE key = ANY(%s)",
                (list(keys),),
            ).fetchall()
        out: dict[str, list[float]] = {}
        for key, blob in rows:
            text = keys.get(key)
            if text is None or not blob:
                continue
            out[text] = [float(x) for x in np.frombuffer(blob, dtype=np.float32)]
        if out:
            logger.info("向量缓存命中 %d/%d 条", len(out), len(texts))
        return out
    except Exception as exc:  # noqa: BLE001 - 缓存读不到不该影响匹配
        logger.debug("读取向量缓存失败（忽略）：%s", exc)
        return {}


def _save_persisted(vectors: dict[str, list[float]]) -> None:
    if not vectors:
        return
    try:
        import numpy as np

        from ..db.pool import get_pool

        payload = [
            (
                _embed_key(text),
                EMBED_MODEL_TAG,
                len(vec),
                np.asarray(vec, dtype=np.float32).tobytes(),
            )
            for text, vec in vectors.items()
            if vec
        ]
        if not payload:
            return
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO embedding_cache (key, model, dim, vector)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    payload,
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 写缓存失败只影响下次是否重算
        logger.debug("写入向量缓存失败（忽略）：%s", exc)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def build_evidence(resume_profile: dict) -> str:
    """把简历里能证明"会什么"的部分拼成一段证据文本。

    只取技能、经历要点、项目要点 —— 学历、城市这些不构成技能证据。
    """
    if not resume_profile:
        return ""

    parts: list[str] = []

    skills = resume_profile.get("skills") or []
    names: list[str] = []
    for item in skills:
        if isinstance(item, str):
            names.append(item)
        else:
            name = item.get("name")
            if name:
                names.append(name)
            evidence = item.get("evidence")
            if evidence:
                parts.append(str(evidence))
    if names:
        parts.append("技能：" + "、".join(names))

    for exp in resume_profile.get("experiences") or []:
        title = exp.get("title")
        company = exp.get("company")
        if title or company:
            parts.append(f"职位：{company or ''} {title or ''}".strip())
        for line in exp.get("highlights") or []:
            parts.append(str(line))

    for proj in resume_profile.get("projects") or []:
        name = proj.get("name")
        tech = proj.get("tech") or []
        if name:
            parts.append("项目：" + str(name))
        if tech:
            parts.append("技术栈：" + "、".join(str(t) for t in tech))
        for line in proj.get("highlights") or []:
            parts.append(str(line))

    return "\n".join(parts)


def recall_pairs(
    evidence_items: list[str],
    jd_items: list[str],
    top_k: int = 2,
    floor: float = 0.0,
) -> dict[str, list[tuple[str, float]]]:
    """向量召回：对每条 JD 要求，返回最像的若干条简历证据。

    **这里刻意不做判定，只做召回。**
    实测（bge-small-zh-v1.5，中文技术语境）：
        正例「具备高并发系统设计经验」↔「支撑日均三千万笔交易」= 0.504（排第 1）
        正例「消息队列中间件应用经验」↔「使用 RabbitMQ 削峰」= 0.443（排第 1）
        负例「熟练使用 Photoshop」↔ 某条无关证据 = 0.513（反而更高）
    也就是说**绝对值没有区分度**（正负分布重叠），但**相对排序是对的**：
    真正的等价证据总能排进 top-k。
    所以正确用法是「向量粗召回 → 强模型逐对判定」，而不是拿余弦相似度直接拍板。
    任何基于相似度阈值的直接判定都会误杀或误放。

    floor 用来砍掉明显不相关的配对，控制送进模型的条目数。
    """
    if not evidence_items or not jd_items:
        return {}

    vectors = embed(list(evidence_items) + list(jd_items))
    if vectors is None:
        return {}

    ev_vectors = vectors[: len(evidence_items)]
    jd_vectors = vectors[len(evidence_items) :]

    out: dict[str, list[tuple[str, float]]] = {}
    for jd_item, jd_vector in zip(jd_items, jd_vectors):
        scored = [
            (text, round(cosine(jd_vector, ev), 4))
            for text, ev in zip(evidence_items, ev_vectors)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        top = [pair for pair in scored[:top_k] if pair[1] >= floor]
        if top:
            out[jd_item] = top
    return out


def build_evidence_items(resume_profile: dict) -> list[str]:
    """拆成一条条独立证据，供向量逐条召回。

    比拼成一整段更准：整段里混着技能名与职责句，任意一条都能把相似度拉高，
    逐条比才能定位到"到底是哪句话证明了这项能力"。
    """
    if not resume_profile:
        return []

    items: list[str] = []

    for skill in resume_profile.get("skills") or []:
        if isinstance(skill, str):
            if skill.strip():
                items.append(skill.strip())
        else:
            name = skill.get("name")
            if name:
                items.append(str(name))
            evidence = skill.get("evidence")
            if evidence:
                items.append(str(evidence))

    for exp in resume_profile.get("experiences") or []:
        title = exp.get("title")
        if title:
            items.append(str(title))
        for line in exp.get("highlights") or []:
            if line:
                items.append(str(line))

    for proj in resume_profile.get("projects") or []:
        name = proj.get("name")
        if name:
            items.append(str(name))
        for tech in proj.get("tech") or []:
            if tech:
                items.append(str(tech))
        for line in proj.get("highlights") or []:
            if line:
                items.append(str(line))

    # 去重但保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def semantic_hits(
    evidence: str,
    jd_items: list[str],
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, float]:
    """返回 {JD 要求: 相似度}，只包含达到阈值的项。

    JD 要求往往是一整句（"具备 Celery/RabbitMQ/Kafka 等消息队列技术的应用经验"），
    简历证据也是散文，所以这里做的是"整句对整段"的相似度，
    而不是逐词比对 —— 逐词对散文没有意义。
    """
    if not evidence or not jd_items:
        return {}
    if len(evidence) > 4000:
        evidence = evidence[:4000]

    vectors = embed([evidence] + list(jd_items))
    if vectors is None:
        return {}

    base = vectors[0]
    hits: dict[str, float] = {}
    for item, vector in zip(jd_items, vectors[1:]):
        score = cosine(base, vector)
        if score >= threshold:
            hits[item] = round(score, 3)
    return hits
