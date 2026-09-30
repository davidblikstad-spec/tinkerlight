"""Entry point: python3 -m tinkerlight [--data DIR] [--port 8080]"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import threading

from .engine import Engine
from .fixtures import ProfileLibrary, to16
from .scheduler import Scheduler
from .store import Store, new_id
from .web import App, serve

log = logging.getLogger("tinkerlight")


def _look(dimmer, c=0, m=0, y=0, cto=0):
    return {"dimmer": to16(dimmer), "shutter": to16(30), "cyan": to16(c),
            "magenta": to16(m), "yellow": to16(y), "cto": to16(cto),
            "color_wheel": 0}


def seed(store: Store) -> None:
    """Starter presets and (disabled) example schedules on first run."""
    looks = [
        ("Off", "#222222", 3, _look(0)),
        ("Open white", "#ffffff", 2, _look(255)),
        ("Warm white", "#ffd9a0", 2, _look(255, cto=255)),
        ("Half warm", "#c9a978", 2, _look(128, cto=200)),
        ("Red", "#ff2020", 2, _look(255, m=255, y=255)),
        ("Amber", "#ff9a1a", 2, _look(255, m=100, y=255)),
        ("Green", "#20ff40", 2, _look(255, c=255, y=255)),
        ("Blue", "#2040ff", 2, _look(255, c=255, m=255)),
        ("Magenta", "#ff30ff", 2, _look(255, m=255)),
        ("Cyan", "#30ffff", 2, _look(255, c=255)),
    ]
    for name, color, fade, vals in looks:
        store.data["presets"].append({"id": new_id("p"), "name": name, "color": color,
                                      "fade": fade, "values": {"*": vals}})
    by_name = {p["name"]: p["id"] for p in store.data["presets"]}
    store.data["schedules"] = [
        {"id": new_id("s"), "name": "Warm at sunset", "enabled": False, "days": [],
         "when": {"type": "sunset", "offset": -15}, "date_from": None, "date_to": None,
         "action": {"type": "preset", "preset": by_name["Warm white"], "fade": 30}},
        {"id": new_id("s"), "name": "Off at night", "enabled": False, "days": [],
         "when": {"type": "time", "time": "23:30"}, "date_from": None, "date_to": None,
         "action": {"type": "preset", "preset": by_name["Off"], "fade": 10}},
    ]
    store.save()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="tinkerlight", description="Small DMX light controller")
    ap.add_argument("--data", default=os.environ.get("TINKERLIGHT_DATA", "/var/lib/tinkerlight"))
    ap.add_argument("--host", default=os.environ.get("TINKERLIGHT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("TINKERLIGHT_PORT", "8080")))
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    store = Store(args.data)
    if store.first_run:
        seed(store)
    library = ProfileLibrary(os.path.join(args.data, "profiles"))
    engine = Engine(store, library)
    scheduler = Scheduler(store, engine)

    s = store.data["settings"]
    if s.get("startup") == "preset" and store.find("presets", s.get("startup_preset") or ""):
        engine.go_preset(store.find("presets", s["startup_preset"]), 0)
    elif s.get("startup") == "blackout":
        engine.set_blackout(True)
    else:
        scheduler.restore_state()

    engine.start()
    scheduler.start()
    httpd = serve(App(store, library, engine, scheduler), args.host, args.port)
    log.info("web UI on http://%s:%d  output: %s", args.host, args.port, engine.output.describe())
    if store.data["auth"].get("default"):
        log.warning("default login admin/admin is active - change it in Admin")

    def shutdown(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        httpd.serve_forever()
    finally:
        scheduler.stop()
        engine.stop()
        log.info("stopped")


if __name__ == "__main__":
    main()
