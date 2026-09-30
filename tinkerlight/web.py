"""Small HTTP server: JSON API + static single-page admin UI.

Auth is HTTP Basic against a PBKDF2 hash in the show file. State-changing
requests must carry the X-Tinkerlight header, which a foreign web page cannot
add without a CORS preflight that this server never approves (CSRF guard).
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import fixtures as fx
from .camera import CameraError
from .store import check_password, hash_password

log = logging.getLogger(__name__)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
MAX_BODY = 2 * 1024 * 1024


class RawResponse:
    """A non-JSON API result (e.g. a JPEG)."""

    def __init__(self, body, ctype, headers=None):
        self.body, self.ctype, self.headers = body, ctype, headers or {}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def validate_patch(fixtures, library):
    used = {}
    ids = set()
    for f in fixtures:
        for key in ("id", "name", "profile", "mode", "address"):
            if f.get(key) in (None, ""):
                raise ApiError(400, "fixture is missing '%s'" % key)
        if f["id"] in ids:
            raise ApiError(400, "duplicate fixture id %s" % f["id"])
        ids.add(f["id"])
        try:
            p = library.get(f["profile"])
        except fx.ProfileError as e:
            raise ApiError(400, str(e))
        if f["mode"] not in p["modes"]:
            raise ApiError(400, "%s: unknown mode %s" % (f["name"], f["mode"]))
        a = int(f["address"])
        f["address"] = a
        end = a + fx.footprint(p, f["mode"]) - 1
        if a < 1 or end > 512:
            raise ApiError(400, "%s: address %d-%d does not fit in 1-512" % (f["name"], a, end))
        for ch in range(a, end + 1):
            if ch in used:
                raise ApiError(400, "%s overlaps %s at channel %d" % (f["name"], used[ch], ch))
            used[ch] = f["name"]


class App:
    """Route table + handlers, independent of the HTTP plumbing (testable)."""

    def __init__(self, store, library, engine, scheduler, camera=None):
        self.store, self.library = store, library
        self.engine, self.scheduler = engine, scheduler
        self.camera = camera
        self.routes = [
            ("GET", r"/api/state", self.get_state),
            ("GET", r"/api/show", self.get_show),
            ("GET", r"/api/camera\.jpg", self.get_camera),
            ("GET", r"/api/profiles", self.get_profiles),
            ("PUT", r"/api/profiles/(?P<pid>[\w-]+)", self.put_profile),
            ("POST", r"/api/profiles/(?P<pid>[\w-]+)/revert", self.revert_profile),
            ("POST", r"/api/live", self.post_live),
            ("POST", r"/api/blackout", self.post_blackout),
            ("POST", r"/api/master", self.post_master),
            ("POST", r"/api/home", self.post_home),
            ("POST", r"/api/command", self.post_command),
            ("POST", r"/api/presets/record", self.record_preset),
            ("POST", r"/api/presets/order", self.order_presets),
            ("PUT", r"/api/presets/(?P<pid>\w+)", self.put_preset),
            ("DELETE", r"/api/presets/(?P<pid>\w+)", self.delete_preset),
            ("POST", r"/api/presets/(?P<pid>\w+)/go", self.go_preset),
            ("POST", r"/api/schedules", self.save_schedule),
            ("DELETE", r"/api/schedules/(?P<sid>\w+)", self.delete_schedule),
            ("POST", r"/api/schedules/(?P<sid>\w+)/run", self.run_schedule),
            ("PUT", r"/api/fixtures", self.put_fixtures),
            ("PUT", r"/api/settings", self.put_settings),
            ("POST", r"/api/password", self.post_password),
            ("POST", r"/api/test", self.post_test),
            ("GET", r"/api/backup", self.get_backup),
            ("POST", r"/api/restore", self.post_restore),
        ]

    def dispatch(self, method, path, body):
        for m, pattern, fn in self.routes:
            if m == method:
                match = re.fullmatch(pattern, path)
                if match:
                    return fn(body or {}, **match.groupdict())
        raise ApiError(404, "not found")

    # -- read ----------------------------------------------------------------
    def get_state(self, body):
        s = self.store.data["settings"]
        loc = s.get("location", {})
        from .scheduler import sun_times
        rise, sset = sun_times(dt.date.today(), float(loc.get("lat", 0)), float(loc.get("lon", 0)))
        return {
            "time": dt.datetime.now().isoformat(timespec="seconds"),
            "status": self.engine.status(),
            "values": self.engine.snapshot(),
            "dmx": list(self.engine.universe),
            "upcoming": self.scheduler.upcoming(),
            "log": self.scheduler.log[:15],
            "sunrise": rise.strftime("%H:%M") if rise else None,
            "sunset": sset.strftime("%H:%M") if sset else None,
            "default_password": bool(self.store.data["auth"].get("default")),
            "camera": self.camera.status() if self.camera else {"enabled": False},
        }

    def get_camera(self, body):
        if not self.camera:
            raise ApiError(404, "no camera")
        try:
            jpeg, taken = self.camera.snapshot()
        except CameraError as e:
            raise ApiError(503, str(e))
        return RawResponse(jpeg, "image/jpeg",
                           {"X-Taken": dt.datetime.fromtimestamp(taken).isoformat(timespec="seconds")})

    def get_show(self, body):
        d = self.store.export()
        d["user"] = self.store.data["auth"].get("user")
        return d

    def get_profiles(self, body):
        return self.library.profiles

    # -- profiles ------------------------------------------------------------
    def put_profile(self, body, pid):
        body["id"] = pid
        try:
            self.library.save(body)
        except fx.ProfileError as e:
            raise ApiError(400, str(e))
        self._patch_changed()
        return {"ok": True}

    def revert_profile(self, body, pid):
        self.library.revert(pid)
        self._patch_changed()
        return {"ok": True}

    def _patch_changed(self):
        try:
            validate_patch([dict(f) for f in self.store.data["fixtures"]], self.library)
        except ApiError as e:
            log.warning("patch no longer valid: %s", e)
        self.engine.sync_patch()

    # -- live control --------------------------------------------------------
    def _targets(self, ids):
        known = [f["id"] for f, _p in self.engine.patched()]
        if not ids:
            return known
        return [i for i in ids if i in known]

    def post_live(self, body):
        values = {k: float(v) for k, v in (body.get("values") or {}).items()}
        fade = float(body.get("fade", 0) or 0)
        for fid in self._targets(body.get("fixtures")):
            self.engine.set_values(fid, values, fade)
        self.engine.active_preset = None
        return {"ok": True}

    def post_blackout(self, body):
        self.engine.set_blackout(bool(body.get("on", True)))
        return {"ok": True}

    def post_master(self, body):
        self.engine.set_grand_master(float(body.get("level", 1)))
        self.store.save()
        return {"ok": True}

    def post_home(self, body):
        self.engine.home(float(body.get("fade", 0) or 0))
        return {"ok": True}

    def post_command(self, body):
        idx = int(body.get("index", -1))
        for f, p in self.engine.patched():
            if f["id"] in self._targets(body.get("fixtures")):
                cmds = p.get("commands", [])
                if not 0 <= idx < len(cmds):
                    raise ApiError(400, "no such command")
                self.engine.run_command(f["id"], cmds[idx])
        return {"ok": True}

    def post_test(self, body):
        if body.get("clear"):
            self.engine.test.clear()
        else:
            ch, v = int(body["channel"]), int(body["value"])
            if not 1 <= ch <= 512:
                raise ApiError(400, "channel must be 1-512")
            if v < 0:
                self.engine.test.pop(ch, None)
            else:
                self.engine.test[ch] = min(255, v)
        return {"ok": True, "test": self.engine.test}

    # -- presets -------------------------------------------------------------
    def record_preset(self, body):
        name = (body.get("name") or "").strip()
        if not name:
            raise ApiError(400, "name required")
        snap = self.engine.targets()
        groups = body.get("groups")        # optional: only record these attribute groups
        values = {}
        for f, p in self.engine.patched():
            if f["id"] not in self._targets(body.get("fixtures")):
                continue
            vals = snap.get(f["id"], {})
            if groups:
                vals = {a: v for a, v in vals.items()
                        if p["attributes"][a].get("group") in groups}
            values[f["id"]] = vals
        existing = self.store.find("presets", body.get("id")) if body.get("id") else None
        preset = dict(existing or {})
        preset.update({"id": body.get("id"), "name": name,
                       "fade": float(body.get("fade", preset.get("fade", 2))),
                       "color": body.get("color", preset.get("color", "#888888")),
                       "values": values})
        return self.store.upsert("presets", preset, "p")

    def put_preset(self, body, pid):
        p = self.store.find("presets", pid)
        if not p:
            raise ApiError(404, "preset not found")
        p = dict(p)
        for key in ("name", "fade", "color", "values"):
            if key in body:
                p[key] = body[key]
        p["fade"] = float(p.get("fade", 0))
        return self.store.upsert("presets", p, "p")

    def order_presets(self, body):
        order = {pid: i for i, pid in enumerate(body.get("ids", []))}
        with self.store.lock:
            self.store.data["presets"].sort(key=lambda p: order.get(p["id"], 1e9))
            self.store.save()
        return {"ok": True}

    def delete_preset(self, body, pid):
        if not self.store.delete("presets", pid):
            raise ApiError(404, "preset not found")
        return {"ok": True}

    def go_preset(self, body, pid):
        p = self.store.find("presets", pid)
        if not p:
            raise ApiError(404, "preset not found")
        fade = body.get("fade")
        self.scheduler.run_action({"type": "preset", "preset": pid, "fade": fade}, "manual")
        return {"ok": True}

    # -- schedules -----------------------------------------------------------
    def save_schedule(self, body):
        from .scheduler import event_time
        entry = {
            "id": body.get("id"),
            "name": (body.get("name") or "Untitled").strip(),
            "enabled": bool(body.get("enabled", True)),
            "days": sorted({int(d) for d in body.get("days", []) if 0 <= int(d) <= 6}),
            "when": body.get("when") or {"type": "time", "time": "18:00"},
            "date_from": body.get("date_from") or None,
            "date_to": body.get("date_to") or None,
            "action": body.get("action") or {"type": "blackout", "on": True},
        }
        if entry["when"].get("type") not in ("time", "sunrise", "sunset"):
            raise ApiError(400, "when.type must be time, sunrise or sunset")
        if entry["action"].get("type") not in ("preset", "blackout", "home", "command"):
            raise ApiError(400, "unknown action type")
        if entry["action"]["type"] == "preset" and \
                not self.store.find("presets", entry["action"].get("preset")):
            raise ApiError(400, "choose a preset")
        try:
            event_time(dict(entry, days=[], date_from=None, date_to=None),
                       dt.date.today(), 0.0, 0.0)
            for key in ("date_from", "date_to"):
                if entry[key]:
                    dt.date.fromisoformat(entry[key])
        except (ValueError, KeyError) as e:
            raise ApiError(400, "invalid time/date: %s" % e)
        return self.store.upsert("schedules", entry, "s")

    def delete_schedule(self, body, sid):
        if not self.store.delete("schedules", sid):
            raise ApiError(404, "schedule not found")
        return {"ok": True}

    def run_schedule(self, body, sid):
        e = self.store.find("schedules", sid)
        if not e:
            raise ApiError(404, "schedule not found")
        try:
            self.scheduler.run_action(e["action"], "manual: " + e.get("name", ""))
        except ValueError as ex:
            raise ApiError(400, str(ex))
        return {"ok": True}

    # -- admin ---------------------------------------------------------------
    def put_fixtures(self, body):
        fixtures = body.get("fixtures")
        if not isinstance(fixtures, list):
            raise ApiError(400, "fixtures list required")
        validate_patch(fixtures, self.library)
        with self.store.lock:
            self.store.data["fixtures"] = fixtures
            self.store.save()
        self.engine.sync_patch()
        return {"ok": True}

    def put_settings(self, body):
        s = self.store.data["settings"]
        with self.store.lock:
            if "output" in body:
                out = dict(s["output"])
                out.update(body["output"])
                if out.get("type") not in ("artnet", "sacn", "enttec_pro", "uart", "none"):
                    raise ApiError(400, "unknown output type")
                s["output"] = out
            if "location" in body:
                s["location"] = {"lat": float(body["location"]["lat"]),
                                 "lon": float(body["location"]["lon"])}
            if "camera" in body:
                cam = dict(s.get("camera", {}))
                c = body["camera"]
                cam.update({"enabled": bool(c.get("enabled")),
                            "device": str(c.get("device") or "/dev/video0"),
                            "width": max(160, min(1920, int(c.get("width", 640)))),
                            "height": max(120, min(1080, int(c.get("height", 480)))),
                            "interval": max(1, min(3600, int(c.get("interval", 5))))})
                if not cam["device"].startswith("/dev/"):
                    raise ApiError(400, "camera device must be under /dev/")
                s["camera"] = cam
            if "startup" in body:
                if body["startup"] not in ("schedule", "blackout", "preset"):
                    raise ApiError(400, "invalid startup mode")
                s["startup"] = body["startup"]
                s["startup_preset"] = body.get("startup_preset")
            self.store.save()
        if "output" in body:
            self.engine.configure_output()
        return {"ok": True, "output_error": self.engine.output_error}

    def post_password(self, body):
        auth = self.store.data["auth"]
        if not check_password(auth, body.get("current", "")):
            raise ApiError(403, "current password is wrong")
        new = body.get("new", "")
        if len(new) < 6:
            raise ApiError(400, "password must be at least 6 characters")
        with self.store.lock:
            self.store.data["auth"] = dict(user=(body.get("user") or auth["user"]).strip(),
                                           **hash_password(new))
            self.store.save()
        return {"ok": True}

    def get_backup(self, body):
        d = self.store.export()
        d["profiles"] = {pid: p for pid, p in self.library.profiles.items() if not p["builtin"]}
        return d

    def post_restore(self, body):
        try:
            for p in (body.get("profiles") or {}).values():
                self.library.save(p)
            fixtures = body.get("fixtures", [])
            validate_patch([dict(f) for f in fixtures], self.library)
            body = {k: v for k, v in body.items() if k != "profiles"}
            self.store.restore(body)
        except (ValueError, fx.ProfileError) as e:
            raise ApiError(400, str(e))
        self.engine.sync_patch()
        self.engine.configure_output()
        return {"ok": True}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tinkerlight"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.address_string(), *args)

        def _send(self, status, body, ctype="application/json", extra=None):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authorized(self):
            h = self.headers.get("Authorization", "")
            if not h.startswith("Basic "):
                return False
            try:
                user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
            except Exception:
                return False
            auth = app.store.data["auth"]
            return user == auth.get("user") and check_password(auth, pw)

        def _handle(self):
            path = self.path.split("?", 1)[0]
            if not self._authorized():
                return self._send(401, {"error": "login required"},
                                  extra={"WWW-Authenticate": 'Basic realm="tinkerlight"'})
            if path.startswith("/api/"):
                return self._api(path)
            return self._static(path)

        def _api(self, path):
            body = None
            if self.command != "GET":
                if self.headers.get("X-Tinkerlight") != "1":
                    return self._send(403, {"error": "missing X-Tinkerlight header"})
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._send(413, {"error": "request too large"})
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
                if not isinstance(body, dict):
                    return self._send(400, {"error": "JSON object expected"})
            try:
                result = app.dispatch(self.command, path, body)
            except ApiError as e:
                return self._send(e.status, {"error": str(e)})
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"error": "bad request: %s" % e})
            except Exception:
                log.exception("API error on %s %s", self.command, path)
                return self._send(500, {"error": "internal error"})
            if isinstance(result, RawResponse):
                return self._send(200, result.body, result.ctype, result.headers)
            extra = None
            if path == "/api/backup":
                name = "tinkerlight-%s.json" % dt.date.today().isoformat()
                extra = {"Content-Disposition": 'attachment; filename="%s"' % name}
            return self._send(200, result, extra=extra)

        def _static(self, path):
            if self.command not in ("GET", "HEAD"):
                return self._send(405, {"error": "method not allowed"})
            if path in ("", "/"):
                path = "/index.html"
            full = os.path.realpath(os.path.join(STATIC_DIR, path.lstrip("/")))
            if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, data, ctype)

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _handle

    return Handler


def serve(app, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd
