"""Ambient mode inside the backend: the scheduler thread, uploads through the SignManager, and the
data behind the /api/ambient/* endpoints.

The scheduler thread runs only when the backend runs as the service (or with --ambient). The
endpoints work either way (settings, preview, status).
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import threading
import time
from datetime import datetime

from . import news as N
from . import rotation as R
from . import scheduler as SC
from . import screens as S
from . import settings as ST
from . import weather as W

log = logging.getLogger("ledsign.ambient")

PANEL = (64, 16)
NEWS_TOP_N = 10
# The size budget of one upload. Clock off: the looping GIF holds as many headlines as fit. Clock
# on: still icons when over; uploads are never closer than refresh_minutes either way.
MAX_PAYLOAD = 160_000
# The scheduler wakes this long before an upload has to start: fresh data and the build.
PREP_SECONDS = 20


def iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


class AmbientController:
    def __init__(self, data_dir: str, mgr, *, run_scheduler: bool, fake: bool = False) -> None:
        self.dir = os.path.join(data_dir, "ambient")
        os.makedirs(self.dir, exist_ok=True)
        self.mgr = mgr
        self.fake = fake
        self.run_scheduler = run_scheduler
        self.settings = ST.AmbientSettings(os.path.join(self.dir, "settings.json"))
        self.weather = W.WeatherCache(self.dir)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._force = False
        self._busy = False
        self._thread: threading.Thread | None = None
        self.state_path = os.path.join(self.dir, "state.json")
        d = self._load_state()
        self.sched = SC.SchedState.from_dict(d.get("sched"))
        self.rot = R.RotationState.from_dict(d.get("rotation"))
        self.brightness_mode: str | None = d.get("brightness_mode")
        self.last: dict = d.get("last") or {}
        self.last_error: str | None = d.get("last_error")
        # 0 = full, 1 or 2 = still icons. Raised when the sign
        # answers "storage full"; stays until the settings change.
        self.lite = int(d.get("lite") or 0)
        self.decision: SC.Decision | None = None
        self.headlines: list = []
        self.news_status: dict = {}
        self.news_updated: float | None = None
        self._news_checked = 0.0
        self.last_plan: dict | None = None

    # -- persistence --------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        with self._lock:
            d = {"sched": self.sched.to_dict(), "rotation": self.rot.to_dict(),
                 "brightness_mode": self.brightness_mode, "last": self.last, "last_error": self.last_error,
                 "lite": self.lite}
        tmp = f"{self.state_path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh)
            os.replace(tmp, self.state_path)
        except OSError:
            log.exception("could not save ambient state")

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        if not self.run_scheduler or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="ambient", daemon=True)
        self._thread.start()
        log.info("ambient scheduler started (settings %s)", self.settings.path)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        time.sleep(1.0)  # let the connection manager start scanning first
        while not self._stop.is_set():
            wait = 30.0
            try:
                wait = self._tick()
            except Exception:  # noqa: BLE001
                log.exception("ambient tick failed")
                wait = 30.0
            self._wake.wait(max(1.0, min(30.0, wait)))
            self._wake.clear()

    # -- the scheduler --------------------------------------------------------------

    def _tick(self) -> float:
        cfg = self.settings.get()
        now = datetime.now().astimezone()
        with self._lock:
            force, self._force = self._force, False
            d = SC.decide(now, cfg, self.sched)
            if force and d.mode in ("day", "night"):
                d.action = d.mode
                d.reason = "run now"
            prev = self.decision
            self.decision = d
        if prev is None or (prev.mode, prev.action, prev.reason) != (d.mode, d.action, d.reason) or d.action != "none":
            log.info("ambient: mode=%s action=%s reason=%s next_upload=%s", d.mode, d.action, d.reason,
                     iso(d.next_upload))
        if d.action == "day":
            self._do_day(cfg, force=force)
        elif d.action == "night":
            self._do_night(cfg)
        else:
            self._refresh_data(cfg, light=d.mode != "day")
        return max(1.0, d.next_check - time.time())

    def _refresh_data(self, cfg: dict, light: bool = False, force: bool = False) -> tuple[dict | None, list]:
        """Weather and news when due (their caches decide); at night only what the status shows."""
        wx = self.weather.get(cfg["location"], cfg["units"])
        if light and self.news_updated and time.time() - self.news_updated < 3600:
            return wx, self.headlines
        if not force and time.time() - self._news_checked < 120:
            return wx, self.headlines
        self._news_checked = time.time()
        try:
            res = N.get_headlines(self.dir, cfg["news_sources"], top_n=NEWS_TOP_N)
            with self._lock:
                self.headlines = res["headlines"]
                self.news_status = res["status"]
                self.news_updated = max([s.get("fetched_at") or 0 for s in res["status"].values()
                                         if s.get("ok")] or [0]) or None
        except Exception:  # noqa: BLE001
            log.exception("news refresh failed")
        return wx, self.headlines

    def _set_brightness(self, level: int, mode: str) -> None:
        if self.brightness_mode == mode:
            return
        if mode == "day":
            # The display may have been switched off by hand overnight: make sure it is on.
            try:
                self.mgr.power(True)
            except Exception as e:  # noqa: BLE001
                log.warning("ambient: display on failed: %s", getattr(e, "message", e))
        try:
            self.mgr.brightness(int(level))
            self.brightness_mode = mode
            log.info("ambient: brightness %d for %s", level, mode)
        except Exception as e:  # noqa: BLE001
            log.warning("ambient: brightness failed: %s", getattr(e, "message", e))

    def _upload(self, job: str, fn, nbytes: int) -> tuple[bool, float, str | None]:
        from ..signmgr import SignError
        t0 = time.monotonic()
        try:
            self.mgr.upload(job, fn, nbytes, PANEL)
            return True, time.monotonic() - t0, None
        except SignError as e:
            return False, time.monotonic() - t0, f"{e.code}: {e.message}"
        except Exception as e:  # noqa: BLE001
            log.exception("ambient upload failed")
            return False, time.monotonic() - t0, f"error: {e}"

    def _check_panel(self) -> str | None:
        size = tuple(self.mgr.panel_size())
        if size != PANEL:
            return f"Ambient mode needs a 64x16 sign; this one is {size[0]}x{size[1]}."
        return None

    def _plan_switch(self, st: R.RotationState, payload_guess: int, force: bool, coordinated: bool,
                     clock_mode: bool) -> tuple[float, dict | None, float, list[float]]:
        """(when the upload should complete, how the new GIF continues the old one, est upload s,
        later moments it may complete instead if the link is too slow for the first)."""
        est = st.estimate_upload(payload_guess)
        now = time.time()
        earliest = now + st.last_build_seconds + st.estimate_upload_slow(payload_guess) + 1.0
        # a loop always starts at its own wrap point, so it may only take over between headlines
        allow = None if clock_mode else {"loop", "tail", "now", "today", "week"}
        pick = R.choose_switch(st, now, earliest, est, force=force, allow=allow) if coordinated else None
        at = pick["at"] if pick else earliest
        cont = pick["cont"] if pick else None
        later = list(pick.get("later") or []) if pick else []
        if cont and (cont["kind"] == "clock") != clock_mode:
            cont = None  # the clock was switched on or off since the last upload: start fresh
        if not clock_mode:
            cont = None
        if clock_mode and cont is None:
            at = R.safe_clock_start(datetime.fromtimestamp(at).astimezone()).timestamp()
            later = []
        return at, cont, est, later

    def _do_day(self, cfg: dict, force: bool = False) -> None:
        self._busy = True
        try:
            err = self._check_panel()
            wx, heads = self._refresh_data(cfg, force=True)
            if err:
                self._record("day", False, 0, 0, err)
                return
            self._set_brightness(cfg["day_brightness"], "day")
            build_cfg = dict(cfg)
            span = cfg["refresh_minutes"] * 60   # never upload more often than this
            if self.lite >= 1:
                build_cfg.update(animate_icons=False)
            with self._lock:
                base = R.RotationState.from_dict(self.rot.to_dict())
                coordinated = self.sched.last_kind == "day"
            kinds = R.cycle_kinds(build_cfg, wx is not None, bool(heads))
            clock_mode = bool(build_cfg["screens"]["clock"]["on"]) or not kinds
            guess = base.last_payload_bytes
            for attempt in range(4):
                st = R.RotationState.from_dict(base.to_dict())
                at, cont, est, later = self._plan_switch(st, guess, force, coordinated, clock_mode)
                start = datetime.fromtimestamp(at).astimezone()
                hold = R.news_hold_ms(est)
                now = datetime.now().astimezone()
                if clock_mode:
                    rot = R.build_rotation(now, build_cfg, wx, heads, st, start=start, span_s=span, cont=cont,
                                           hold_ms=hold)
                else:
                    rot = R.build_loop(now, build_cfg, wx, heads, st, start=start, span_s=span,
                                       budget_bytes=MAX_PAYLOAD)
                est_real = st.estimate_upload(rot.payload_bytes)
                begin = at - st.estimate_upload_slow(rot.payload_bytes)
                if attempt == 3:
                    break
                if clock_mode and rot.payload_bytes > MAX_PAYLOAD and build_cfg.get("animate_icons", True):
                    build_cfg.update(animate_icons=False)
                    log.info("ambient: %d B is over %d B, trying still icons", rot.payload_bytes, MAX_PAYLOAD)
                    continue
                if begin < time.time() - 0.5 or (clock_mode and R.news_hold_ms(est_real) > hold + 500):
                    guess = rot.payload_bytes    # bigger than the guess: aim later, hold longer
                    continue
                break
            if rot.payload_bytes > MAX_PAYLOAD:
                # one refresh interval of this content is over the budget: the interval stays (no
                # faster uploads), the upload is just bigger
                log.warning("ambient: %d B is over the %d B budget for %d min; keeping uploads %d min apart",
                            rot.payload_bytes, MAX_PAYLOAD, cfg["refresh_minutes"], cfg["refresh_minutes"])
            self.last_plan = rot.summary()
            prim = rot.primary
            log.info("ambient: built %d B GIF, %d frames, %.0f s %s from %s (%s), news %.0f px/s, %d headlines, "
                     "handover %s at %s, est upload %.1f s, build %.2f s", len(rot.gif), rot.frames,
                     rot.total_ms / 1000, "loop" if rot.loop else "content", rot.start.strftime("%H:%M:%S"),
                     f"continues the {cont['kind']}" if cont else "fresh",
                     rot.motion.px_per_s if rot.motion else 0,
                     sum(len(s.items) for s in rot.segments if s.kind == "news"),
                     prim["kind"] if prim else "-",
                     datetime.fromtimestamp(prim["w0"]).strftime("%H:%M:%S") if prim else "-",
                     rot.upload_seconds, rot.build_seconds)
            wait = begin - time.time()
            if wait > 0:
                log.info("ambient: upload starts in %.1f s so it completes at %s", wait,
                         rot.start.strftime("%H:%M:%S"))
                if self._stop.wait(wait):
                    return
                with self._lock:
                    if self.sched.last_kind == "manual" or (self.sched.paused_until or 0) > time.time():
                        log.info("ambient: a manual send came first; this upload is dropped")
                        return
            gif = rot.gif
            to_mono = time.monotonic() - time.time()
            finish_at = [rot.start.timestamp() + to_mono] + [t + to_mono for t in later]
            paced = {"s": 0.0}

            async def send(sign, progress):
                try:
                    return await sign.send_gif(gif, progress=progress, finish_at=finish_at)
                finally:
                    paced["s"] = float(getattr(sign, "last_paced_seconds", 0.0) or 0.0)
            started = time.time()
            ok, secs, err = self._upload("ambient", send, rot.payload_bytes)
            if ok:
                drift = (started + secs) - rot.start.timestamp()
                if rot.loop and later and drift > 0:
                    # a slow link made a later wrap point instead: the loop is just that much further on
                    period = (rot.windows[0].get("period") or 0) if rot.windows else 0
                    if period and drift > period / 2:
                        log.info("ambient: the link was slow; the switch used a later wrap point")
                st.drift = round(drift, 2)
                st.learn_upload(rot.payload_bytes, secs - paced["s"])
                with self._lock:
                    self.rot = st
                log.info("ambient: uploaded in %.1f s (%.1f s of it paced; link alone %.1f s, estimated %.1f s), "
                         "content start off by %+.1f s", secs, paced["s"], secs - paced["s"], rot.upload_seconds,
                         drift)
            if not ok and err and err.startswith("storage_full") and self.lite < 2:
                self.lite += 1
                log.warning("ambient: the sign is out of space; next rotations use still icons (level %d)",
                            self.lite)
            self._record("day", ok, rot.payload_bytes, secs, err, started)
            if ok:
                est_next = st.estimate_upload(st.last_payload_bytes)
                nxt = R.choose_switch(st, time.time(), time.time(), est_next)
                if nxt:
                    due = (nxt["at"] - st.estimate_upload_slow(st.last_payload_bytes) - st.last_build_seconds
                           - PREP_SECONDS)
                    with self._lock:
                        self.sched.next_due_at = due
                    self._save_state()
                    log.info("ambient: next upload prepares at %s for the %s handover at %s",
                             datetime.fromtimestamp(due).strftime("%H:%M:%S"), nxt["kind"],
                             datetime.fromtimestamp(nxt["at"]).strftime("%H:%M:%S"))
        finally:
            self._busy = False

    def _do_night(self, cfg: dict) -> None:
        self._busy = True
        try:
            err = self._check_panel()
            if err:
                self._record("night", False, 0, 0, err)
                return
            frame = R.night_still(cfg.get("night_text", ""))
            import iledsign
            nbytes = len(iledsign.program_payload(PANEL[0], PANEL[1], [frame]))
            started = time.time()
            ok, secs, err = self._upload("ambient_night", lambda s, p: s.send_program([frame], progress=p), nbytes)
            if ok:
                self._set_brightness(cfg["night_brightness"], "night")
                log.info("ambient: night screen (%s) uploaded", "black" if not cfg.get("night_text") else "dim text")
            self._record("night", ok, nbytes, secs, err, started)
        finally:
            self._busy = False

    def _record(self, kind: str, ok: bool, nbytes: int, secs: float, err: str | None,
                started: float | None = None) -> None:
        started = started or time.time()
        with self._lock:
            SC.record_upload(self.sched, kind, ok, started)
            self.last = {"at": started, "kind": kind, "ok": ok, "bytes": nbytes, "seconds": round(secs, 1)}
            self.last_error = None if ok else err
        if not ok:
            log.warning("ambient: %s upload failed: %s", kind, err)
        self._save_state()

    # -- API ------------------------------------------------------------------------

    def get_settings(self) -> dict:
        return self.settings.get()

    def update_settings(self, patch: dict) -> dict:
        before = self.settings.get()
        after = self.settings.update(patch)
        with self._lock:
            self.lite = 0
            if before["day_brightness"] != after["day_brightness"] and self.brightness_mode == "day":
                self.brightness_mode = None
            if before["night_brightness"] != after["night_brightness"] and self.brightness_mode == "night":
                self.brightness_mode = None
            if before["night_text"] != after["night_text"] and self.sched.last_kind == "night":
                self.sched.last_kind = None  # show the new night screen
        self._save_state()
        self.wake()
        log.info("ambient settings changed: %s", sorted(k for k in after if after[k] != before.get(k)))
        return after

    def on_manual_send(self) -> None:
        cfg = self.settings.get()
        with self._lock:
            SC.record_manual(self.sched, time.time(), cfg["pause_minutes_on_manual"])
        self._save_state()
        log.info("ambient: manual send, paused for %d min", cfg["pause_minutes_on_manual"])

    def pause(self, minutes: float | None) -> dict:
        cfg = self.settings.get()
        try:
            m = float(minutes if minutes is not None else cfg["pause_minutes_on_manual"] or 30)
        except (TypeError, ValueError):
            m = 30.0
        m = max(1.0, min(24 * 60.0, m))
        with self._lock:
            SC.pause(self.sched, time.time(), m)
        self._save_state()
        self.wake()
        log.info("ambient: paused for %.0f min", m)
        return self.status()

    def resume(self) -> dict:
        with self._lock:
            SC.resume(self.sched)
        self._save_state()
        self.wake()
        log.info("ambient: resumed")
        return self.status()

    def run_now(self) -> dict:
        with self._lock:
            self.sched.paused_until = None
            self.sched.last_attempt_at = None
            self._force = True
        self._save_state()
        self.wake()
        log.info("ambient: run now")
        return self.status()

    def preview(self, minutes: float | None = 1.0) -> dict:
        cfg = self.settings.get()
        try:
            m = float(minutes if minutes is not None else 1.0)
        except (TypeError, ValueError):
            m = 1.0
        m = max(0.5, min(10.0, m))
        wx = self.weather.get(cfg["location"], cfg["units"])
        heads = self.headlines
        if not heads:
            try:
                heads = N.get_headlines(self.dir, cfg["news_sources"], top_n=NEWS_TOP_N)["headlines"]
            except Exception:  # noqa: BLE001
                log.exception("preview: news failed")
                heads = []
        now = datetime.now().astimezone()
        with self._lock:
            st = R.RotationState.from_dict(self.rot.to_dict())
        rot = R.build_rotation(now, cfg, wx, heads, st, minutes=m, color_scale=1.0, start=now, handover=False)
        return {"gif": base64.b64encode(rot.gif).decode("ascii"), "bytes": len(rot.gif), "frames": rot.frames,
                "seconds": round(rot.total_ms / 1000, 2),
                "segments": [{"kind": s["kind"], "ms": s["ms"]} for s in rot.summary()["segments"]]}

    def status(self) -> dict:
        cfg = self.settings.get()
        now = datetime.now().astimezone()
        with self._lock:
            d = SC.decide(now, cfg, copy.deepcopy(self.sched))
            mode = d.mode if self.run_scheduler else "off"
            last = dict(self.last)
            heads = list(self.headlines)
            news_status = dict(self.news_status)
            next_upload = d.next_upload if mode in ("day", "night", "paused") else None
            if mode == "night" and self.sched.last_kind == "night":
                next_upload = d.next_upload  # the 08:00 start
            out = {
                "mode": mode,
                "enabled": cfg["enabled"],
                "location_set": bool(cfg["location"]),
                "running": bool(self.run_scheduler),
                "busy": self._busy,
                "paused_until": iso(self.sched.paused_until) if mode == "paused" else None,
                "last_upload": iso(last.get("at")),
                "last_upload_kind": last.get("kind"),
                "last_upload_ok": last.get("ok"),
                "last_upload_bytes": last.get("bytes"),
                "last_upload_seconds": last.get("seconds"),
                "next_upload": iso(next_upload),
                "last_error": self.last_error,
                "reason": d.reason,
            }
        wx = self.weather.entry.get("data") if self.weather.entry else None
        if wx:
            wx = dict(wx, fetched_at=self.weather.entry.get("fetched_at"),
                      stale=time.time() - float(self.weather.entry.get("fetched_at") or 0) > W.STALE_SECONDS)
        out["weather"] = W.summary(wx, now.replace(tzinfo=None))
        out["headlines"] = [{"source": h.tag, "title": h.title, "published": iso(h.published),
                             "outlets": h.outlets} for h in heads]
        out["news_updated"] = iso(self.news_updated)
        feeds = []
        for sid, meta in N.SOURCES.items():
            stt = news_status.get(sid) or {}
            feeds.append({"id": sid, "name": meta["name"], "enabled": bool(cfg["news_sources"].get(sid)),
                          "ok": stt.get("ok"), "last_error": stt.get("error")})
        out["feeds"] = feeds
        out["upload_rate"] = round(self.rot.bytes_per_second)
        out["news_px_per_s"] = round(S.news_motion(cfg.get("news_speed")).px_per_s, 1)
        out["lite"] = self.lite
        out["last_plan"] = ({k: self.last_plan.get(k) for k in ("bytes", "frames", "seconds", "start", "end",
                                                                 "upload_seconds", "handover", "news_px_per_s", "loop")}
                            if self.last_plan else None)
        return out

    def geocode(self, q: str) -> list[dict]:
        return [{k: r.get(k) for k in ("name", "admin1", "country", "lat", "lon", "timezone")}
                for r in W.geocode(q)]
