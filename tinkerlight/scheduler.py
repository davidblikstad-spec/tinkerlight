"""Time-of-day scheduler.

A schedule entry:
  {"id", "name", "enabled": true,
   "days": [0..6],                  # Monday = 0; empty = every day
   "when": {"type": "time", "time": "18:30"}
         | {"type": "sunrise"|"sunset", "offset": -15},   # minutes
   "date_from": "2026-12-01" | null, "date_to": "2026-12-31" | null,
   "action": {"type": "preset", "preset": "<id>", "fade": 5}
           | {"type": "blackout", "on": true}
           | {"type": "home", "fade": 3}
           | {"type": "command", "fixture": "<id>|*", "command": <index>}}
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import threading
import time

log = logging.getLogger(__name__)


# ------------------------------------------------------------ sun times -----
def sun_times(day: dt.date, lat: float, lon: float):
    """Sunrise/sunset for `day` as local naive datetimes (NOAA algorithm).
    Returns (None, None) for polar day/night."""
    n = day.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    la = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl))
              - math.tan(la) * math.tan(decl))
    if not -1 <= cos_ha <= 1:
        return None, None
    ha = math.degrees(math.acos(cos_ha))
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)

    def at(minutes):
        utc = midnight + dt.timedelta(minutes=minutes)
        return utc.astimezone().replace(tzinfo=None)   # system local time

    return at(720 - 4 * (lon + ha) - eqtime), at(720 - 4 * (lon - ha) - eqtime)


# ------------------------------------------------------------ evaluation ----
def _parse_date(s):
    return dt.date.fromisoformat(s) if s else None


def event_time(entry: dict, day: dt.date, lat: float, lon: float):
    """When `entry` fires on `day` (local naive datetime), or None."""
    days = entry.get("days") or []
    if days and day.weekday() not in days:
        return None
    df, dto = _parse_date(entry.get("date_from")), _parse_date(entry.get("date_to"))
    if df and day < df or dto and day > dto:
        return None
    w = entry.get("when", {})
    if w.get("type", "time") == "time":
        hh, mm = [int(x) for x in w.get("time", "00:00").split(":")[:2]]
        return dt.datetime(day.year, day.month, day.day, hh, mm)
    rise, sset = sun_times(day, lat, lon)
    base = rise if w["type"] == "sunrise" else sset
    if base is None:
        return None
    return (base + dt.timedelta(minutes=float(w.get("offset", 0)))).replace(second=0, microsecond=0)


def events_between(schedules, start: dt.datetime, end: dt.datetime, lat, lon):
    """All (time, entry) with start < time <= end, chronologically."""
    out = []
    day = start.date() - dt.timedelta(days=1)   # offsets can cross midnight
    while day <= end.date() + dt.timedelta(days=1):
        for e in schedules:
            if not e.get("enabled", True):
                continue
            try:
                t = event_time(e, day, lat, lon)
            except (ValueError, KeyError, TypeError):
                continue
            if t and start < t <= end:
                out.append((t, e))
        day += dt.timedelta(days=1)
    out.sort(key=lambda x: x[0])
    return out


class Scheduler:
    def __init__(self, store, engine, now=dt.datetime.now):
        self.store = store
        self.engine = engine
        self.now = now
        self.last_check = None
        self.log = []                # recent firings, newest first
        self._stop = threading.Event()
        self._thread = None

    def _loc(self):
        loc = self.store.data["settings"].get("location", {})
        return float(loc.get("lat", 0)), float(loc.get("lon", 0))

    def run_action(self, action: dict, source: str = "manual", instant: bool = False) -> None:
        kind = action.get("type")
        fade = action.get("fade")
        fade = 0.0 if instant else (None if fade in (None, "") else float(fade))
        if kind == "preset":
            p = self.store.find("presets", action.get("preset"))
            if p is None:
                raise ValueError("preset not found")
            self.engine.set_blackout(False)
            self.engine.go_preset(p, fade)          # None = preset's own fade
        elif kind == "blackout":
            self.engine.set_blackout(bool(action.get("on", True)))
        elif kind == "home":
            self.engine.home(fade or 0.0)
        elif kind == "command":
            if instant:
                return          # never replay resets/lamp commands after reboot
            fid = action.get("fixture", "*")
            for f, p in self.engine.patched():
                if fid in ("*", f["id"]):
                    cmds = p.get("commands", [])
                    idx = int(action.get("command", 0))
                    if 0 <= idx < len(cmds):
                        self.engine.run_command(f["id"], cmds[idx])
        else:
            raise ValueError("unknown action '%s'" % kind)
        self.log.insert(0, {"at": self.now().isoformat(timespec="seconds"),
                            "source": source, "action": action})
        del self.log[50:]

    def _fire(self, entry: dict, instant=False) -> None:
        try:
            self.run_action(entry["action"], ("restore: " if instant else "schedule: ") + entry.get("name", ""), instant)
        except Exception as e:
            log.warning("schedule %s failed: %s", entry.get("name"), e)

    def restore_state(self) -> None:
        """After boot / power loss: re-apply what the schedule says should be
        active now, by replaying the last week's state-setting events."""
        now = self.now()
        lat, lon = self._loc()
        events = events_between(self.store.data["schedules"],
                                now - dt.timedelta(days=7), now, lat, lon)
        look, blackout = None, None
        for _t, e in events:
            k = e["action"].get("type")
            if k in ("preset", "home"):
                look, blackout = e, None     # a preset recall clears blackout
            elif k == "blackout":
                blackout = e
        if look:
            self._fire(look, instant=True)
        if blackout:
            self._fire(blackout, instant=True)
        self.last_check = now.replace(microsecond=0)

    def tick(self) -> None:
        now = self.now().replace(microsecond=0)
        if self.last_check is None:
            self.last_check = now
            return
        if now < self.last_check or now - self.last_check > dt.timedelta(minutes=10):
            # Clock jumped (typically NTP syncing after a boot without RTC):
            # don't fire a burst of stale events, re-derive the state instead.
            log.info("clock jumped %s -> %s, restoring scheduled state", self.last_check, now)
            self.restore_state()
            self.last_check = now
            return
        lat, lon = self._loc()
        for _t, e in events_between(self.store.data["schedules"], self.last_check, now, lat, lon):
            self._fire(e)
        self.last_check = now

    def upcoming(self, hours: int = 48):
        now = self.now()
        lat, lon = self._loc()
        ev = events_between(self.store.data["schedules"], now,
                            now + dt.timedelta(hours=hours), lat, lon)
        return [{"at": t.isoformat(timespec="minutes"), "id": e["id"],
                 "name": e.get("name", ""), "action": e["action"]} for t, e in ev[:20]]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(1.0)
