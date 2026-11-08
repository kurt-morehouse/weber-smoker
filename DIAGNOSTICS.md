# Diagnostics

How to find out what's wrong, one part of the system at a time. Each section
lists the symptom, what usually causes it, and the commands that tell you
which.

Commands marked **Pi** run on the Pi (`ssh pi@raspberrypi`, or your values
from `pi.env`); **Mac** ones run in the project folder on the Mac. On the Pi,
most commands assume you're in `~/smoker/control`.

Wiring reference: [`docs/wiring.md`](docs/wiring.md) and
`docs/wiring_diagram.svg`.

> **Safety:** keep the heating elements **unplugged** for everything here
> except the last step of section 6. Unplug the smoker before touching any
> 120V wiring.

## Contents

0. [Where to start](#0-where-to-start)
1. [Power to the Pi](#1-power-to-the-pi)
2. [Network, SSH and the Pi's address](#2-network-ssh-and-the-pis-address)
3. [The smoker service and preflight](#3-the-smoker-service-and-preflight)
4. [config.yaml](#4-configyaml)
5. [Thermocouples (MAX31855 boards and SPI)](#5-thermocouples-max31855-boards-and-spi)
6. [Relays (SSRs) and the 120V side](#6-relays-ssrs-and-the-120v-side)
7. [Chef iQ probes and Bluetooth](#7-chef-iq-probes-and-bluetooth)
8. [Live stream to the Mac](#8-live-stream-to-the-mac)
9. [Mac backend and dashboard](#9-mac-backend-and-dashboard)
10. [Cooks, safety cutoffs and temperature control](#10-cooks-safety-cutoffs-and-temperature-control)
11. [Code, tests, deploy and git](#11-code-tests-deploy-and-git)
12. [Collecting information to ask for help](#12-collecting-information-to-ask-for-help)

---

## 0. Where to start

Work from the bottom up. Each layer needs the one below it:

1. **Power**: `vcgencmd get_throttled` says `0x0` (section 1)
2. **Network**: you can `ssh` to the Pi (section 2)
3. **Preflight**: every line PASS, apart from WARNs (sections 3–7)
4. **Service**: `systemctl status smoker` says `active (running)` (section 3)
5. **Live stream**: `./start.sh` says "Pi is streaming" (section 8)
6. **Dashboard**: Live mode shows "Pi connected" (section 9)

The single most useful command, on the Pi:

```bash
sudo systemctl stop smoker
cd ~/smoker/control && .venv/bin/python preflight.py
```

It checks almost everything and says what failed. Stop the service first:
otherwise its own preflight, retried every 15 s, can hold the relay lock
while you test.

---

## 1. Power to the Pi

A Pi short on power looks broken in unrelated ways: flaky Wi-Fi and
Bluetooth, random restarts, SD card corruption. Rule this out first.

**Check (Pi):**

```bash
vcgencmd get_throttled
```

| Result | Meaning |
|---|---|
| `throttled=0x0` | Fine since boot |
| `0x50000` | Under-voltage happened earlier, not now |
| `0x50005` | Under-voltage **now**, and the CPU is being slowed |
| other | Decode bits: 0 under-voltage now, 1 freq capped, 2 throttled, 3 soft temp limit; bits 16–19 the same, "since boot" |

**Pi 3B LEDs:** red = power, **steady on** when the supply is above ~4.63V,
off or flickering when it dips. Green = SD card activity.

**Causes and fixes**

- **Thin or long micro-USB cable**: the most common cause on a 3B. Use a
  short cable sold for charging or for the Pi (20 AWG power wires).
- **Supply can't hold 5V under load**: use the official Pi 3 supply
  (5.1V 2.5A). The extra 0.1V makes up for cable loss.
- **Extras on the Pi's USB or 5V pins**: unplug them while testing. The
  MAX31855 boards and SSR inputs draw only a few mA from 3.3V and aren't the
  cause.

Reboot after fixing, run a full preflight, and check again: it should stay
`0x0`.

---

## 2. Network, SSH and the Pi's address

The Pi's address, user and folder are in **`pi.env`** (`SMOKER_PI_HOST`,
`SMOKER_PI_USER`, `SMOKER_PI_DIR`). `deploy.sh`, `start.sh` and the backend
all read it.

**"Could not resolve hostname" or `ping raspberrypi` fails (Mac)**

A home network usually has no DNS domain; names work through mDNS or the
router (see below). Try each:

```bash
ping -c 2 raspberrypi
ping -c 2 raspberrypi.local
ping -c 2 192.168.1.122        # the Pi's IP, if you know it
```

Use whichever answers in `pi.env`. On the Pi, `hostname -I` prints its IP.
For a permanent fix, give the Pi a **DHCP reservation** in your router so its
IP never changes, and put that IP in `pi.env`.

**`ssh` asks for a password every time, or "Permission denied (publickey)"**

The key from before the crash is gone. Make a new one (Mac):

```bash
ls ~/.ssh/id_ed25519 || ssh-keygen -t ed25519
ssh-copy-id pi@raspberrypi
ssh pi@raspberrypi true && echo "key login works"
```

**"REMOTE HOST IDENTIFICATION HAS CHANGED" (after reflashing the SD card)**

```bash
ssh-keygen -R raspberrypi
ssh-keygen -R raspberrypi.local
```

then `ssh` again and accept the new key.

**Pi reachable, then drops off Wi-Fi**: check power first (section 1). Then,
on the Pi, `iw dev wlan0 link` shows signal strength, and
`journalctl -b | grep -i wlan` shows disconnects.

---

**Pi enable Wi-Fi**:

```bash
sudo nmcli dev wifi connect "Morehousegnet" password "M0r3h0us34321!"
  
```

## 3. The smoker service and preflight

`smoker.service` runs `preflight.py` first (`ExecStartPre`). If any check
FAILs, the service doesn't start, and systemd retries every 15 s. Then it runs
`main.py`. After it stops, however it stopped, `tools/relays_off.py` forces
both relay pins low.

**Status and logs (Pi):**

```bash
systemctl status smoker --no-pager
journalctl -u smoker -n 60 --no-pager      # last 60 lines
journalctl -u smoker -f                     # follow live; Ctrl+C to stop
journalctl -u smoker -b --no-pager | grep -E "FAIL|WARNING|Error"
```

| Status | Meaning |
|---|---|
| `active (running)` | Fine. main.py prints a status line every 30 s. |
| `activating (auto-restart)`, `ExecStartPre ... status=1/FAILURE` | Preflight failed: run it by hand (below) |
| `ExecStart ... status=2` | config.yaml rejected (section 4) |
| `status=3` | RPi.GPIO or spidev won't load in the venv (below) |
| `status=4` | Another program holds the relays (GPIO lock, below) |
| `inactive (dead)` | Stopped: `sudo systemctl start smoker` |
| `could not be found` | Not installed: `~/smoker/control/install_service.sh` |

**Run preflight by hand (Pi):**

```bash
sudo systemctl stop smoker
cd ~/smoker/control && .venv/bin/python preflight.py
```

| Check | Fails when | Go to |
|---|---|---|
| SPI | `/dev/spidev0.0` or `/dev/spidev0.1` missing | section 5 |
| SPI1 overlay | a forbidden `spi1-2cs`/`3cs` line (takes relay 1's pin) | section 5 |
| MAX31855 zone1 / zone2 | the board doesn't answer, or the probe has a fault | section 5 |
| GPIO library | RPi.GPIO won't load | below |
| GPIO | can't set up the relay pins | section 6 |
| Relays off | a relay pin reads HIGH before anything turned it on | section 6 |
| Live stream (WARN) | websockets missing or too old | section 8 |
| Bluetooth (WARN) | adapter off or busy, or no probes heard | section 7 |

WARN lines never stop the service. Each program prints `REAL GPIO` or
`SIMULATED` first: on the Pi, it must say REAL GPIO.

**"RPi.GPIO won't load" or "spidev won't load"**

```bash
sudo apt install python3-rpi.gpio python3-spidev
ls ~/smoker/control/.venv || ~/smoker/control/install_service.sh
grep include-system-site-packages ~/smoker/control/.venv/pyvenv.cfg   # must say true
```

The venv must be made with `--system-site-packages` to see apt's RPi.GPIO
and spidev. If it says `false`, delete `.venv` and run `install_service.sh`
again. On Raspberry Pi OS, `RPi.GPIO 0.7.1a4` is the rpi-lgpio compatibility
version, and that's fine.

**"the relays are held by …" (GPIO lock)**: only one program may drive the
relays. Usually the service is running: `sudo systemctl stop smoker`. To see
who holds it: `cat /tmp/smoker-gpio.lock`. A crashed holder releases it
automatically.

**Service stuck restarting**: fix whatever preflight reports, then
`sudo systemctl restart smoker`. After changing code: `./deploy.sh push`
(Mac), then restart. After changing `systemd/smoker.service.in`, run
`install_service.sh` again.

---

## 4. config.yaml

Every setting is checked at startup. A bad value stops `main.py` with a
message naming the setting.

**Check it by hand (Pi or Mac):**

```bash
cd ~/smoker/control && .venv/bin/python settings.py     # Mac: uv run python settings.py
```

It prints `config OK`, or every problem at once:

| Message | Fix |
|---|---|
| `pid.kp: missing` | Restore the key (compare with GitHub's copy) |
| `pid.kpp: unknown setting (misspelled?)` | Fix the spelling |
| `spi.bus moved to zones.<zone>.spi_bus` | Old config: delete `spi.bus`, give each zone `spi_bus` |
| `... outside 1024..65535` | Value out of range |
| `GPIO18 belongs to SPI1 CE0` | That pin is taken by SPI; pick another relay pin |
| `both zones use the same gpio_pin` / `(spi_bus, spi_chip_select)` | Each zone needs its own |
| `SPI1 only has chip select 0` | Only if a zone is on SPI1: with spi1-1cs it must use `spi_chip_select: 0` |

To start over: `git checkout control/config.yaml` restores the committed copy.

---

## 5. Thermocouples (MAX31855 boards and SPI)

| Zone | Bus | Vin | GND | DO (SO) | CS | CLK (SCK) | Device |
|---|---|---|---|---|---|---|---|
| 1 | SPI0 | pin 1 | pin 6 | pin 21 (GPIO9) | pin 24 (GPIO8) | pin 23 (GPIO11) | `/dev/spidev0.0` |
| 2 | SPI0 | pin 17 | pin 9 | pin 21 (GPIO9), shared | pin 26 (GPIO7) | pin 23 (GPIO11), shared | `/dev/spidev0.1` |

Both boards share SPI0: DO and CLK are jumpered together on pins 21 and 23,
and each board has its own CS. Probe: **T+ yellow, T− red** (US colors).

### 5.1 SPI devices missing (preflight: SPI FAIL)

```bash
ls /dev/spidev*                                   # want spidev0.0 and spidev0.1
grep -n "spi" /boot/firmware/config.txt           # /boot/config.txt on older OS
```

- No `spidev0.*`: `sudo raspi-config nonint do_spi 0`, then reboot. That
  gives both `spidev0.0` and `spidev0.1`; SPI1 isn't needed.
- **Never use `dtoverlay=spi1-2cs` or `spi1-3cs`.** Their extra chip selects
  are GPIO17 and GPIO16. Preflight fails if one is present.

### 5.2 Are the SPI pins assigned to SPI?

```bash
pinctrl get 7-11               # older OS: raspi-gpio get 7-11
```

GPIO 7–11 should say `a0` (SPI0). `ip` or `op`
means something else has taken the pin. Reboot and check again.
`pinctrl get 22,23` must **not** say a0 or a4 (those are the relays).

### 5.3 Read the raw values (Pi, service stopped)

```bash
cd ~/smoker/control
.venv/bin/python - <<'EOF'
import spidev
for cs, name in ((0, "zone1 SPI0 CE0"), (1, "zone2 SPI0 CE1")):
    s = spidev.SpiDev(); s.open(0, cs); s.max_speed_hz = 500000; s.mode = 0
    for _ in range(3):
        raw = int.from_bytes(bytes(s.xfer2([0]*4)), "big")
        tc = raw >> 18; tc = tc - 0x4000 if tc & 0x2000 else tc
        cj = (raw >> 4) & 0xFFF; cj = cj - 0x1000 if cj & 0x800 else cj
        print(f"{name}: 0x{raw:08X}  probe {tc*0.25*9/5+32:6.1f}F  "
              f"board {cj*0.0625*9/5+32:5.1f}F  fault bit {raw>>16 & 1}  OC/SCG/SCV {raw & 7:03b}")
    s.close()
EOF
```

| Raw value | Meaning | Check |
|---|---|---|
| `0x00000000` (all zeros) | **The board sent nothing.** The Pi's MISO pins are pulled down, so any break reads as zeros. | 5.4 |
| `0xFFFFFFFF` (all ones) | DO stuck high | DO shorted to 3.3V; wrong pin |
| fault bit 1, `001` | Thermocouple **open**: probe unplugged or broken | probe screws, probe continuity |
| fault bit 1, `010` | Probe **shorted to GND** | probe touching the grill body with bare wire; frayed insulation |
| fault bit 1, `100` | Probe **shorted to VCC** | damaged wiring |
| sensible board temp, probe ≈ room temp | **Working** | — |

### 5.4 All zeros: the board isn't answering

Most likely first:

1. **Header pins not soldered.** Boards often ship with the pins loose. A
   jumper pushed onto an unsoldered pin makes no reliable contact.
2. **DO on MOSI instead of MISO.** Both boards' DO go to pin **21**, not
   pin 19.
3. **No power.** Measure Vin to GND **on the board**: about 3.3V.
4. **CS on the wrong pin** (24 for zone 1, 26 for zone 2). Without CS the
   board never drives DO.
5. **CLK missing** (pin 23 for both).
6. **The other board drags the shared DO line.** If zone 1 stops reading as
   soon as the zone 2 board is connected, the zone 2 board is faulty.

**Loopback test: is it the Pi or the board?** Unplug both boards' DO wires
from pin 21. Put a jumper from **pin 19 to pin 21**, then:

```bash
.venv/bin/python -c "
import spidev
for cs in (0, 1):
    s = spidev.SpiDev(); s.open(0, cs); s.max_speed_hz = 500000
    print(f'SPI0 CE{cs}:', s.xfer2([0xA5, 0x5A, 0x12, 0x34])); s.close()"
```

- `[165, 90, 18, 52]`: the Pi's SPI works, so the problem is that board or
  its wiring.
- `[0, 0, 0, 0]`: SPI0 isn't working on the Pi (5.1, 5.2).

Remove the jumpers afterwards.

**Swap test:** swap the two boards' CS wires (pins 24 and 26). If the fault
moves with the board, it's the board. If it stays with the CS pin, it's that
wire or pin.

### 5.5 Readings that are wrong rather than missing

| Symptom | Cause |
|---|---|
| Temperature **falls** when you warm the probe | T+ and T− swapped (yellow is +) |
| Reads a few degrees off at room temp | Normal ±2°C tolerance; compare in ice water (32°F) and boiling water (~212°F) |
| Jumps around, occasional faults | Probe wire run next to 120V wiring; loose screw terminal; under-voltage (section 1) |
| Board temperature high (the `*_board_f` column) | Enclosure hot. Readings drift; safety turns both relays off above `safety.max_board_temp_f` |
| Reads but is wildly high/low | Not a K-type probe, or a K probe on extension wire that isn't K-type |

---

## 6. Relays (SSRs) and the 120V side

| Zone | Pi pin | GPIO | SSR terminal | Ground |
|---|---|---|---|---|
| 1 | 15 | GPIO22 | SSR 1, terminal 3 (+) | pin 14 to terminal 4 (−) |
| 2 | 16 | GPIO23 | SSR 2, terminal 3 (+) | pin 20 to terminal 4 (−) |

Active-high: GPIO HIGH = SSR on. Terminals 1 and 2 are the AC switch.

### 6.1 Test the control side (elements unplugged)

```bash
sudo systemctl stop smoker
cd ~/smoker/control && .venv/bin/python ssr_test.py
```

It shows `1 = zone1 GPIO22 (pin 15)` and `2 = zone2 GPIO23 (pin 16)`. Pulse
each zone and watch **that SSR's LED** light, and only that one. Every pulse
is logged:

```bash
tail -20 ~/smoker/data/ssr_test.log
```

| Symptom | Cause |
|---|---|
| Wrong SSR lights | Control wires swapped between SSRs |
| No LED | Wire on the wrong pin; terminals 3 and 4 swapped (polarity matters); bad ground |
| LED dim or flickers | The Pi's 3.3V is marginal for that SSR. Drive it through an NPN transistor (2N2222, 1kΩ base) from 5V |
| `the relays are held by main.py` | Stop the service first (section 3) |
| `only runs from an interactive terminal` | Run it in an ssh session, not through a script or pipe |

**Pin check without the test tool:** `pinctrl get 22,23` shows `lo` (off) or
`hi` (on). `tools/relays_off.py` forces both low at any time.

### 6.2 Relay pin HIGH at preflight ("Relays off" FAIL)

Something drove the pin high before preflight. Run
`.venv/bin/python tools/relays_off.py`, then check for other programs:
`ps aux | grep -i -E "python|gpio" | grep -v grep`.

### 6.3 The 120V side (unplugged while wiring)

- Each zone: **L (hot) → 15A AC-rated breaker/fuse → SSR terminal 1**;
  **terminal 2 → element → N (neutral)**. Never fuse or switch neutral.
- Ground (green) bonded to the grill body and the metal enclosure.
- The breaker on the parts list is a 30A car-audio part: make sure it's rated
  for 120V AC, and use about 15A per zone.

| Symptom | Cause |
|---|---|
| Element never heats, SSR LED on | Breaker tripped or off; terminal 1/2 not connected; element open (measure ~12Ω cold for 1200W) |
| Element stays on with the LED off | SSR failed shorted (they fail "on"): replace it. Off-state leakage can also make an LED bulb glow dimly, but not an element heat |
| Breaker trips | Element shorted to the grill body; both zones on one 20A circuit at full power (set `power.max_combined_duty: 1.6`) |
| SSR very hot | No heat sink or no thermal paste |

**First live test:** lid open, someone watching, one zone at a time, with
`ssr_test.py`.

---

## 7. Chef iQ probes and Bluetooth

Probes only log. They never control the relays, and a Bluetooth problem never
stops the smoker (it's a WARN in preflight).

**Bluetooth adapter (Pi):**

```bash
rfkill list                                   # Bluetooth must not be "Soft blocked: yes"
bluetoothctl show | head -8                   # look for "Powered: yes"
sudo rfkill unblock bluetooth
sudo systemctl restart bluetooth && sleep 5 && sudo bluetoothctl power on
grep -n -i "bt\|bluetooth" /boot/firmware/config.txt
```

| Symptom | Fix |
|---|---|
| `No powered Bluetooth adapters found` / `POWERED_OFF` | The unblock and power-on commands above |
| `org.bluez.Error.Busy` | Wait a few seconds after unblocking, restart bluetooth, try again; reboot. Check power (section 1) |
| Boot config has `dtoverlay=disable-bt` | Delete that line and reboot |
| Commands typed on one line fail | Separate them with `&&` as above, or press Enter after each |

**No probes heard ("no Chef iQ probes heard")**

- Take the probes **out of the charger**: they only broadcast when out.
- Readings start after a probe's **name packet** ("Probe 1") arrives, up to
  ~30 s. After that its address is remembered in `data/chefiq_slots.json`.
- A probe renamed in the app keeps its new slot: "Probe 2" always lands in
  the `probe2_*` columns.

**See the raw packets (Pi or Mac):**

```bash
cd ~/smoker/control
.venv/bin/python chefiq_logger.py --raw        # Mac: uv run python chefiq_logger.py --raw
tail -5 ../data/raw/chefiq_log.csv
```

Payloads starting `0150…` are temperature packets, `0350…` status, `0050…`
names. Only V3 (CQ60, protocol 5.x) is verified.

| Symptom | Cause |
|---|---|
| t1–t4 blank, food and ambient fine | Normal above 212°F: the internal sensors cap there |
| Values go blank mid-cook | Silent > 30 s (`chefiq.stale_seconds`): out of range or battery |
| Battery warning on the dashboard | ≤ 20%: charge before the next cook |
| Mac logger hears nothing | macOS Bluetooth permission for Terminal: System Settings → Privacy & Security → Bluetooth |

---

## 8. Live stream to the Mac

`main.py` serves rows on `ws://<pi>:8765`. The Mac backend connects to it.

**Is it listening? (Mac)**

```bash
nc -z -G 3 raspberrypi 8765 && echo "listening" || echo "not listening"
```

(`./start.sh` runs the same check.) On the Pi:
`ss -ltnp | grep 8765`.

| Symptom | Cause |
|---|---|
| "nothing is listening on port 8765" | Service not running (section 3) |
| Log says `WARNING: live stream off: … websockets …` | `cd ~/smoker/control && ~/.local/bin/uv sync` |
| Log says `can't listen on 0.0.0.0:8765` | Port taken: `sudo ss -ltnp \| grep 8765` shows by whom |
| Port open but the dashboard shows no rows | Firewall on the Pi (rare): `sudo ufw status` |

**Cook commands refused**

| Message | Fix |
|---|---|
| `the Pi has no command token file` | `./deploy.sh push` from the Mac (sends `control/command_token`), then restart the service |
| `wrong command token` | The Mac and Pi tokens differ: `./deploy.sh push`, restart |
| `no command token on the Mac` | Run `./start.sh` once (it creates one), or check `control/command_token` exists |

---

## 9. Mac backend and dashboard

**Start (Mac):** `./start.sh`, then open http://localhost:8000.
`./start.sh --offline` replays saved cooks without the Pi.

| Message or symptom | Fix |
|---|---|
| `no backend venv` | `cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| `the Pi at … doesn't answer` | Section 2, or `--offline` |
| `Address already in use` | Another copy is running. Close it, or `SMOKER_PORT=8001 ./start.sh` |
| `no SMOKER_PI_HOST` | `pi.env` missing or empty: restore with `git checkout pi.env` |
| Dashboard blank | Look at the browser console (Option-Cmd-J); check the backend log lines `[backend]` |
| "Pi not reachable" in Live | Backend can't reach the Pi (sections 2, 8). Hover the pill for the exact error |
| "last row N s ago" in red | Rows stopped arriving: service stopped or the Pi went offline |
| No cooks in the list | `./deploy.sh pull` to copy them from the Pi into `data/` |
| Photo upload refused (HEIC) | `backend/.venv/bin/pip install pillow-heif`, or export as JPEG |
| `ws proxy error: write EPIPE` | Harmless (old Vite setup): a browser tab closed mid-send |

**Backend health (Mac):**

```bash
curl -s localhost:8000/api/cooks | head -c 300
cd backend && .venv/bin/python -m pytest -q
```

---

## 10. Cooks, safety cutoffs and temperature control

The `safety` column (and the red banner on the dashboard) says why a relay is
off.

| Safety message | Meaning | Clears when |
|---|---|---|
| `thermocouple fault: open` (etc.) | That zone's probe failed: that zone off | the probe reads again |
| `stale reading (N s old)` | No good reading for `safety.stale_seconds` | readings resume |
| `over-temperature: 305 F > 300 F` | **Both** zones off, latched | you Pause then Resume, or start a new cook |
| `enclosure too hot` | Board temperature above `safety.max_board_temp_f`; both off, latched | cool the enclosure, then Resume |

Relays are also off whenever no cook is running (IDLE) or the cook is paused.

**Temperature control**

| Symptom | Adjust in `config.yaml` (`pid:`) |
|---|---|
| Overshoots the target, then oscillates | Lower `kp`; raise `kd` |
| Settles below the target | Raise `ki` (slowly), or `integral_limit` |
| Slow to reach the target with duty under 100% | Raise `kp` |
| Relay clicks on and off rapidly | Raise `relay.window_seconds` or `relay.min_on_seconds` |
| Both zones capped below target | `power.max_combined_duty` is limiting them (intended on one circuit) |

Change one value at a time, and compare cooks on the dashboard. After editing:
`./deploy.sh push` and `sudo systemctl restart smoker` (this ends a running
cook).

**Try it without hardware (Mac):**

```bash
cd control && uv run python main.py --simulate
```

---

## 11. Code, tests, deploy and git

**Tests**

```bash
cd ~/smoker/control && ~/.local/bin/uv run pytest     # Pi (uv run uses the project venv)
cd control && uv run pytest                           # Mac
cd backend && .venv/bin/python -m pytest              # Mac
```

Run tests with `uv run`, not the system `python3`: the system Python lacks
websockets, and the live-stream tests then fail. They now say why.

**Deploy (Mac):**

```bash
./deploy.sh push       # control/ to the Pi (also sends command_token)
./deploy.sh pull       # finished cooks back to data/
```

Prints `pushing control/ to pi@raspberrypi:smoker/control/`. If the target is
wrong, fix `pi.env`.

**git**

| Message | Fix |
|---|---|
| `There is no tracking information for the current branch` | This copy isn't the GitHub clone (e.g. the old zip). Clone fresh: `git clone https://github.com/kurt-morehouse/weber-smoker.git` |
| `refusing to merge unrelated histories` | Same: clone fresh, copy over `control/command_token` and `data/` |
| `push declined due to email privacy restrictions` | `git config user.email 3169884+kurt-morehouse@users.noreply.github.com`, then `git commit --amend --reset-author --no-edit` |
| Your local edits conflict with a pull | `git stash`, `git pull`, then `git stash pop` and keep what you need |

---

## 12. Collecting information to ask for help

Run this on the Pi and paste the output:

```bash
cd ~/smoker/control
{
  echo "== model";      cat /proc/device-tree/model; echo
  echo "== os";         grep PRETTY /etc/os-release
  echo "== power";      vcgencmd get_throttled
  echo "== ip";         hostname -I
  echo "== code";       git -C ~/smoker log --oneline -1 2>/dev/null || ls -l ~/smoker/control/main.py
  echo "== spi";        ls /dev/spidev* 2>&1; grep -n "spi\|bt" /boot/firmware/config.txt
  echo "== pins";       pinctrl get 7-11,22,23 2>/dev/null || raspi-gpio get 2>/dev/null | grep -E "GPIO (7|8|9|10|11|22|23):"
  echo "== bluetooth";  rfkill list bluetooth; bluetoothctl show 2>/dev/null | grep -E "Powered|Name"
  echo "== service";    systemctl status smoker --no-pager | head -5
  echo "== log";        journalctl -u smoker -n 25 --no-pager
  echo "== config";     .venv/bin/python settings.py | tail -1
} 2>&1
```

And for the thermocouples, the raw-value script from section 5.3.
