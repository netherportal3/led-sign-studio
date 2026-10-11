"""Pure day/night/pause decisions for ambient mode (no I/O, no clock reads: pass `now`).

    decide(now, cfg, state) -> Decision(mode, action, next_check, next_upload, reason)

mode:   "off" (disabled, or no location picked yet), "paused" (a manual send or the Pause button),
        "day", "night"
action: "none", "day" (build and upload a rotation GIF), "night" (upload the night screen)

Rules:
- Off until a location is set: nothing is uploaded without one.
- Day = active_start <= local time < active_end (a window may wrap past midnight; equal = always day).
- Day: upload when the sign is not showing a day rotation yet, or when the rotation on the sign asks
  for the next one (`next_due_at`, timed so the switch lands on its handover window), or else
  refresh_minutes after the last one.
- Night: upload the night screen once, then nothing until the window opens again.
- Paused: nothing until paused_until. When it ends, the next decision uploads again right away
  (the sign shows the manual content until then).
- A failed upload is retried after RETRY_SECONDS; attempts are never closer than MIN_GAP_SECONDS.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

RETRY_SECONDS = 60
# A guard against runaway loops only: the service times each upload to a handover window, and with
# news alone those come about every 75 s (the decision comes about 20 s before the upload).
MIN_GAP_SECONDS = 30
IDLE_CHECK_SECONDS = 30
NO_LOCATION = "no location set"


@dataclass
class SchedState:
    last_kind: str | None = None        # "day", "night", "manual" or None (nothing sent yet)
    last_upload_at: float | None = None  # epoch of the last successful ambient upload (its start)
    last_attempt_at: float | None = None
    last_ok: bool = True
    paused_until: float | None = None
    next_due_at: float | None = None     # when to start preparing the next day upload (set by the service)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "SchedState":
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s


@dataclass
class Decision:
    mode: str
    action: str
    next_check: float
    next_upload: float | None
    reason: str


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_active(now: datetime, start: str, end: str) -> bool:
    s, e = _minutes(start), _minutes(end)
    t = now.hour * 60 + now.minute
    if s == e:
        return True
    if s < e:
        return s <= t < e
    return t >= s or t < e  # wraps past midnight


def next_boundary(now: datetime, hhmm: str) -> datetime:
    """The next local time (strictly after now) whose clock reads hhmm."""
    m = _minutes(hhmm)
    cand = now.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand


def decide(now: datetime, cfg: dict, st: SchedState) -> Decision:
    t = now.timestamp()
    if not cfg.get("enabled", True):
        return Decision("off", "none", t + IDLE_CHECK_SECONDS, None, "ambient mode is off")
    if not cfg.get("location"):
        return Decision("off", "none", t + IDLE_CHECK_SECONDS, None, NO_LOCATION)

    start, end = cfg.get("active_start", "08:00"), cfg.get("active_end", "22:00")
    active = is_active(now, start, end)
    refresh = max(1, int(cfg.get("refresh_minutes", 5))) * 60

    if st.paused_until and t < st.paused_until:
        nxt = st.paused_until
        return Decision("paused", "none", min(nxt, t + IDLE_CHECK_SECONDS), nxt, "paused")

    # retry pacing after a failure, and never two uploads within MIN_GAP_SECONDS
    earliest = 0.0
    if st.last_attempt_at:
        earliest = st.last_attempt_at + (RETRY_SECONDS if not st.last_ok else MIN_GAP_SECONDS)

    if active:
        if st.last_kind != "day" or not st.last_upload_at:
            due_at = t
            reason = "start of day" if st.last_kind in (None, "night") else "resume after a manual send"
        else:
            due_at = st.next_due_at or (st.last_upload_at + refresh)
            reason = "refresh"
        if not st.last_ok:
            reason = "retry"
        due_at = max(due_at, earliest)
        if due_at <= t:
            return Decision("day", "day", t + IDLE_CHECK_SECONDS, t, reason)
        return Decision("day", "none", min(due_at, t + IDLE_CHECK_SECONDS), due_at, "waiting")

    # night
    day_at = next_boundary(now, start).timestamp()
    if st.last_kind != "night":
        due_at = max(t, earliest)
        if due_at <= t:
            return Decision("night", "night", t + IDLE_CHECK_SECONDS, day_at, "night screen")
        return Decision("night", "none", min(due_at, t + IDLE_CHECK_SECONDS), due_at, "night screen retry")
    return Decision("night", "none", min(day_at, t + IDLE_CHECK_SECONDS), day_at, "night")


def record_upload(st: SchedState, kind: str, ok: bool, at: float) -> SchedState:
    """Update the state after an upload attempt of `kind` ("day" or "night")."""
    st.last_attempt_at = at
    st.last_ok = ok
    if ok:
        st.last_kind = kind
        st.last_upload_at = at
        st.next_due_at = None
    return st


def record_manual(st: SchedState, at: float, pause_minutes: int) -> SchedState:
    """A manual send from the app: the sign shows the user's content; pause ambient."""
    st.last_kind = "manual"
    st.paused_until = at + pause_minutes * 60 if pause_minutes > 0 else None
    return st


def pause(st: SchedState, at: float, minutes: float) -> SchedState:
    st.paused_until = at + max(0.0, minutes) * 60
    return st


def resume(st: SchedState) -> SchedState:
    st.paused_until = None
    if st.last_kind == "day":
        # make the next decision upload right away (the sign may show something else now)
        st.last_kind = "manual"
    return st
