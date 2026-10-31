# Rebuild notes (2026-10-31)

A disk crash took the project on both the Mac and the Pi. This version was
rebuilt from the README, the design doc, the wiring work and the few code
pieces that survived in a Claude chat. It follows the same design and file
layout, but it is **new code**, not a copy of the old.

## What survived exactly

- `deploy.sh`
- `control/tools/start_stop_ssr.py`
- the design doc's structure, CSV columns and safety rules (now in
  `weber_smoker_project_structure.md`)
- `wiring.md`, `wiring_diagram.svg/.png` and the photos in `photos/`

Everything else was written again.

## What's different from before

| Area | Before | Now |
|---|---|---|
| Dashboard | React + TypeScript + Vite on port 5173, needed Node | Plain HTML + JS + uPlot in `frontend/`, served by the backend on **port 8000**. No Node, no build. |
| `start.sh` | ran backend and Vite dev server | runs the backend only (it serves the dashboard); makes `control/command_token` if missing |
| Chef iQ decoding | own decoder with a CRC check | layout from the MIT-licensed chefiq-ble project, checked against its real CQ60 captures; packets are validated by layout instead of CRC |
| CSV | columns as in the design doc | same columns, plus five at the end: `zone1_board_f`, `zone2_board_f`, `zone1_fault`, `zone2_fault`, `safety` |
| Journal | proteins, setup, outcome, notes, photos | the same plus a 1–5 rating |
| Probe t1–t4 | blank at their 212 °F cap | same (any t1–t4 at ≥ 100 °C is logged blank) |
| Bluetooth in preflight | PASS / FAIL | WARN only: probes never control the relays, so no probes shouldn't stop the smoker |
| Relay safety on a hard kill | | new: `tools/relays_off.py` runs after the service stops (systemd `ExecStopPost`), so a relay can't stay on if `main.py` is killed outright |
| Old cooks | | gone with the drives |

## Tested here

- `control/`: 99 tests (config checks, MAX31855 decoding, relay
  time-proportioning and failsafe, PID, safety latches, power cap, Chef iQ
  decoding with real captures, cook lifecycle, live WebSocket server with
  history, resume and command tokens, preflight, ssr_test).
  The tests confirm zone 1 = GPIO17 + SPI0 (`/dev/spidev0.0`) and
  zone 2 = GPIO27 + SPI1 (`/dev/spidev1.0`), that `ssr_test.py` touches only
  the chosen zone's pin and leaves it low on every exit path, and that a dead
  zone 2 board fails preflight with "no response".
- `backend/`: 21 tests (settings, CSV parsing, replay player, journal with
  metadata stripping, the live relay against a fake Pi including reconnect and
  resume).
- `analysis/`: merge test.
- End to end: `main.py --simulate` + backend + dashboard in Chromium:
  replay, scrubbing, journal, and a live cook started, re-targeted and
  stopped from the browser.
- Simulated 3-hour cook: zones held 225 / 250 °F within about ±1.5 °F.

## Not tested: check on the real hardware

These need the Pi, the boards and the probes, which the rebuild couldn't
reach. Do them in this order, **elements unplugged** until step 5:

1. `install_service.sh` on a fresh Pi OS: apt packages, SPI0, the SPI1
   overlay and reboot, venv, service.
2. `preflight.py`: both thermocouples at room temperature, both relay pins
   low, `spidev0.0` and `spidev1.0` present, Bluetooth hears the probes.
3. `ssr_test.py`: each SSR's LED lights for its own zone only.
4. Chef iQ probes on the Pi (bleak + BlueZ) and on the Mac
   (`chefiq_logger.py`): readings appear after the "Probe N" name packet.
5. A supervised first cook with the lid open: the PID gains in
   `config.yaml` are starting points for this grill, tune from the CSV.
