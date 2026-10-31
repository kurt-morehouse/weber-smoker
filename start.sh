#!/usr/bin/env bash
# Start the Mac side: checks the Pi, then runs the backend, which also serves
# the dashboard. Open http://localhost:8000. Ctrl+C stops everything.
#
#   ./start.sh              live view of the Pi + replay of saved cooks
#   ./start.sh --offline    replay only; don't look for the Pi
#   ./start.sh --probes     also run the Mac's Chef iQ logger (backup copy)
#
# The Pi's address, user and port come from pi.env (override for one run, e.g.
# SMOKER_PI_HOST=192.168.1.50 ./start.sh). SMOKER_PORT: the dashboard (8000).
#
# First time: cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# pi.env gives defaults; variables already set in the environment win.
while IFS='=' read -r key value; do
    case "$key" in ''|\#*) continue ;; esac
    [ -z "${!key:-}" ] && export "$key=$value"
done < "$HERE/pi.env"
PI_HOST="$SMOKER_PI_HOST"
PI_USER="$SMOKER_PI_USER"
PI_PORT="$SMOKER_PI_PORT"
PORT="${SMOKER_PORT:-8000}"
CHECK_TIMEOUT_S=2
OFFLINE=0
PROBES=0
for arg in "$@"; do
    case "$arg" in
        --offline) OFFLINE=1 ;;
        --probes)  PROBES=1 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '==> %s\n' "$*"; }
fail() { printf '!!  %s\n' "$*" >&2; exit 1; }

PY="$HERE/backend/.venv/bin/python"
[ -x "$PY" ] || fail "no backend venv. First time: cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

if [ ! -s "$HERE/control/command_token" ]; then
    say "making control/command_token (the UI's key for cook commands; ./deploy.sh push sends it to the Pi)"
    "$PY" -c "import secrets; print(secrets.token_urlsafe(24))" > "$HERE/control/command_token"
    chmod 600 "$HERE/control/command_token"
    say "  run ./deploy.sh push and restart the Pi service so the Pi has it too"
fi

if [ "$OFFLINE" = 1 ]; then
    export SMOKER_LIVE=0
    say "offline: replay only"
else
    say "checking the Pi at $PI_HOST"
    if ! ping -c 1 -t "$CHECK_TIMEOUT_S" "$PI_HOST" >/dev/null 2>&1 && \
       ! ping -c 1 -W "$CHECK_TIMEOUT_S" "$PI_HOST" >/dev/null 2>&1; then
        fail "the Pi at $PI_HOST doesn't answer. Is it on and on Wi-Fi? (change SMOKER_PI_HOST in pi.env, or --offline to replay without it)"
    fi
    if nc -z -G "$CHECK_TIMEOUT_S" "$PI_HOST" "$PI_PORT" >/dev/null 2>&1 || \
       nc -z -w "$CHECK_TIMEOUT_S" "$PI_HOST" "$PI_PORT" >/dev/null 2>&1; then
        say "Pi is streaming on port $PI_PORT"
    else
        say "Pi is reachable but nothing is listening on port $PI_PORT; the backend will keep retrying."
        say "  check it with: ssh $PI_USER@$PI_HOST 'systemctl status smoker; journalctl -u smoker -n 40'"
    fi
fi
export SMOKER_PI_HOST="$PI_HOST" SMOKER_PI_PORT="$PI_PORT"

PIDS=()
cleanup() {
    for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
    wait 2>/dev/null
    say "stopped"
}
trap cleanup EXIT INT TERM

(cd "$HERE/backend" && exec "$PY" -m uvicorn server:app --port "$PORT" 2>&1 | sed -u 's/^/[backend] /') &
PIDS+=($!)

if [ "$PROBES" = 1 ]; then
    if command -v uv >/dev/null 2>&1; then
        (cd "$HERE/control" && exec caffeinate -i uv run python chefiq_logger.py 2>&1 | sed -u 's/^/[probes] /') &
        PIDS+=($!)
    else
        say "--probes needs uv (brew install uv); skipping the Chef iQ logger"
    fi
fi

say "dashboard on http://localhost:$PORT  (Ctrl+C stops everything)"
wait
