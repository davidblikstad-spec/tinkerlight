#!/bin/sh
# Install / update Tinkerlight on the board. Run from the repo root:
#   sudo ./deploy/install.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi
cd "$(dirname "$0")/.."

python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))' || {
    echo "Python 3.7 or newer is required (apt install python3)" >&2
    exit 1
}

id tinkerlight >/dev/null 2>&1 || useradd --system --home /var/lib/tinkerlight --shell /usr/sbin/nologin tinkerlight
usermod -aG dialout,video tinkerlight

# webcam snapshots (optional feature); don't fail the install when offline
command -v fswebcam >/dev/null 2>&1 || apt-get install -y fswebcam || \
    echo "note: could not install fswebcam - webcam snapshots will not work" >&2

mkdir -p /opt/tinkerlight /var/lib/tinkerlight
rm -rf /opt/tinkerlight/tinkerlight
cp -r tinkerlight /opt/tinkerlight/
find /opt/tinkerlight -name '__pycache__' -prune -exec rm -rf {} +
chown -R tinkerlight:tinkerlight /var/lib/tinkerlight

install -m 644 deploy/tinkerlight.service /etc/systemd/system/tinkerlight.service

# The scheduler needs a correct clock; the Tinker Board has no RTC battery by default.
if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-ntp true || true
fi

systemctl daemon-reload
systemctl enable tinkerlight.service
systemctl restart tinkerlight.service

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Tinkerlight is running:  http://${IP:-<board-ip>}/   (login admin / admin - change it!)"
echo "Logs:                    journalctl -u tinkerlight -f"
