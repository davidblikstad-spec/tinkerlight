"""Webcam snapshots for the web UI.

Frames are grabbed on demand (no background streaming, to keep the CPU free
for DMX) with fswebcam, or ffmpeg as a fallback, and cached briefly so several
viewers share one capture.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

MIN_AGE = 1.0          # seconds a frame is reused before grabbing a new one


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
    def __init__(self, store, command=capture_command):
        self.store = store
        self.command = command
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
