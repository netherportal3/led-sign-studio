"""Ambient settings: defaults, deep-merge, validation, load/save.

Shape (what GET /api/ambient/settings returns):
{enabled, location: {name, lat, lon, timezone} or null, units: "F" or "C", clock_24h, active_start,
 active_end, refresh_minutes, screens: {clock: {on, seconds}, now: {on}, today: {on}, week: {on},
 news: {on}}, news_sources: {<id>: bool}, day_brightness, day_color_scale, night_brightness,
 night_text, pause_minutes_on_manual, news_speed}

There is no default location: ambient mode stays off until one is picked in the Ambient tab.
Units and the clock format default to the Windows user locale.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from functools import lru_cache

from . import news, screens


@lru_cache(maxsize=1)
def locale_defaults() -> tuple[str, bool]:
    """(units, clock_24h) from the Windows user locale. Elsewhere, or if that fails: Celsius, 24-hour."""
    units, clock_24h = "C", True
    if os.name != "nt":
        return units, clock_24h
    try:
        import ctypes
        get = ctypes.windll.kernel32.GetLocaleInfoEx
        buf = ctypes.create_unicode_buffer(85)
        # None = the user's default locale. LOCALE_IMEASURE: "1" = US measures, "0" = metric.
        if get(None, 0x0D, buf, len(buf)):
            units = "F" if buf.value.strip() == "1" else "C"
        # LOCALE_STIMEFORMAT, for example "h:mm:ss tt" (12-hour) or "HH:mm:ss" (24-hour).
        if get(None, 0x1003, buf, len(buf)):
            clock_24h = "H" in buf.value
    except (AttributeError, OSError, ValueError):
        pass
    return units, clock_24h


DEFAULTS: dict = {
    "enabled": True,
    "location": None,
    "units": locale_defaults()[0],
    "clock_24h": locale_defaults()[1],
    "active_start": "08:00",
    "active_end": "22:00",
    "refresh_minutes": 5,
    "screens": {
        "clock": {"on": True, "seconds": 25},
        "now": {"on": True},
        "today": {"on": True},
        "week": {"on": True},
        "news": {"on": True},
    },
    "news_sources": {sid: bool(meta["default"]) for sid, meta in news.SOURCES.items()},
    "day_brightness": 3,
    "day_color_scale": 0.7,
    "night_brightness": 1,
    # Empty = the night screen is all black (the sign looks off). Text shows very dim.
    "night_text": "",
    "pause_minutes_on_manual": 30,
    # News scroll speed: "slow", "normal", "fast", or a number of pixels per second (10 to 75).
    "news_speed": screens.DEFAULT_NEWS_SPEED,
    # Size control (not in the UI): still icons make the GIF smaller.
    "animate_icons": True,
    # 0 = off. Otherwise no GIF frame is longer than this (long stills are split), in case the sign
    # plays long frame delays wrong.
    "max_frame_ms": 0,
}

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SCREEN_IDS = ("clock", "now", "today", "week", "news")


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _num(v, lo, hi, default, cast=float):
    try:
        x = cast(v)
    except (TypeError, ValueError):
        return default
    if x != x:  # NaN
        return default
    return max(lo, min(hi, x))


def _hhmm(v, default: str) -> str:
    m = _TIME_RE.match(str(v or "").strip())
    if not m:
        return default
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _speed(v) -> str | int | float:
    """A preset name, or px/s clamped to the supported range; anything else is the default."""
    if isinstance(v, str):
        t = v.strip().lower()
        if t in screens.NEWS_SPEEDS:
            return t
        try:
            v = float(t)
        except ValueError:
            return screens.DEFAULT_NEWS_SPEED
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        return screens.DEFAULT_NEWS_SPEED
    x = max(screens.MIN_NEWS_SPEED, min(screens.MAX_NEWS_SPEED, float(v)))
    return int(x) if x == int(x) else round(x, 1)


def location(loc) -> dict | None:
    """A place with real coordinates, or None. Its time zone comes from the place search."""
    if not isinstance(loc, dict):
        return None
    lat = _num(loc.get("lat"), -90.0, 90.0, None)
    lon = _num(loc.get("lon"), -180.0, 180.0, None)
    if lat is None or lon is None:
        return None
    return {
        "name": str(loc.get("name") or f"{lat:.2f}, {lon:.2f}")[:80],
        "lat": lat,
        "lon": lon,
        "timezone": str(loc.get("timezone") or "auto")[:64],
    }


def validate(s: dict) -> dict:
    """Clamp and fill everything; unknown keys are dropped."""
    d = DEFAULTS
    s = deep_merge(d, s if isinstance(s, dict) else {})
    out: dict = {}
    out["enabled"] = bool(s.get("enabled"))
    out["location"] = location(s.get("location"))
    units = str(s.get("units")).upper()
    out["units"] = units if units in ("C", "F") else d["units"]
    out["clock_24h"] = bool(s.get("clock_24h"))
    out["active_start"] = _hhmm(s.get("active_start"), d["active_start"])
    out["active_end"] = _hhmm(s.get("active_end"), d["active_end"])
    out["refresh_minutes"] = int(_num(s.get("refresh_minutes"), 3, 15, d["refresh_minutes"], int))
    scr = s.get("screens") if isinstance(s.get("screens"), dict) else {}
    out["screens"] = {}
    for sid in SCREEN_IDS:
        v = scr.get(sid) if isinstance(scr.get(sid), dict) else {}
        out["screens"][sid] = {"on": bool(v.get("on", True))}
    clock = scr.get("clock") if isinstance(scr.get("clock"), dict) else {}
    out["screens"]["clock"]["seconds"] = int(_num(clock.get("seconds"), 10, 50, 25, int))
    srcs = s.get("news_sources") if isinstance(s.get("news_sources"), dict) else {}
    out["news_sources"] = {sid: bool(srcs.get(sid, meta["default"])) for sid, meta in news.SOURCES.items()}
    out["day_brightness"] = int(_num(s.get("day_brightness"), 1, 10, d["day_brightness"], int))
    out["day_color_scale"] = round(_num(s.get("day_color_scale"), 0.1, 1.0, d["day_color_scale"]), 2)
    out["night_brightness"] = int(_num(s.get("night_brightness"), 1, 10, d["night_brightness"], int))
    out["night_text"] = str(s.get("night_text") or "")[:20]
    out["pause_minutes_on_manual"] = int(_num(s.get("pause_minutes_on_manual"), 0, 24 * 60,
                                              d["pause_minutes_on_manual"], int))
    out["news_speed"] = _speed(s.get("news_speed"))
    out["animate_icons"] = bool(s.get("animate_icons", True))
    mf = int(_num(s.get("max_frame_ms"), 0, 600000, 0, int))
    out["max_frame_ms"] = 0 if mf <= 0 else max(1000, mf)
    return out


def hhmm_to_minutes(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


class AmbientSettings:
    """Thread-safe settings file at <data>/ambient/settings.json."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return validate(json.load(fh))
        except (OSError, ValueError):
            return validate({})

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, patch: dict) -> dict:
        with self._lock:
            self._data = validate(deep_merge(self._data, patch if isinstance(patch, dict) else {}))
            self._save()
            return copy.deepcopy(self._data)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self.path)
