"""Webcam snapshots for the web UI.

Frames are grabbed on demand (no background streaming, to keep the CPU free
for DMX) with fswebcam, or ffmpeg as a fallback, and cached briefly so several
viewers share one capture.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import struct
import subprocess
import threading
import time

MIN_AGE = 1.0          # seconds a frame is reused before grabbing a new one

VIDIOC_QUERYCAP = 0x80685600       # _IOR("V", 0, struct v4l2_capability), 104 bytes
CAP_VIDEO_CAPTURE = 0x00000001
CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
CAP_VIDEO_M2M_MPLANE = 0x00004000
CAP_VIDEO_M2M = 0x00008000
CAP_DEVICE_CAPS = 0x80000000


class CameraError(Exception):
    pass


def capture_command(cfg: dict):
    dev = cfg.get("device") or "/dev/video0"
    size = "%dx%d" % (int(cfg.get("width", 640)), int(cfg.get("height", 480)))
    if shutil.which("fswebcam"):
        # -S skips the first frames so auto-exposure has settled
        return ["fswebcam", "-q", "-d", dev, "-r", size, "-S", "5",
                "--no-banner", "--jpeg", "80", "-"]
    if shutil.which("ffmpeg"):
        return ["ffmpeg", "-loglevel", "error", "-f", "v4l2", "-video_size", size,
                "-i", dev, "-vf", "select=gte(n\\,5)", "-frames:v", "1",
                "-f", "image2", "-vcodec", "mjpeg", "-q:v", "5", "-"]
    raise CameraError("no capture tool: sudo apt install fswebcam")


class Camera:
    def __init__(self, store, command=capture_command, lister=None):
        self.store = store
        self.command = command
        self.lister = lister          # None = list_cameras() (swapped out in tests)
        self.lock = threading.Lock()
        self.frame = None
        self.frame_time = 0.0
        self.error = None

    @property
    def cfg(self) -> dict:
        return self.store.data["settings"].get("camera", {})

    def snapshot(self):
        """(jpeg bytes, unix time taken). Raises CameraError."""
        if not self.cfg.get("enabled"):
            raise CameraError("camera is disabled")
        with self.lock:           # concurrent viewers wait for the same grab
            if self.frame and time.time() - self.frame_time < MIN_AGE:
                return self.frame, self.frame_time
            dev = self.cfg.get("device") or "/dev/video0"
            if not os.path.exists(dev):
                self.error = "%s not found - is the webcam plugged in?" % dev
                raise CameraError(self.error)
            try:
                p = subprocess.run(self.command(self.cfg), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=15)
            except subprocess.TimeoutExpired:
                self.error = "capture timed out"
                raise CameraError(self.error)
            except OSError as e:
                self.error = str(e)
                raise CameraError(self.error)
            if p.returncode != 0 or not p.stdout.startswith(b"\xff\xd8"):
                msg = p.stderr.decode("utf-8", "replace").strip().splitlines()
                self.error = msg[-1] if msg else "capture failed (exit %d)" % p.returncode
                raise CameraError(self.error)
            self.frame, self.frame_time, self.error = p.stdout, time.time(), None
            return self.frame, self.frame_time

    def status(self) -> dict:
        c = self.cfg
        return {"enabled": bool(c.get("enabled")), "interval": c.get("interval", 5),
                "error": self.error}

    def devices(self) -> list:
        return (self.lister or list_cameras)()


def querycap(node: str) -> int:
    """V4L2 capabilities of one device node (the node's own caps where reported)."""
    fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    try:
        buf = fcntl.ioctl(fd, VIDIOC_QUERYCAP, bytes(104))
    finally:
        os.close(fd)
    caps, device_caps = struct.unpack_from("<II", buf, 84)
    return device_caps if caps & CAP_DEVICE_CAPS else caps


def is_camera(caps: int) -> bool:
    """Captures frames and is not a memory-to-memory codec (the SoC's own nodes)."""
    return bool(caps & (CAP_VIDEO_CAPTURE | CAP_VIDEO_CAPTURE_MPLANE)) and \
        not caps & (CAP_VIDEO_M2M | CAP_VIDEO_M2M_MPLANE)


def list_cameras(sys_dir="/sys/class/video4linux", dev_dir="/dev", query=querycap) -> list:
    """Connected capture devices as [{"device", "name", "node"}].

    "device" is the /dev/v4l/by-id link when there is one, because /dev/videoN
    numbers can change between boots; the board's own codec nodes are skipped.
    """
    try:
        entries = sorted(os.listdir(sys_dir), key=lambda n: (len(n), n))
    except OSError:
        return []
    by_id = {}
    link_dir = os.path.join(dev_dir, "v4l", "by-id")
    if os.path.isdir(link_dir):
        for link in sorted(os.listdir(link_dir)):
            path = os.path.join(link_dir, link)
            by_id.setdefault(os.path.basename(os.path.realpath(path)), path)
    cams = []
    for entry in entries:
        if not entry.startswith("video"):
            continue
        node = os.path.join(dev_dir, entry)
        try:
            if not is_camera(query(node)):
                continue
        except OSError:
            # can't open it (permissions): fall back to "USB and first index"
            try:
                with open(os.path.join(sys_dir, entry, "index")) as f:
                    first = f.read().strip() == "0"
            except OSError:
                first = True
            if not (entry in by_id and first):
                continue
        try:
            with open(os.path.join(sys_dir, entry, "name"), encoding="utf-8", errors="replace") as f:
                name = f.read().strip()
        except OSError:
            name = entry
        cams.append({"device": by_id.get(entry, node), "name": name or entry, "node": node})
    return cams
