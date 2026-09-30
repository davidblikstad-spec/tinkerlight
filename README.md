# Tinkerlight

Turns an **ASUS Tinker Board (rev 1.2)**, or any small Linux board, into a small
stand-alone DMX controller for a **Martin MAC Ultra Performance** (or several of
them). It has presets, a time-of-day / sunset scheduler, and a web interface for
administration and programming.

- **No dependencies.** Plain Python 3.7+ standard library, so it runs on stock
  TinkerOS (Debian 10, armhf) or Armbian. There is no `pip` step and nothing to compile.
- **DMX out** over Art-Net or sACN on the Ethernet port, through an Enttec DMX USB Pro,
  or as raw DMX on a UART (FTDI "Open DMX" dongle or the board's own UART + RS-485 chip).
- **Programmer:** sliders grouped by intensity, colour, beam, framing and position.
  It also has a pan/tilt pad, a colour picker (converted to CMY) and named wheel slots.
- **Presets:** record the current look, recall it with a fade time, and reorder or
  recolour it. You can record only some attribute groups, such as colour without position.
- **Scheduler:** fixed times or sunrise/sunset ± an offset, filtered by weekday and
  date range. Actions are recall preset, blackout on/off, home, or a fixture command.
- **Power-loss safe:** on boot it restores whatever the schedule says should be
  active at the current time. When NTP later corrects the clock (the Tinker Board
  has no RTC battery) it restores the state again instead of firing a burst of old events.
- **Webcam (optional):** plug in any USB webcam to see a live snapshot of the rig
  on the Live tab, which helps when you are working remotely over Tailscale. Frames are
  only grabbed while someone is looking.
- **Admin:** output settings, patch with overlap checks, fixture profile editor,
  raw channel test, DMX monitor, login, and backup/restore.

## ⚠ Check the fixture profile first

The built-in profile (`tinkerlight/profiles/martin-mac-ultra-performance.json`) is
transcribed from the DMX protocol tables in the *MAC Ultra Performance User Guide*
rev. H (firmware 2.3.x). It covers Extended (58 ch), Basic (48 ch) and Compact (42 ch)
modes, the named colour, gobo and prism slots, and reset/hibernation/fan commands.
It has not been checked on a real fixture yet:

1. Check the fixture's firmware (INFORMATION → FW VERSION). Other versions may lay channels out differently.
2. Use **Admin → Channel test** to drive single channels on the real fixture and confirm them.
3. Fix anything that differs in **Admin → Fixture profile**, then set `"verified": true`.

Your edited profile is saved in the data directory, so updates do not overwrite it.
**Revert to built-in** discards your edits.

## Wiring options

| Output | Hardware | Notes |
|---|---|---|
| **Art-Net** (default) | Ethernet cable board → fixture (or a switch) | Set the fixture to Art-Net and give it an IP on the same subnet. Use the fixture IP (or its subnet broadcast, e.g. `2.255.255.255`) as destination. |
| **sACN** | Same | Multicast by default, or unicast to the fixture IP. |
| **Enttec USB Pro** | Enttec DMX USB Pro / DMXking ultraDMX Pro | Usually `/dev/ttyUSB0`. Most reliable 5-pin/3-pin DMX option. |
| **UART** | FTDI Open DMX dongle, or Tinker Board UART pin → MAX485/SN75176 (DE+RE tied high) → XLR | Timing is best-effort from user space. Prefer one of the above. |

## Install on the Tinker Board

**Starting from an empty SD card?** Follow [docs/TINKERBOARD-SETUP.md](docs/TINKERBOARD-SETUP.md).

```sh
# on the board (TinkerOS or Armbian), with network access
sudo apt install -y git python3
git clone <this repo> tinkerlight && cd tinkerlight
sudo ./deploy/install.sh
```

Then browse to `http://<board-ip>/` and log in as **admin / admin**. Change the
password under Admin. After that:

1. **Admin → DMX output**: choose the output and press Apply. The pill at the top right turns green.
2. **Admin → Patch**: set the fixture's DMX address and mode to match the fixture's menu.
3. **Admin → Location**: enter your latitude/longitude for sunrise/sunset.
   Set the board's time zone with `sudo timedatectl set-timezone Europe/Stockholm`.
4. **Admin → Fixture profile**: verify it (see above).
5. **Live**: build looks and record presets.
6. **Schedule**: add entries. Two disabled examples are included.

Update later with `git pull && sudo ./deploy/install.sh`. Your show data in
`/var/lib/tinkerlight` is kept.

### Run by hand / on a laptop

```sh
python3 -m tinkerlight --data ./data --port 8080     # http://localhost:8080
python3 -m unittest discover -s tests                 # tests
```

## Data

Everything lives in `/var/lib/tinkerlight/show.json`. It is written atomically,
with a `.bak` copy of the previous version. User-edited profiles go in
`profiles/` next to it. **Admin → Backup** downloads the whole show, including
edited profiles.

## HTTP API

Every page and API call uses HTTP Basic auth. Requests that change state must
send the header `X-Tinkerlight: 1`. This lets other systems, such as a show
controller, trigger presets. Some examples:

```sh
curl -u admin:pw -H 'X-Tinkerlight: 1' -X POST http://board/api/presets/<id>/go -d '{"fade": 3}'
curl -u admin:pw -H 'X-Tinkerlight: 1' -X POST http://board/api/blackout -d '{"on": true}'
curl -u admin:pw http://board/api/state
```

## Layout

```
tinkerlight/
  __main__.py     startup, first-run seed presets
  engine.py       live values, fades, grand master, blackout, 40 fps output thread
  outputs.py      Art-Net, sACN, Enttec Pro, UART drivers
  fixtures.py     profile loading/validation, 8/16-bit encoding
  scheduler.py    schedule evaluation, NOAA sunrise/sunset, restore after boot
  store.py        JSON persistence, password hashing
  web.py          HTTP server + JSON API
  static/         web UI (plain HTML/CSS/JS)
  profiles/       built-in fixture profiles
deploy/           systemd unit + install script
tests/            unittest suite
```
