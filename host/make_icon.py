"""Generate the LED Sign Studio icon.

A dark rounded square holding a grid of glowing LED dots in a diagonal rainbow.
Every size is drawn on its own pixel grid (dots snapped to whole pixels), so the
small sizes stay crisp instead of being a blurry downscale of the big one.

Run with the venv python from the repo root:

    .venv\\Scripts\\python host\\make_icon.py

Writes app\\assets\\icon.ico (16 to 256 px) and app\\assets\\icon.png (256 px).
"""

from __future__ import annotations

import colorsys
import io
import struct
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "app" / "assets"

ICO_SIZES = [16, 20, 24, 30, 32, 36, 40, 48, 64, 72, 96, 128, 256]
SS = 8  # supersampling factor; geometry is snapped to whole target pixels

# Per-size layout: (dots per side, dot size px, dot pitch px, frame inset px).
# Margins are symmetric: inset + (n - 1) * pitch + dot + margin == size.
LAYOUT = {
    16: (5, 2, 3, 0),
    20: (5, 2, 3, 0),
    24: (7, 2, 3, 0),
    30: (5, 4, 5, 0),
    32: (5, 4, 5, 0),
    36: (5, 4, 6, 0),
    40: (6, 4, 6, 0),
    48: (6, 5, 7, 1),
    64: (7, 6, 7, 2),
    72: (7, 6, 8, 2),
    96: (8, 7, 9, 3),
    128: (8, 10, 12, 4),
    256: (8, 19, 25, 8),
}

BG_TOP = (36, 40, 54)
BG_BOTTOM = (12, 14, 20)
BG_FLAT = (20, 22, 31)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix(c1, c2, t):
    return tuple(int(round(lerp(a, b, t))) for a, b in zip(c1, c2))


def led_color(col: int, row: int, n: int) -> tuple[int, int, int]:
    # Diagonal rainbow: red at top-left through violet at bottom-right.
    t = (col + row) / (2 * (n - 1))
    hue = t * 0.80
    r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
    return int(r * 255), int(g * 255), int(b * 255)


def rounded_mask(size: int, inset: int, radius: float) -> Image.Image:
    big = size * SS
    mask = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(mask)
    lo = inset * SS
    hi = big - inset * SS - 1
    d.rounded_rectangle((lo, lo, hi, hi), radius=radius * SS, fill=255)
    return mask


def background(size: int, inset: int, radius: float) -> Image.Image:
    big = size * SS
    mask = rounded_mask(size, inset, radius)
    if size <= 24:
        fill = Image.new("RGBA", (big, big), BG_FLAT + (255,))
    else:
        grad = Image.new("RGBA", (1, big))
        for y in range(big):
            grad.putpixel((0, y), mix(BG_TOP, BG_BOTTOM, y / (big - 1)) + (255,))
        fill = grad.resize((big, big), Image.NEAREST)
    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    out.paste(fill, (0, 0), mask)
    if size >= 32:
        # Thin light rim along the top edge, like a glass bezel.
        rim = Image.new("L", (big, big), 0)
        rd = ImageDraw.Draw(rim)
        lo = inset * SS
        hi = big - inset * SS - 1
        w = max(SS, size * SS // 96)
        rd.rounded_rectangle((lo, lo, hi, hi), radius=radius * SS, outline=255, width=w)
        fade = Image.new("L", (1, big))
        for y in range(big):
            fade.putpixel((0, y), int(70 * max(0.0, 1 - y / (big * 0.6))))
        rim = ImageChops.multiply(rim, fade.resize((big, big), Image.NEAREST))
        white = Image.new("RGBA", (big, big), (255, 255, 255, 255))
        out = Image.composite(white, out, rim)
        out.putalpha(mask)
    return out


def dot_boxes(size: int):
    n, dot, pitch, _inset = LAYOUT[size]
    span = (n - 1) * pitch + dot
    margin = (size - span) // 2
    assert (size - span) % 2 == 0, (size, span)
    for row in range(n):
        for col in range(n):
            x0 = margin + col * pitch
            y0 = margin + row * pitch
            yield col, row, (x0 * SS, y0 * SS, (x0 + dot) * SS - 1, (y0 + dot) * SS - 1)


def render(size: int) -> Image.Image:
    n, dot, _pitch, inset = LAYOUT[size]
    big = size * SS
    radius = max(2.0, (size - 2 * inset) * 0.22)
    img = background(size, inset, radius)
    bg_alpha = img.getchannel("A")

    round_dots = dot >= 4

    if size >= 48:
        # Glow: soft colored halos under the dots, kept inside the frame.
        glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        grow = dot * SS * 0.2
        for col, row, (x0, y0, x1, y1) in dot_boxes(size):
            c = led_color(col, row, n)
            gd.ellipse((x0 - grow, y0 - grow, x1 + grow, y1 + grow), fill=c + (255,))
        glow = glow.filter(ImageFilter.GaussianBlur(dot * SS * 0.45))
        ga = glow.getchannel("A").point(lambda v: int(v * 0.55))
        glow.putalpha(ImageChops.multiply(ga, bg_alpha))
        img = Image.alpha_composite(img, glow)

    dots = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    for col, row, (x0, y0, x1, y1) in dot_boxes(size):
        c = led_color(col, row, n)
        if not round_dots:
            dd.rectangle((x0, y0, x1, y1), fill=c + (255,))
            continue
        # Round LED with a hot, whiter core fading to the pure color at the rim.
        steps = 10 if size >= 64 else 4
        w = x1 - x0
        for i in range(steps):
            t = i / (steps - 1)
            shrink = t * w * 0.36
            core = 0.55 if size >= 64 else 0.30
            col_i = mix(c, (255, 255, 255), core * t * t)
            box = (x0 + shrink, y0 + shrink, x1 - shrink, y1 - shrink)
            if dot <= 6:
                # Too few pixels for a true circle: a soft rounded square reads
                # as a round LED without turning into a plus sign.
                r = max(1.0, (box[2] - box[0]) * 0.3)
                dd.rounded_rectangle(box, radius=r, fill=col_i + (255,))
            else:
                dd.ellipse(box, fill=col_i + (255,))
    img = Image.alpha_composite(img, dots)

    return img.resize((size, size), Image.BOX)


def bmp_entry(img: Image.Image) -> bytes:
    """32-bit BGRA DIB with an AND mask, the classic ICO frame format."""
    w, h = img.size
    px = img.convert("RGBA").tobytes()
    rows = []
    for y in range(h - 1, -1, -1):  # bottom-up
        row = bytearray()
        for x in range(w):
            r, g, b, a = px[(y * w + x) * 4:(y * w + x) * 4 + 4]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    xor = b"".join(rows)
    mask_stride = ((w + 31) // 32) * 4
    and_mask = bytearray()
    for y in range(h - 1, -1, -1):
        bits = bytearray(mask_stride)
        for x in range(w):
            if px[(y * w + x) * 4 + 3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        and_mask += bits
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(xor) + len(and_mask), 0, 0, 0, 0)
    return header + xor + bytes(and_mask)


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def write_ico(path: Path, images: dict[int, Image.Image]) -> None:
    sizes = sorted(images)
    blobs = [png_bytes(images[s]) if s >= 256 else bmp_entry(images[s]) for s in sizes]
    out = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))
    offset = 6 + 16 * len(sizes)
    for s, blob in zip(sizes, blobs):
        dim = 0 if s >= 256 else s
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for blob in blobs:
        out += blob
    path.write_bytes(bytes(out))


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    images = {s: render(s) for s in ICO_SIZES}
    write_ico(ASSETS / "icon.ico", images)
    images[256].save(ASSETS / "icon.png", optimize=True)
    print(f"wrote {ASSETS / 'icon.ico'} ({', '.join(map(str, ICO_SIZES))}) and icon.png")


if __name__ == "__main__":
    main()
