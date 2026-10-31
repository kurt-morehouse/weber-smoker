import copy
import sys
from pathlib import Path

import yaml

CONTROL = Path(__file__).resolve().parent.parent
if str(CONTROL) not in sys.path:
    sys.path.insert(0, str(CONTROL))

from settings import CONFIG_PATH, load_config  # noqa: E402


def raw_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def deep_update(d, u):
    for k, v in u.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            deep_update(d[k], v)
        else:
            d[k] = v
    return d


def write_config(tmp_path, overrides=None, delete=None):
    """Write a copy of config.yaml with changes into tmp_path; return its path.
    Paths are pointed into tmp_path so tests never touch real files."""
    c = copy.deepcopy(raw_config())
    c["paths"] = {
        "data_dir": str(tmp_path / "data"),
        "chefiq_raw_log": str(tmp_path / "data/raw/chefiq_log.csv"),
        "chefiq_slots_file": str(tmp_path / "data/chefiq_slots.json"),
        "gpio_lock_file": str(tmp_path / "gpio.lock"),
        "ssr_test_log": str(tmp_path / "data/ssr_test.log"),
        "command_token_file": str(tmp_path / "command_token"),
    }
    if overrides:
        deep_update(c, overrides)
    for dotted in delete or []:
        node = c
        *parents, last = dotted.split(".")
        for p in parents:
            node = node[p]
        del node[last]
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(c))
    return p


def config(tmp_path, overrides=None):
    return load_config(write_config(tmp_path, overrides))


class FakeGPIO:
    """Records every call RPi.GPIO would get. Levels: 1 = HIGH, 0 = LOW."""
    BCM, BOARD, OUT, IN, HIGH, LOW = "BCM", "BOARD", "OUT", "IN", 1, 0
    VERSION = "fake"

    def __init__(self):
        self.mode = None
        self.levels = {}
        self.setup_pins = []
        self.writes = []            # (pin, level)
        self.cleaned = []

    def setwarnings(self, flag):
        pass

    def setmode(self, mode):
        if self.mode not in (None, mode):
            raise ValueError("A different mode has already been set!")
        self.mode = mode

    def setup(self, pin, direction, initial=0):
        self.setup_pins.append(pin)
        self.levels[pin] = initial

    def output(self, pin, level):
        self.writes.append((pin, level))
        self.levels[pin] = level

    def input(self, pin):
        return self.levels.get(pin, 0)

    def cleanup(self, pins=None):
        self.cleaned.append(pins)

    def touched(self):
        return set(self.setup_pins) | {p for p, _ in self.writes}


class ManualClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeSpiDev:
    """Stands in for spidev.SpiDev; returns a fixed 32-bit frame."""
    opened = []                      # class-level log of (bus, device)
    frames = {}                      # (bus, device) -> raw int

    def __init__(self):
        self.bus = self.device = None
        self.max_speed_hz = 0
        self.mode = None

    def open(self, bus, device):
        self.bus, self.device = bus, device
        FakeSpiDev.opened.append((bus, device))

    def xfer2(self, data):
        raw = FakeSpiDev.frames.get((self.bus, self.device), 0)
        return list(raw.to_bytes(4, "big"))

    def close(self):
        pass
