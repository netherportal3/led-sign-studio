"""Preview CLI: fetch live weather and news, build the rotation GIF, write LED-style PNG sheets.

    .venv\\Scripts\\python.exe app\\backend\\ambient\\preview.py [--out DIR] [--minutes N] [--offline]

Writes to app\\data\\ambient\\preview\\ by default:
    rotation.gif          the exact GIF that would be uploaded (1x, color scale applied)
    rotation_x8.gif       the same, enlarged for viewing
    sheet_screens.png     every screen, a few frames each, as LED dots
    sheet_icons.png       every weather icon, day and night, over a few animation steps
    sheet_edge.png        edge cases: 10:58 vs 1:05, -5 and 102 degrees, a very long headline
    rotation.json         the segment plan, size and upload estimate
Nothing is sent to the sign.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(os.path.dirname(HERE))
ROOT_DIR = os.path.dirname(APP_DIR)
for _p in (ROOT_DIR, APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

LED_SCALE = 8
UNLIT = (26, 26, 30)


def _dot_mask(w: int, h: int, s: int = LED_SCALE) -> Image.Image:
    tile = Image.new("L", (s, s), 0)
    ImageDraw.Draw(tile).ellipse((1, 1, s - 2, s - 2), fill=255)
    mask = Image.new("L", (w * s, h * s))
    for y in range(h):
        for x in range(w):
            mask.paste(tile, (x * s, y * s))
    return mask


_MASKS: dict = {}


def led(img: Image.Image, s: int = LED_SCALE) -> Image.Image:
    """One frame as round LED dots with dim unlit dots, on a dark background."""
    key = (img.size, s)
    if key not in _MASKS:
        _MASKS[key] = _dot_mask(img.width, img.height, s).convert("RGB")
    up = img.convert("RGB").resize((img.width * s, img.height * s), Image.NEAREST)
    up = ImageChops.lighter(up, Image.new("RGB", up.size, UNLIT))
    return ImageChops.multiply(up, _MASKS[key])


def sheet(rows: list[tuple[str, list[Image.Image]]], path: str, s: int = LED_SCALE, per_row: int = 3) -> None:
    """rows = [(label, [frames...])]; frames are laid out per_row across, labels on the left."""
    label_w = 150
    cell_w, cell_h = 64 * s + 10, 16 * s + 10
    lines = []
    for label, frames in rows:
        for i in range(0, max(1, len(frames)), per_row):
            lines.append((label if i == 0 else "", frames[i:i + per_row]))
    out = Image.new("RGB", (label_w + per_row * cell_w + 10, 10 + len(lines) * cell_h), (12, 12, 14))
    d = ImageDraw.Draw(out)
    for li, (label, frames) in enumerate(lines):
        y = 10 + li * cell_h
        d.text((8, y + 8), label, fill=(200, 200, 200))
        for fi, f in enumerate(frames):
            out.paste(led(f, s), (label_w + fi * cell_w, y))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out.save(path)


def pick(frames: list, n: int = 3) -> list[Image.Image]:
    """n frames spread over a frame list [(img, ms)] by time."""
    if not frames:
        return []
    total = sum(ms for _i, ms in frames)
    out = []
    for k in range(n):
        t = total * (k + 0.5) / n
        acc = 0
        for img, ms in frames:
            acc += ms
            if acc >= t:
                out.append(img)
                break
    return out


def upscale_gif(gif: bytes, s: int = 8) -> bytes:
    import io
    src = Image.open(io.BytesIO(gif))
    frames, durs = [], []
    try:
        while True:
            frames.append(led(src.convert("RGB"), s))
            durs.append(src.info.get("duration", 100))
            src.seek(src.tell() + 1)
    except EOFError:
        pass
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=durs, loop=0)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    from backend.ambient import news as N
    from backend.ambient import rotation as R
    from backend.ambient import screens as S
    from backend.ambient import settings as ST
    from backend.ambient import weather as W

    ap = argparse.ArgumentParser(description="Build the ambient rotation and preview sheets (nothing is sent).")
    ap.add_argument("--out", default=os.path.join(APP_DIR, "data", "ambient", "preview"))
    ap.add_argument("--data", default=os.path.join(APP_DIR, "data", "ambient"))
    ap.add_argument("--minutes", type=int, default=None, help="content before the handover (default refresh)")
    ap.add_argument("--offline", action="store_true", help="use the caches only")
    ap.add_argument("--gif-x8", action="store_true", help="also write an enlarged GIF (slow)")
    a = ap.parse_args(argv)

    cfg = ST.AmbientSettings(os.path.join(a.data, "settings.json")).get()
    now = time.time()
    wc = W.WeatherCache(a.data)
    if a.offline:
        wc.last_attempt = now
    wx = wc.get(cfg["location"], cfg["units"], now)
    if a.offline:
        cache = N.NewsCache(a.data)
        heads = N.select(cache.items(N.enabled_sources(cfg["news_sources"])), now)
        nres = {"headlines": heads, "errors": {}, "status": cache.status()}
    else:
        nres = N.get_headlines(a.data, cfg["news_sources"], now=now)
    heads = nres["headlines"]
    if not cfg["location"]:
        print("no location set: pick one in the Ambient tab to get the weather")
    print(f"weather: {'ok' if wx else 'none'}{' (stale)' if wx and wx.get('stale') else ''}"
          f"{' error ' + str(wx.get('error')) if wx and wx.get('error') else ''}")
    for sid, st in sorted(nres["status"].items()):
        print(f"feed {sid:9s} ok={st['ok']} items={st['items']} {st['error'] or ''}")
    print("headlines:")
    for h in heads:
        print(f"  [{h.tag}{'+' + str(h.outlets - 1) if h.outlets > 1 else ''}] {h.title}")

    # screens sheet
    local = datetime.now()
    minute = local.replace(second=0, microsecond=0)
    rows = []
    rows.append(("clock", pick(S.clock_frames(minute, wx, 25000), 3)))
    if wx:
        rows.append(("now", pick(S.now_frames(wx, 35000, units=cfg["units"]), 3)))
        tf = S.today_frames(wx, 35000, at=local, units=cfg["units"])
        rows.append(("today", [tf[0][0], tf[min(len(tf) - 1, 10)][0], tf[-1][0]]))
        wf = S.week_frames(wx, 35000, at=local, units=cfg["units"])
        rows.append(("week", [f for f, _ms in wf]))
    motion = S.news_motion(cfg.get("news_speed"))
    if heads:
        items, total = S.plan_news(heads, 35000, motion)
        nf = S.news_frames(items, total, motion, now=now)
        rows.append((f"news ({len(items)})", pick(nf, 6)))
    rows.append(("night (default)", [S.night_frame("")]))
    rows.append(("night (text)", [S.night_frame("gn :)")]))
    sheet(rows, os.path.join(a.out, "sheet_screens.png"))

    # icons sheet
    from backend.ambient import glyphs as G
    irows = []
    for kind in ("clear", "partly", "cloudy", "fog", "drizzle", "rain", "snow", "storm"):
        for day in ((True, False) if kind in ("clear", "partly") else (True,)):
            imgs = []
            for t in (0, 700, 1400, 2100, 2800, 3500):
                img = S.blank()
                G.blit(img, G.icon_large(kind, day, t), 1, 1)
                G.blit(img, G.icon_mini(kind), 20, 4)
                imgs.append(img)
            irows.append((f"{kind}{'' if day else ' night'}", imgs[:3]))
    sheet(irows, os.path.join(a.out, "sheet_icons.png"))

    # edge cases
    erows = []
    fake = R.fake_weather(temp=-5, hi=102, lo=-12, code=95)
    erows.append(("10:58 / 1:05", [S.clock_frames(minute.replace(hour=10, minute=58), wx, 1000)[0][0],
                                    S.clock_frames(minute.replace(hour=13, minute=5), wx, 1000)[0][0],
                                    S.clock_frames(minute.replace(hour=12, minute=0), None, 1000)[0][0]]))
    erows.append(("hi 102 lo -12", [S.clock_frames(minute.replace(hour=10, minute=58), fake, 1000)[0][0],
                                     S.now_frames(fake, 1000)[0][0],
                                     S.now_frames(R.fake_weather(temp=102, feels=110, code=0), 1000)[0][0]]))
    erows.append(("week edge", [f for f, _ms in S.week_frames(fake, 4000, at=local)][:3]))
    long_h = N.Headline(key="x", title=("UN Security Council holds emergency session as ceasefire talks between "
                                        "the warring parties collapse for the third time in a week, raising fears "
                                        "of a wider regional war across the Horn of Africa and the Red Sea"),
                        source="un", tag="UN", sources=["un", "bbc", "aj"], published=now - 7200, score=20, rank=9)
    li = [S.NewsItem(long_h, S.item_title(long_h))]
    lf = S.news_frames(li, S.item_ms(li[0], motion), motion, now=now)
    erows.append(("long headline", pick(lf, 3)))
    sheet(erows, os.path.join(a.out, "sheet_edge.png"))

    # the rotation
    state = R.RotationState()
    rot = R.build_rotation(datetime.now().astimezone(), cfg, wx, heads, state,
                           span_s=a.minutes * 60 if a.minutes else None)
    with open(os.path.join(a.out, "rotation.gif"), "wb") as fh:
        fh.write(rot.gif)
    if a.gif_x8:
        with open(os.path.join(a.out, "rotation_x8.gif"), "wb") as fh:
            fh.write(upscale_gif(rot.gif))
    info = rot.summary()
    with open(os.path.join(a.out, "rotation.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=2)
    print(f"rotation: {info['bytes']} B GIF, {info['frames']} frames, {info['seconds']:.0f} s long, "
          f"payload {info['payload_bytes']} B, est upload {info['upload_seconds']:.1f} s, "
          f"starts {info['start']}")
    for seg in info["segments"]:
        print(f"  {seg['start'][11:19]} {seg['kind']:6s} {seg['ms'] / 1000:5.1f}s {seg.get('note', '')}")
    print("wrote", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
