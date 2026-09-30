import base64
import datetime as dt
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import sys

from tinkerlight import fixtures as fx
from tinkerlight.camera import Camera, CameraError, is_camera, list_cameras

CAP_CAPTURE, CAP_META, CAP_M2M_MP, CAP_M2M = 0x1, 0x00800000, 0x4000, 0x8000
from tinkerlight.__main__ import seed
from tinkerlight.engine import Engine
from tinkerlight.outputs import artnet_packet, sacn_packet, ArtNetOutput
from tinkerlight.scheduler import Scheduler, events_between, sun_times
from tinkerlight.store import Store, check_password
from tinkerlight.web import App, ApiError, serve

PID = "martin-mac-ultra-performance"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_env(clock=None):
    d = tempfile.mkdtemp()
    store = Store(d)
    seed(store)
    lib = fx.ProfileLibrary(d + "/profiles")
    eng = Engine(store, lib, clock=clock or Clock())
    return store, lib, eng


def fake_capture(cfg):
    return [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xd8fakejpeg')"]


class CameraTests(unittest.TestCase):
    def cam(self, command=fake_capture, **cfg):
        store = Store(tempfile.mkdtemp())
        self.dev = tempfile.NamedTemporaryFile()
        store.data["settings"]["camera"].update(dict({"enabled": True, "device": self.dev.name}, **cfg))
        return Camera(store, command=command)

    def test_snapshot_and_cache(self):
        cam = self.cam()
        jpeg, t1 = cam.snapshot()
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertEqual(cam.snapshot()[1], t1)          # reused within MIN_AGE

    def test_disabled_and_missing_device(self):
        cam = self.cam(enabled=False)
        with self.assertRaises(CameraError):
            cam.snapshot()
        cam = self.cam(device="/dev/does-not-exist")
        with self.assertRaises(CameraError):
            cam.snapshot()
        self.assertIn("not found", cam.status()["error"])

    def test_failed_capture_reports_stderr(self):
        bad = lambda cfg: [sys.executable, "-c", "import sys; sys.stderr.write('no such device'); sys.exit(1)"]
        cam = self.cam(command=bad)
        with self.assertRaises(CameraError):
            cam.snapshot()
        self.assertEqual(cam.error, "no such device")

    def test_list_cameras_skips_codecs_and_prefers_by_id(self):
        # Mirrors the Tinker Board: video0-3 are the SoC's codec nodes, video4/5 a USB webcam.
        root = tempfile.mkdtemp()
        sysd, devd = os.path.join(root, "sys"), os.path.join(root, "dev")
        nodes = {"video0": ("rockchip-rga", 0, CAP_M2M), "video1": ("rk3288-vpu-enc", 0, CAP_M2M_MP),
                 "video3": ("rkvdec", 0, CAP_M2M_MP), "video4": ("HD 720P Webcam", 0, CAP_CAPTURE),
                 "video5": ("HD 720P Webcam", 1, CAP_META), "video10": ("Other cam", 0, CAP_CAPTURE)}
        os.makedirs(os.path.join(devd, "v4l", "by-id"))
        for n, (name, idx, _caps) in nodes.items():
            os.makedirs(os.path.join(sysd, n))
            with open(os.path.join(sysd, n, "name"), "w") as f:
                f.write(name + "\n")
            with open(os.path.join(sysd, n, "index"), "w") as f:
                f.write("%d\n" % idx)
            open(os.path.join(devd, n), "w").close()
        link = os.path.join(devd, "v4l", "by-id", "usb-Sonix_USB_2.0_Camera-video-index0")
        os.symlink(os.path.join(devd, "video4"), link)
        caps = {os.path.join(devd, n): c for n, (_name, _idx, c) in nodes.items()}
        cams = list_cameras(sysd, devd, query=caps.__getitem__)
        self.assertEqual(cams, [
            {"device": link, "name": "HD 720P Webcam", "node": os.path.join(devd, "video4")},
            {"device": os.path.join(devd, "video10"), "name": "Other cam",
             "node": os.path.join(devd, "video10")},
        ])

    def test_list_cameras_without_permission_falls_back_to_by_id(self):
        root = tempfile.mkdtemp()
        sysd, devd = os.path.join(root, "sys"), os.path.join(root, "dev")
        os.makedirs(os.path.join(devd, "v4l", "by-id"))
        for n, idx in (("video0", 0), ("video4", 0), ("video5", 1)):
            os.makedirs(os.path.join(sysd, n))
            with open(os.path.join(sysd, n, "index"), "w") as f:
                f.write("%d\n" % idx)
            open(os.path.join(devd, n), "w").close()
        for n, idx in (("video4", 0), ("video5", 1)):
            os.symlink(os.path.join(devd, n), os.path.join(devd, "v4l", "by-id", "cam-index%d" % idx))

        def denied(node):
            raise PermissionError(node)
        cams = list_cameras(sysd, devd, query=denied)
        self.assertEqual([c["node"] for c in cams], [os.path.join(devd, "video4")])
        self.assertEqual(list_cameras(os.path.join(root, "missing"), devd), [])

    def test_capability_filter(self):
        self.assertTrue(is_camera(CAP_CAPTURE))
        self.assertTrue(is_camera(0x1000))                      # multi-planar capture
        self.assertFalse(is_camera(CAP_M2M | CAP_CAPTURE))      # codec that also "captures"
        self.assertFalse(is_camera(CAP_META))


class PacketTests(unittest.TestCase):
    def test_artnet(self):
        pkt = artnet_packet(bytes(range(256)) * 2, 7, net=1, subnet=2, universe=3)
        self.assertEqual(pkt[:8], b"Art-Net\x00")
        self.assertEqual(struct.unpack("<H", pkt[8:10])[0], 0x5000)
        self.assertEqual(pkt[12], 7)
        self.assertEqual(pkt[14], 0x23)
        self.assertEqual(pkt[15], 1)
        self.assertEqual(struct.unpack(">H", pkt[16:18])[0], 512)
        self.assertEqual(len(pkt), 18 + 512)

    def test_sacn_lengths(self):
        pkt = sacn_packet(bytes(512), 5, 1, b"\x01" * 16)
        self.assertEqual(len(pkt), 638)
        self.assertEqual(pkt[4:16], b"ASC-E1.17\x00\x00\x00")
        # root, framing and DMP flags+length fields
        self.assertEqual(struct.unpack(">H", pkt[16:18])[0], 0x7000 | (638 - 16))
        self.assertEqual(struct.unpack(">H", pkt[38:40])[0], 0x7000 | (638 - 38))
        self.assertEqual(struct.unpack(">H", pkt[115:117])[0], 0x7000 | (638 - 115))
        self.assertEqual(struct.unpack(">H", pkt[113:115])[0], 1)   # universe
        self.assertEqual(pkt[111], 5)                                # sequence
        self.assertEqual(struct.unpack(">H", pkt[123:125])[0], 513)  # property count
        self.assertEqual(pkt[125], 0)                                # start code


class FixtureTests(unittest.TestCase):
    def test_builtin_profile_valid(self):
        lib = fx.ProfileLibrary(tempfile.mkdtemp())
        p = lib.get(PID)
        self.assertEqual(fx.footprint(p, "extended"), 58)
        self.assertEqual(fx.footprint(p, "basic"), 48)

    def test_encode_16_and_8_bit(self):
        p = fx.ProfileLibrary(tempfile.mkdtemp()).get(PID)
        uni = bytearray(512)
        fx.encode(p, "extended", {"dimmer": 0x1234, "shutter": fx.to16(30)}, uni, 101)
        self.assertEqual(uni[100], 30)      # ch 1 of fixture at 101
        self.assertEqual(uni[101], 0x12)
        self.assertEqual(uni[102], 0x34)

    def test_profile_matches_manual(self):
        # Spot checks against Tables 2 and 3 of the MAC Ultra Performance User Guide rev. H.
        p = fx.ProfileLibrary(tempfile.mkdtemp()).get(PID)
        self.assertEqual(fx.footprint(p, "compact"), 42)
        expect = {
            "extended": {"cto": (10, 11), "gobo2_rot": (17, 18), "iris": (24, 25), "zoom": (26, 27),
                         "blade1_in": (30, 31), "pan": (48, 49), "tilt": (50, 51), "control": (52, None),
                         "fx_sync": (58, None)},
            "basic": {"cto": (10, 11), "iris": (24, None), "zoom": (25, 26), "blade1_in": (29, None),
                      "frame_rot": (37, None), "pan": (38, 39), "control": (42, None), "fx_sync": (48, None)},
        }
        for mode, chans in expect.items():
            got = {c["attr"]: (c["ch"], c.get("fine")) for c in p["modes"][mode]["channels"]}
            for attr, want in chans.items():
                self.assertEqual(got[attr], want, "%s %s" % (mode, attr))
        # 16-bit centre must be exactly 32768; 32896 already means CCW rotation on gobo channels.
        self.assertEqual(fx.defaults(p)["gobo_rot"], 32768)
        self.assertEqual(fx.defaults(p)["pan"], 32768)

    def test_validate_rejects_overlap(self):
        p = json.loads(json.dumps(fx.ProfileLibrary(tempfile.mkdtemp()).get(PID)))
        p["modes"]["basic"]["channels"][1]["ch"] = 1
        with self.assertRaises(fx.ProfileError):
            fx.validate_profile(p)


class EngineTests(unittest.TestCase):
    def test_fade_and_snap(self):
        clock = Clock()
        store, lib, eng = make_env(clock)
        eng.set_values("f1", {"dimmer": 65535, "color_wheel": fx.to16(100)}, fade=2)
        eng.render()
        self.assertEqual(eng.universe[11], 100)         # snap attr jumps
        self.assertEqual(eng.universe[1], 0)
        clock.t += 1
        eng.render()
        self.assertAlmostEqual(eng.universe[1], 127, delta=1)
        clock.t += 1.5
        eng.render()
        self.assertEqual(eng.universe[1], 255)
        self.assertFalse(eng.fades)

    def test_blackout_and_master(self):
        store, lib, eng = make_env()
        eng.set_values("f1", {"dimmer": 65535})
        eng.set_grand_master(0.5)
        eng.render()
        self.assertEqual(eng.universe[1], 127)
        eng.set_blackout(True)
        eng.render()
        self.assertEqual(eng.universe[1], 0)
        self.assertEqual(eng.universe[0], 30)          # shutter untouched

    def test_preset_wildcard(self):
        store, lib, eng = make_env()
        red = next(p for p in store.data["presets"] if p["name"] == "Red")
        eng.go_preset(red, 0)
        eng.render()
        self.assertEqual(eng.universe[1], 255)
        self.assertEqual(eng.universe[5], 255)   # magenta coarse (ch 6)
        self.assertEqual(eng.active_preset, red["id"])

    def test_command_hold(self):
        clock = Clock()
        store, lib, eng = make_env(clock)
        eng.run_command("f1", {"label": "Reset", "attr": "control", "value": 200, "hold": 3})
        eng.render()
        self.assertEqual(eng.universe[51], 200)       # control is ch 52 in extended
        clock.t += 4
        eng.render()
        self.assertEqual(eng.universe[51], 0)

    def test_test_channel_override(self):
        store, lib, eng = make_env()
        eng.test[300] = 77
        eng.render()
        self.assertEqual(eng.universe[299], 77)


class SchedulerTests(unittest.TestCase):
    def test_sun_times_stockholm(self):
        rise, sset = sun_times(dt.date(2026, 6, 21), 59.33, 18.07)
        self.assertLess(rise, sset)
        self.assertGreater((sset - rise).total_seconds() / 3600, 17.5)

    def test_polar(self):
        self.assertEqual(sun_times(dt.date(2026, 6, 21), 78.2, 15.6), (None, None))

    def entry(self, **kw):
        e = {"id": "s1", "name": "t", "enabled": True, "days": [],
             "when": {"type": "time", "time": "18:00"}, "action": {"type": "blackout", "on": True}}
        e.update(kw)
        return e

    def test_days_and_dates(self):
        e = self.entry(days=[0], date_to="2026-10-31")   # Mondays until end of Oct
        ev = events_between([e], dt.datetime(2026, 10, 1), dt.datetime(2026, 11, 30), 0, 0)
        self.assertEqual([t.day for t, _ in ev], [5, 12, 19, 26])

    def test_tick_fires_once(self):
        store, lib, eng = make_env()
        now = [dt.datetime(2026, 10, 5, 17, 59, 58)]
        store.data["schedules"] = [self.entry()]
        s = Scheduler(store, eng, now=lambda: now[0])
        s.tick()
        now[0] += dt.timedelta(seconds=3)
        s.tick()
        self.assertTrue(eng.blackout)
        eng.set_blackout(False)
        now[0] += dt.timedelta(seconds=1)
        s.tick()
        self.assertFalse(eng.blackout)

    def test_restore_state(self):
        store, lib, eng = make_env()
        warm = next(p for p in store.data["presets"] if p["name"] == "Warm white")
        off = next(p for p in store.data["presets"] if p["name"] == "Off")
        store.data["schedules"] = [
            self.entry(id="a", when={"type": "time", "time": "18:00"},
                       action={"type": "preset", "preset": warm["id"], "fade": 30}),
            self.entry(id="b", when={"type": "time", "time": "23:00"},
                       action={"type": "preset", "preset": off["id"]}),
        ]
        s = Scheduler(store, eng, now=lambda: dt.datetime(2026, 10, 5, 20, 0))
        s.restore_state()
        self.assertEqual(eng.active_preset, warm["id"])
        self.assertFalse(eng.fades)          # applied instantly
        s = Scheduler(store, eng, now=lambda: dt.datetime(2026, 10, 6, 7, 0))
        s.restore_state()
        self.assertEqual(eng.active_preset, off["id"])

    def test_clock_jump_restores(self):
        store, lib, eng = make_env()
        store.data["schedules"] = [self.entry()]
        now = [dt.datetime(2020, 1, 1, 0, 0)]
        s = Scheduler(store, eng, now=lambda: now[0])
        s.tick()
        now[0] = dt.datetime(2026, 10, 5, 19, 0)       # NTP sync
        s.tick()
        self.assertTrue(eng.blackout)
        self.assertEqual(len(s.log), 1)


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store, lib, eng = make_env()
        cls.store, cls.eng = store, eng
        store.data["settings"]["output"].update(type="none")
        cls.dev = tempfile.NamedTemporaryFile()        # stands in for /dev/video0
        store.data["settings"]["camera"].update(enabled=True, device=cls.dev.name)
        cams = [{"device": "/dev/v4l/by-id/usb-cam-video-index0", "name": "Webcam", "node": "/dev/video4"}]
        cls.app = App(store, lib, eng, Scheduler(store, eng),
                      Camera(store, command=fake_capture, lister=lambda: cams))
        cls.httpd = serve(cls.app, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, method, path, body=None, auth=("admin", "admin"), header=True):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if auth:
            r.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode())
        if header:
            r.add_header("X-Tinkerlight", "1")
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_auth_required(self):
        self.assertEqual(self.req("GET", "/api/state", auth=None)[0], 401)
        self.assertEqual(self.req("GET", "/api/state", auth=("admin", "nope"))[0], 401)
        self.assertEqual(self.req("GET", "/api/state")[0], 200)

    def test_csrf_header_required(self):
        self.assertEqual(self.req("POST", "/api/blackout", {"on": True}, header=False)[0], 403)

    def test_static_and_traversal(self):
        code, body = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"Tinkerlight", body)
        self.assertEqual(self.req("GET", "/../store.py")[0], 404)

    def test_live_record_go(self):
        self.assertEqual(self.req("POST", "/api/live", {"values": {"dimmer": 30000, "cto": 65535}})[0], 200)
        code, body = self.req("POST", "/api/presets/record", {"name": "Test look", "groups": ["Intensity"]})
        self.assertEqual(code, 200)
        p = json.loads(body)
        self.assertEqual(set(p["values"]["f1"]), {"dimmer", "shutter"})
        self.req("POST", "/api/home")
        self.assertEqual(self.req("POST", "/api/presets/%s/go" % p["id"], {"fade": 0})[0], 200)
        self.assertEqual(self.eng.snapshot()["f1"]["dimmer"], 30000)

    def test_schedule_validation(self):
        code, _ = self.req("POST", "/api/schedules", {"name": "x", "when": {"type": "time", "time": "25:99"},
                                                     "action": {"type": "blackout", "on": True}})
        self.assertEqual(code, 400)
        code, body = self.req("POST", "/api/schedules", {"name": "x", "days": [0, 6],
                                                        "when": {"type": "sunset", "offset": -10},
                                                        "action": {"type": "home", "fade": 5}})
        self.assertEqual(code, 200)
        sid = json.loads(body)["id"]
        self.assertEqual(self.req("POST", "/api/schedules/%s/run" % sid)[0], 200)
        self.assertEqual(self.req("DELETE", "/api/schedules/%s" % sid)[0], 200)

    def test_patch_overlap_rejected(self):
        fixtures = [
            {"id": "f1", "name": "A", "profile": PID, "mode": "extended", "address": 1},
            {"id": "f2", "name": "B", "profile": PID, "mode": "extended", "address": 50},
        ]
        code, body = self.req("PUT", "/api/fixtures", {"fixtures": fixtures})
        self.assertEqual(code, 400)
        self.assertIn(b"overlaps", body)

    def test_camera_endpoint(self):
        code, body = self.req("GET", "/api/camera.jpg")
        self.assertEqual(code, 200)
        self.assertTrue(body.startswith(b"\xff\xd8"))
        code, _ = self.req("PUT", "/api/settings", {"camera": {"enabled": True, "device": "/etc/passwd"}})
        self.assertEqual(code, 400)

    def test_camera_list_endpoint(self):
        code, body = self.req("GET", "/api/cameras")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["cameras"][0]["node"], "/dev/video4")

    def test_backup_roundtrip(self):
        code, body = self.req("GET", "/api/backup")
        self.assertEqual(code, 200)
        backup = json.loads(body)
        self.assertNotIn("auth", backup)
        self.assertEqual(self.req("POST", "/api/restore", backup)[0], 200)

    def test_password_change(self):
        code, _ = self.req("POST", "/api/password", {"current": "wrong", "new": "secret123"})
        self.assertEqual(code, 403)
        # checked on a copy so the other tests keep admin/admin
        self.assertTrue(check_password(self.store.data["auth"], "admin"))


class OutputTests(unittest.TestCase):
    def test_artnet_over_udp(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2)
        out = ArtNetOutput("127.0.0.1", 0, 0, 1, port=rx.getsockname()[1])
        out.send(bytes([9]) + bytes(511))
        pkt = rx.recv(1024)
        self.assertEqual(pkt[18], 9)
        self.assertEqual(pkt[14], 1)
        out.close()
        rx.close()


if __name__ == "__main__":
    unittest.main()
