"""Fixture profiles and DMX encoding.

Attribute values are held internally at 16-bit resolution (0-65535). The
profile file speaks 8-bit DMX (0-255), because that is what the manufacturer's
protocol table prints; to8/to16 convert between the two.
"""
from __future__ import annotations

import json
import os

BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "profiles")


def to16(v8: float) -> int:
    return max(0, min(65535, int(round(float(v8) * 257))))


def to8(v16: float) -> int:
    return max(0, min(255, int(round(float(v16) / 257))))


class ProfileError(ValueError):
    pass


def validate_profile(p: dict) -> None:
    for key in ("id", "attributes", "modes"):
        if key not in p:
            raise ProfileError("profile is missing '%s'" % key)
    attrs = p["attributes"]
    if not isinstance(attrs, dict) or not attrs:
        raise ProfileError("profile has no attributes")
    for name, mode in p["modes"].items():
        fp = int(mode.get("footprint", 0))
        if not 1 <= fp <= 512:
            raise ProfileError("mode %s: footprint must be 1-512" % name)
        used = {}
        for c in mode.get("channels", []):
            if c.get("attr") not in attrs:
                raise ProfileError("mode %s: unknown attribute '%s'" % (name, c.get("attr")))
            for key in ("ch", "fine"):
                if c.get(key) is None:
                    continue
                ch = int(c[key])
                if not 1 <= ch <= fp:
                    raise ProfileError("mode %s: channel %d outside footprint %d" % (name, ch, fp))
                if ch in used:
                    raise ProfileError("mode %s: channel %d used by both %s and %s"
                                       % (name, ch, used[ch], c["attr"]))
                used[ch] = c["attr"]
    for cmd in p.get("commands", []):
        if cmd.get("attr") not in attrs:
            raise ProfileError("command '%s': unknown attribute" % cmd.get("label"))


class ProfileLibrary:
    """Built-in profiles, overridden by user-edited copies in the data dir."""

    def __init__(self, user_dir: str):
        self.user_dir = user_dir
        os.makedirs(user_dir, exist_ok=True)
        self.profiles = {}
        self.reload()

    def reload(self) -> None:
        self.profiles = {}
        for d in (BUILTIN_DIR, self.user_dir):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".json"):
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        p = json.load(f)
                    validate_profile(p)
                    p["builtin"] = d == BUILTIN_DIR
                    self.profiles[p["id"]] = p

    def get(self, pid: str) -> dict:
        if pid not in self.profiles:
            raise ProfileError("unknown profile '%s'" % pid)
        return self.profiles[pid]

    def save(self, p: dict) -> None:
        validate_profile(p)
        p = dict(p)
        p.pop("builtin", None)
        safe = "".join(ch for ch in p["id"] if ch.isalnum() or ch in "-_")
        if not safe:
            raise ProfileError("invalid profile id")
        path = os.path.join(self.user_dir, safe + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        self.reload()

    def revert(self, pid: str) -> None:
        """Drop the user copy so the built-in version applies again."""
        path = os.path.join(self.user_dir, pid + ".json")
        if os.path.exists(path):
            os.remove(path)
        self.reload()


def defaults(profile: dict) -> dict:
    return {a: to16(spec.get("default", 0)) for a, spec in profile["attributes"].items()}


def encode(profile: dict, mode: str, values: dict, universe: bytearray, address: int) -> None:
    """Write one fixture's attribute values into the universe buffer."""
    m = profile["modes"][mode]
    base = address - 1
    for c in m["channels"]:
        v = int(values.get(c["attr"], 0))
        coarse = base + int(c["ch"]) - 1
        if c.get("fine") is not None:
            fine = base + int(c["fine"]) - 1
            if 0 <= coarse < 512:
                universe[coarse] = v >> 8
            if 0 <= fine < 512:
                universe[fine] = v & 0xFF
        elif 0 <= coarse < 512:
            universe[coarse] = to8(v)


def footprint(profile: dict, mode: str) -> int:
    return int(profile["modes"][mode]["footprint"])
