"""生成品牌图标（扩展图标 + 网页 logo）。

为什么用脚本画而不是丢一张设计稿进来
    图标会在 5 个尺寸上出现（16 / 32 / 48 / 128 / 256），手绘一次再逐尺寸导出，
    小尺寸必然糊。这里用 4 倍超采样 + LANCZOS 缩放，每个尺寸都是独立渲染的；
    而且改配色只改一个常量，不用回去翻设计软件。

形象
    「小新求职」—— 红衣黄裤、粗眉圆脸、额头露出来的短发小孩。
    角色形象属于《蜡笔小新》（原作 臼井仪人 / 版权 双叶社），
    本项目按用户要求做个人本地使用，不做商业分发、不对外提供服务。
    如果要公开发布或商用，这个图标必须换掉 —— 角色形象受版权保护。

用法：python scripts/make_brand.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
STATIC = ROOT / "backend" / "static"

# 配色取自角色本身：红衣 + 黄裤，压在冷色底板上对比最强，小尺寸也能一眼认出
BG_FROM = (99, 91, 255)
BG_TO = (56, 189, 248)
SKIN = (255, 219, 189)
HAIR = (26, 24, 38)
SHIRT = (226, 58, 52)
BLUSH = (255, 148, 148)

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


def draw_icon(size: int, compact: bool = False) -> Image.Image:
    """画一张 size×size 的图标。compact=True 时只画头（小尺寸用）。"""
    s = size * SS
    radius = int(s * 0.22)

    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(_gradient(s), (0, 0), mask)
    d = ImageDraw.Draw(img, "RGBA")

    # 背景雷达弧：暗示"盯着"，同时给小尺寸一点纹理
    for r, alpha in ((0.86, 24), (0.66, 28), (0.46, 32)):
        box = [int(s * (0.5 - r / 2)), int(s * (0.5 - r / 2)),
               int(s * (0.5 + r / 2)), int(s * (0.5 + r / 2))]
        d.arc(box, start=200, end=340, fill=(255, 255, 255, alpha), width=max(2, int(s * 0.012)))

    if compact:
        cx, cy = s * 0.50, s * 0.50
        R = s * 0.33
        body_top = s * 0.82
    else:
        cx, cy = s * 0.50, s * 0.42
        R = s * 0.245
        body_top = s * 0.60

    # ---------- 身体：红上衣，做成半身像 ----------
    # 试过把黄短裤也画出来，但在方形图标里只剩底部一条黄边，
    # 看起来像人被一条黄杠截断 —— 半身像反而干净，红衣服本身已经足够认人
    bw = R * 2.55
    d.rounded_rectangle(
        [cx - bw / 2, body_top, cx + bw / 2, s * 1.16],
        radius=int(bw * 0.34), fill=SHIRT,
    )
    d.rectangle(
        [cx - bw / 2 + 2, body_top + R * 0.45, cx + bw / 2 - 2, s * 1.16],
        fill=SHIRT,
    )

    # 脖子：一小段，被上衣压住一半
    d.ellipse(
        [cx - R * 0.24, cy + R * 0.62, cx + R * 0.24, cy + R * 1.02],
        fill=(242, 200, 168),
    )

    # ---------- 头 ----------
    d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=SKIN)

    # ---------- 头发：只有头顶一片 + 两侧鬓角 ----------
    # 这个形象的关键是**额头大面积露出来**。用整圆盖住上半会让脸只剩一条缝，
    # 认不出是谁；所以先画整头黑，再把额头以下挖掉
    hair = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hair)
    hd.ellipse([cx - R, cy - R * 1.02, cx + R, cy + R * 0.98], fill=HAIR)
    hd.rectangle([0, cy - R * 0.34, s, s], fill=(0, 0, 0, 0))
    img.alpha_composite(hair)
    d = ImageDraw.Draw(img, "RGBA")

    # 鬓角：起于发际线上方（和头顶那一片连起来），到耳朵高度收住。
    # 必须窄——宽一点就变成戴耳机，这个坑踩过一次
    for sign in (-1, 1):
        x_out = cx + sign * R * 0.90
        d.rounded_rectangle(
            [x_out - R * 0.055, cy - R * 0.40, x_out + R * 0.055, cy + R * 0.02],
            radius=int(R * 0.05), fill=HAIR,
        )

    # ---------- 五官 ----------
    # 眉毛：又粗又横，是这个形象第一眼认得出的地方。
    # 做成斜的会变成"生气脸"—— 先试了外高内低的梯形，整张脸立刻凶了起来
    brow_y = cy - R * 0.13
    brow_w = R * 0.46
    brow_h = R * 0.125
    for sign in (-1, 1):
        bx = cx + sign * R * 0.40
        d.rounded_rectangle(
            [bx - brow_w / 2, brow_y - brow_h / 2, bx + brow_w / 2, brow_y + brow_h / 2],
            radius=int(brow_h * 0.42), fill=HAIR,
        )

    # 眼睛：黑豆 + 高光
    eye_r = R * 0.125
    for sign in (-1, 1):
        ex = cx + sign * R * 0.36
        ey = cy + R * 0.16
        d.ellipse([ex - eye_r, ey - eye_r * 1.05, ex + eye_r, ey + eye_r * 1.05], fill=HAIR)
        d.ellipse(
            [ex - eye_r * 0.30, ey - eye_r * 0.82, ex + eye_r * 0.34, ey - eye_r * 0.18],
            fill=(255, 255, 255, 235),
        )

    # 腮红
    for sign in (-1, 1):
        ex = cx + sign * R * 0.62
        ey = cy + R * 0.44
        d.ellipse(
            [ex - R * 0.15, ey - R * 0.09, ex + R * 0.15, ey + R * 0.09],
            fill=BLUSH + (140,),
        )

    # 嘴：细一点的小笑。线一粗就变成胡子（第一版就是这个问题）
    d.arc(
        [cx - R * 0.20, cy + R * 0.38, cx + R * 0.20, cy + R * 0.60],
        start=20, end=160, fill=HAIR, width=max(2, int(R * 0.06)),
    )

    return img.resize((size, size), Image.LANCZOS)


def draw_web_logo(size: int) -> Image.Image:
    """网页侧栏用的方形圆角 logo。"""
    return draw_icon(size)


def main() -> int:
    EXT.mkdir(parents=True, exist_ok=True)
    STATIC.mkdir(parents=True, exist_ok=True)

    for size in (16, 32, 48, 128, 256):
        # 16/32 太小，画全身会糊成一团 —— 只画头
        out = EXT / f"icon{size}.png"
        draw_icon(size, compact=size <= 32).save(out, optimize=True)
        print(f"  {out.relative_to(ROOT)}  {size}x{size}")

    for size in (96, 192):
        out = STATIC / f"logo{size}.png"
        draw_web_logo(size).save(out, optimize=True)
        print(f"  {out.relative_to(ROOT)}  {size}x{size}")

    print("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
