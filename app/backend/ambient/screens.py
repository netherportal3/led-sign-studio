"""The ambient screens for a 64x16 panel, as frame lists [(RGB image, ms), ...].

Every function fills exactly `duration_ms` (a multiple of 10 ms, since GIF delays are centiseconds)
and merges identical consecutive frames. Colors are full strength; rotation applies the day color
scale at the end.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from PIL import Image

from . import glyphs as G
from . import news as N
from . import weather as W

WIDTH, HEIGHT = 64, 16
Frame = tuple[Image.Image, int]

NIGHT_COLOR = (12, 6, 0)  # a very dim warm color for night text; channels <= 12


def blank() -> Image.Image:
    return Image.new("RGB", (WIDTH, HEIGHT))


def sample(duration_ms: int, tick_ms: int, render: Callable[[int], Image.Image]) -> list[Frame]:
    """Render at every tick and merge repeats. Durations stay multiples of 10 ms."""
    duration_ms = int(round(duration_ms / 10)) * 10
    tick_ms = max(10, int(round(tick_ms / 10)) * 10)
    frames: list[Frame] = []
    last = None
    t = 0
    while t < duration_ms:
        d = min(tick_ms, duration_ms - t)
        img = render(t)
        b = img.tobytes()
        if frames and b == last:
            frames[-1] = (frames[-1][0], frames[-1][1] + d)
        else:
            frames.append((img, d))
            last = b
        t += d
    return frames


def _r(v) -> int | None:
    return None if v is None else int(round(float(v)))


def _stale_mark(img: Image.Image) -> None:
    img.putpixel((0, 15), (60, 0, 0))


# ---------------------------------------------------------------- clock


def clock_text(t: datetime, h24: bool = False) -> str:
    if h24:
        return f"{t.hour:02d}:{t.minute:02d}"
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d}"


def _hilo_block(hi: int | None, lo: int | None) -> tuple[int, Callable[[Image.Image, int], None]]:
    """(width, draw(img, x_right)) for the right-hand high over low."""
    hs = "" if hi is None else str(hi)
    ls = "" if lo is None else str(lo)
    font = G.FONT_5X7
    w = max(G.text_width(hs, font), G.text_width(ls, font))
    if w > 11:
        font = G.FONT_TINY
        w = max(G.text_width(hs, font), G.text_width(ls, font))
    big = font is G.FONT_5X7

    def draw(img: Image.Image, x_right: int) -> None:
        if hs:
            G.draw_text(img, x_right - G.text_width(hs, font) + 1, 0 if big else 2, hs, G.HI_WARM, font)
        if ls:
            G.draw_text(img, x_right - G.text_width(ls, font) + 1, 9 if big else 9, ls, G.LO_COOL, font)
    return w, draw


def clock_frames(minute: datetime, wx: dict | None, duration_ms: int, *, h24: bool = False,
                 t0_ms: int = 0, animate: bool = True) -> list[Frame]:
    text = clock_text(minute, h24)
    cur = (wx or {}).get("current") or {}
    today = W.today_entry(wx, minute) if wx else None
    cond = W.condition(cur.get("code"), cur.get("is_day", True)) if wx else None
    hi = _r(today.get("hi")) if today else None
    lo = _r(today.get("lo")) if today else None

    base = blank()
    if cond:
        hw, draw_hilo = _hilo_block(hi, lo)
        draw_hilo(base, WIDTH - 1)
        left, right = 13 + 1, WIDTH - 1 - hw - 1
    else:
        left, right = 0, WIDTH - 1
    tw = G.tall_width(text)
    x = left + max(0, (right - left + 1 - tw) // 2)
    G.draw_tall(base, x, 1, text, G.WARM_WHITE)
    if wx and wx.get("stale"):
        _stale_mark(base)
    if not cond:
        return sample(duration_ms, duration_ms, lambda t: base)

    def render(t: int) -> Image.Image:
        img = base.copy()
        G.blit(img, G.icon_large(cond["icon"], cond["day"], (t0_ms + t) if animate else 0), 0, 1)
        return img
    return sample(duration_ms, G.ICON_TICK_MS.get(cond["icon"], 500) if animate else duration_ms, render)


# ---------------------------------------------------------------- now


def _degree(img: Image.Image, x: int, y: int, color) -> None:
    for dx, dy in ((1, 0), (0, 1), (2, 1), (1, 2)):
        img.putpixel((x + dx, y + dy), color)


def now_frames(wx: dict, duration_ms: int, *, units: str = "F", t0_ms: int = 0, at: datetime | None = None,
               animate: bool = True) -> list[Frame]:
    cur = wx.get("current") or {}
    temp = _r(cur.get("temp"))
    feels = _r(cur.get("feels"))
    wind = _r(cur.get("wind"))
    cond = W.condition(cur.get("code"), cur.get("is_day", True))
    base = blank()
    x = 14
    ts = "--" if temp is None else str(temp)
    G.draw_tall(base, x, 1, ts, G.temp_color(temp, units))
    x += G.tall_width(ts)
    _degree(base, x + 1, 1, G.temp_color(temp, units))
    x += 5
    col_w = WIDTH - x
    kind = "clear_night" if cond["icon"] == "clear" and not cond["day"] else cond["icon"]
    word = G.fit_words(cond["words"], col_w)
    G.draw_text(base, x + max(0, (col_w - G.text_width(word)) // 2), 2, word, G.ICON_COLORS.get(kind, G.LABEL_GRAY))
    lines = []
    if feels is not None:
        lines.append(("FEELS", "FL", feels, G.temp_color(feels, units)))
    if wind is not None:
        lines.append(("WIND", "W", wind, (170, 200, 230)))
    if wx.get("stale"):
        _stale_mark(base)

    variants = []
    for label, short, val, col in lines:
        img = base.copy()
        vs = str(val)
        for lab in (label, short):
            w = G.text_width(lab) + 3 + G.text_width(vs)
            if w <= col_w:
                break
        xx = x + max(0, (col_w - w) // 2)
        xx = G.draw_text(img, xx, 9, lab, G.LABEL_GRAY) + 3
        G.draw_text(img, xx, 9, vs, col)
        variants.append(img)
    if not variants:
        variants = [base]
    swap_ms = 7000

    def render(t: int) -> Image.Image:
        img = variants[(t // swap_ms) % len(variants)].copy()
        G.blit(img, G.icon_large(cond["icon"], cond["day"], (t0_ms + t) if animate else 0), 0, 1)
        return img
    tick = math.gcd(G.ICON_TICK_MS.get(cond["icon"], 500), swap_ms) if animate else swap_ms
    return sample(duration_ms, tick, render)


# ---------------------------------------------------------------- today


def today_frames(wx: dict, duration_ms: int, *, at: datetime | None = None, units: str = "F") -> list[Frame]:
    """The next 24 hours as bars (height and color = temperature, dimmer at night), precipitation
    chance as blue dots along the top, high over low on the left."""
    hours = W.next_hours(wx, at, 24)
    base = blank()
    if not hours:
        return sample(duration_ms, duration_ms, lambda t: base)
    temps = [float(h["temp"]) for h in hours]
    hi, lo = max(temps), min(temps)
    his, los = str(int(round(hi))), str(int(round(lo)))
    font = G.FONT_5X7 if max(G.text_width(his, G.FONT_5X7), G.text_width(los, G.FONT_5X7)) <= 11 else G.FONT_TINY
    lw = max(G.text_width(his, font), G.text_width(los, font))
    G.draw_text(base, lw - G.text_width(his, font), 0 if font is G.FONT_5X7 else 2, his, G.temp_color(hi, units), font)
    G.draw_text(base, lw - G.text_width(los, font), 9, los, G.temp_color(lo, units), font)
    gx0 = WIDTH - 2 * len(hours)
    span = max(1e-6, hi - lo)
    cols = []
    for i, h in enumerate(hours):
        t = float(h["temp"])
        height = 2 + int(round((t - lo) / span * 10))  # 2..12 rows, bars rest on row 15
        col = G.temp_color(t, units)
        night = not h.get("is_day", 1)
        body = G.scale(col, 0.28 if night else 0.42)
        cap = G.scale(col, 0.7 if night else 1.0)
        pop = h.get("pop") or 0
        cols.append((gx0 + 2 * i, height, body, cap, pop))
    if wx.get("stale"):
        _stale_mark(base)

    def draw_upto(n: int) -> Image.Image:
        img = base.copy()
        px = img.load()
        for x, height, body, cap, pop in cols[:n]:
            top = 16 - height
            for xx in (x, x + 1):
                for y in range(top + 1, 16):
                    px[xx, y] = body
                px[xx, top] = cap
            if pop >= 20:
                k = 0.35 if pop < 40 else (0.65 if pop < 70 else 1.0)
                px[x, 0] = G.scale(G.RAIN, k)
                px[x + 1, 0] = G.scale(G.RAIN, k)
        return img

    reveal_step = 70
    full = len(cols)
    return sample(duration_ms, reveal_step, lambda t: draw_upto(min(full, 1 + t // reveal_step)))


# ---------------------------------------------------------------- week

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def week_frames(wx: dict, duration_ms: int, *, at: datetime | None = None, units: str = "F",
                days_per_page: int = 2) -> list[Frame]:
    """Two days per page: large icon, day name, high and low. Pages advance evenly."""
    days = W.upcoming_days(wx, at, 8)
    if not days:
        return sample(duration_ms, duration_ms, lambda t: blank())
    today_key = (at or datetime.now()).strftime("%Y-%m-%d")
    pages = [days[i:i + days_per_page] for i in range(0, len(days), days_per_page)]
    imgs = []
    for page in pages:
        img = blank()
        for ci, d in enumerate(page):
            cx = ci * 32
            cond = W.condition(d.get("code"), True)
            G.blit(img, G.icon_large(cond["icon"], True, 0), cx, 2)
            dt = datetime.strptime(d["date"], "%Y-%m-%d")
            name = DAY_NAMES[dt.weekday()]
            is_today = d["date"] == today_key
            G.draw_text(img, cx + 15, 0, name, (255, 190, 60) if is_today else G.WARM_WHITE, G.FONT_5X7)
            hv, lv = _r(d.get("hi")), _r(d.get("lo"))
            hc, lc = G.temp_color(d.get("hi"), units), G.temp_color(d.get("lo"), units)
            # "57 41" fits beside the icon; wide pairs like "15 -12" start closer to the icon,
            # then tighten the gap; anything wider is clipped at the cell edge.
            for x0, gap in ((cx + 15, 3), (cx + 14, 2), (cx + 13, 2), (cx + 13, 1)):
                if x0 + G.temp_width(hv) + gap + G.temp_width(lv) <= cx + 32:
                    break
            cell = Image.new("RGB", (32, 16))
            x = G.draw_temp(cell, x0 - cx, 10, hv, hc) + 1 + gap
            G.draw_temp(cell, x, 10, lv, lc)
            box = cell.crop((13, 9, 32, 16))
            img.paste(box, (cx + 13, 9), box.convert("L").point(lambda v: 255 if v else 0))
            pop = d.get("pop") or 0
            if pop >= 30:  # a small blue mark under the day name when rain or snow is likely
                k = 0.45 if pop < 60 else 1.0
                for xx in range(cx + 15, cx + 15 + max(2, int(round(pop / 100 * 16)))):
                    if xx < cx + 32:
                        img.putpixel((xx, 8), G.scale(G.RAIN, k))
        if wx.get("stale"):
            _stale_mark(img)
        imgs.append(img)
    per = max(10, (duration_ms // len(imgs)) // 10 * 10)
    out: list[Frame] = []
    used = 0
    for i, img in enumerate(imgs):
        d = per if i < len(imgs) - 1 else duration_ms - used
        if d <= 0:
            break
        out.append((img, d))
        used += d
    return out


# ---------------------------------------------------------------- news
#
# A headline is a marquee: its source tag sits on the top row, and the title enters at the right
# edge and scrolls until it has left at the left edge. Titles are never cut; only a title wider
# than MAX_TITLE_PX is shortened, at a word boundary, with "...".

HEADLINE_COLOR = (255, 226, 180)
NEWS_GAP = 300           # the next tag over an empty row before a title starts to enter
TEXT_Y = 7
MAX_TITLE_PX = 900       # about 150 characters; real headlines are well under this
RESUME_HOLD_MS = 1000    # a GIF that continues a held headline holds it this long, then scrolls on

# Speed presets in px/s. The sign plays 40 to 60 ms frames smoothly; shorter ones do not go faster.
NEWS_SPEEDS = {"slow": 20, "normal": 33, "fast": 50}
DEFAULT_NEWS_SPEED = "normal"
MIN_NEWS_SPEED, MAX_NEWS_SPEED = 10, 75


@dataclass(frozen=True)
class Motion:
    """How the title moves: `step` px every `ms` milliseconds."""
    step: int
    ms: int

    @property
    def px_per_s(self) -> float:
        return self.step * 1000 / self.ms


def _motions() -> list[Motion]:
    out = [Motion(1, ms) for ms in range(40, 110, 10)]          # slow speeds may use longer frames
    out += [Motion(s, ms) for s in (2, 3) for ms in (40, 50, 60)]
    return out


def speed_px_s(speed) -> float:
    """The px/s a `news_speed` setting asks for: a preset name or a number."""
    if isinstance(speed, str) and speed.strip().lower() in NEWS_SPEEDS:
        return float(NEWS_SPEEDS[speed.strip().lower()])
    if isinstance(speed, (int, float)) and not isinstance(speed, bool) and speed == speed:
        return float(min(MAX_NEWS_SPEED, max(MIN_NEWS_SPEED, speed)))
    return float(NEWS_SPEEDS[DEFAULT_NEWS_SPEED])


def news_motion(speed=DEFAULT_NEWS_SPEED) -> Motion:
    """The smooth step and frame time nearest to the asked speed (fewest frames on a tie)."""
    v = speed_px_s(speed)
    return min(_motions(), key=lambda m: (round(abs(math.log(m.px_per_s / v)), 6), -m.ms))


@dataclass
class NewsItem:
    """One headline in a news segment. `from_hold`: this GIF starts with the title already held at
    the left edge (it continues the previous GIF). `hold_ms`: extra time held at the left edge."""
    headline: object
    title: str
    hold_ms: int = 0
    from_hold: bool = False
    gap_ms: int = NEWS_GAP     # the tag over an empty row before the title enters


def _hd(h) -> dict:
    return h.to_dict() if hasattr(h, "to_dict") else dict(h)


def fit_title(title: str, max_px: int = MAX_TITLE_PX) -> str:
    """The title unchanged when it fits, else cut at a word boundary with "..."."""
    if G.text_width(title, G.FONT_5X7) <= max_px:
        return title
    words = title.split()
    while len(words) > 1 and G.text_width(" ".join(words) + "...", G.FONT_5X7) > max_px:
        words.pop()
    out = " ".join(words).rstrip(",;:-") + "..."
    while G.text_width(out, G.FONT_5X7) > max_px and len(out) > 4:  # one giant word
        out = out[:-4] + "..."
    return out


def item_title(h) -> str:
    return fit_title(h.title if hasattr(h, "title") else h["title"])


def scroll_frames(title: str, motion: Motion) -> int:
    """Frames from the first column entering to the title gone (the last frame is empty)."""
    tw = G.text_width(title, G.FONT_5X7)
    return -(-(WIDTH + tw) // motion.step)


def hold_index(motion: Motion) -> int:
    """The frame where the title's first letter reaches the left edge (x = 0 or 1)."""
    return WIDTH // motion.step


def hold_offset_ms(motion: Motion) -> int:
    """From the start of an item (gap included) to its hold frame."""
    return NEWS_GAP + (hold_index(motion) - 1) * motion.ms


def item_ms(it: NewsItem, motion: Motion) -> int:
    n = scroll_frames(it.title, motion)
    if it.from_hold:
        return RESUME_HOLD_MS + (n - hold_index(motion) + 1) * motion.ms
    return it.gap_ms + n * motion.ms + it.hold_ms


def headline_ms(title: str, motion: Motion | None = None) -> int:
    return item_ms(NewsItem(None, fit_title(title)), motion or news_motion())


def plan_news(headlines: list, slot_ms: int, motion: Motion, *, first: NewsItem | None = None,
              overrun_ms: int = 0) -> tuple[list[NewsItem], int]:
    """Whole headlines for a slot of about `slot_ms`: the count whose total is nearest the slot,
    running over by at most `overrun_ms` (one headline always goes in, whatever its length).
    `first` continues a held headline. Returns (items, total ms)."""
    items: list[NewsItem] = [first] if first else []
    total = item_ms(first, motion) if first else 0
    best = (list(items), total) if items else None
    for h in headlines:
        it = NewsItem(h, item_title(h))
        t = total + item_ms(it, motion)
        if items and t - slot_ms > overrun_ms:
            break
        items.append(it)
        total = t
        if best is None or abs(total - slot_ms) <= abs(best[1] - slot_ms):
            best = (list(items), total)
        if total >= slot_ms:
            break
    return best if best else ([], 0)


def headline_strip(title: str) -> Image.Image:
    """The title with a panel width of empty space on both sides."""
    tw = G.text_width(title, G.FONT_5X7)
    img = Image.new("RGB", (tw + 2 * WIDTH, 8))
    G.draw_text(img, WIDTH, 0, title, HEADLINE_COLOR, G.FONT_5X7)
    return img


def _age_text(published: float | None, now: float) -> str:
    if not published:
        return ""
    h = max(0, (now - published) / 3600)
    if h < 1:
        return f"{max(1, int(h * 60))}M"
    return f"{int(h)}H"


def _news_band(h: "N.Headline | dict", now: float) -> Image.Image:
    img = blank()
    d = _hd(h)
    src = d.get("source")
    meta = N.SOURCES.get(src, {})
    tag = d.get("tag") or meta.get("tag") or str(src).upper()
    color = meta.get("color", (200, 200, 200))
    x = G.draw_text(img, 0, 0, tag, color)
    extra = len(d.get("sources") or []) - 1
    if extra > 0:
        G.draw_text(img, x + 3, 0, f"+{extra}", G.DIM_GRAY)
    age = _age_text(d.get("published"), now)
    if age:
        G.draw_text(img, WIDTH - G.text_width(age), 0, age, G.DIM_GRAY)
    return img


def item_frames(it: NewsItem, motion: Motion, now: float) -> list[Frame]:
    band = _news_band(it.headline, now)
    strip = headline_strip(it.title)
    n = scroll_frames(it.title, motion)
    k_hold = hold_index(motion)

    def at(k: int) -> Image.Image:
        img = band.copy()
        x0 = k * motion.step          # the title's left edge is at WIDTH - x0
        img.paste(strip.crop((x0, 0, x0 + WIDTH, 8)), (0, TEXT_Y))
        return img
    frames: list[Frame] = []
    if it.from_hold:
        frames.append((at(k_hold), RESUME_HOLD_MS + motion.ms))
        ks = range(k_hold + 1, n + 1)
    else:
        frames.append((band, it.gap_ms))
        ks = range(1, n + 1)
    for k in ks:
        frames.append((at(k), motion.ms + (it.hold_ms if k == k_hold and not it.from_hold else 0)))
    return frames


def news_frames(items: list[NewsItem], duration_ms: int, motion: Motion, *,
                now: float | None = None) -> list[Frame]:
    """Frames for a news segment: every item in full, then the last tag over an empty row until
    `duration_ms` (which must be at least the items' total)."""
    now = now or time.time()
    frames: list[Frame] = []
    for it in items:
        frames.extend(item_frames(it, motion, now))
    used = sum(ms for _i, ms in frames)
    if used > duration_ms:
        raise ValueError(f"news items need {used} ms, the segment has {duration_ms} ms")
    if duration_ms > used:
        pad = _news_band(items[-1].headline, now) if items else blank()
        frames.append((pad, duration_ms - used))
    return frames


# ---------------------------------------------------------------- night


def night_frame(text: str = "") -> Image.Image:
    """All black by default (the sign looks off). With text: centered, very dim (channels <= 12)."""
    img = blank()
    text = (text or "").strip()
    if not text:
        return img
    font = G.FONT_5X7
    w = G.text_width(text, font)
    if w > WIDTH:
        font = G.FONT_TINY
        w = G.text_width(text, font)
    y = 4 if font is G.FONT_5X7 else 5
    G.draw_text(img, max(0, (WIDTH - w) // 2), y, text, NIGHT_COLOR, font)
    return img
