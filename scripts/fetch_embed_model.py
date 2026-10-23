"""下载本地语义模型（bge-small-zh-v1.5 的 ONNX 版本）。

为什么不用 fastembed 直接拉
    fastembed 走 HuggingFace 的缓存机制，缓存里用符号链接组织文件；
    Windows 上符号链接常常建不出来，结果它要找的
    snapshots/<rev>/model_optimized.onnx 不存在（文件其实已经下到 blobs 里了）。
    改成用 snapshot_download 的 local_dir 模式：直接把文件落到项目目录，
    不依赖符号链接，也不依赖系统临时目录。

模型选的是 Qdrant 维护的 ONNX 转换版（BAAI 官方仓库只发 safetensors/pytorch）。

用法：
    .venv/Scripts/python.exe scripts/fetch_embed_model.py
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "data" / "models" / "bge-small-zh-v1.5"

REPO = "Qdrant/bge-small-zh-v1.5"
FILES = [
    "model_optimized.onnx",
    "tokenizer.json",
    "config.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
]


def main() -> int:
    # huggingface.co 在部分校园网下不可达，默认走国内镜像
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("缺少 huggingface_hub，请先 pip install huggingface_hub", file=sys.stderr)
        return 1

    TARGET.mkdir(parents=True, exist_ok=True)
    print(f"下载 {REPO} → {TARGET}")
    path = snapshot_download(
        repo_id=REPO,
        local_dir=str(TARGET),
        allow_patterns=FILES,
        max_workers=4,
    )
    print("完成：", path)
    for name in FILES:
        f = TARGET / name
        print(f"  {'OK ' if f.exists() else 'MISS'} {name}"
              f"{f'  {f.stat().st_size // 1024}KB' if f.exists() else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
