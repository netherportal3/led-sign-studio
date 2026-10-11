"""Open-Meteo forecast (free, no key) with a disk cache and stale handling, plus WMO code mapping."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

log = logging.getLogger("ledsign.ambient.weather")

USER_AGENT = "LEDSignStudio/1.0"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
REFRESH_SECONDS = 15 * 60
STALE_SECONDS = 60 * 60      # older than this is shown marked stale
RETRY_SECONDS = 2 * 60       # after a failure, wait this long before trying again
TIMEOUT = 15

# icon kinds: clear, partly, cloudy, fog, drizzle, rain, snow, storm
# word alternatives go longest first; the screen picks the longest that fits.
WMO: dict[int, tuple[str, list[str]]] = {
    0: ("clear", ["CLEAR"]),
    1: ("clear", ["MOSTLY CLEAR", "CLEAR"]),
    2: ("partly", ["PARTLY CLOUDY", "PT CLOUDY", "P CLOUDY", "CLOUDS"]),
    3: ("cloudy", ["CLOUDY"]),
    45: ("fog", ["FOG"]),
    48: ("fog", ["ICY FOG", "FOG"]),
    51: ("drizzle", ["DRIZZLE"]),
    53: ("drizzle", ["DRIZZLE"]),
    55: ("drizzle", ["DRIZZLE"]),
    56: ("drizzle", ["ICY DRIZZLE", "DRIZZLE"]),
    57: ("drizzle", ["ICY DRIZZLE", "DRIZZLE"]),
    61: ("rain", ["LIGHT RAIN", "RAIN"]),
    63: ("rain", ["RAIN"]),
    65: ("rain", ["HEAVY RAIN", "RAIN"]),
    66: ("rain", ["ICY RAIN", "ICE"]),
    67: ("rain", ["ICY RAIN", "ICE"]),
    71: ("snow", ["LIGHT SNOW", "SNOW"]),
    73: ("snow", ["SNOW"]),
    75: ("snow", ["HEAVY SNOW", "SNOW"]),
    77: ("snow", ["SNOW"]),
    80: ("rain", ["SHOWERS", "SHOWER", "RAIN"]),
    81: ("rain", ["SHOWERS", "SHOWER", "RAIN"]),
    82: ("rain", ["DOWNPOUR", "RAIN"]),
    85: ("snow", ["FLURRIES", "SNOW"]),
    86: ("snow", ["SNOW SHOWERS", "SNOW"]),
    95: ("storm", ["STORM", "T-STORM"]),
    96: ("storm", ["HAIL STORM", "STORM"]),
    99: ("storm", ["HAIL STORM", "STORM"]),
}


def condition(code: int | None, is_day: bool = True) -> dict:
    """{icon, words, word} for a WMO code. Clear by day reads SUNNY."""
    kind, words = WMO.get(int(code) if code is not None else -1, ("cloudy", ["CLOUDY"]))
    if kind == "clear" and is_day:
        words = ["MOSTLY SUNNY", "SUNNY"] if code == 1 else ["SUNNY"]
    return {"icon": kind, "words": list(words), "word": words[-1], "day": bool(is_day)}


def _get_json(url: str, timeout: float = TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2 * 1024 * 1024).decode("utf-8"))


def forecast_url(lat: float, lon: float, tz: str, units: str = "F") -> str:
    q = {
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "current": "temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m,relative_humidity_2m",
        "hourly": "temperature_2m,precipitation_probability,weather_code,is_day",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max,sunrise,sunset",
        "temperature_unit": "celsius" if units == "C" else "fahrenheit",
        "wind_speed_unit": "kmh" if units == "C" else "mph",
        "precipitation_unit": "mm" if units == "C" else "inch",
        "timezone": tz or "auto", "forecast_days": "8",
    }
    return FORECAST_URL + "?" + urllib.parse.urlencode(q)


def normalize(raw: dict) -> dict:
    """Open-Meteo JSON -> {current, hourly: [...], daily: [...], timezone, utc_offset_seconds}."""
    cur = raw.get("current") or {}
    h = raw.get("hourly") or {}
    d = raw.get("daily") or {}

    def col(src, key, i):
        v = src.get(key) or []
        return v[i] if i < len(v) else None

    hourly = [{"time": t, "temp": col(h, "temperature_2m", i), "pop": col(h, "precipitation_probability", i),
               "code": col(h, "weather_code", i), "is_day": col(h, "is_day", i)}
              for i, t in enumerate(h.get("time") or [])]
    daily = [{"date": t, "hi": col(d, "temperature_2m_max", i), "lo": col(d, "temperature_2m_min", i),
              "code": col(d, "weather_code", i), "pop": col(d, "precipitation_probability_max", i),
              "sunrise": col(d, "sunrise", i), "sunset": col(d, "sunset", i)}
             for i, t in enumerate(d.get("time") or [])]
    return {
        "current": {"time": cur.get("time"), "temp": cur.get("temperature_2m"),
                    "feels": cur.get("apparent_temperature"), "code": cur.get("weather_code"),
                    "is_day": bool(cur.get("is_day", 1)), "wind": cur.get("wind_speed_10m"),
                    "humidity": cur.get("relative_humidity_2m")},
        "hourly": hourly,
        "daily": daily,
        "timezone": raw.get("timezone"),
        "utc_offset_seconds": raw.get("utc_offset_seconds"),
    }


class WeatherCache:
    """<cache_dir>/weather_cache.json: {key, fetched_at, data}. Keeps the last good data when offline."""

    def __init__(self, cache_dir: str) -> None:
        self.path = os.path.join(cache_dir, "weather_cache.json")
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.last_attempt = 0.0
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.entry = json.load(fh)
        except (OSError, ValueError):
            self.entry = {}

    def get(self, location: dict | None, units: str = "F", now: float | None = None,
            force: bool = False) -> dict | None:
        """Fresh or cached forecast with `fetched_at`, `stale`, `error`; None when nothing is known
        (or no location is set)."""
        if not location:
            return None
        now = now or time.time()
        key = f"{location['lat']:.4f},{location['lon']:.4f},{location.get('timezone')},{units}"
        with self._lock:
            have = self.entry if self.entry.get("key") == key else {}
            age = now - float(have.get("fetched_at") or 0)
            due = force or not have or age >= REFRESH_SECONDS
            if due and (force or now - self.last_attempt >= RETRY_SECONDS or not have):
                self.last_attempt = now
                try:
                    raw = _get_json(forecast_url(location["lat"], location["lon"], location.get("timezone"), units))
                    have = {"key": key, "fetched_at": now, "data": normalize(raw)}
                    self.entry = have
                    self.last_error = None
                    self._save()
                except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                    self.last_error = f"{type(e).__name__}: {e}"[:200]
                    log.warning("weather fetch failed: %s", self.last_error)
            if not have:
                return None
            out = dict(have["data"])
            out["fetched_at"] = have["fetched_at"]
            out["stale"] = now - float(have["fetched_at"]) > STALE_SECONDS
            out["error"] = self.last_error
            return out

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.entry, fh)
        os.replace(tmp, self.path)


def summary(w: dict | None, now: datetime | None = None) -> dict:
    """The API's weather summary: {updated, stale, temp, condition, high, low}."""
    if not w:
        return {"updated": None, "stale": True, "temp": None, "condition": None, "high": None, "low": None}
    cur = w.get("current") or {}
    today = today_entry(w, now)
    cond = condition(cur.get("code"), cur.get("is_day", True))
    return {
        "updated": datetime.fromtimestamp(w["fetched_at"]).isoformat(timespec="seconds") if w.get("fetched_at") else None,
        "stale": bool(w.get("stale")),
        "temp": _r(cur.get("temp")),
        "condition": cond["words"][0].title(),
        "high": _r(today.get("hi")) if today else None,
        "low": _r(today.get("lo")) if today else None,
    }


def _r(v):
    return None if v is None else int(round(float(v)))


def today_entry(w: dict, now: datetime | None = None) -> dict | None:
    days = w.get("daily") or []
    if not days:
        return None
    key = (now or datetime.now()).strftime("%Y-%m-%d")
    for d in days:
        if d.get("date") == key:
            return d
    return days[0]


def upcoming_days(w: dict, now: datetime | None = None, n: int = 7) -> list[dict]:
    key = (now or datetime.now()).strftime("%Y-%m-%d")
    days = [d for d in (w.get("daily") or []) if (d.get("date") or "") >= key]
    return days[:n]


def next_hours(w: dict, now: datetime | None = None, n: int = 24) -> list[dict]:
    """Hourly entries from the current hour on (local times as Open-Meteo returns them)."""
    key = (now or datetime.now()).strftime("%Y-%m-%dT%H:00")
    hrs = [h for h in (w.get("hourly") or []) if (h.get("time") or "") >= key and h.get("temp") is not None]
    return hrs[:n]


def geocode(q: str, count: int = 8) -> list[dict]:
    """Open-Meteo place search. 'Springfield, IL' searches 'Springfield' and prefers matches to 'IL'."""
    q = (q or "").strip()
    if not q:
        return []
    name, _, hint = q.partition(",")
    url = GEOCODE_URL + "?" + urllib.parse.urlencode({"name": name.strip(), "count": 20, "language": "en",
                                                      "format": "json"})
    res = _get_json(url).get("results") or []
    out = [{"name": r.get("name"), "admin1": r.get("admin1"), "country": r.get("country"),
            "country_code": r.get("country_code"), "lat": r.get("latitude"), "lon": r.get("longitude"),
            "timezone": r.get("timezone")} for r in res]
    hint = hint.strip().lower()
    if hint:
        abbrev = US_STATES.get(hint.upper(), hint)

        def match(r):
            fields = [str(r.get("admin1") or "").lower(), str(r.get("country") or "").lower(),
                      str(r.get("country_code") or "").lower()]
            return 0 if any(abbrev.lower() == f or f.startswith(abbrev.lower()) for f in fields if f) else 1
        out.sort(key=match)
    return out[:count]


US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
