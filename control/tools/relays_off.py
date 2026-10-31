"""Drive both relay pins low. systemd runs this after main.py stops
(ExecStopPost), however it stopped.

RPi.GPIO leaves a pin as it was if the process is killed outright (SIGKILL,
out of memory), and main.py can't clean up then. This makes sure a relay that
was on doesn't stay on until the next start. Safe to run by hand any time:

    .venv/bin/python tools/relays_off.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hardware  # noqa: E402
from settings import ZONES, ConfigError, load_config  # noqa: E402

FALLBACK_PINS = (17, 27)       # if config.yaml can't be read, use the wired pins


def relay_pins(cfg_path=None):
    try:
        cfg = load_config(cfg_path)
        return sorted({cfg["zones"][z]["gpio_pin"] for z in ZONES} | set(FALLBACK_PINS))
    except ConfigError:
        return list(FALLBACK_PINS)


def all_off(gpio, pins):
    gpio.setwarnings(False)
    gpio.setmode(gpio.BCM)
    for p in pins:
        gpio.setup(p, gpio.OUT, initial=gpio.LOW)
        gpio.output(p, gpio.LOW)
    return pins


def main(argv=None, gpio=None):
    pins = relay_pins(argv[0] if argv else None)
    try:
        gpio = gpio or hardware.load_gpio()
    except hardware.HardwareUnavailable as e:
        print(f"relays_off: {e}")
        return 1
    all_off(gpio, pins)
    print("relays off: " + ", ".join(f"GPIO{p}" for p in pins))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
