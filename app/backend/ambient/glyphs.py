"""Bitmap glyphs for ambient screens: tall clock digits, text helpers, temperature colors, and
weather icons (large 13x13 animated, mini 7x7 static).

Colors here are full strength; the rotation scales them (day color scale) at the very end.
Icon animations are slow and gentle: positions step every 300 to 700 ms, glows fade with a sine,
nothing ever blinks off.
"""
from __future__ import annotations

import math

from PIL import Image

from ..pixelfont import PIXEL_5X7, TINY_3X5, PixelFont

RGB = tuple[int, int, int]

# ---------------------------------------------------------------- text


def text_width(text: str, font: PixelFont = TINY_3X5, spacing: int = 1) -> int:
    if not text:
        return 0
    return sum(len(font.glyph(ch)[0]) for ch in text) + spacing * (len(text) - 1)


def draw_text(img: Image.Image, x: int, y: int, text: str, color: RGB, font: PixelFont = TINY_3X5,
              spacing: int = 1) -> int:
    """Draw with the glyph cell's top row at y. Returns the x after the last glyph."""
    px = img.load()
    w, h = img.size
    for ch in text:
        g = font.glyph(ch)
        gw = len(g[0])
        for gy, row in enumerate(g):
            yy = y + gy
            if 0 <= yy < h:
                for gx, c in enumerate(row):
                    if c == "#":
                        xx = x + gx
                        if 0 <= xx < w:
                            px[xx, yy] = color
        x += gw + spacing
    return x - spacing


def temp_width(v: int | None) -> int:
    """Width of a tiny-font temperature drawn by draw_temp (a 2 px minus)."""
    if v is None:
        return text_width("--")
    w = text_width(str(abs(int(v))))
    return w + 3 if v < 0 else w


def draw_temp(img: Image.Image, x: int, y: int, v: int | None, color: RGB) -> int:
    if v is None:
        return draw_text(img, x, y, "--", color)
    if v < 0:
        img.putpixel((x, y + 2), color) if 0 <= x < img.width else None
        img.putpixel((x + 1, y + 2), color) if 0 <= x + 1 < img.width else None
        x += 3
    return draw_text(img, x, y, str(abs(int(v))), color)


def fit_words(options: list[str], max_w: int, font: PixelFont = TINY_3X5) -> str:
    for o in options:
        if text_width(o, font) <= max_w:
            return o
    # last resort: cut the shortest option
    o = min(options, key=len)
    while o and text_width(o, font) > max_w:
        o = o[:-1]
    return o


# ---------------------------------------------------------------- tall clock digits (7x14, 2 px strokes)

_TALL = {
    "0": [".#####.", "#######", "##...##", "##...##", "##...##", "##...##", "##...##",
          "##...##", "##...##", "##...##", "##...##", "##...##", "#######", ".#####."],
    "1": ["..##", ".###", "####", "..##", "..##", "..##", "..##",
          "..##", "..##", "..##", "..##", "..##", "..##", "..##"],
    "2": [".#####.", "#######", "##...##", ".....##", ".....##", ".....##", ".######",
          "######.", "##.....", "##.....", "##.....", "##.....", "#######", "#######"],
    "3": [".#####.", "#######", "##...##", ".....##", ".....##", ".....##", "..#####",
          "..#####", ".....##", ".....##", ".....##", "##...##", "#######", ".#####."],
    "4": ["##...##", "##...##", "##...##", "##...##", "##...##", "##...##", "#######",
          "#######", ".....##", ".....##", ".....##", ".....##", ".....##", ".....##"],
    "5": ["#######", "#######", "##.....", "##.....", "##.....", "##.....", "######.",
          "#######", ".....##", ".....##", ".....##", "##...##", "#######", ".#####."],
    "6": [".#####.", "#######", "##...##", "##.....", "##.....", "##.....", "######.",
          "#######", "##...##", "##...##", "##...##", "##...##", "#######", ".#####."],
    "7": ["#######", "#######", ".....##", ".....##", "....##.", "....##.", "...##..",
          "...##..", "...##..", "..##...", "..##...", "..##...", "..##...", "..##..."],
    "8": [".#####.", "#######", "##...##", "##...##", "##...##", "##...##", ".#####.",
          "#######", "##...##", "##...##", "##...##", "##...##", "#######", ".#####."],
    "9": [".#####.", "#######", "##...##", "##...##", "##...##", "##...##", "#######",
          ".######", ".....##", ".....##", ".....##", "##...##", "#######", ".#####."],
    ":": ["..", "..", "..", "##", "##", "..", "..", "..", "..", "##", "##", "..", "..", ".."],
    "-": ["....", "....", "....", "....", "....", "....", "####", "####", "....", "....", "....",
          "....", "....", "...."],
    " ": ["."] * 14,
}
TALL_H = 14


def tall_width(text: str, spacing: int = 1) -> int:
    if not text:
        return 0
    return sum(len(_TALL.get(ch, _TALL[" "])[0]) for ch in text) + spacing * (len(text) - 1)


def draw_tall(img: Image.Image, x: int, y: int, text: str, color: RGB, spacing: int = 1) -> int:
    px = img.load()
    w, h = img.size
    for ch in text:
        g = _TALL.get(ch, _TALL[" "])
        for gy, row in enumerate(g):
            for gx, c in enumerate(row):
                if c == "#" and 0 <= x + gx < w and 0 <= y + gy < h:
                    px[x + gx, y + gy] = color
        x += len(g[0]) + spacing
    return x - spacing


# ---------------------------------------------------------------- colors

# Temperature (F) to color: cold violet-blue, icy blue, teal, green, yellow, orange, red.
_TEMP_STOPS_F = [(-10, (150, 90, 255)), (10, (70, 100, 255)), (32, (40, 170, 255)), (45, (0, 215, 200)),
                 (55, (80, 230, 90)), (65, (220, 225, 0)), (75, (255, 160, 0)), (85, (255, 90, 0)),
                 (95, (255, 35, 35))]


def temp_color(t: float | None, units: str = "F") -> RGB:
    if t is None:
        return (160, 160, 160)
    f = t * 9 / 5 + 32 if units == "C" else float(t)
    f = round(f / 5) * 5  # 5-degree bands keep the palette small
    stops = _TEMP_STOPS_F
    if f <= stops[0][0]:
        return stops[0][1]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if f <= t1:
            k = (f - t0) / (t1 - t0)
            return tuple(int(round(a + (b - a) * k)) for a, b in zip(c0, c1))  # type: ignore[return-value]
    return stops[-1][1]


def scale(c: RGB, k: float) -> RGB:
    return (int(c[0] * k + 0.5), int(c[1] * k + 0.5), int(c[2] * k + 0.5))


def breathe(t_ms: int, period_ms: int, lo: float, hi: float, steps: int = 6, phase: float = 0.0) -> float:
    """A smooth 0..1 sine glow quantized to `steps` levels (fewer frames, fewer colors)."""
    v = 0.5 - 0.5 * math.cos(2 * math.pi * ((t_ms / period_ms) + phase))
    v = round(v * (steps - 1)) / (steps - 1)
    return lo + (hi - lo) * v


WARM_WHITE: RGB = (255, 226, 180)
HI_WARM: RGB = (255, 130, 40)
LO_COOL: RGB = (70, 160, 255)
DIM_GRAY: RGB = (110, 110, 120)
LABEL_GRAY: RGB = (150, 150, 160)

SUN: RGB = (255, 200, 0)
SUN_EDGE: RGB = (255, 140, 0)
RAY: RGB = (255, 150, 0)
MOON: RGB = (255, 225, 150)
STAR: RGB = (200, 210, 255)
CLOUD_HI: RGB = (235, 238, 245)
CLOUD: RGB = (185, 190, 205)
CLOUD_SH: RGB = (115, 120, 140)
STORM_HI: RGB = (175, 140, 210)
STORM: RGB = (125, 95, 170)
STORM_SH: RGB = (80, 55, 120)
RAIN: RGB = (40, 120, 255)
DRIZZLE: RGB = (100, 175, 255)
SNOW: RGB = (230, 240, 255)
BOLT: RGB = (255, 220, 40)
FOG1: RGB = (165, 170, 180)
FOG2: RGB = (115, 120, 135)

ICON_COLORS = {  # the condition word color per icon kind
    "clear": (255, 190, 0), "clear_night": (255, 225, 150), "partly": (235, 225, 170), "cloudy": (200, 205, 215),
    "fog": (170, 175, 185), "drizzle": (110, 180, 255), "rain": (60, 140, 255), "snow": (230, 240, 255),
    "storm": (190, 140, 255),
}

# ---------------------------------------------------------------- sprites

Pixels = list[tuple[int, int, RGB]]


def _sprite(rows: list[str], colors: dict[str, RGB], ox: int = 0, oy: int = 0) -> Pixels:
    out = []
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch in colors:
                out.append((ox + x, oy + y, colors[ch]))
    return out


_CLOUD_L = [  # 13 x 7
    ".....####....",
    "..HH.HHHHH...",
    ".HHHWWWWWWHH.",
    "WWWWWWWWWWWWW",
    "WWWWWWWWWWWWW",
    "WWWWWWWWWWWWW",
    ".SSSSSSSSSSS.",
]
_CLOUD_M = [  # 10 x 6, for partly cloudy
    "....###...",
    ".HH.HHHH..",
    "HHWWWWWWH.",
    "WWWWWWWWWW",
    "WWWWWWWWWW",
    ".SSSSSSSS.",
]
_CLOUD_BACK = [  # 9 x 5, the back cloud of "cloudy"
    "...###...",
    ".#######.",
    "#########",
    "#########",
    ".#######.",
]
_BOLT = [  # 5 x 7
    "...##",
    "..##.",
    ".##..",
    "#####",
    "..##.",
    ".##..",
    ".#...",
]
_CLOUD_COLORS = {"H": CLOUD_HI, "W": CLOUD, "S": CLOUD_SH, "#": CLOUD}
_STORM_COLORS = {"H": STORM_HI, "W": STORM, "S": STORM_SH, "#": STORM}


def _sun(cx: float, cy: float, r: float, ray_k: float, rays: list[tuple[int, int]]) -> Pixels:
    out: Pixels = []
    ray = scale(RAY, ray_k)
    for x, y in rays:
        out.append((x, y, ray))
    ri = int(math.ceil(r)) + 1
    for y in range(int(cy) - ri, int(cy) + ri + 1):
        for x in range(int(cx) - ri, int(cx) + ri + 1):
            d2 = (x - cx) ** 2 + (y - cy) ** 2
            if d2 <= r * r:
                out.append((x, y, SUN_EDGE if d2 > (r - 1.1) ** 2 else SUN))
    return out


_SUN_RAYS_L = [(6, 0), (6, 1), (6, 11), (6, 12), (0, 6), (1, 6), (11, 6), (12, 6),
               (1, 1), (2, 2), (11, 1), (10, 2), (1, 11), (2, 10), (11, 11), (10, 10)]


def _moon(cx: float, cy: float, r: float, cut_dx: float, cut_dy: float, cut_r: float) -> Pixels:
    out: Pixels = []
    ri = int(r) + 2
    for y in range(int(cy) - ri, int(cy) + ri + 1):
        for x in range(int(cx) - ri, int(cx) + ri + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r and (x - cx - cut_dx) ** 2 + (y - cy - cut_dy) ** 2 > cut_r ** 2:
                out.append((x, y, MOON))
    return out


def _drops(t_ms: int, xs: list[int], top: int, bottom: int, step_ms: int, color: RGB, length: int = 1,
           wiggle: bool = False) -> Pixels:
    out: Pixels = []
    span = bottom - top + 1 + length
    for i, x in enumerate(xs):
        pos = (t_ms // step_ms + i * 3) % span
        for k in range(length):
            y = top + pos - k
            if top <= y <= bottom:
                xx = x + ((pos // 2) % 2 if wiggle and i % 2 == 0 else 0) - ((pos // 2) % 2 if wiggle and i % 2 else 0)
                out.append((xx, y, scale(color, 1.0 if k == 0 else 0.55)))
    return out


def icon_large(kind: str, day: bool, t_ms: int) -> Pixels:
    """13 x 13 animated icon pixels at time t_ms (relative to the icon's own clock)."""
    if kind == "clear":
        if day:
            return _sun(6, 6, 3.6, breathe(t_ms, 4000, 0.35, 1.0), _SUN_RAYS_L)
        px = _moon(5.5, 6.5, 5.0, 2.6, -1.8, 4.3)
        for i, (x, y) in enumerate([(10, 2), (12, 7), (8, 11)]):
            px.append((x, y, scale(STAR, breathe(t_ms, 3600, 0.2, 1.0, phase=i / 3))))
        return px
    if kind == "partly":
        drift = 1 if (t_ms // 3000) % 2 else 0
        if day:
            rays = [(8, 0), (12, 4), (11, 1), (5, 1)]
            px = _sun(8.5, 4.5, 2.7, breathe(t_ms, 4000, 0.35, 1.0), rays)
        else:
            px = _sprite(["..###.", ".##...", "##....", "##....", "##....", ".##...", "..###."], {"#": MOON}, 6, 0)
            px.append((2, 1, scale(STAR, breathe(t_ms, 3600, 0.2, 1.0))))
            px.append((12, 2, scale(STAR, breathe(t_ms, 3600, 0.2, 1.0, phase=0.5))))
        return px + _sprite(_CLOUD_M, _CLOUD_COLORS, 0 + drift, 6)
    if kind == "cloudy":
        back = 1 - ((t_ms // 3500) % 2)
        front = (t_ms // 3500) % 2
        px = _sprite(_CLOUD_BACK, {"#": CLOUD_SH}, 4 - back, 1)
        return px + _sprite(_CLOUD_L, _CLOUD_COLORS, 0 + front - 1 if front else 0, 5)
    if kind == "fog":
        px: Pixels = []
        lines = [(2, 11, FOG1, 0.0), (5, 9, FOG2, 0.5), (8, 11, FOG1, 0.25), (11, 8, FOG2, 0.75)]
        for y, length, col, ph in lines:
            off = int(round(1 + math.sin(2 * math.pi * (t_ms / 6000 + ph))))
            for x in range(length):
                px.append((off + x, y, col))
        return px
    if kind in ("drizzle", "rain", "snow"):
        cloud = _sprite(_CLOUD_L, _CLOUD_COLORS, 0, 0)
        if kind == "drizzle":
            return cloud + _drops(t_ms, [2, 6, 10], 8, 12, 450, DRIZZLE)
        if kind == "rain":
            return cloud + _drops(t_ms, [1, 4, 7, 10], 8, 12, 250, RAIN, length=2)
        return cloud + _drops(t_ms, [2, 6, 10], 8, 12, 650, SNOW, wiggle=True)
    if kind == "storm":
        cloud = _sprite(_CLOUD_L, _STORM_COLORS, 0, 0)
        k = breathe(t_ms, 3000, 0.3, 1.0, steps=5)
        bolt = _sprite(_BOLT, {"#": scale(BOLT, k)}, 4, 6)
        return cloud + bolt + _drops(t_ms, [1, 11], 8, 12, 300, RAIN, length=2)
    return _sprite(_CLOUD_L, _CLOUD_COLORS, 0, 3)


# Icon kinds whose large icon changes over time, and how often (ms) it can change.
ICON_TICK_MS = {"clear": 500, "partly": 500, "cloudy": 500, "fog": 500, "drizzle": 450, "rain": 250,
                "snow": 650, "storm": 300}


_MINI = {
    "clear": (["...r...", ".r...r.", "..YYY..", "r.YYY.r", "..YYY..", ".r...r.", "...r..."],
              {"r": RAY, "Y": SUN}),
    "partly": (["....r.r", ".....Y.", "..HH.YY", ".HWWW..", "WWWWWW.", ".SSSSS.", "......."],
               {"r": RAY, "Y": SUN, "H": CLOUD_HI, "W": CLOUD, "S": CLOUD_SH}),
    "cloudy": ([".......", "...HH..", ".HHWWH.", "WWWWWWW", "WWWWWWW", ".SSSSS.", "......."],
               {"H": CLOUD_HI, "W": CLOUD, "S": CLOUD_SH}),
    "fog": ([".......", "AAAAAA.", ".......", ".BBBBBB", ".......", "AAAAA..", "......."],
            {"A": FOG1, "B": FOG2}),
    "drizzle": (["..HH...", ".HWWWH.", "WWWWWWW", ".SSSSS.", ".......", ".d...d.", "...d..."],
                {"H": CLOUD_HI, "W": CLOUD, "S": CLOUD_SH, "d": DRIZZLE}),
    "rain": (["..HH...", ".HWWWH.", "WWWWWWW", ".SSSSS.", ".......", ".b.b.b.", "b.b.b.."],
             {"H": CLOUD_HI, "W": CLOUD, "S": CLOUD_SH, "b": RAIN}),
    "snow": (["..HH...", ".HWWWH.", "WWWWWWW", ".SSSSS.", ".......", ".s...s.", "...s..."],
             {"H": CLOUD_HI, "W": CLOUD, "S": CLOUD_SH, "s": SNOW}),
    "storm": (["..HH...", ".HWWWH.", "WWWWWWW", ".SSYSS.", "...Y...", "..Y....", ".Y....."],
              {"H": STORM_HI, "W": STORM, "S": STORM_SH, "Y": BOLT}),
}


def icon_mini(kind: str) -> Pixels:
    rows, colors = _MINI.get(kind, _MINI["cloudy"])
    return _sprite(rows, colors)


def blit(img: Image.Image, pixels: Pixels, ox: int, oy: int) -> None:
    px = img.load()
    w, h = img.size
    for x, y, c in pixels:
        xx, yy = ox + x, oy + y
        if 0 <= xx < w and 0 <= yy < h:
            px[xx, yy] = c


FONT_5X7 = PIXEL_5X7
FONT_TINY = TINY_3X5
