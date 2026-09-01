"""生成 PhoneType 正式图标 assets/app.ico（多帧：16/32/48/64/128/256）。

设计：圆角深蓝底 + 白色输入光标「|」与键盘按键方块，体现「手机当键盘输入」的语义。
必须是真正的多帧 ICO，否则 PyInstaller --icon 与 Windows 任务栏会回退到默认（Python 羽毛）图标。

注：PIL 12.x 的 ICO writer 对 append_images 有 bug，只能写出单帧，因此这里手动构造
PNG-in-ICO（每个尺寸作为 PNG 嵌入 ICO 目录）。现代 Windows 与 PyInstaller 均支持。
"""
import io
import os
import struct
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "app.ico")
SIZES = [16, 32, 48, 64, 128, 256]
BG = (37, 99, 235, 255)          # 主蓝
BG2 = (29, 78, 216, 255)         # 描边深蓝
FG = (255, 255, 255, 255)        # 白


def make(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(1, size // 16)
    r = max(2, size // 5)
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=r, fill=BG, outline=BG2, width=max(1, size // 64))

    # 键盘按键方块（底部两格）
    kw = size * 0.18
    kh = size * 0.14
    ky = size * 0.62
    k1x = size * 0.30 - kw / 2
    k2x = size * 0.70 - kw / 2
    d.rounded_rectangle([k1x, ky, k1x + kw, ky + kh], radius=max(1, size // 32), fill=(255, 255, 255, 60))
    d.rounded_rectangle([k2x, ky, k2x + kw, ky + kh], radius=max(1, size // 32), fill=(255, 255, 255, 60))

    # 输入光标「|」（顶部，亮白）
    cx = size * 0.5
    cw = max(1, size // 16)
    ch = size * 0.34
    cy0 = size * 0.20
    d.rounded_rectangle([cx - cw / 2, cy0, cx + cw / 2, cy0 + ch], radius=cw // 2, fill=FG)

    return img


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # 将每个尺寸渲染为 PNG 字节，再手动拼装成多帧 ICO
    pngs = []
    for s in SIZES:
        buf = io.BytesIO()
        make(s).save(buf, format="PNG")
        pngs.append(buf.getvalue())

    # ICONDIR: reserved(0) + type(1) + count
    header = struct.pack("<HHH", 0, 1, len(pngs))
    # ICONDIRENTRY 紧随其后的偏移
    entries = b""
    image_data = b""
    base_offset = 6 + 16 * len(pngs)
    for s, png in zip(SIZES, pngs):
        # 尺寸为 256 时目录里写 0（ICO 约定）
        w = 0 if s >= 256 else s
        h = 0 if s >= 256 else s
        entry = struct.pack(
            "<BBBBHHII",
            w,          # bWidth
            h,          # bHeight
            0,          # bColorCount (0 = >256)
            0,          # bReserved
            1,          # wPlanes
            32,         # wBitCount
            len(png),   # dwBytesInRes
            base_offset + len(image_data),  # dwImageOffset
        )
        entries += entry
        image_data += png

    with open(OUT, "wb") as f:
        f.write(header + entries + image_data)
    print("wrote", OUT, "frames:", len(pngs))


if __name__ == "__main__":
    main()
