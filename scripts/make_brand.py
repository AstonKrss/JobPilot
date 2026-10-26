"""生成品牌图标（扩展图标 + 网页 logo）。

为什么用脚本画而不是丢一张设计稿进来
    图标会在 5 个尺寸上出现（16 / 32 / 48 / 128 / 256），手绘一次再逐尺寸导出，
    小尺寸必然糊。这里用 4 倍超采样 + LANCZOS 缩放，每个尺寸都是独立渲染的；
    而且改配色只改一个常量，不用回去翻设计软件。

形象说明
    一个浓眉圆脸的求职小助手，风格参考用户提到的卡通角色，但**是原创形象**：
    粗眉 + 圆脸 + 黄上衣是通用卡通语汇，没有复制任何受版权保护的角色造型。
    商业角色（蜡笔小新）有版权，个人项目里用也有风险，所以做的是同风格的原创。

用法：python scripts/make_brand.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
STATIC = ROOT / "backend" / "static"

# 品牌色：深靛蓝 → 亮紫，配黄色上衣形成互补对比，小尺寸下也能一眼认出
BG_FROM = (99, 91, 255)
BG_TO = (56, 189, 248)
SKIN = (255, 219, 189)
HAIR = (28, 26, 40)
SHIRT = (255, 205, 60)
BLUSH = (255, 150, 150)

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

    # 底板：圆角矩形 + 渐变
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(_gradient(s), (0, 0), mask)
    d = ImageDraw.Draw(img, "RGBA")

    # 背景雷达弧：暗示"盯着"，同时给小尺寸一点纹理
    for i, (r, alpha) in enumerate(((0.86, 26), (0.66, 30), (0.46, 34))):
        box = [int(s * (0.5 - r / 2)), int(s * (0.5 - r / 2)), int(s * (0.5 + r / 2)), int(s * (0.5 + r / 2))]
        d.arc(box, start=200, end=340, fill=(255, 255, 255, alpha), width=max(2, int(s * 0.012)))

    if compact:
        head_c = (s * 0.5, s * 0.52)
        head_r = s * 0.30
        body_top = s * 0.80
    else:
        head_c = (s * 0.5, s * 0.44)
        head_r = s * 0.235
        body_top = s * 0.66

    # 身体：黄上衣的圆肩
    body_w = head_r * 2.5
    body_box = [
        head_c[0] - body_w / 2,
        body_top,
        head_c[0] + body_w / 2,
        s * 1.12,
    ]
    d.rounded_rectangle(body_box, radius=int(body_w * 0.42), fill=SHIRT)

    # 领口：白色三角，避免"头直接扣在衣服上"
    collar_y = body_top + head_r * 0.05
    d.polygon(
        [
            (head_c[0] - head_r * 0.34, collar_y),
            (head_c[0] + head_r * 0.34, collar_y),
            (head_c[0], collar_y + head_r * 0.5),
        ],
        fill=(255, 255, 255, 235),
    )

    # 脖子（被领口压住一半，只留一点点）
    d.ellipse(
        [head_c[0] - head_r * 0.22, head_c[1] + head_r * 0.6,
         head_c[0] + head_r * 0.22, head_c[1] + head_r * 1.05],
        fill=(240, 200, 168),
    )

    # 头
    d.ellipse(
        [head_c[0] - head_r, head_c[1] - head_r, head_c[0] + head_r, head_c[1] + head_r],
        fill=SKIN,
    )

    # 头发：上半圆 + 一点点刘海
    d.pieslice(
        [head_c[0] - head_r * 1.03, head_c[1] - head_r * 1.06,
         head_c[0] + head_r * 1.03, head_c[1] + head_r * 0.98],
        start=180, end=360, fill=HAIR,
    )
    # 刘海下沿：三个小三角，比一条直线更像手绘。
    # 收得比最初浅一点：刘海压到 -0.24 的时候，眉毛和眼睛全被盖住，
    # 整张脸只剩一张嘴 —— 眉毛是这个形象最认得出的特征，必须露出来
    for dx in (-0.55, 0.0, 0.55):
        bx = head_c[0] + head_r * dx
        by = head_c[1] - head_r * 0.44
        w = head_r * 0.30
        d.polygon([(bx - w, by), (bx + w, by), (bx + dx * head_r * 0.15, by + head_r * 0.20)], fill=HAIR)

    # 眉毛：粗、略斜，是这个形象最认得出的特征
    brow_w = head_r * 0.42
    brow_h = head_r * 0.15
    for dx in (-0.40, 0.40):
        bx = head_c[0] + head_r * dx
        by = head_c[1] - head_r * 0.09
        d.rounded_rectangle(
            [bx - brow_w / 2, by - brow_h / 2, bx + brow_w / 2, by + brow_h / 2],
            radius=int(brow_h * 0.5),
            fill=HAIR,
        )

    # 眼睛
    eye_r = head_r * 0.145
    for dx in (-0.38, 0.38):
        ex = head_c[0] + head_r * dx
        ey = head_c[1] + head_r * 0.17
        d.ellipse([ex - eye_r, ey - eye_r * 1.1, ex + eye_r, ey + eye_r * 1.1], fill=HAIR)
        # 高光：没有它眼睛会像两个洞
        d.ellipse(
            [ex - eye_r * 0.28, ey - eye_r * 0.86, ex + eye_r * 0.32, ey - eye_r * 0.22],
            fill=(255, 255, 255, 235),
        )

    # 腮红
    for dx in (-0.66, 0.66):
        ex = head_c[0] + head_r * dx
        ey = head_c[1] + head_r * 0.46
        d.ellipse(
            [ex - head_r * 0.16, ey - head_r * 0.10, ex + head_r * 0.16, ey + head_r * 0.10],
            fill=BLUSH + (150,),
        )

    # 嘴：一条小弧，配合腮红是"笑着"的表情
    d.arc(
        [head_c[0] - head_r * 0.26, head_c[1] + head_r * 0.34,
         head_c[0] + head_r * 0.26, head_c[1] + head_r * 0.66],
        start=20, end=160, fill=HAIR, width=max(2, int(head_r * 0.11)),
    )

    # 缩回目标尺寸。LANCZOS 保留边缘，超采样消锯齿
    return img.resize((size, size), Image.LANCZOS)


def draw_web_logo(size: int) -> Image.Image:
    """网页侧栏用的方形圆角 logo：不做透明底，和卡片融合。"""
    icon = draw_icon(size)
    return icon


def main() -> int:
    EXT.mkdir(parents=True, exist_ok=True)
    STATIC.mkdir(parents=True, exist_ok=True)

    for size in (16, 32, 48, 128, 256):
        # 16/32 太小，画全身会糊成一团 —— 只画头
        img = draw_icon(size, compact=size <= 32)
        out = EXT / f"icon{size}.png"
        img.save(out, optimize=True)
        print(f"  {out.relative_to(ROOT)}  {size}x{size}")

    for size in (96, 192):
        out = STATIC / f"logo{size}.png"
        draw_web_logo(size).save(out, optimize=True)
        print(f"  {out.relative_to(ROOT)}  {size}x{size}")

    print("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
