"""向库里导入几条设计过的 JD，覆盖不同的硬约束场景。

这些 JD 不是随手编的：每条都对应一个必须被正确处理的判定分支，
同时充当 M2 匹配逻辑的第一批冒烟样本（完整评测集另有 30–50 条人工标注）。

覆盖的分支：
  1. 高度匹配       → 应通过且得分靠前
  2. 学历不达标     → 应被学历否决
  3. 外包岗位       → 工作性质由偏好决定
  4. 实习 / 在校    → 应被工作性质否决（默认只找全职）
  5. 异地           → 应被城市否决

用法：.venv/Scripts/python.exe scripts/seed_jobs.py
"""

from __future__ import annotations

import json

import httpx

API = "http://127.0.0.1:8000/api/jobs/ingest"

JOBS: list[tuple[str, str]] = [
    (
        "后端-高匹配",
        """高级后端工程师
北京 · 全职 · 25-40K·14薪

岗位职责：
1. 负责核心交易系统的架构设计与开发；
2. 优化系统性能，保障高并发场景下的稳定性；
3. 参与技术方案评审，指导初级工程师。

任职要求：
1. 统招本科及以上学历，计算机相关专业；
2. 3-5年后端开发经验；
3. 精通 Go 或 Java，熟悉 MySQL、Redis、Kafka；
4. 有分布式系统设计经验者优先；
5. 熟悉 Kubernetes 者优先。""",
    ),
    (
        "算法-学历不符",
        """机器学习算法工程师
上海 · 全职 · 40-70K·16薪

岗位职责：
1. 负责推荐系统召回与排序模型的研发与迭代；
2. 跟进业界前沿算法并落地到业务。

任职要求：
1. 硕士及以上学历，计算机、数学相关专业；
2. 3年以上机器学习工程经验；
3. 精通 PyTorch 或 TensorFlow；
4. 有大规模推荐系统经验者优先。""",
    ),
    (
        "外包-后端",
        """后端开发工程师（外包驻场）
北京 · 外包 · 15-22K

岗位职责：
1. 按甲方需求完成业务系统后端开发；
2. 配合完成联调与上线支持。

任职要求：
1. 本科及以上学历；
2. 2年以上 Java 开发经验；
3. 熟悉 Spring Boot、MySQL。""",
    ),
    (
        "实习-在校",
        """后端开发实习生
杭州 · 实习 · 200-300元/天

岗位职责：
1. 参与业务系统后端功能开发；
2. 在导师指导下完成模块设计与实现。

任职要求：
1. 本科及以上在读，计算机相关专业；
2. 熟悉 Python 或 Go；
3. 每周到岗 4 天以上。""",
    ),
    (
        "异地-城市不符",
        """资深后端工程师
深圳 · 全职 · 35-55K·15薪

岗位职责：
1. 负责支付清结算系统设计与研发；
2. 保障资金链路的一致性与可用性。

任职要求：
1. 本科及以上学历；
2. 5年以上后端开发经验；
3. 精通 Go，熟悉 PostgreSQL、Kafka；
4. 有金融支付领域经验者优先。""",
    ),
]


def main() -> int:
    ok = 0
    for name, text in JOBS:
        try:
            resp = httpx.post(
                API, json={"source": "manual", "raw_text": text}, timeout=180
            )
            if resp.status_code != 200:
                print(f"[FAIL] {name}: http={resp.status_code} {resp.text[:160]}")
                continue
            data = resp.json()
            p = data["profile"]
            print(
                f"[OK] {name}: job_id={data['job_id']} "
                f"edu={p['edu_level']} ft={p['edu_fulltime_required']} "
                f"nature={p['job_nature']} city={p['city']} "
                f"exp={p['exp_min']}-{p['exp_max']} "
                f"must={len(p['must_have_skills'])} nice={len(p['nice_to_have_skills'])} "
                f"¥{data['cost_cny']}"
            )
            print(f"     must={json.dumps(p['must_have_skills'], ensure_ascii=False)}")
            ok += 1
        except Exception as exc:
            print(f"[ERR] {name}: {type(exc).__name__}: {exc}")
    print(f"\n{ok}/{len(JOBS)} 导入成功")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
