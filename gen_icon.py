# -*- coding: utf-8 -*-
"""生成「南苑抢课助手」全套图标资源。

产出（放到 assets/）：
  - icon.svg              矢量源文件（程序内/网页可引用）
  - icon.png              512x512 主图标（程序内 logo 用）
  - icon-64.png           64x64（托盘用）
  - icon-16.png / icon-32.png / icon-48.png  小尺寸（favicon）
  - favicon.ico           16/32/48 多尺寸（浏览器标签页）
  - app.ico               16/32/48/64/128/256 多尺寸（PyInstaller exe 图标）

设计：蓝底圆角方块 + 白色打开的书本 + 右上角红色闪电（「抢」的动态感），
     纯几何图形，零字体依赖，任意尺寸缩放下都清晰。
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "assets"
OUT.mkdir(exist_ok=True)

# 主色（沿用现有 UI 的 #2f6feb 蓝）
BLUE = (47, 111, 235, 255)
BLUE_DARK = (29, 79, 216, 255)
WHITE = (255, 255, 255, 255)
RED = (255, 77, 79, 255)


def draw_icon(size: int) -> Image.Image:
    """在 size×size 画布上画图标，坐标按 0..64 归一化再缩放。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0

    def R(*vals):
        return [v * s for v in vals]

    # 1) 蓝底圆角方块（整个画布）
    d.rounded_rectangle(R(2, 2, 62, 62), radius=14 * s, fill=BLUE)
    # 左上高光（用浅蓝而非半透明白，避免在蓝底上叠出黑边）
    d.rounded_rectangle(R(9, 7, 28, 11), radius=2 * s, fill=(120, 160, 245, 255))

    # 2) 白色打开的书本（两页）
    # 左页
    d.polygon(
        [R(16, 16), R(29, 12), R(29, 50), R(16, 52)],
        fill=WHITE,
    )
    # 右页
    d.polygon(
        [R(48, 16), R(35, 12), R(35, 50), R(48, 52)],
        fill=(235, 240, 255, 255),
    )
    # 书脊中线
    d.line(R(32, 12, 32, 50), fill=BLUE, width=int(2 * s))
    # 书页横线（左页两行、右页两行）
    for y in (26, 34):
        d.line(R(20, y, 25, y - 1), fill=(200, 214, 240, 255), width=int(1.5 * s))
        d.line(R(39, y - 1, 44, y), fill=(200, 214, 240, 255), width=int(1.5 * s))

    # 3) 右上角红色闪电（「抢」的动感）—— 抬高避开书本
    d.polygon(
        [R(50, 4), R(43, 20), R(49, 20), R(42, 36), R(54, 16), R(48, 16)],
        fill=RED,
    )

    return img


# 生成各尺寸 PNG
sizes = {
    "icon.png": 512,
    "icon-64.png": 64,
    "icon-48.png": 48,
    "icon-32.png": 32,
    "icon-16.png": 16,
}
for name, sz in sizes.items():
    draw_icon(sz).save(OUT / name)
    print(f"  生成 {name} ({sz}x{sz})")

# favicon.ico（16/32/48）
fav = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
fav.save(
    OUT / "favicon.ico",
    append_images=[draw_icon(16), draw_icon(32)],
    sizes=[(48, 48), (16, 16), (32, 32)],
)
print("  生成 favicon.ico (16/32/48)")

# app.ico（PyInstaller exe 用，多尺寸）
app = draw_icon(256)
app.save(
    OUT / "app.ico",
    append_images=[
        draw_icon(16), draw_icon(24), draw_icon(32),
        draw_icon(48), draw_icon(64), draw_icon(128),
    ],
    sizes=[(256, 256), (16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128)],
)
print("  生成 app.ico (16/24/32/48/64/128/256)")

# 导出 SVG 矢量源（程序内/网页引用）
def _svg_path():
    # 手写一个与 draw_icon 一致的 SVG（矢量，任意缩放不糊）
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect x="2" y="2" width="60" height="60" rx="14" fill="#2f6feb"/>
  <rect x="9" y="7" width="19" height="4" rx="2" fill="#78a0f5"/>
  <path d="M16 16 L29 12 L29 50 L16 52 Z" fill="#ffffff"/>
  <path d="M48 16 L35 12 L35 50 L48 52 Z" fill="#ebf0ff"/>
  <rect x="31" y="12" width="2" height="38" fill="#2f6feb"/>
  <path d="M20 26 L25 25 M20 34 L25 33 M39 25 L44 26 M39 33 L44 34" stroke="#c8d6f0" stroke-width="1.5"/>
  <path d="M50 4 L43 20 L49 20 L42 36 L54 16 L48 16 Z" fill="#ff4d4f"/>
</svg>"""

(OUT / "icon.svg").write_text(_svg_path(), encoding="utf-8")
print("  生成 icon.svg")

print("\n完成：", OUT)
