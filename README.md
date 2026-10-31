# Weber Genesis II Dual-Zone Smoker

A Raspberry Pi converts a Weber Genesis II into a two-zone electric smoker.
Each zone has its own 1200W element, solid state relay, MAX31855 thermocouple
and PID loop. Chef iQ wireless probes log meat temperatures alongside.

> **Rebuilt on 2026-10-31** after a disk crash took the original code on both
> the Mac and the Pi. Same design, new code. What changed and what still needs
> checking on the real hardware: [`docs/REBUILD_NOTES.md`](docs/REBUILD_NOTES.md).
> **Push this to a private GitHub repo** so it can't be lost again.

## Layout

| Folder | Runs on | What it is |
|---|---|---|
| `control/` | Pi | Control loop: probes, PID, relays, safety, CSV logging, live stream. Own `pyproject.toml`. |
| `backend/` | Mac | FastAPI server: replays saved cooks, relays the Pi's live stream, cook journal, serves the dashboard. |
| `frontend/` | Mac (browser) | The dashboard: plain HTML + JavaScript + uPlot, no build step. |
| `data/` | Mac | Cook CSVs copied off the Pi. The backend lists these. Not in git. |
| `analysis/` | Mac | `merge_probes.py` for lining up the Pi's and the Mac's probe logs. |
| `docs/` | | Design, wiring diagram, parts list, photos. |

`control/` and `backend/` share no code. Their contract is the CSV column
list in [`docs/weber_smoker_project_structure.md`](docs/weber_smoker_project_structure.md)
and the WebSocket message formats at the top of `control/live_server.py` and
`backend/replay_player.py`.

Wiring: [`docs/wiring.md`](docs/wiring.md) and `docs/wiring_diagram.svg`.

## Setting up from scratch

### Mac

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python generate_sample_cook.py      # a demo cook in backend/data/
cd ..
./start.sh --offline                          # http://localhost:8000
```

The first `./start.sh` also makes `control/command_token`, the key the
dashboard uses for cook commands. `./deploy.sh push` copies it to the Pi.

To try the sample cook: `SMOKER_DATA_DIR=backend/data ./start.sh --offline`.

For the controller's tests and simulator on the Mac, install
[uv](https://docs.astral.sh/uv/) (`brew install uv`):

```bash
cd control
uv sync
uv run pytest
uv run python main.py --simulate         # fake probes, relays and grill
```

### Pi

Raspberry Pi OS (Bookworm or later) on your Wi-Fi.

**The Pi's address and user live in one file, [`pi.env`](pi.env)**
(`SMOKER_PI_HOST=raspberrypi`, `SMOKER_PI_USER=pi`, `SMOKER_PI_DIR=smoker`).
`deploy.sh`, `start.sh` and the backend all read it, so if your Pi's name,
IP address or user changes, edit that file and nothing else. The commands
below use the shipped values, `pi@raspberrypi`; substitute yours if you
changed them.

1. From the Mac, set up SSH keys (the old key was on the lost drive):
   ```bash
   ssh-keygen -t ed25519                  # if ~/.ssh/id_ed25519 doesn't exist
   ssh-copy-id pi@raspberrypi
   ```
2. Push the code and install the service:
   ```bash
   ./deploy.sh push
   ssh pi@raspberrypi '~/smoker/control/install_service.sh'
   ```
   `install_service.sh` installs RPi.GPIO, spidev and BlueZ from apt, turns on
   SPI0 and SPI1 (`dtoverlay=spi1-1cs`, reboots if it had to add it), makes
   the venv with uv, and installs and enables `smoker.service`, filling in
   the user and folder it's run from (nothing in the service file is
   hardcoded).
3. Watch it come up: `ssh pi@raspberrypi 'journalctl -u smoker -f'`

## Running

At boot `preflight.py` checks SPI (both buses), the SPI1 overlay, both
MAX31855 boards, RPi.GPIO, the relay pins and Bluetooth (PASS, FAIL or WARN
for each). On any FAIL the service doesn't start and systemd retries every
15 s. Then `main.py` starts IDLE: relays held off, readings streaming on the
live WebSocket, no cook CSV. A cook is started, paused and stopped from the
dashboard.

```bash
ssh pi@raspberrypi
sudo systemctl status smoker                        # or: journalctl -u smoker -f
cd ~/smoker/control && .venv/bin/python preflight.py   # the hardware checks on their own
```

Only `--simulate` fakes hardware: without it, `main.py`, `preflight.py` and
`ssr_test.py` refuse to start if RPi.GPIO or spidev won't load, showing the
import error. Each prints `REAL GPIO` or `SIMULATED` first, and each cook's
`_meta.json` records `"hardware": "real"` or `"simulated"`.

Every setting is in `control/config.yaml`, checked at startup: a missing,
misspelled or out-of-range value stops `main.py` (and the service retries
until it's fixed). `python settings.py` checks it by hand.

### After changing code

```bash
./deploy.sh push                                    # after changing control/
ssh pi@raspberrypi 'cd smoker/control && ~/.local/bin/uv sync'   # after dependency changes
ssh pi@raspberrypi 'sudo systemctl restart smoker'               # pick up the new code (ends a cook)
./deploy.sh pull                                    # copy finished cooks to data/
```

`./deploy.sh pull` copies finished cooks (CSVs, their `_meta.json` and any
`.txt` files) from the Pi's `data/` to `data/` on the Mac, never overwriting
a file already there.

### Testing the relays by hand

**Unplug the heating elements first.**

```bash
sudo systemctl stop smoker
cd ~/smoker/control
.venv/bin/python ssr_test.py      # pulses one zone at a time, up to ssr_test.max_pulse_seconds
sudo systemctl start smoker
```

It only runs from a terminal, asks for the zone number again before each
pulse, shows the GPIO and physical pin (zone 1 = GPIO17 = pin 11, zone 2 =
GPIO27 = pin 13), refuses to start while `main.py` holds the relays (GPIO
lock) and logs every pulse to `data/ssr_test.log`.

`tools/start_stop_ssr.py` is the older bare pin check (both relays on for
10 s, physical pins 11 and 13, no lock or failsafe). Prefer `ssr_test.py`.

## The dashboard

`./start.sh` checks the Pi is reachable, then runs the backend, which serves
the dashboard on http://localhost:8000. Ctrl+C stops everything.
`--offline` skips the Pi for replaying saved cooks; `--probes` also runs the
Mac's Chef iQ logger.

**Replay**: pick a cook, press Play, and set the speed. Dragging the scrub bar
jumps playback to that row. The top chart shows both zones against their
targets, with each relay's duty cycle as a strip underneath. The bottom chart
shows each meat probe's food and ambient temperatures; **Show t1–t4** adds
its internal sensors. A probe at 20 % battery or less raises a warning above
the charts. The table lists the latest value of every field: a value missing
from the current row is greyed and shows its last reading and age.

**Live** shows the cook running on the Pi right now, with the cook controls
(start, pause with a reason, resume, change setpoints, stop). The bar shows
whether the backend can reach the Pi, how long ago the last row arrived
(red after 5 s), and which file it's recording to. The backend connects to the
Pi on startup and stays connected: it retries with backoff while the Pi is
off, resumes by sequence number after a dropout, and records the stream to
`data/live_<date>_<time>.csv`. It finds the Pi through `SMOKER_PI_HOST` and
`SMOKER_PI_PORT` in `pi.env` (an environment variable of the same name
overrides it for one run), or set `SMOKER_LIVE=0` to turn it off.

The **Journal** card keeps notes and photos for the cook on screen, on the Mac
only (`data/journal/<csv stem>/`): proteins, setup, outcome, rating, notes and
photos (metadata and location removed; HEIC needs `pip install pillow-heif`),
each photo also shown as a dot on the charts at the time it was taken.

Backend tests: `cd backend && .venv/bin/python -m pytest`.

## Logging Chef iQ probes from the Mac

The Pi's cook CSV already logs every Chef iQ reading (`probeN_food_f`,
`probeN_t1_f`..`t4_f`, `probeN_ambient_f`, `probeN_battery_pct`). Because the
probes broadcast over Bluetooth, the Mac can log them on its own as a backup,
for example when the Pi is out of range. Its file uses the shorter names
`probeN_food`, `probeN_t1`..`t4`, `probeN_ambient`, `probeN_battery`; the
backend reads both.

```bash
cd control
caffeinate -i uv run python chefiq_logger.py      # Ctrl+C to stop
```

`caffeinate -i` keeps the Mac from idle-sleeping for as long as the logger
runs; the lid must stay open. The first time, macOS asks Terminal for
Bluetooth permission. A probe's readings start once its name packet
("Probe 2") arrives, which takes up to 30 s; after that it's remembered in
`data/chefiq_mac_slots.json`. Options: `--source pi|mac`, `--out`,
`--interval`, `--stale-after`, `--raw` (logs raw advertisements).

After the cook, line the two files up by nearest timestamp:

```bash
cd analysis
uv run --no-project --with pandas python merge_probes.py \
    ../data/cook_<...>.csv ../data/chefiq_mac_<...>.csv -o merged.csv
uv run --no-project --with pandas --with pytest pytest test_merge_probes.py
```

## Power

Each element draws about 10A at 120V, so both zones at full power draw about
20A. That is more than a 20A circuit should carry continuously (about 16A).
Either power the zones from separate circuits or cap combined duty in software
with `power.max_combined_duty: 1.6` in `control/config.yaml` (it ships at 2.0,
which is no cap). Each zone needs a 120V **AC-rated** ~15A breaker or fuse on
its hot leg; see [`docs/wiring.md`](docs/wiring.md).
