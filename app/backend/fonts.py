"""Font list and text masks (1 byte per pixel, 0 or 255) for the text tool."""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from .pixelfont import PIXEL_FONTS, PixelFont

log = logging.getLogger("ledsign.fonts")

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

# (id, label, file) in display order. Only files that exist are offered.
TTF_CANDIDATES = [
    ("arialbd", "Arial Bold", "arialbd.ttf"),
    ("verdanab", "Verdana Bold", "verdanab.ttf"),
    ("tahomabd", "Tahoma Bold", "tahomabd.ttf"),
    ("segoeuib", "Segoe UI Bold", "segoeuib.ttf"),
    ("consolab", "Consolas Bold", "consolab.ttf"),
    ("impact", "Impact", "impact.ttf"),
    ("trebucbd", "Trebuchet Bold", "trebucbd.ttf"),
    ("comicbd", "Comic Sans Bold", "comicbd.ttf"),
    ("georgiab", "Georgia Bold", "georgiab.ttf"),
    ("courbd", "Courier New Bold", "courbd.ttf"),
]

DEFAULT_FONT = "pixel5x7"
PIXEL_MAX_SCALE = 4
TTF_MIN_SIZE, TTF_MAX_SIZE = 5, 64
MAX_TEXT_CHARS = 2000
MAX_MASK_WIDTH = 20000


@dataclass
class _Line:
    img: Image.Image      # mode "L", 0 or 255
    baseline: int         # row of the baseline inside img
    ref_top: int          # cap top relative to the baseline (negative = above)
    ref_bottom: int       # last cap row relative to the baseline (0 = the baseline row)
    ink: tuple[int, int] | None  # first/last lit row relative to the baseline


_ttf_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_default_size_cache: dict[tuple[str, int], int] = {}
_cache_lock = threading.Lock()


def _ttf_path(file: str) -> str:
    return os.path.join(FONT_DIR, file)


def _ttf(font_id: str, size: int) -> ImageFont.FreeTypeFont:
    key = (font_id, size)
    with _cache_lock:
        f = _ttf_cache.get(key)
        if f is None:
            file = next(c[2] for c in TTF_CANDIDATES if c[0] == font_id)
            f = ImageFont.truetype(_ttf_path(file), size)
            _ttf_cache[key] = f
        return f


def available_ttf() -> list[tuple[str, str, str]]:
    return [c for c in TTF_CANDIDATES if os.path.exists(_ttf_path(c[2]))]


def _ttf_default_size(font_id: str, height: int) -> int:
    """Largest size whose capitals plus descenders ("Hgy") fit in `height` rows."""
    key = (font_id, height)
    if key in _default_size_cache:
        return _default_size_cache[key]
    best = 8
    for size in range(6, min(TTF_MAX_SIZE, height * 3) + 1):
        _w, h = _ink_size_ttf(font_id, size, "Hgy")
        if h <= height:
            best = size
        elif size > best + 4:
            break
    _default_size_cache[key] = best
    return best


def _ink_size_ttf(font_id: str, size: int, text: str) -> tuple[int, int]:
    f = _ttf(font_id, size)
    d = ImageDraw.Draw(Image.new("L", (1, 1)))
    d.fontmode = "1"
    l, t, r, b = d.textbbox((0, 0), text, font=f, anchor="ls")
    return r - l, b - t


def font_list(height: int = 16) -> list[dict]:
    out: list[dict] = [
        {"id": "pixel5x7", "label": "Pixel 5x7", "kind": "pixel", "default_size": 2 if height >= 16 else 1,
         "min_size": 1, "max_size": PIXEL_MAX_SCALE},
        {"id": "tiny3x5", "label": "Tiny 3x5", "kind": "pixel", "default_size": 1,
         "min_size": 1, "max_size": PIXEL_MAX_SCALE},
    ]
    for font_id, label, _file in available_ttf():
        try:
            size = _ttf_default_size(font_id, height)
        except Exception:  # noqa: BLE001  (a broken font file should not hide the others)
            log.exception("font %s failed to load", font_id)
            continue
        out.append({"id": font_id, "label": label, "kind": "ttf", "default_size": size,
                    "min_size": TTF_MIN_SIZE, "max_size": TTF_MAX_SIZE})
    return out


# ---------------------------------------------------------------- line rendering


def _pixel_scale(font: PixelFont, size: int | float | None) -> int:
    if not size:
        return 1
    size = int(size)
    if size <= PIXEL_MAX_SCALE:
        return max(1, size)
    # Bigger numbers are read as a pixel height (e.g. a TrueType size carried over).
    return max(1, min(PIXEL_MAX_SCALE, size // font.cell_height))


def _render_pixel_line(font: PixelFont, text: str, scale: int) -> _Line:
    glyphs = [font.glyph(ch) for ch in text]
    width = sum(len(g[0]) for g in glyphs) + max(0, len(glyphs) - 1)
    base = (font.baseline + 1) * scale - 1
    ref_top = font.cap_top * scale - base
    # An empty line is 0 px wide so it does not widen the block; it still takes vertical room.
    img = Image.new("L", (width * scale, font.cell_height * scale), 0)
    if width:
        px = img.load()
        x = 0
        for g in glyphs:
            for row, line in enumerate(g):
                for col, c in enumerate(line):
                    if c == "#":
                        for dy in range(scale):
                            for dx in range(scale):
                                px[(x + col) * scale + dx, row * scale + dy] = 255
            x += len(g[0]) + 1
    ink = img.getbbox() if width else None
    ink_rows = (ink[1] - base, ink[3] - 1 - base) if ink else None
    return _Line(img, base, ref_top, 0, ink_rows)


def _render_ttf_line(font_id: str, size: int, text: str) -> _Line:
    f = _ttf(font_id, size)
    ascent, descent = f.getmetrics()
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    probe.fontmode = "1"
    _hl, ht, _hr, hb = probe.textbbox((0, 0), "H", font=f, anchor="ls")
    ref_top, ref_bottom = ht, hb - 1
    if not text.strip():
        adv = int(round(f.getlength(text))) if text else 0
        return _Line(Image.new("L", (adv, ascent + descent), 0), ascent, ref_top, ref_bottom, None)
    l, t, r, b = probe.textbbox((0, 0), text, font=f, anchor="ls")
    pad_top = max(0, -t - ascent)
    pad_bottom = max(0, b - descent)
    h = ascent + descent + pad_top + pad_bottom
    base = ascent + pad_top
    img = Image.new("L", (max(1, r - l), h), 0)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    d.text((-l, base), text, font=f, fill=255, anchor="ls")
    img = img.point(lambda v: 255 if v >= 128 else 0)
    ink = img.getbbox()
    ink_rows = (ink[1] - base, ink[3] - 1 - base) if ink else None
    return _Line(img, base, ref_top, ref_bottom, ink_rows)


def _line_pitch_ttf(font_id: str, size: int) -> int:
    f = _ttf(font_id, size)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    probe.fontmode = "1"
    _l, t, _r, b = probe.textbbox((0, 0), "Hgjpqy", font=f, anchor="ls")
    return (b - t) + max(1, size // 10)


# ---------------------------------------------------------------- masks


def resolve_font(font_id: str | None) -> tuple[str, str]:
    """Returns (kind, id). Unknown ids fall back to the default pixel font."""
    if font_id in PIXEL_FONTS:
        return "pixel", font_id  # type: ignore[return-value]
    if font_id and any(c[0] == font_id for c in available_ttf()):
        return "ttf", font_id
    if font_id:
        log.info("unknown font %r, using %s", font_id, DEFAULT_FONT)
    return "pixel", DEFAULT_FONT


def text_mask(text: str, font_id: str | None, size: int | float | None, height: int,
              info: dict | None = None) -> tuple[int, int, bytes]:
    """Render `text` (lines split on \\n) into a mask exactly `height` rows tall.

    Lines are stacked, each centered horizontally; the block is centered vertically on the ink plus
    the capital-letter box, so typing lowercase letters does not make the text jump. A block taller
    than `height` is center-cropped; `info["cropped"]` (when a dict is passed) says so.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")[:MAX_TEXT_CHARS]
    kind, fid = resolve_font(font_id)
    lines_text = text.split("\n")
    if kind == "pixel":
        font = PIXEL_FONTS[fid]
        scale = _pixel_scale(font, size)
        lines = [_render_pixel_line(font, t, scale) for t in lines_text]
        pitch = (font.cell_height + font.line_gap) * scale
    else:
        sz = max(TTF_MIN_SIZE, min(TTF_MAX_SIZE, int(size or _ttf_default_size(fid, height))))
        lines = [_render_ttf_line(fid, sz, t) for t in lines_text]
        pitch = _line_pitch_ttf(fid, sz)

    top = bottom = None
    for i, ln in enumerate(lines):
        base = i * pitch
        rows = [ln.ref_top + base, ln.ref_bottom + base]
        if ln.ink:
            rows += [ln.ink[0] + base, ln.ink[1] + base]
        top = min(rows) if top is None else min(top, *rows)
        bottom = max(rows) if bottom is None else max(bottom, *rows)
    assert top is not None and bottom is not None
    block_h = bottom - top + 1
    width = min(MAX_MASK_WIDTH, max([ln.img.width for ln in lines] + [1]))
    out = Image.new("L", (width, height), 0)
    oy = (height - block_h) // 2 - top  # may be negative: a block taller than the panel is center-cropped
    for i, ln in enumerate(lines):
        if ln.img.width == 0:
            continue
        x = (width - ln.img.width) // 2
        y = oy + i * pitch - ln.baseline
        out.paste(ln.img, (x, y))
    if info is not None:
        info["cropped"] = any(ln.ink and (oy + i * pitch + ln.ink[0] < 0 or oy + i * pitch + ln.ink[1] >= height)
                              for i, ln in enumerate(lines))
    return width, height, out.tobytes()
