"""生成品牌图标（扩展图标 + 网页 logo）。

素材
    assets/brand/shinchan.png —— 小新立绘（用户提供的素材）。
    原图已经抠好透明底，但人物是**紧裁**的：头发顶到上边缘、手脚贴着左右下边缘，
    所以合成时必须自己留出内边距，否则放大之后人会顶满整个方块。

为什么用脚本而不是丢五张图进来
    图标要出 16 / 32 / 48 / 128 / 256 五个尺寸。手工导出五遍，改一次素材就要
    重来五遍；脚本是 4 倍超采样、每个尺寸独立合成，小尺寸还会自动换成头像特写
    （全身像在 16px 下只剩一团色块）。

版权
    角色形象属于《蜡笔小新》（原作 臼井仪人 / 版权 双叶社）。
    本项目按用户要求做个人本地使用，不商业分发、不对外提供服务。
    要公开发布或商用，这个素材必须换掉。

用法：python scripts/make_brand.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
STATIC = ROOT / "backend" / "static"
SRC = ROOT / "assets" / "brand" / "shinchan.png"

# 底板沿用品牌靛蓝渐变：人物是红黄暖色，压在冷底上对比最强，
# 小尺寸也能一眼看出是人而不是一块色斑
BG_FROM = (99, 91, 255)
BG_TO = (56, 189, 248)

SS = 4  # 超采样倍数


def _gradient(size: int) -> Image.Image:
    """对角线性渐变。逐像素算比 PIL 的线性渐变可控：能保证两个角都是目标色。"""
    y, x = np.mgrid[0:size, 0:size]
    t = (x + y) / (2 * size - 2)
    r = (BG_FROM[0] + (BG_TO[0] - BG_FROM[0]) * t).astype(np.uint8)
    g = (BG_FROM[1] + (BG_TO[1] - BG_FROM[1]) * t).astype(np.uint8)
    b = (BG_FROM[2] + (BG_TO[2] - BG_FROM[2]) * t).astype(np.uint8)
    arr = np.dstack([r, g, b, np.full_like(r, 255)])
    return Image.fromarray(arr, "RGBA")


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _load_figure() -> Image.Image:
    if not SRC.exists():
        raise SystemExit(f"找不到素材：{SRC.relative_to(ROOT)}")
    return Image.open(SRC).convert("RGBA")


def _head_crop(fig: Image.Image) -> Image.Image:
    """裁出头像。小尺寸只用脸，全身像在 16px 下会糊成一团色块。

    横向中心取 0.56 而不是 0.5：原图左手举在左侧，人物的头其实偏右，
    按正中裁会把脸切掉一半。
    """
    w, h = fig.size
    side = int(min(h * 0.62, w))
    cx = int(w * 0.56)
    left = max(0, min(w - side, cx - side // 2))
    return fig.crop((left, 0, left + side, side))


def draw_icon(size: int, compact: bool = False) -> Image.Image:
    """合成一张 size×size 图标。compact=True 时用头像特写（16/32 用）。"""
    s = size * SS
    mask = _rounded_mask(s, int(s * 0.22))

    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    base.paste(_gradient(s), (0, 0), mask)

    fig = _head_crop(_load_figure()) if compact else _load_figure()
    # 内边距：留 6%（头像）/ 9%（全身），否则人会顶满方块、贴着圆角
    box = s * (0.88 if not compact else 0.94)
    scale = min(box / fig.width, box / fig.height)
    fw, fh = max(1, int(fig.width * scale)), max(1, int(fig.height * scale))
    fig = fig.resize((fw, fh), Image.LANCZOS)

    base.alpha_composite(fig, ((s - fw) // 2, (s - fh) // 2))

    # 再套一次圆角：人物可能压到角上，超出底板的部分要切掉
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    return out.resize((size, size), Image.LANCZOS)


def main() -> int:
    EXT.mkdir(parents=True, exist_ok=True)
    STATIC.mkdir(parents=True, exist_ok=True)

    for size in (16, 32, 48, 128, 256):
        out = EXT / f"icon{size}.png"
        draw_icon(size, compact=size <= 32).save(out, optimize=True)
        print(f"  {out.relative_to(ROOT)}  {size}x{size}")

    # 侧栏 logo 只有 36px，用头像特写：全身像在 36px 下脸只剩十几个像素，
    # 而侧栏里用户唯一需要认出来的是"这是谁"
    for size in (96, 192):
        out = STATIC / f"logo{size}.png"
        draw_icon(size, compact=True).save(out, optimize=True)
        print(f"  {out.relative_to(ROOT)}  {size}x{size}")

    print("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
