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
# The Pi's address, user and folder come from pi.env. Override for one run with
# the same names, e.g.  SMOKER_PI_HOST=192.168.1.50 ./deploy.sh

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# pi.env gives defaults; variables already set in the environment win.
while IFS='=' read -r key value; do
    case "$key" in ''|\#*) continue ;; esac
    [ -z "${!key:-}" ] && export "$key=$value"
done < "$HERE/pi.env"

PI_HOST="$SMOKER_PI_USER@$SMOKER_PI_HOST"
PI_DIR="$SMOKER_PI_DIR"      # relative to the Pi user's home

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
