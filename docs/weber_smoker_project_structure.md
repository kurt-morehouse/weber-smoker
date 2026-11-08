# Weber Genesis II Dual-Zone Smoker: Project Structure

```
weber-smoker/
├── README.md                 # setup, running, the dashboard
├── DIAGNOSTICS.md            # troubleshooting, one part of the system at a time
├── deploy.sh                 # push control/ to the Pi, pull cooks back
├── start.sh                  # Mac: backend + dashboard (and optional probe logger)
├── pi.env                    # the Pi's address, user and folder: the one place to change them
│
├── control/                  # runs continuously on the Raspberry Pi (self-contained)
│   ├── pyproject.toml        # Pi dependencies (uv)
│   ├── config.yaml           # per-zone pins and buses, targets, PID, safety limits
│   ├── settings.py           # loads and checks config.yaml
│   ├── hardware.py           # loads RPi.GPIO / spidev; REAL GPIO or SIMULATED banner
│   ├── main.py               # entry point: both zone loops, commands, live rows
│   ├── pid_controller.py     # PID logic, one instance per zone
│   ├── relay_driver.py       # GPIO time-proportioning of one SSR, with failsafe
│   ├── grill_probe.py        # reads one MAX31855 (both on SPI0, CE0 / CE1)
│   ├── safety.py             # per-zone fault, over-temp, stale data, power cap
│   ├── cook.py               # IDLE / COOKING / PAUSED and the cook commands
│   ├── data_logger.py        # writes synchronized rows to CSV at a fixed interval
│   ├── live_server.py        # WebSocket: rows out, commands in (port 8765)
│   ├── gpio_lock.py          # one process at a time drives the relays
│   ├── chefiq_decode.py      # Chef iQ packet decoder (CQ60 V3 layout)
│   ├── chefiq_ble.py         # scans Chef iQ BLE (up to 3 meat probes)
│   ├── chefiq_logger.py      # standalone Chef iQ probe logger (Mac or Pi)
│   ├── preflight.py          # hardware checks before main.py starts
│   ├── ssr_test.py           # manual one-zone relay pulse test
│   ├── install_service.sh    # one-time Pi setup
│   ├── systemd/smoker.service.in # filled in with the user and folder by install_service.sh
│   ├── tools/relays_off.py   # forces both relay pins low (systemd ExecStopPost)
│   ├── tools/start_stop_ssr.py   # older bare pin check
│   └── tests/                # pytest, no hardware needed
│
├── backend/                  # runs on the Mac (self-contained)
│   ├── requirements.txt
│   ├── server.py             # FastAPI: /api/*, /ws/replay, /ws/live, serves frontend/
│   ├── settings.py           # SMOKER_* environment variables
│   ├── replay.py             # lists and parses cook CSVs
│   ├── replay_player.py      # play / pause / speed / scrub over WebSocket
│   ├── live.py               # relays the Pi's live stream, records it, forwards commands
│   ├── journal.py            # per-cook notes and photos
│   ├── labels.py             # column labels and units
│   ├── generate_sample_cook.py
│   └── tests/
│
├── frontend/                 # the dashboard: index.html, app.js, app.css, vendor/uPlot
├── data/                     # one CSV per cook, copied off the Pi (not in git)
│   ├── cook_YYYY-MM-DD_HHMMSS_<name>.csv  + _meta.json
│   ├── live_<date>_<time>.csv             # backend's recording of the live stream
│   ├── chefiq_<source>_<date>_<time>.csv  # standalone probe logger
│   └── journal/<csv stem>/                # notes and photos, Mac only
├── analysis/
│   ├── merge_probes.py       # line up Pi and Mac probe logs
│   └── test_merge_probes.py
└── docs/
```

The Pi and Mac halves share no code. Their contract is the CSV columns below
and the message formats documented at the top of `control/live_server.py`,
`backend/live.py` and `backend/replay_player.py`.

## Zone and sensor mapping

| Zone | Element | Relay | Grill thermocouple | Drives |
|---|---|---|---|---|
| 1 | 1200W (~10A at 120V) | SSR 1, GPIO22 (pin 15) | MAX31855 #1 (SPI0 CE0, `/dev/spidev0.0`) | its own PID loop |
| 2 | 1200W (~10A at 120V) | SSR 2, GPIO23 (pin 16) | MAX31855 #2 (SPI0 CE1, `/dev/spidev0.1`) | its own PID loop |

### Thermocouple wiring

Both boards share SPI0: CLK and DO are jumpered together; each has its own CS.

| Zone | Bus | CLK | DO (MISO) | CS | Device |
|---|---|---|---|---|---|
| 1 | SPI0 | pin 23 / GPIO11 | pin 21 / GPIO9 | pin 24 / GPIO8 (CE0) | `/dev/spidev0.0` |
| 2 | SPI0 | pin 23 / GPIO11 (shared) | pin 21 / GPIO9 (shared) | pin 26 / GPIO7 (CE1) | `/dev/spidev0.1` |

Power: zone 1 board on pin 1 (3.3V) + pin 6 (GND), zone 2 on pin 17 (3.3V) +
pin 9 (GND). The MAX31855 has no data-in line, so SPI0 MOSI (pin 19 / GPIO10)
is not wired, but the SPI driver still claims it. The bus and chip select are
`zones.<zone>.spi_bus` / `spi_chip_select` in `control/config.yaml`.

SPI0 is turned on with `sudo raspi-config nonint do_spi 0` (`dtparam=spi=on`),
which gives both `spidev0.0` and `spidev0.1`. SPI1 isn't used.

**If SPI1 is ever enabled, use `spi1-1cs`, never `spi1-2cs` or `spi1-3cs`.** SPI1 CE1 is GPIO17 and CE2
is GPIO16; a 2cs/3cs overlay hands them to the SPI driver (GPIO17 was zone 1's
relay pin before the relays moved to GPIO22/23). `settings.py` refuses a relay pin on a bus in use (SPI0: GPIO 7-11,
SPI1: GPIO 18-21) and any SPI1 chip select but 0; `preflight.py` fails if the
boot config has a 2cs/3cs overlay, whether or not a zone uses SPI1.

Chef iQ probes monitor the meat only. They are logged alongside the grill
temps but never drive the relays.

## CSV columns (logged at a steady rate, for the later liquid neural network)

| Column | Meaning |
|---|---|
| timestamp | ISO 8601 time of the sample, with UTC offset |
| zone1_temp_f, zone2_temp_f | grill thermocouple readings; blank on a fault |
| zone1_relay_state, zone2_relay_state | commanded relay duty cycle, 0.0 (off) to 1.0 (full on) |
| zone1_target_f, zone2_target_f | setpoint for each zone |
| probeN_food_f (N = 1-3) | Chef iQ food temp as the probe reports it; slot = the probe's own "Probe N" name |
| probeN_t1_f .. probeN_t4_f | the probe's internal sensors, t1 at the tip to t4 nearest the handle; blank at their 212 F cap |
| probeN_ambient_f | the probe's handle-end (air) sensor |
| probeN_battery_pct | the probe's battery, whole percent |
| cook_state | cooking or paused |
| zone1_board_f, zone2_board_f | MAX31855 cold-junction (enclosure) temperature |
| zone1_fault, zone2_fault | open, short_gnd, short_vcc or spi_error; blank when fine |
| safety | why a relay is held off, if it is |

Blank cells mean no recent reading. New columns are only ever added at the
end. Outdoor temperature (`outdoor_temp_f`, SwitchBot) is planned, not built.

## Safety

- Open or disconnected thermocouple: that zone's relay goes off.
- Over-temperature ceiling (`safety.max_temp_f`, 300 F): both relays go off,
  latched until the cook is resumed or restarted from the UI.
- Enclosure too hot (`safety.max_board_temp_f`): both off, latched.
- Stale data (no good reading within `safety.stale_seconds`): relay off.
- Chef iQ dropouts never affect the control loop.
- Relay failsafe: if the loop stops refreshing a relay for
  `relay.failsafe_timeout_s`, the driver's own thread turns it off.
- Relays off on exit, SIGTERM and errors; the GPIO lock stops two programs
  driving them at once.
- Optional combined-duty cap (`power.max_combined_duty`) for running both
  zones on one 20A circuit.
