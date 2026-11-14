"""生成 MovSP 的品牌图标。

沿用原项目的视觉语言（黑底 + 橙块 + 白字），
把 MovSP 换成 MovSP，并去掉「WEB」副标题——它现在是媒体库应用，不只是网页版。
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

ORANGE = (255, 153, 0, 255)
BLACK = (0, 0, 0, 255)
WHITE = (255, 255, 255, 255)

SIZE = 256
SCALE = 4  # 先画大图再缩小，边缘更平滑
W = SIZE * SCALE


def load_font(size):
    candidates = [
        r"C:\Windows\Fonts\seguisb.ttf",   # Segoe UI Semibold
        r"C:\Windows\Fonts\segoeuib.ttf",  # Segoe UI Bold
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for path in candidates:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def build(size=SIZE):
    S = size * SCALE
    img = Image.new("RGBA", (S, S), BLACK)
    draw = ImageDraw.Draw(img)

    # 「Mov」白色 + 「SP」橙色底黑字，复刻原图的分段结构
    font_big = load_font(int(S * 0.40))
    left, right = "Mov", "SP"

    box_left = draw.textbbox((0, 0), left, font=font_big)
    box_right = draw.textbbox((0, 0), right, font=font_big)
    w_left = box_left[2] - box_left[0]
    w_right = box_right[2] - box_right[0]
    pad = int(S * 0.045)           # 橙块内边距
    gap = int(S * 0.012)

    # 总宽度 = 左字 + 间隙 + (右字 + 两侧内边距)，必须留出左右边距
    margin = int(S * 0.085)
    available = S - margin * 2
    total = w_left + gap + w_right + pad * 2
    if total > available:
        # 超宽时按比例缩小字号，保证左右都不出界
        font_big = load_font(int(S * 0.40 * available / total))
        box_left = draw.textbbox((0, 0), left, font=font_big)
        box_right = draw.textbbox((0, 0), right, font=font_big)
        w_left = box_left[2] - box_left[0]
        w_right = box_right[2] - box_right[0]
        pad = int(S * 0.04)
        gap = int(S * 0.01)
        total = w_left + gap + w_right + pad * 2

    start_x = (S - total) / 2
    cap_h = box_left[3] - box_left[1]
    baseline_y = (S - cap_h) / 2 - box_left[1] - int(S * 0.04)

    # 白色 Mov
    draw.text((start_x - box_left[0], baseline_y), left, font=font_big, fill=WHITE)

    # 橙色圆角块 + 黑色 SP
    block_x0 = start_x + w_left + gap
    block_y0 = baseline_y + box_left[1] - pad
    block_x1 = block_x0 + w_right + pad * 2
    block_y1 = baseline_y + box_left[3] + pad
    radius = int(S * 0.035)
    draw.rounded_rectangle([block_x0, block_y0, block_x1, block_y1],
                           radius=radius, fill=ORANGE)
    draw.text((block_x0 + pad - box_right[0], baseline_y), right,
              font=font_big, fill=BLACK)

    # 底部橙色细线（原有图标的元素），保留作为「媒体库」下划线
    line_y = int(S * 0.735)
    line_x0 = int(S * 0.19)
    line_x1 = int(S * 0.81)
    draw.rectangle([line_x0, line_y, line_x1, line_y + int(S * 0.012)],
                   fill=ORANGE)

    # 副标题：Media Library
    font_small = load_font(int(S * 0.105))
    sub = "MEDIA LIBRARY"
    sb = draw.textbbox((0, 0), sub, font=font_small)
    sw = sb[2] - sb[0]
    draw.text(((S - sw) / 2 - sb[0], line_y + int(S * 0.045)), sub,
              font=font_small, fill=ORANGE)

    return img.resize((size, size), Image.LANCZOS)


def main():
    out_dir = Path("movsp/web/assets")
    logo = build(256)
    logo.save(out_dir / "movsp-logo.png")
    # 多尺寸 ICO
    logo.save(out_dir / "movsp-logo.ico",
              sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("已生成 movsp-logo.png / movsp-logo.ico")

    # 文档站同步
    docs = Path("docs/assets")
    docs.mkdir(parents=True, exist_ok=True)
    logo.save(docs / "movsp-logo.png")
    logo.save(docs / "movsp-logo.ico",
              sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("已同步到 docs/assets")


if __name__ == "__main__":
    main()
