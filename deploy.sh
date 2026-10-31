#!/usr/bin/env bash
# Sync the smoker between the Mac and the Pi.
#
#   ./deploy.sh          push control/ to the Pi, then pull cooks back
#   ./deploy.sh push     only push control/
#   ./deploy.sh pull     only pull cooks (CSVs, their _meta.json, and .txt logs)
#
# Push copies control/ to $PI_DIR/control, leaving out .venv, caches and data.
# Files deleted locally are not deleted on the Pi.
# Pull copies $PI_DIR/data/*.csv, *_meta.json and *.txt into data/ and never
# overwrites a file that already exists here, so pull a cook after it has
# finished (its metadata is rewritten until the cook stops).
#
# Override the target with PI_HOST / PI_DIR, e.g.
#   PI_HOST=pi@raspberrypi.local ./deploy.sh

set -euo pipefail

PI_HOST="${PI_HOST:-pi@192.168.1.229}"
PI_DIR="${PI_DIR:-smoker}"   # relative to the Pi user's home
HERE="$(cd "$(dirname "$0")" && pwd)"

push() {
    echo "==> pushing control/ to $PI_HOST:$PI_DIR/control/"
    ssh "$PI_HOST" "mkdir -p $PI_DIR/control $PI_DIR/data"
    rsync -av \
        --exclude '.venv/' \
        --exclude '__pycache__/' \
        --exclude '.pytest_cache/' \
        --exclude 'data/' \
        "$HERE/control/" "$PI_HOST:$PI_DIR/control/"
}

pull() {
    echo "==> pulling cooks from $PI_HOST:$PI_DIR/data/ into data/"
    mkdir -p "$HERE/data"
    rsync -av --ignore-existing \
        --include '*.csv' --include '*_meta.json' --include '*.txt' --exclude '*' \
        "$PI_HOST:$PI_DIR/data/" "$HERE/data/"
}

case "${1:-all}" in
    push) push ;;
    pull) pull ;;
    all)  push; pull ;;
    *)    echo "usage: $0 [push|pull]" >&2; exit 2 ;;
esac
