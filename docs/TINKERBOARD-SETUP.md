# Tinker Board setup: from an empty SD card to a running controller

This guide is for the ASUS Tinker Board (rev 1.2) running **Armbian** (Debian-based,
still maintained). ASUS's own TinkerOS also works, but it is old Debian 10 and no
longer updated.

## 0. What you need

- microSD card, 16 GB or more, a good brand (A1/A2, "Endurance" cards are best
  for 24/7 use).
- **5 V / 3 A micro-USB power supply.** The Tinker Board is sensitive to weak
  PSUs and thin cables. Undervoltage causes random reboots and SD card corruption.
- Heatsink on the main chip. The RK3288 runs hot and throttles without one.
- Ethernet cable, and a computer with an SD card reader.
- Optional: HDMI screen and USB keyboard, for when you can't find the board on the network.

## 1. Download the OS

1. Go to <https://www.armbian.com/tinkerboard/>.
2. Download the current **Debian, Minimal / IOT (CLI)** image. You don't need a desktop.
   The file is a `.img.xz`. Don't unpack it; the flasher does that.

## 2. Flash the SD card

1. Install **Raspberry Pi Imager** (<https://www.raspberrypi.com/software/>) or
   **balenaEtcher** (<https://etcher.balena.io/>).
2. Raspberry Pi Imager: *Choose OS → Use custom* → pick the Armbian `.img.xz` →
   *Choose storage* → your SD card → **No** to OS customisation (that only
   works for Raspberry Pi OS) → *Write*.
   Etcher: *Flash from file* → *Select target* → *Flash*.
3. Wait for the write **and verify** to finish, then eject the card.

## 3. First boot

1. Put the card in the Tinker Board and connect Ethernet to your router or switch.
   Then connect power.
2. Wait about 2 minutes. On the first boot Armbian expands the file system and reboots itself once.
3. Find the board's IP address:
   - your router's DHCP client list (the hostname is `tinkerboard`), or
   - `nmap -sn 192.168.1.0/24` from your computer (use your own subnet), or
   - an HDMI screen, which prints the IP at the login prompt.

## 4. Log in and do the first-run wizard

```sh
ssh root@<board-ip>          # first password: 1234
```

Armbian then asks you to:
1. set a new **root password**,
2. choose a shell (bash is fine),
3. create a **normal user** (e.g. `david`) with its own password,
4. confirm locale and time zone.

Log out, then log back in as your new user: `ssh david@<board-ip>`.

## 5. Update and basic settings

```sh
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3
sudo timedatectl set-timezone Europe/Stockholm     # your zone: timedatectl list-timezones
sudo timedatectl set-ntp true
timedatectl                                        # "System clock synchronized: yes"
sudo hostnamectl set-hostname tinkerlight          # optional
sudo reboot
```

The clock matters, because the scheduler runs on it. The board has no clock
battery fitted by default, so it gets the time from the internet (NTP) at every
boot. Tinkerlight re-applies the correct scheduled state once the time is synced.

## 6. Network: pick a layout

**A. Board and fixture on the same venue network (simplest).**
Give the board a fixed address with a **DHCP reservation in your router**.
Give the MAC Ultra a fixed IP on the same subnet from its control panel.

**B. Direct cable board ↔ fixture, admin over Wi-Fi.**
The Tinker Board has built-in Wi-Fi. Use it for the web interface, and the
Ethernet port only for Art-Net to the fixture:

```sh
sudo armbian-config     # Network → Wi-Fi: join your Wi-Fi
                        # Network → set a static IP on eth0, e.g. 2.0.0.1 / 255.0.0.0, no gateway
```
Set the fixture to e.g. `2.0.0.10 / 255.0.0.0`.

> In layout B, set Tinkerlight's Art-Net destination to the **fixture's IP**
> (e.g. `2.0.0.10`) or the directed broadcast `2.255.255.255`, not
> `255.255.255.255`. A global broadcast may leave through Wi-Fi instead of Ethernet.

## 7. Install Tinkerlight

The repository is private, so git will ask for credentials. Use your GitHub
username, and as password a **personal access token**: GitHub → Settings →
Developer settings → Fine-grained tokens, read-only access to `tinkerlight`.

```sh
cd ~
git clone https://github.com/davidblikstad-spec/tinkerlight.git
cd tinkerlight
sudo ./deploy/install.sh
systemctl status tinkerlight          # should say "active (running)"
```

Open **http://<board-ip>/** in a browser and log in with **admin / admin**.
Change the password straight away under **Admin → Login**.

To update later: `cd ~/tinkerlight && git pull && sudo ./deploy/install.sh`.
Your show data in `/var/lib/tinkerlight` is kept.

## 8. Set up the fixture (MAC Ultra Performance control panel)

- **DMX mode:** Extended (or Basic). It must match the mode in Tinkerlight's patch.
- **DMX address:** e.g. 1. It must match the patch.
- **Control input / network:** enable Art-Net (or sACN), set the fixture's IP
  address and subnet mask as in step 6, and choose the universe.
- **Universe numbering:** Art-Net universe 0 in Tinkerlight = the first Art-Net
  universe. Some menus call this 0, others 1. For sACN, universe 1 is 1 on both sides.

## 9. Set up Tinkerlight in the web UI

1. **Admin → DMX output:** Art-Net, destination IP (see step 6), universe → Apply.
   The pill at the top right should turn green.
2. **Admin → Patch:** set mode and address as on the fixture → Save patch.
3. **Admin → Location & startup:** latitude/longitude (for sunrise/sunset).
   Set "On power-up" to *Restore what the schedule says*.
4. **Admin → Fixture profile:** check every channel against the DMX table in the
   MAC Ultra Performance User Guide. Use **Channel test** to check a channel on the
   real fixture. Save, and set `"verified": true` in the JSON.
5. **Live:** build looks and **Record** presets.
6. **Schedule:** add entries, e.g. "Warm white at sunset −15 min", "Off at 23:30".
7. **Admin → Backup:** download a backup once everything works.

## 9b. Optional: webcam

Plug a USB webcam into the board. `install.sh` already installed `fswebcam`.
Check that the camera is seen with `ls /dev/video*`. In **Admin → Webcam**, tick
*Show webcam*, keep `/dev/video0` and save. The Live tab now shows a snapshot
that refreshes every few seconds.

If the picture fails, the Live tab shows the reason. Check it by hand:
`fswebcam -d /dev/video0 test.jpg`. Some cameras create two devices; if so, try
`/dev/video1`. Keep the resolution at 640×480 so the Tinker Board isn't loaded down.

## 10. Make it last

- When everything works, take an image of the SD card, e.g. with Win32 Disk
  Imager / `dd`. Then a dead card is a 10-minute swap.
- Keep the backup JSON somewhere safe. It restores into a fresh install.
- Don't pull the power while you are saving settings. Tinkerlight writes files
  atomically, but SD cards dislike power cuts in general.

## Troubleshooting

| Symptom | Check |
|---|---|
| Can't reach web UI | `systemctl status tinkerlight`, `journalctl -u tinkerlight -f`, correct IP, port 80 |
| Red "Output error" pill | Hover it for the reason; for USB interfaces check `ls /dev/ttyUSB*` |
| Fixture doesn't react | Same universe on both sides? Fixture IP reachable (`ping`)? Is Art-Net going out on eth0? `sudo apt install tcpdump && sudo tcpdump -i eth0 udp port 6454` |
| Wrong things move | The fixture profile's channel numbers don't match the manual. See step 9.4 |
| Schedule fires at the wrong time | `timedatectl`: time zone and "synchronized: yes" |
| Random reboots | Power supply or cable too weak, or no heatsink |
