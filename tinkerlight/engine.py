"""Playback engine: holds live attribute values, runs fades and feeds the
DMX output at a fixed frame rate from its own thread."""
from __future__ import annotations

import logging
import threading
import time

from . import fixtures as fx
from .outputs import NullOutput, make_output

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, store, library, clock=time.monotonic):
        self.store = store
        self.library = library
        self.clock = clock
        self.lock = threading.RLock()
        self.values = {}          # fid -> {attr: float 0..65535}
        self.fades = {}           # (fid, attr) -> (start, target, t0, duration)
        self.holds = {}           # (fid, attr) -> (value, until) for timed commands
        self.test = {}            # raw channel (1-512) -> value 0..255
        self.blackout = False
        self.last_action = "Started"
        self.active_preset = None
        self.universe = bytearray(512)
        self.output = NullOutput()
        self.output_error = None
        self.frames = 0
        self._stop = threading.Event()
        self._thread = None
        self.sync_patch()

    # -- patch ---------------------------------------------------------------
    def patched(self):
        """(fixture, profile) pairs for fixtures whose profile/mode resolve."""
        out = []
        for f in self.store.data["fixtures"]:
            try:
                p = self.library.get(f["profile"])
            except fx.ProfileError:
                continue
            if f.get("mode") in p["modes"]:
                out.append((f, p))
        return out

    def sync_patch(self) -> None:
        with self.lock:
            ids = set()
            for f, p in self.patched():
                ids.add(f["id"])
                cur = self.values.setdefault(f["id"], {})
                for attr, v in fx.defaults(p).items():
                    cur.setdefault(attr, float(v))
                for attr in list(cur):
                    if attr not in p["attributes"]:
                        del cur[attr]
            for fid in list(self.values):
                if fid not in ids:
                    del self.values[fid]
            for key in list(self.fades):
                if key[0] not in ids:
                    del self.fades[key]

    # -- output --------------------------------------------------------------
    def configure_output(self) -> None:
        cfg = self.store.data["settings"]["output"]
        with self.lock:
            try:
                self.output.close()
            except Exception:
                pass
            try:
                self.output = make_output(cfg)
                self.output_error = None
            except Exception as e:  # e.g. USB dongle not plugged in
                self.output = NullOutput()
                self.output_error = "%s: %s" % (cfg.get("type"), e)
                log.warning("output unavailable: %s", self.output_error)

    def start(self) -> None:
        self.configure_output()
        self._thread = threading.Thread(target=self._run, name="dmx", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        # No blackout here: a service restart should not blink the rig; the
        # fixture's own DMX-loss setting decides what happens if we stay down.
        with self.lock:
            try:
                self.output.close()
            except Exception:
                pass

    def _run(self) -> None:
        next_retry = 0.0
        while not self._stop.is_set():
            fps = max(1, min(44, int(self.store.data["settings"]["output"].get("fps", 40))))
            t = time.monotonic()
            data = self.render()
            try:
                self.output.send(data)
                self.frames += 1
            except Exception as e:
                self.output_error = str(e)
                if t >= next_retry:      # try to reopen a lost serial device
                    next_retry = t + 3
                    self.configure_output()
            self._stop.wait(max(0.0, 1.0 / fps - (time.monotonic() - t)))

    # -- value control -------------------------------------------------------
    def _is_snap(self, profile, attr) -> bool:
        return bool(profile["attributes"].get(attr, {}).get("snap"))

    def set_values(self, fid: str, values: dict, fade: float = 0.0) -> None:
        with self.lock:
            prof = None
            for f, p in self.patched():
                if f["id"] == fid:
                    prof = p
            if prof is None:
                return
            now = self.clock()
            cur = self.values[fid]
            for attr, target in values.items():
                if attr not in prof["attributes"]:
                    continue
                target = max(0.0, min(65535.0, float(target)))
                if fade > 0 and not self._is_snap(prof, attr):
                    self.fades[(fid, attr)] = (self._current(fid, attr, now), target, now, fade)
                else:
                    self.fades.pop((fid, attr), None)
                    cur[attr] = target

    def _current(self, fid, attr, now) -> float:
        f = self.fades.get((fid, attr))
        if not f:
            return self.values[fid][attr]
        start, target, t0, dur = f
        k = min(1.0, (now - t0) / dur)
        return start + (target - start) * k

    def go_preset(self, preset: dict, fade: float = None) -> None:
        if fade is None:
            fade = float(preset.get("fade", 0))
        with self.lock:
            vals = preset.get("values", {})
            for f, _p in self.patched():
                v = vals.get(f["id"], vals.get("*"))
                if v:
                    self.set_values(f["id"], v, fade)
            self.active_preset = preset["id"]
            self.last_action = "Preset '%s' (%.1fs)" % (preset.get("name", "?"), fade)

    def home(self, fade: float = 0.0) -> None:
        with self.lock:
            for f, p in self.patched():
                self.set_values(f["id"], fx.defaults(p), fade)
            self.active_preset = None
            self.last_action = "Home / defaults"

    def set_blackout(self, on: bool) -> None:
        with self.lock:
            self.blackout = bool(on)
            self.last_action = "Blackout on" if on else "Blackout off"

    def set_grand_master(self, level: float) -> None:
        self.store.data["settings"]["grand_master"] = max(0.0, min(1.0, float(level)))

    def run_command(self, fid: str, command: dict) -> None:
        """Hold a control value (e.g. reset) for command['hold'] seconds."""
        with self.lock:
            hold = float(command.get("hold", 5))
            self.holds[(fid, command["attr"])] = (fx.to16(command["value"]), self.clock() + hold)
            self.last_action = "Command '%s'" % command.get("label", "?")

    def snapshot(self) -> dict:
        """Current (fading) values, rounded, per fixture."""
        with self.lock:
            now = self.clock()
            return {fid: {a: int(round(self._current(fid, a, now))) for a in vals}
                    for fid, vals in self.values.items()}

    def targets(self) -> dict:
        """Where every attribute is heading (fade targets), for recording."""
        with self.lock:
            out = {fid: {a: int(round(v)) for a, v in vals.items()}
                   for fid, vals in self.values.items()}
            for (fid, attr), (_s, target, _t0, _d) in self.fades.items():
                out[fid][attr] = int(round(target))
            return out

    # -- rendering -----------------------------------------------------------
    def render(self) -> bytes:
        with self.lock:
            now = self.clock()
            for key, (start, target, t0, dur) in list(self.fades.items()):
                if now - t0 >= dur:
                    self.values[key[0]][key[1]] = target
                    del self.fades[key]
            for key, (_v, until) in list(self.holds.items()):
                if now >= until:
                    del self.holds[key]
            gm = float(self.store.data["settings"].get("grand_master", 1.0))
            uni = bytearray(512)
            for f, p in self.patched():
                fid = f["id"]
                if fid not in self.values:
                    continue
                vals = {a: self._current(fid, a, now) for a in self.values[fid]}
                for attr, spec in p["attributes"].items():
                    if spec.get("intensity") and attr in vals:
                        vals[attr] = 0.0 if self.blackout else vals[attr] * gm
                for (hfid, attr), (v, _u) in self.holds.items():
                    if hfid == fid:
                        vals[attr] = v
                fx.encode(p, f["mode"], vals, uni, int(f["address"]))
            for ch, v in self.test.items():
                if 1 <= ch <= 512:
                    uni[ch - 1] = v
            self.universe = uni
            return bytes(uni)

    def status(self) -> dict:
        return {
            "output": self.output.describe(),
            "output_error": self.output_error,
            "frames": self.frames,
            "blackout": self.blackout,
            "grand_master": self.store.data["settings"].get("grand_master", 1.0),
            "active_preset": self.active_preset,
            "last_action": self.last_action,
            "fading": len(self.fades),
            "test_channels": len(self.test),
        }
