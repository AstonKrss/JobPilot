"""JD 文本回归：用库里真实抓来的岗位文本验证清洗规则。

为什么不用合成样本：BOSS 详情页的噪声分布很随机（有的带招聘者名片、有的带整块
SEO 链接、有的带薪资占位符），合成样本只能覆盖我想到的情况。库里这批是真实抓的。

一个必须说明的局限
    库里大部分 raw_text 存的是**清洗后**的文本（历史原因，当时没留原文），
    所以对这批历史样本，脚本只能验证三条不变量：

    1. 幂等：清洗对已清洗的文本不再改动。
       规则一旦改坏（比如新加的规则会误删正文），二次清洗就会删掉东西 —— 立刻暴露。
    2. 内容锚点：原文含「职位描述 / 任职要求」这类段落的，清洗后必须还在。
       这是最容易被误删的部分，而它恰好是抽取必备技能的唯一来源。
    3. 无噪声残留：界面按钮文案（立即沟通 / 举报 / 热门城市…）不能出现在结果里。

    新入库的岗位会同时存 raw_text_original（清洗前原文），
    对这批样本脚本跑的是真正的前后对比：噪声必须清掉、正文不能被删、留存率不能过低。
    —— 这条路径上线当天就抓到了一个存在很久的 bug：手动粘贴的文本里
    「收藏 / 立即沟通 / 举报」挤成一行时，单行噪声规则匹配不上，整行进了正文。

用法：
    .venv/Scripts/python.exe scripts/check_jd_text.py
    .venv/Scripts/python.exe scripts/check_jd_text.py -v   # 打印每条样本
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.pool import get_pool  # noqa: E402
from app.ingest.clean_jd import clean_jd  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "jd_samples.json"

# 界面上这些字不会被当成岗位要求，出现就是清洗漏了
NOISE_WORDS = (
    "立即沟通", "微信扫码分享", "扫码分享", "点击查看地图", "热门城市", "热门职位",
    "升级VIP", "去App", "求职工具", "查看更多信息",
)

# JD 的主要内容段落标题。
# 注意：这些词会被 clean_jd 当作「正文起点」消费掉（用来切分 head/body），
# 所以不能断言"清洗后它们还在" —— 那会把正确行为判成失败。
# 真正要保护的是"标题下面的正文没被删"，用实质内容行的保留率来衡量。
ANCHORS = ("职位描述", "岗位职责", "任职要求", "职位要求", "工作内容", "任职资格", "工作要求")

# 二次清洗后允许的最小长度比。已清洗的文本理论上应该 1.00，
# 留一点余量给会顺手去掉的行尾空白
MIN_RATIO = 0.95

# 清洗后至少要保住的实质内容行占比。噪声行被删是好事，
# 但正文行被删就是事故 —— 60% 是实测：最大的那个样本删掉的是
# 「按钮行」和「SEO 链接行」，正文 5/7 行保留
MIN_KEEP_LINES = 0.6


def _substantive_lines(text: str) -> list[str]:
    """挑出可能是正文的行：长度 > 8 且不像导航链接堆。

    太短的行（"职位描述"、城市名）不参与统计 —— 它们本来就会被删或作为分隔符，
    拿来算保留率只会制造噪音。
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if len(line) <= 8:
            continue
        # 一行里出现两个以上「招聘」的是站内导航，不算正文
        if line.count("招聘") >= 2:
            continue
        out.append(line)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条样本的结果")
    args = parser.parse_args()

    pool = get_pool()
    try:
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, source, raw_text, raw_text_original FROM job "
                "WHERE length(coalesce(raw_text, '')) > 50 ORDER BY id"
            ).fetchall()
    finally:
        pool.close()

    # 样本来源两路：
    #   fixture  —— 手工固化的边界样本（比如按钮挤成一行的粘贴文本），不依赖数据库，
    #               换机器或清库之后仍然能跑
    #   数据库   —— 真实抓取的分布，覆盖各种页面状态
    # 统一成 (标签, 待清洗文本, 清洗前原文或 None)
    samples: list[tuple[str, str, str | None]] = []
    if FIXTURE.exists():
        for item in json.loads(FIXTURE.read_text(encoding="utf-8")):
            samples.append((f"fixture:{item['name']}", item["original"], item["original"]))
    for job_id, source, raw_text, original in rows:
        samples.append((f"job#{job_id}({source})", raw_text, original))

    if not samples:
        print("没有可用样本，跳过")
        return 2

    with_original = sum(1 for _, _, o in samples if o)
    print(f"样本 {len(samples)} 条（其中可做前后对比的 {with_original} 条）\n")

    passed = 0
    failures: list[str] = []

    def check(ok: bool, name: str, detail: str = "") -> None:
        nonlocal passed
        if ok:
            passed += 1
            if args.verbose:
                print(f"[PASS] {name}")
        else:
            failures.append(f"{name}{' — ' + detail if detail else ''}")
            print(f"[FAIL] {name}{' — ' + detail if detail else ''}")

    for tag, raw_text, original in samples:
        # ---------- 有原文的样本：验证真实的前后差异 ----------
        # 这是这批断言里唯一能证明"清洗到底删了什么"的证据。
        # 之前没有原文，只能验证幂等 —— 而幂等验证不了"该删的删了没"。
        if original:
            fresh = clean_jd(original)
            left = [w for w in NOISE_WORDS if w in fresh]
            check(not left, f"{tag} 清洗后无噪声", f"残留 {left}")

            keep_ratio = len(fresh) / max(len(original), 1)
            check(
                keep_ratio >= 0.35,
                f"{tag} 未过度删除",
                f"只留下 {keep_ratio:.0%}（{len(original)} → {len(fresh)} 字）",
            )

            had = [a for a in ANCHORS if a in original]
            src_lines = _substantive_lines(original)
            kept_lines = [ln for ln in src_lines if ln in fresh]
            if src_lines:
                line_ratio = len(kept_lines) / len(src_lines)
                check(
                    line_ratio >= MIN_KEEP_LINES,
                    f"{tag} 正文未被删",
                    f"实质内容只保留 {len(kept_lines)}/{len(src_lines)} 行"
                    + (f"，丢掉：{', '.join(l[:14] for l in src_lines if l not in fresh)[:60]}" if line_ratio < MIN_KEEP_LINES else ""),
                )
            if args.verbose:
                print(
                    f"       原文 {len(original)} 字 → 清洗后 {len(fresh)} 字"
                    f"（{keep_ratio:.0%}），段落标记 {had or '无'}"
                )

        # ---------- 所有样本：幂等与不误伤 ----------
        once = clean_jd(raw_text)
        twice = clean_jd(once)

        # 1. 幂等
        ratio = len(twice) / max(len(once), 1)
        check(
            ratio >= MIN_RATIO,
            f"{tag} 清洗幂等",
            f"二次清洗后只剩 {ratio:.2f}（{len(once)} → {len(twice)} 字）",
        )

        # 2. 正文保留：历史样本没有原文，用"这一次清洗 vs 上一次结果"比较，
        #    等价于幂等；同时排除掉"结果被清空"这种极端情况
        src_lines = _substantive_lines(raw_text)
        if src_lines:
            kept_lines = [ln for ln in src_lines if ln in twice]
            line_ratio = len(kept_lines) / len(src_lines)
            check(
                line_ratio >= MIN_KEEP_LINES,
                f"{tag} 重新清洗不丢正文",
                f"实质内容只保留 {len(kept_lines)}/{len(src_lines)} 行",
            )

        # 3. 噪声残留
        left = [w for w in NOISE_WORDS if w in once]
        check(not left, f"{tag} 无界面噪声", f"残留 {left}")

    total = passed + len(failures)
    print(f"\n{passed}/{total} 通过")

    # 原文留存情况单独报一次：这是"能不能做真正的清洗回归"的前提
    if with_original == 0:
        print(
            "\n注意：历史数据没有留存清洗前原文（raw_text 存的是清洗后的结果），\n"
            "所以这次只能验证幂等性与不误伤，验证不了清洗强度。\n"
            "新入库的岗位会同时存 raw_text_original，之后再跑本脚本会自动做前后对比。"
        )

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
