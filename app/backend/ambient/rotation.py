"""One upload = one GIF: content from the moment the upload completes to a handover window, then a
still weather tail. The next upload is timed to complete inside that window.

The sign restarts playback from frame 0 when an upload completes, so the switch from the old GIF to
the new one can only be timed, not synced. The rules:

- The clock shows the correct minute at every moment. A clock segment for minute M lies inside
  [M + CLOCK_EARLY, M + 60 s - CLOCK_LATE], so it stays right when the upload finishes a few seconds
  early or late. A clock piece that has to cross a minute (only after a long overrun or with the
  clock alone) is split at the minute boundary.
- Every headline scrolls fully in and fully out. A news segment holds whole headlines only; the
  clock before it absorbs the slack, or the news runs a little into the next clock.
- The switch lands on a handover window, never mid-headline: with the clock on, the window is the
  last clock segment, and the new GIF starts on that same clock. With the clock off, it is a
  headline held still at the left edge (or a weather screen), and the new GIF starts with that same
  held headline (or screen). A switch that misses its window lands on the still tail.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from PIL import Image

import iledsign

from . import screens as S

UPLOAD_BYTES_PER_SECOND = 9000.0
UPLOAD_OVERHEAD_SECONDS = 1.5      # connect check, program header, end ack
PAYLOAD_OVERHEAD_BYTES = 46        # gif_payload wrapper around the GIF
# The link's speed changes from one connection to the next (6 to 15 KB/s seen). A timed upload
# starts as if the link ran at this share of the learned speed, then paces its chunks to finish on
# time (iledsign send_payload finish_at), so it lands on time unless the link is slower still.
SLOW_RATE_FACTOR = 0.65
# The sign does not play frames shorter than this any faster, so shorter ones would make the GIF
# run long and drift off the clock. They are folded into a neighbor.
MIN_FRAME_MS = 40

CLOCK_EARLY_MS = 3000              # a clock starts at least this long after its minute begins
CLOCK_LATE_MS = 8000               # ... and ends at least this long before the next minute
CLOCK_MIN_MS = 8000                # the shortest clock segment
MIN_OTHER_MS = 10000               # a shorter slot before the next clock shows the clock instead
NEWS_OVERRUN_MS = 6000             # a news slot may run this far into the next clock
HANDOVER_CLOCK_MIN_MS = 20000      # the last clock (the handover window) is at least this long
NO_CLOCK_SLOT_MS = 60000           # without the clock: each screen (or block of headlines) runs this long
RESUME_SCREEN_MS = 30000           # a weather screen continued from the previous GIF
DEFAULT_CYCLE = ["now", "today", "week", "news", "news"]
WEATHER_SCREENS = {"now", "today", "week"}
# After the content, a still weather screen (no clock) holds this long. A switch that misses its
# window, or a failed upload, shows this instead of times that are minutes old.
TAIL_MS = 10 * 60 * 1000

# Where a switch may land inside a window, in seconds: a margin for finishing early, a margin for
# finishing late, each growing with the upload time (its error grows with it).
SWITCH_EARLY = (3.0, 0.06)          # clock and weather windows: base, share of the upload time
SWITCH_LATE = (3.0, 0.10)
NEWS_SWITCH_EARLY = 1.0             # early only skips the held title's entry; no text is lost
FORCE_WAIT_MAX = 75.0               # "run now" waits at most this long for a window
# Clock off: the GIF loops by itself. The first headline's lead-in is this long (a short pause once
# a loop, where the next upload lands); weather screens in a loop run LOOP_SCREEN_MS each.
LOOP_GAP_MS = 1500
LOOP_SCREEN_MS = 30000
LOOP_WEATHER_WINDOW_MS = 3000       # a loop without news switches during its first screen's start
LOOP_BYTES_PER_FRAME = 125          # first guess of the GIF cost of a scrolling frame


def news_hold_ms(upload_seconds: float) -> int:
    """How long a handover headline is held: the late margin plus a little."""
    late = SWITCH_LATE[0] + SWITCH_LATE[1] * upload_seconds
    return int(round((NEWS_SWITCH_EARLY + late + 1.0) * 100)) * 10


@dataclass
class RotationState:
    """What carries over between uploads, including the plan of the GIF now on the sign."""
    cycle_index: int = 0
    news_shown: dict = field(default_factory=dict)   # headline key -> epoch last shown
    last_payload_bytes: int = 60000
    last_build_seconds: float = 2.0
    bytes_per_second: float = UPLOAD_BYTES_PER_SECOND
    overhead_seconds: float = UPLOAD_OVERHEAD_SECONDS
    # The GIF on the sign: its planned start, handover windows, content end and tail end (epochs,
    # as planned), and how much later than planned it really started (None until known).
    plan_start: float | None = None
    windows: list = field(default_factory=list)
    content_end: float | None = None
    tail_end: float | None = None
    drift: float | None = None
    next_after: float | None = None      # the next scheduled switch comes no sooner (planned epoch)
    loop_bytes_per_frame: float | None = None

    def estimate_upload(self, payload_bytes: int) -> float:
        return payload_bytes / max(500.0, self.bytes_per_second) + self.overhead_seconds

    def estimate_upload_slow(self, payload_bytes: int) -> float:
        """How long to allow for a timed upload: a slow link, the pacing absorbs the rest."""
        return payload_bytes / max(500.0, self.bytes_per_second * SLOW_RATE_FACTOR) + self.overhead_seconds

    def learn_upload(self, payload_bytes: int, seconds: float) -> None:
        """Blend a measured upload into the throughput estimate (ignores outliers)."""
        if payload_bytes < 5000 or seconds <= self.overhead_seconds:
            return
        bps = payload_bytes / max(0.5, seconds - self.overhead_seconds)
        if 1500 <= bps <= 30000:
            self.bytes_per_second = round(0.6 * self.bytes_per_second + 0.4 * bps, 1)

    def to_dict(self) -> dict:
        return {"cycle_index": self.cycle_index, "news_shown": dict(self.news_shown),
                "last_payload_bytes": self.last_payload_bytes, "last_build_seconds": self.last_build_seconds,
                "bytes_per_second": self.bytes_per_second, "overhead_seconds": self.overhead_seconds,
                "plan_start": self.plan_start, "windows": [dict(w) for w in self.windows],
                "content_end": self.content_end, "tail_end": self.tail_end, "drift": self.drift,
                "next_after": self.next_after, "loop_bytes_per_frame": self.loop_bytes_per_frame}

    @classmethod
    def from_dict(cls, d: dict | None) -> "RotationState":
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, v)
        if not isinstance(s.news_shown, dict):
            s.news_shown = {}
        if not isinstance(s.windows, list):
            s.windows = []
        return s


@dataclass
class Segment:
    kind: str          # clock, now, today, week, news
    start: datetime    # aware local time this segment starts playing
    ms: int
    minute: datetime | None = None   # clock: the minute shown
    note: str = ""
    items: list = field(default_factory=list)   # news: S.NewsItem
    window: tuple[int, int] | None = None        # a handover window inside: (offset ms, length ms)
    primary: bool = False                        # the window the next upload aims for
    cont: bool = False                           # continues the previous GIF's handover screen

    @property
    def end(self) -> datetime:
        return self.start + timedelta(milliseconds=self.ms)


@dataclass
class Rotation:
    gif: bytes
    frames: int
    total_ms: int
    start: datetime
    segments: list[Segment]
    payload_bytes: int
    upload_seconds: float
    build_seconds: float
    color_scale: float
    tail_ms: int = 0
    windows: list = field(default_factory=list)
    motion: S.Motion | None = None
    loop: bool = False                  # the sign loops this GIF by itself (no clock, no tail)

    @property
    def primary(self) -> dict | None:
        return next((w for w in self.windows if w.get("primary")), None)

    def summary(self) -> dict:
        p = self.primary
        return {
            "bytes": len(self.gif), "payload_bytes": self.payload_bytes, "frames": self.frames,
            "seconds": self.total_ms / 1000, "tail_seconds": self.tail_ms / 1000,
            "upload_seconds": round(self.upload_seconds, 1),
            "build_seconds": round(self.build_seconds, 2), "start": self.start.isoformat(timespec="seconds"),
            "end": (self.start + timedelta(milliseconds=self.total_ms)).isoformat(timespec="seconds"),
            "color_scale": self.color_scale, "loop": self.loop,
            "news_px_per_s": round(self.motion.px_per_s, 1) if self.motion else None,
            "handover": ({"kind": p["kind"], "from": _iso(p["w0"]), "to": _iso(p["w1"])} if p else None),
            "segments": [{"kind": s.kind, "start": s.start.isoformat(timespec="milliseconds"), "ms": s.ms,
                          "minute": s.minute.strftime("%H:%M") if s.minute else None, "note": s.note,
                          "window": list(s.window) if s.window else None, "primary": s.primary,
                          "cont": s.cont}
                         for s in self.segments],
        }


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds") if epoch else None


def _floor_minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def _ms(a: datetime, b: datetime) -> int:
    """Whole 10 ms steps from a to b."""
    return int(round((b - a).total_seconds() * 100)) * 10


def _round10(t: datetime) -> datetime:
    return t.replace(microsecond=(t.microsecond // 10000) * 10000)


def cycle_kinds(cfg: dict, have_weather: bool, have_news: bool) -> list[str]:
    scr = cfg.get("screens", {})
    out = []
    for k in DEFAULT_CYCLE:
        if not scr.get(k, {}).get("on", True):
            continue
        if k in WEATHER_SCREENS and not have_weather:
            continue
        if k == "news" and not have_news:
            continue
        out.append(k)
    return out


def safe_clock_start(t: datetime) -> datetime:
    """The first moment at or after t where a fresh clock can start with room for early and late
    finishes (and still fit its shortest length inside the minute)."""
    m = _floor_minute(t)
    lo = m + timedelta(milliseconds=CLOCK_EARLY_MS + 1000)
    hi = m + timedelta(seconds=60) - timedelta(milliseconds=CLOCK_LATE_MS + CLOCK_MIN_MS + 4000)
    if t < lo:
        return lo
    if t > hi:
        return lo + timedelta(minutes=1)
    return t


def _order_news(headlines: list, shown: dict, skip: str | None = None) -> list:
    """Never-shown first (by rank), then least recently shown."""
    hs = [h for h in headlines if _key(h) != skip]
    return sorted(hs, key=lambda h: (shown.get(_key(h), 0), -_rank(h)))


def _key(h) -> str:
    return h.key if hasattr(h, "key") else str((h or {}).get("key"))


def _rank(h) -> float:
    return float(h.rank if hasattr(h, "rank") else (h or {}).get("rank") or 0)


@dataclass
class Plan:
    segments: list[Segment]
    cycle_index: int
    shown: dict


def plan_segments(start: datetime, minutes: float, *, clock_seconds: int = 25, clock_on: bool = True,
                  kinds: list[str] | None = None, cycle_index: int = 0, headlines: list | None = None,
                  shown: dict | None = None, motion: S.Motion | None = None, handover: bool = False,
                  cont: dict | None = None, hold_ms: int = 6000) -> tuple[list[Segment], int]:
    """Lay out segments from `start` (aware) for about `minutes`. Returns (segments, next cycle index).
    See plan() for the details; this form keeps only the segments."""
    p = plan(start, minutes * 60, clock_seconds=clock_seconds, clock_on=clock_on, kinds=kinds,
             cycle_index=cycle_index, headlines=headlines, shown=shown, motion=motion, handover=handover,
             cont=cont, hold_ms=hold_ms)
    return p.segments, p.cycle_index


def plan(start: datetime, span_s: float, *, clock_seconds: int = 25, clock_on: bool = True,
         kinds: list[str] | None = None, cycle_index: int = 0, headlines: list | None = None,
         shown: dict | None = None, motion: S.Motion | None = None, handover: bool = True,
         cont: dict | None = None, hold_ms: int = 6000) -> Plan:
    """Segments from `start`, contiguous and in 10 ms steps.

    handover=True: the content runs about `span_s`, then ends with a handover window (marked
    `primary`). handover=False (previews): the content simply runs at least `span_s`.
    `cont` continues the previous GIF's handover screen: {"kind": "clock"} or {"kind": "news",
    "headline": {...}} or {"kind": <weather screen>}. `shown` (headline key -> epoch) is updated
    with the planned start of every headline placed.
    """
    kinds = list(kinds or [])
    headlines = list(headlines or [])
    shown = dict(shown or {})
    motion = motion or S.news_motion()
    if "news" in kinds and not headlines:
        kinds = [k for k in kinds if k != "news"]
    if not kinds:
        clock_on = True
    start = _round10(start)
    target = start + timedelta(seconds=span_s)
    segs: list[Segment] = []
    idx = cycle_index
    E = timedelta(milliseconds=CLOCK_EARLY_MS)
    L = timedelta(milliseconds=CLOCK_LATE_MS)
    CMIN = timedelta(milliseconds=CLOCK_MIN_MS)
    C = timedelta(seconds=clock_seconds)
    minute = timedelta(minutes=1)

    def add(kind: str, a: datetime, b: datetime, **kw) -> Segment | None:
        ms = _ms(a, b)
        if ms <= 0:
            return None
        seg = Segment(kind, a, ms, **kw)
        segs.append(seg)
        return seg

    def add_clock(a: datetime, b: datetime, *, window: bool = True, primary: bool = False) -> None:
        """Clock pieces from a to b, split at every minute boundary."""
        while a < b:
            m = _floor_minute(a)
            e = min(b, m + minute)
            last = segs[-1] if segs else None
            if last and last.kind == "clock" and last.minute == m and last.end == a and not primary:
                last.ms += _ms(a, e)
                last.window = (0, last.ms) if window else last.window
            else:
                ms = _ms(a, e)
                if ms > 0:
                    segs.append(Segment("clock", a, ms, m, window=(0, ms) if window else None, primary=primary))
            a = e

    def mark(items: list, a: datetime) -> None:
        t = a
        for it in items:
            if it.headline is not None:
                shown[_key(it.headline)] = t.timestamp()
            t += timedelta(milliseconds=S.item_ms(it, motion))

    def add_news(a: datetime, ms: int, items: list, **kw) -> Segment:
        seg = Segment("news", a, ms, items=items, **kw)
        seg.note = " | ".join(it.title[:48] for it in items)
        segs.append(seg)
        mark(items, a)
        return seg

    def cont_item() -> S.NewsItem | None:
        if not cont or cont.get("kind") != "news" or not cont.get("headline") or "news" not in kinds:
            return None
        h = cont["headline"]
        return S.NewsItem(h, S.item_title(h), from_hold=True)

    t = start
    first_item = None
    if not clock_on and cont:
        if cont.get("kind") == "news":
            first_item = cont_item()
        elif cont.get("kind") in kinds:
            add(cont["kind"], t, t + timedelta(milliseconds=RESUME_SCREEN_MS), cont=True)
            t = segs[-1].end
            idx = kinds.index(cont["kind"]) + 1

    limit = start + timedelta(seconds=span_s) + timedelta(minutes=10)   # a guard against odd settings
    if clock_on:
        placed_other = False
        while True:
            if t > limit:
                break
            m = _floor_minute(t - E)                  # the minute whose clock zone holds t
            zone_end = m + minute - L
            anchor_next = m + minute + E
            if handover and placed_other and t >= target:   # never sooner than refresh_minutes
                end = max(zone_end, t + timedelta(milliseconds=HANDOVER_CLOCK_MIN_MS))
                add_clock(t, end, primary=True)
                for s in segs:
                    if s.kind == "clock" and s.start >= t:
                        s.primary = True
                break
            if not kinds:  # the clock alone: switch digits exactly on the minute
                add_clock(t, anchor_next)
                t = anchor_next
                placed_other = True
                if not handover and t >= target:
                    break
                continue
            ce = max(m + E + C, t + CMIN)
            if t + CMIN <= zone_end:
                ce = min(ce, zone_end)
            if ce > zone_end or anchor_next - ce < timedelta(milliseconds=MIN_OTHER_MS):
                add_clock(t, anchor_next)             # no room for another screen this minute
                t = anchor_next
                continue
            slot = _ms(ce, anchor_next)
            kind = kinds[idx % len(kinds)]
            idx += 1
            if kind == "news":
                order = _order_news(headlines, shown)
                items, d = S.plan_news(order, slot, motion, overrun_ms=NEWS_OVERRUN_MS)
                slack = slot - d
                ce2 = ce
                pad = 0
                if slack > 0:
                    ce2 = min(ce + timedelta(milliseconds=slack), max(ce, zone_end))
                    pad = slack - _ms(ce, ce2)
                add_clock(t, ce2)
                add_news(ce2, d + pad, items)
                t = ce2 + timedelta(milliseconds=d + pad)
            else:
                add_clock(t, ce)
                add(kind, ce, anchor_next)
                t = anchor_next
            placed_other = True
            if not handover and t >= target:
                break
    else:
        if first_item is not None:
            order = _order_news(headlines, shown, skip=_key(first_item.headline))
            items, d = S.plan_news(order, NO_CLOCK_SLOT_MS, motion, first=first_item, overrun_ms=NO_CLOCK_SLOT_MS)
            seg = add_news(t, d, items)
            seg.cont = True
            t = seg.end
            idx = kinds.index("news") + 1
        while t <= limit:
            kind = kinds[idx % len(kinds)]
            lead = timedelta(milliseconds=S.hold_offset_ms(motion) if kind == "news" else 0)
            if handover and segs and t + lead >= target:
                if kind == "news":
                    h = _order_news(headlines, shown)[0]
                    it = S.NewsItem(h, S.item_title(h), hold_ms=hold_ms)
                    d = S.item_ms(it, motion)
                    win = (S.hold_offset_ms(motion), hold_ms + motion.ms)
                    add_news(t, d, [it], window=win, primary=True)
                else:
                    add(kind, t, t + timedelta(milliseconds=NO_CLOCK_SLOT_MS), window=(0, NO_CLOCK_SLOT_MS),
                        primary=True)
                idx += 1
                break
            idx += 1
            if kind == "news":
                order = _order_news(headlines, shown)
                slot = NO_CLOCK_SLOT_MS
                if handover:   # the block before the handover only fills the time that is left
                    slot = max(10, min(slot, _ms(t, target - lead)))
                items, d = S.plan_news(order, slot, motion, overrun_ms=NO_CLOCK_SLOT_MS)
                t = add_news(t, d, items).end
            else:
                t = add(kind, t, t + timedelta(milliseconds=NO_CLOCK_SLOT_MS)).end
            if not handover and t >= target:
                break
    return Plan(segs, idx % max(1, len(kinds) or 1), shown)


def loop_segments(start: datetime, kinds: list[str], items: list, motion: S.Motion) -> list[Segment]:
    """One pass of a self-looping GIF (no clock): the screens in cycle order, starting with news when
    it is on, the headline set split over the news slots. The first headline's longer lead-in
    (LOOP_GAP_MS) is the wrap point and the handover window."""
    seq = list(kinds)
    if "news" in seq:
        i = seq.index("news")
        seq = seq[i:] + seq[:i]
    n_slots = seq.count("news")
    groups: list[list] = []
    if n_slots:
        per, extra = divmod(len(items), n_slots)
        k = 0
        for j in range(n_slots):
            take = per + (1 if j < extra else 0)
            groups.append(items[k:k + take])
            k += take
    segs: list[Segment] = []
    t = start
    for kind in seq:
        if kind == "news":
            group = groups.pop(0)
            if not group:
                continue
            ms = sum(S.item_ms(it, motion) for it in group)
            seg = Segment("news", t, ms, items=group)
            seg.note = " | ".join(it.title[:48] for it in group)
        else:
            seg = Segment(kind, t, LOOP_SCREEN_MS)
        segs.append(seg)
        t = seg.end
    if segs:
        first = segs[0]
        gap = first.items[0].gap_ms if first.kind == "news" else LOOP_WEATHER_WINDOW_MS
        first.window = (0, gap)
        first.primary = True
    return segs


def build_loop(now: datetime, cfg: dict, wx: dict | None, headlines: list, state: RotationState, *,
               start: datetime | None = None, budget_bytes: int = 160_000, span_s: float | None = None,
               color_scale: float | None = None) -> Rotation:
    """With the clock off nothing depends on the time of day, so the GIF is a set of whole headlines
    (and the weather screens that are on) that the sign loops by itself: the last frame wraps
    cleanly to the first. As many headlines as fit `budget_bytes`, least recently shown first, so
    the sets rotate through every ranked headline across uploads. The next upload is due `span_s`
    (refresh_minutes) after this one starts, at the loop's wrap point."""
    t_build = time.monotonic()
    if now.tzinfo is None:
        now = now.astimezone()
    k = cfg.get("day_color_scale", 0.7) if color_scale is None else color_scale
    span = span_s if span_s is not None else cfg.get("refresh_minutes", 5) * 60
    kinds = cycle_kinds(cfg, wx is not None, bool(headlines))
    if not kinds:
        raise ValueError("a loop needs weather or news")
    motion = S.news_motion(cfg.get("news_speed"))
    if start is None:
        start = now + timedelta(seconds=state.last_build_seconds + state.estimate_upload_slow(state.last_payload_bytes))
    start = _round10(start)
    order = _order_news(headlines, state.news_shown) if "news" in kinds else []

    def make(n: int):
        items = [S.NewsItem(h, S.item_title(h), gap_ms=LOOP_GAP_MS if i == 0 else S.NEWS_GAP)
                 for i, h in enumerate(order[:n])]
        segs = loop_segments(start, [kd for kd in kinds if kd != "news" or items], items, motion)
        frames = render_segments(segs, cfg, wx, now_epoch=now.timestamp(), motion=motion)
        gif = encode(frames, k, int(cfg.get("max_frame_ms") or 0))
        return segs, gif

    # first guess from the frame count, then step down (or once up) on the real size
    bpf = state.loop_bytes_per_frame or LOOP_BYTES_PER_FRAME
    est = 3000 + sum(LOOP_SCREEN_MS / 1000 * 220 for kd in kinds if kd in WEATHER_SCREENS)
    n = 0
    for h in order:
        f = S.scroll_frames(S.item_title(h), motion) + 1
        if n and est + f * bpf > budget_bytes:
            break
        est += f * bpf
        n += 1
    segs, gif = make(n)
    tried_up = False
    while True:
        size = len(gif) + PAYLOAD_OVERHEAD_BYTES
        if size > budget_bytes and n > 1:
            n -= 1
            segs, gif = make(n)
            if tried_up:
                break
            continue
        if size < budget_bytes * 0.85 and n < len(order) and not tried_up:
            tried_up = True
            segs2, gif2 = make(n + 1)
            if len(gif2) + PAYLOAD_OVERHEAD_BYTES <= budget_bytes:
                n, segs, gif = n + 1, segs2, gif2
        break
    payload = len(gif) + PAYLOAD_OVERHEAD_BYTES
    total = sum(s.ms for s in segs)
    windows = windows_of(segs)
    for w in windows:
        w["kind"] = "loop"
        w["period"] = total / 1000
    frames_n = _count_frames(gif)
    rot = Rotation(gif=gif, frames=frames_n, total_ms=total, tail_ms=0, start=segs[0].start, segments=segs,
                   payload_bytes=payload, upload_seconds=state.estimate_upload(payload),
                   build_seconds=time.monotonic() - t_build, color_scale=k, windows=windows, motion=motion,
                   loop=True)
    for h in order[:n]:
        state.news_shown[_key(h)] = rot.start.timestamp()
    cutoff = now.timestamp() - 86400
    state.news_shown = {key: v for key, v in state.news_shown.items() if v >= cutoff}
    if frames_n:
        state.loop_bytes_per_frame = round(len(gif) / frames_n, 1)
    state.last_payload_bytes = payload
    state.last_build_seconds = round(min(30.0, max(0.5, time.monotonic() - t_build)), 2)
    state.plan_start = rot.start.timestamp()
    state.windows = windows
    state.content_end = None
    state.tail_end = None
    state.next_after = rot.start.timestamp() + span
    state.drift = None
    rot.build_seconds = time.monotonic() - t_build
    return rot


def windows_of(segs: list[Segment]) -> list[dict]:
    """Handover windows as epochs (as planned): every clock segment, and the primary window."""
    out = []
    for s in segs:
        if not s.window:
            continue
        w0 = s.start.timestamp() + s.window[0] / 1000
        w = {"kind": s.kind, "w0": w0, "w1": w0 + s.window[1] / 1000, "primary": s.primary,
             "minute": s.minute.timestamp() if s.minute else None, "headline": None}
        if s.kind == "news" and s.items:
            h = s.items[0].headline
            w["headline"] = S._hd(h)
        out.append(w)
    # the pieces of a last clock that crosses a minute form one primary window
    prim = [w for w in out if w["primary"]]
    if len(prim) > 1:
        prim[0]["w1"] = prim[-1]["w1"]
        out = [w for w in out if not w["primary"] or w is prim[0]]
    return out


def switch_margins(kind: str, upload_seconds: float) -> tuple[float, float]:
    """(early, late) seconds kept free at the two ends of a window of this kind."""
    late = SWITCH_LATE[0] + SWITCH_LATE[1] * upload_seconds
    if kind == "news":
        return NEWS_SWITCH_EARLY, late
    return SWITCH_EARLY[0] + SWITCH_EARLY[1] * upload_seconds, late


def choose_switch(state: RotationState, now: float, earliest: float, upload_seconds: float, *,
                  force: bool = False, allow: set | None = None) -> dict | None:
    """When the next upload should complete: a moment inside a handover window of the GIF on the
    sign (or its still tail), at or after `earliest`. Scheduled refreshes aim for the primary
    window (a loop's wrap point no sooner than `next_after`); "run now" takes the first window
    within FORCE_WAIT_MAX (a loop's next wrap point). `allow` limits the window kinds. None = no
    known window (start fresh, as soon as possible).

    Returns {"at": epoch, "kind": ..., "cont": {...} or None, "window": {...}, "later": [epochs]}
    where `later` are the next wrap points of a loop, for an upload that cannot make `at`.
    """
    if not state.windows or state.drift is None:
        return None
    d = float(state.drift)
    for w in state.windows:
        if w.get("kind") != "loop" or (allow is not None and "loop" not in allow):
            continue
        period = float(w.get("period") or 0)
        if period <= 0:
            continue
        mid = (w["w0"] + w["w1"]) / 2 + d
        lo = earliest if force else max(earliest, (state.next_after or 0) + d)
        k = max(0, math.ceil((lo - mid) / period))
        at = mid + k * period
        if force and at - now > max(FORCE_WAIT_MAX, period + 5):
            return None
        return {"at": at, "kind": "loop", "cont": None, "window": w,
                "later": [at + i * period for i in range(1, 4)]}
    if state.content_end is None:
        return None
    cands = [w for w in state.windows if (force or w.get("primary"))
             and (allow is None or w["kind"] in allow)]
    tail = {"kind": "tail", "w0": state.content_end + 0.5, "w1": (state.tail_end or state.content_end) - 30,
            "primary": False}
    for w in sorted(cands, key=lambda w: w["w0"]) + [tail]:
        early, late = (0.0, 0.0) if w["kind"] == "tail" else switch_margins(w["kind"], upload_seconds)
        lo = w["w0"] + d + early
        hi = w["w1"] + d - late
        at = max(earliest, lo)
        if w["kind"] == "clock" and w.get("minute"):
            at = max(at, w["minute"] + CLOCK_EARLY_MS / 1000)
        if at > hi:
            continue
        if force and at - now > FORCE_WAIT_MAX:
            return None
        cont = None
        if w["kind"] == "clock":
            cont = {"kind": "clock"}
        elif w["kind"] == "news" and w.get("headline"):
            cont = {"kind": "news", "headline": w["headline"]}
        elif w["kind"] in WEATHER_SCREENS:
            cont = {"kind": w["kind"]}
        return {"at": at, "kind": w["kind"], "cont": cont, "window": w, "later": []}
    return None


def apply_scale(img: Image.Image, k: float) -> Image.Image:
    if k >= 0.999:
        return img
    lut = [min(255, int(v * k + 0.5)) for v in range(256)] * 3
    return img.point(lut)


def render_segments(segs: list[Segment], cfg: dict, wx: dict | None, *, now_epoch: float,
                    motion: S.Motion | None = None) -> list[tuple[Image.Image, int]]:
    frames: list[tuple[Image.Image, int]] = []
    units = cfg.get("units", "F")
    h24 = bool(cfg.get("clock_24h"))
    animate = bool(cfg.get("animate_icons", True))
    motion = motion or S.news_motion(cfg.get("news_speed"))
    for seg in segs:
        # icons animate on real time, so a GIF that takes over mid-screen keeps the same phase
        t0 = int(seg.start.timestamp() * 1000)
        if seg.kind == "clock":
            fr = S.clock_frames(seg.minute, wx, seg.ms, h24=h24, t0_ms=t0, animate=animate)
        elif seg.kind == "now":
            fr = S.now_frames(wx, seg.ms, units=units, t0_ms=t0, animate=animate)
        elif seg.kind == "today":
            fr = S.today_frames(wx, seg.ms, at=seg.start.replace(tzinfo=None), units=units)
        elif seg.kind == "week":
            fr = S.week_frames(wx, seg.ms, at=seg.start.replace(tzinfo=None), units=units)
        elif seg.kind == "news":
            fr = S.news_frames(seg.items, seg.ms, motion, now=now_epoch)
        else:
            fr = [(S.blank(), seg.ms)]
        assert sum(ms for _i, ms in fr) == seg.ms, (seg.kind, seg.ms, sum(ms for _i, ms in fr))
        frames.extend(fr)
    return frames


def merge_frames(frames: list[tuple[Image.Image, int]]) -> list[tuple[Image.Image, int]]:
    out: list[tuple[Image.Image, int]] = []
    last = None
    for img, ms in frames:
        b = img.tobytes()
        if out and b == last:
            out[-1] = (out[-1][0], out[-1][1] + ms)
        else:
            out.append((img, ms))
            last = b
    return out


def fold_short(frames: list[tuple[Image.Image, int]], min_ms: int = MIN_FRAME_MS) -> list[tuple[Image.Image, int]]:
    """Frames shorter than the sign plays fold into a neighbor, so the total time stays exact."""
    out: list[tuple[Image.Image, int]] = []
    carry = 0
    for img, ms in frames:
        if ms < min_ms and out:
            out[-1] = (out[-1][0], out[-1][1] + ms)
        elif ms + carry < min_ms:
            carry += ms
        else:
            out.append((img, ms + carry))
            carry = 0
    if carry and out:
        out[-1] = (out[-1][0], out[-1][1] + carry)
    return out


def split_long(frames: list[tuple[Image.Image, int]], max_ms: int) -> list[tuple[Image.Image, int]]:
    """Cut frames longer than max_ms into pieces. Consecutive pieces differ by 8 in one channel of the
    brightest pixel (invisible on a lit LED, and enough to survive palette mapping), so the encoder
    cannot merge them back. All-black frames stay whole: any marker there would show as a blink."""
    if not max_ms:
        return frames
    out: list[tuple[Image.Image, int]] = []
    for img, ms in frames:
        data = img.tobytes()
        best = max(range(0, len(data), 3), key=lambda i: data[i] + data[i + 1] + data[i + 2])
        if ms <= max_ms or data[best] + data[best + 1] + data[best + 2] == 0:
            out.append((img, ms))
            continue
        px = list(data[best:best + 3])
        ch = px.index(max(px))
        px[ch] = px[ch] - 8 if px[ch] >= 8 else px[ch] + 8
        alt = img.copy()
        alt.putpixel(((best // 3) % img.width, (best // 3) // img.width), tuple(px))
        k = 0
        while ms > 0:
            piece = min(max_ms, ms)
            if 0 < ms - piece < 100:  # no tiny last piece
                piece = ms
            out.append((img if k % 2 == 0 else alt, piece))
            ms -= piece
            k += 1
    return out


def encode(frames: list[tuple[Image.Image, int]], color_scale: float = 1.0, max_frame_ms: int = 0) -> bytes:
    frames = fold_short(merge_frames([(apply_scale(img, color_scale), ms) for img, ms in frames]))
    frames = split_long(frames, max_frame_ms)
    imgs = [f for f, _ms in frames]
    durs = [max(MIN_FRAME_MS, ms) for _f, ms in frames]
    return iledsign.encode_gif(imgs, durs)


def tail_frame(cfg: dict, wx: dict | None):
    """The still screen after the planned minutes: current weather without a clock, or black."""
    if not wx:
        return S.blank()
    return S.now_frames(wx, 1000, units=cfg.get("units", "F"), animate=False)[0][0]


def build_rotation(now: datetime, cfg: dict, wx: dict | None, headlines: list, state: RotationState, *,
                   minutes: float | None = None, color_scale: float | None = None,
                   start: datetime | None = None, tail_ms: int | None = None, span_s: float | None = None,
                   cont: dict | None = None, handover: bool | None = None,
                   hold_ms: int | None = None) -> Rotation:
    """Build the upload GIF. `now` must be timezone-aware local time. `state` is updated in place
    (cycle position, which headlines were shown, and the new GIF's plan), so pass a copy for a
    preview.

    `start`: when the upload is expected to complete (the GIF's first frame). By default, now plus
    the build and estimated upload time, moved to a safe moment for a fresh clock.
    `span_s`: content length before the handover window (default refresh_minutes). `minutes`
    (previews): plain content of that length with no handover window and no tail.
    """
    t_build = time.monotonic()
    if now.tzinfo is None:
        now = now.astimezone()
    preview = minutes is not None and handover is not True
    if handover is None:
        handover = not preview
    span = span_s if span_s is not None else (minutes * 60 if minutes is not None
                                              else cfg.get("refresh_minutes", 5) * 60)
    k = cfg.get("day_color_scale", 0.7) if color_scale is None else color_scale
    kinds = cycle_kinds(cfg, wx is not None, bool(headlines))
    clock_cfg = cfg.get("screens", {}).get("clock", {})
    clock_on = bool(clock_cfg.get("on", True)) or not kinds
    motion = S.news_motion(cfg.get("news_speed"))
    est = state.estimate_upload(state.last_payload_bytes)
    if start is None:
        start = now + timedelta(seconds=state.last_build_seconds + est)
        if clock_on and not cont:
            start = safe_clock_start(start)
    hold = hold_ms if hold_ms is not None else news_hold_ms(est)
    p = plan(start, span, clock_seconds=int(clock_cfg.get("seconds", 25)), clock_on=clock_on, kinds=kinds,
             cycle_index=state.cycle_index, headlines=headlines, shown=state.news_shown, motion=motion,
             handover=handover, cont=cont, hold_ms=hold)
    segs = p.segments
    frames = render_segments(segs, cfg, wx, now_epoch=now.timestamp(), motion=motion)
    tail = tail_ms if tail_ms is not None else (TAIL_MS if handover else 0)
    if tail:
        frames.append((tail_frame(cfg, wx), tail))
    gif = encode(frames, k, int(cfg.get("max_frame_ms") or 0))
    payload = len(gif) + PAYLOAD_OVERHEAD_BYTES
    total = sum(s.ms for s in segs)
    windows = windows_of(segs) if handover else []
    rot = Rotation(gif=gif, frames=_count_frames(gif), total_ms=total, tail_ms=tail, start=segs[0].start,
                   segments=segs, payload_bytes=payload, upload_seconds=state.estimate_upload(payload),
                   build_seconds=time.monotonic() - t_build, color_scale=k, windows=windows, motion=motion)
    # forget headlines not seen in a day
    cutoff = now.timestamp() - 86400
    state.news_shown = {key: v for key, v in p.shown.items() if v >= cutoff}
    state.cycle_index = p.cycle_index
    state.last_payload_bytes = payload
    state.last_build_seconds = round(min(30.0, max(0.5, time.monotonic() - t_build)), 2)
    state.plan_start = rot.start.timestamp()
    state.windows = windows
    end = rot.start.timestamp() + total / 1000
    state.content_end = end
    state.tail_end = end + tail / 1000
    state.next_after = rot.start.timestamp() + span
    state.drift = None
    rot.build_seconds = time.monotonic() - t_build
    return rot


def _count_frames(gif: bytes) -> int:
    import io
    try:
        return getattr(Image.open(io.BytesIO(gif)), "n_frames", 1)
    except Exception:  # noqa: BLE001
        return 0


def night_gif(text: str = "") -> bytes:
    """The night screen as a one-frame GIF (all black unless night text is set)."""
    return iledsign.encode_gif([S.night_frame(text)], [1000])


def night_still(text: str = "") -> bytes:
    """Raw RGB bytes of the night screen, for a still upload."""
    return S.night_frame(text).tobytes()


def fake_weather(temp: float = 72, hi: float = 75, lo: float = 55, code: int = 2, feels: float | None = None,
                 is_day: bool = True, days: int = 8) -> dict:
    """Synthetic forecast for previews and tests."""
    now = datetime.now()
    hourly = []
    for i in range(30):
        t = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i)
        x = (t.hour - 15) / 24 * 6.283
        hourly.append({"time": t.strftime("%Y-%m-%dT%H:00"), "temp": lo + (hi - lo) * (0.5 + 0.5 * __import__("math").cos(x)),
                       "pop": (i * 13) % 100, "code": code, "is_day": 1 if 7 <= t.hour < 19 else 0})
    daily = []
    codes = [0, 2, 3, 45, 53, 63, 73, 95]
    for i in range(days):
        d = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append({"date": d, "hi": hi - i * 3 if i else hi, "lo": lo + i if i else lo, "code": codes[i % len(codes)],
                      "pop": (i * 23) % 100, "sunrise": None, "sunset": None})
    return {"current": {"time": None, "temp": temp, "feels": temp - 4 if feels is None else feels, "code": code,
                        "is_day": is_day, "wind": 12, "humidity": 50},
            "hourly": hourly, "daily": daily, "fetched_at": time.time(), "stale": False, "error": None}
