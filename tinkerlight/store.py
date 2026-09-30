"""Show file persistence: one JSON document, written atomically."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import threading
import uuid

DEFAULT_SHOW = {
    "version": 1,
    "settings": {
        "output": {
            "type": "artnet",          # artnet | sacn | enttec_pro | uart | none
            "host": "255.255.255.255",  # Art-Net destination (broadcast or fixture IP)
            "net": 0, "subnet": 0, "universe": 0,
            "sacn_universe": 1, "sacn_host": "", "priority": 100,
            "serial_port": "/dev/ttyUSB0",
            "fps": 40,
        },
        "location": {"lat": 59.3293, "lon": 18.0686},
        "startup": "schedule",          # schedule | blackout | preset
        "startup_preset": None,
        "grand_master": 1.0,
    },
    "auth": {},
    "fixtures": [
        {"id": "f1", "name": "MAC Ultra 1", "profile": "martin-mac-ultra-performance",
         "mode": "extended", "address": 1},
    ],
    "presets": [],
    "schedules": [],
}


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:8]


def hash_password(pw: str, salt: str = None) -> dict:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 100000)
    return {"salt": salt, "hash": h.hex()}


def check_password(auth: dict, pw: str) -> bool:
    if not auth.get("salt"):
        return False
    return secrets.compare_digest(hash_password(pw, auth["salt"])["hash"], auth["hash"])


def _merge_defaults(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if k not in dst:
            dst[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            _merge_defaults(dst[k], v)


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "show.json")
        self.lock = threading.RLock()
        self.first_run = not os.path.exists(self.path)
        if self.first_run:
            self.data = copy.deepcopy(DEFAULT_SHOW)
        else:
            self.data = self._load()
        _merge_defaults(self.data, DEFAULT_SHOW)
        if not self.data["auth"]:
            self.data["auth"] = dict(user="admin", **hash_password("admin"))
            self.data["auth"]["default"] = True
        self.save()

    def _load(self) -> dict:
        for path in (self.path, self.path + ".bak"):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                continue
        return copy.deepcopy(DEFAULT_SHOW)

    def save(self) -> None:
        with self.lock:
            text = json.dumps(self.data, indent=2)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(self.path):
                os.replace(self.path, self.path + ".bak")
            os.replace(tmp, self.path)
            try:  # make the rename durable across power loss
                dfd = os.open(self.data_dir, os.O_RDONLY)
                os.fsync(dfd)
                os.close(dfd)
            except OSError:
                pass

    # -- helpers -------------------------------------------------------------
    def find(self, kind: str, item_id: str):
        for item in self.data[kind]:
            if item["id"] == item_id:
                return item
        return None

    def upsert(self, kind: str, item: dict, prefix: str) -> dict:
        with self.lock:
            if not item.get("id"):
                item["id"] = new_id(prefix)
            items = self.data[kind]
            for i, old in enumerate(items):
                if old["id"] == item["id"]:
                    items[i] = item
                    break
            else:
                items.append(item)
            self.save()
            return item

    def delete(self, kind: str, item_id: str) -> bool:
        with self.lock:
            before = len(self.data[kind])
            self.data[kind] = [x for x in self.data[kind] if x["id"] != item_id]
            if len(self.data[kind]) != before:
                self.save()
                return True
            return False

    def export(self) -> dict:
        with self.lock:
            d = copy.deepcopy(self.data)
            d.pop("auth", None)
            return d

    def restore(self, d: dict) -> None:
        with self.lock:
            for key in ("fixtures", "presets", "schedules", "settings"):
                if key not in d:
                    raise ValueError("backup is missing '%s'" % key)
            auth = self.data["auth"]
            self.data = copy.deepcopy(d)
            self.data["auth"] = auth
            _merge_defaults(self.data, DEFAULT_SHOW)
            self.save()
