#!/usr/bin/env bash
# One-time setup on the Pi: apt packages, SPI, venv, systemd service.
#
#   ~/smoker/control/install_service.sh
#
# Safe to run again: it only adds what's missing, then (re)starts the service.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo "==> apt packages (RPi.GPIO, spidev, Bluetooth)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-rpi.gpio python3-spidev python3-venv bluez curl

echo "==> SPI0 (dtparam=spi=on)"
sudo raspi-config nonint do_spi 0

BOOT=/boot/firmware/config.txt
[ -f "$BOOT" ] || BOOT=/boot/config.txt
REBOOT=0
if grep -Eq '^\s*dtoverlay=spi1-(2|3)cs' "$BOOT"; then
    echo "!! $BOOT has spi1-2cs/3cs: that takes GPIO17 (relay 1). Fix it by hand." >&2
    exit 1
fi
if ! grep -Eq '^\s*dtoverlay=spi1-1cs' "$BOOT"; then
    echo "==> SPI1: adding dtoverlay=spi1-1cs to $BOOT (backup at $BOOT.bak)"
    sudo cp "$BOOT" "$BOOT.bak"
    echo "dtoverlay=spi1-1cs" | sudo tee -a "$BOOT" >/dev/null
    REBOOT=1
fi

echo "==> uv"
if [ ! -x "$HOME/.local/bin/uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$HOME/.local/bin/uv"

if [ ! -d .venv ]; then
    echo "==> venv (with system site packages, for apt's RPi.GPIO and spidev)"
    "$UV" venv --system-site-packages .venv
fi
"$UV" sync

mkdir -p ../data
if [ ! -s command_token ]; then
    echo "!! no command_token here: the UI's cook commands will be refused." >&2
    echo "   Make one on the Mac (see README) and ./deploy.sh push." >&2
fi

echo "==> systemd service"
sudo cp systemd/smoker.service /etc/systemd/system/smoker.service
sudo systemctl daemon-reload
sudo systemctl enable smoker

if [ "$REBOOT" = 1 ]; then
    echo "==> SPI1 was just enabled: rebooting in 5 s (the service starts after)"
    sleep 5
    sudo reboot
fi
sudo systemctl restart smoker
echo "==> done. Watch it with: journalctl -u smoker -f"
