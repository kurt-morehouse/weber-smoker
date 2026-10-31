"""Loads and checks control/config.yaml.

load_config() returns the config as a plain dict, or raises ConfigError
listing every problem at once: a missing key, an unknown (misspelled) key, a
value of the wrong type or out of range, or settings that clash (both zones
on one relay pin, a relay pin that an SPI bus owns, ...).

Relative paths under paths: are resolved against the config file's folder.

    python settings.py              # check config.yaml and print it
"""
import sys
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).with_name("config.yaml")
ZONES = ("zone1", "zone2")

BCM_PINS = (2, 27)          # usable BCM GPIO numbers on the 40-pin header
SPI_BUSES = (0, 1)
SPI_CHIP_SELECTS = (0, 1)   # CE0 / CE1
# SPI1 is enabled with dtoverlay=spi1-1cs, which creates only CE0;
# spi1-2cs/3cs would add CE1 = GPIO17, zone 1's relay pin.
SPI1_CHIP_SELECTS = (0,)
# Pins an SPI bus owns once enabled. A relay must not use them.
SPI_BUS_PINS = {
    0: {7: "SPI0 CE1", 8: "SPI0 CE0", 9: "SPI0 MISO", 10: "SPI0 MOSI", 11: "SPI0 SCLK"},
    1: {18: "SPI1 CE0", 19: "SPI1 MISO", 20: "SPI1 MOSI", 21: "SPI1 SCLK"},
}
MAX31855_MAX_SPI_HZ = 5_000_000


class ConfigError(Exception):
    """config.yaml is missing, unreadable, or has invalid values."""


class Num:
    """A number in [lo, hi]."""

    def __init__(self, lo, hi, integer=False):
        self.lo, self.hi, self.integer = lo, hi, integer

    def check(self, value, where):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{where}: expected a number, got {value!r}"
        if self.integer and int(value) != value:
            return f"{where}: expected a whole number, got {value!r}"
        if not self.lo <= value <= self.hi:
            return f"{where}: {value} is outside {self.lo}..{self.hi}"
        return None


class Bool:
    def check(self, value, where):
        if not isinstance(value, bool):
            return f"{where}: expected true or false, got {value!r}"
        return None


class Text:
    def check(self, value, where):
        if not isinstance(value, str) or not value.strip():
            return f"{where}: expected text, got {value!r}"
        return None


ZONE_SCHEMA = {
    "gpio_pin": Num(*BCM_PINS, integer=True),
    "spi_bus": Num(*SPI_BUSES, integer=True),
    "spi_chip_select": Num(*SPI_CHIP_SELECTS, integer=True),
    "default_setpoint_f": Num(100, 300),
}

SCHEMA = {
    "zones": {z: ZONE_SCHEMA for z in ZONES},
    "spi": {
        "max_speed_hz": Num(1, MAX31855_MAX_SPI_HZ, integer=True),
    },
    "loop": {"interval_seconds": Num(0.2, 10)},
    "pid": {
        "kp": Num(0, 10),
        "ki": Num(0, 1),
        "kd": Num(0, 100),
        "integral_limit": Num(0, 1),
    },
    "relay": {
        "window_seconds": Num(1, 120),
        "min_on_seconds": Num(0, 10),
        "failsafe_timeout_s": Num(1, 60),
    },
    "safety": {
        "max_temp_f": Num(150, 500),
        "stale_seconds": Num(1, 60),
        "max_board_temp_f": Num(80, 200),
        "max_setpoint_f": Num(100, 500),
        "min_setpoint_f": Num(32, 300),
    },
    "power": {"max_combined_duty": Num(0.1, 2.0)},
    "chefiq": {
        "enabled": Bool(),
        "max_probes": Num(1, 6, integer=True),
        "stale_seconds": Num(5, 600),
    },
    "paths": {
        "data_dir": Text(),
        "chefiq_raw_log": Text(),
        "chefiq_slots_file": Text(),
        "gpio_lock_file": Text(),
        "ssr_test_log": Text(),
        "command_token_file": Text(),
    },
    "ssr_test": {
        "max_pulse_seconds": Num(0.5, 30),
        "refresh_seconds": Num(0.1, 5),
    },
    "websocket": {
        "enabled": Bool(),
        "host": Text(),
        "port": Num(1024, 65535, integer=True),
        "history_rows": Num(10, 100_000, integer=True),
        "retry_seconds": Num(0.5, 300),
        "max_pending_commands": Num(1, 1000, integer=True),
    },
    "simulation": {
        "start_temp_f": Num(-20, 200),
        "board_temp_f": Num(-20, 200),
        "ambient_f": Num(-20, 120),
        "heat_rate_f_per_s": Num(0.01, 20),
        "loss_per_s": Num(0, 1),
        "speedup": Num(0.1, 1000),
        "fake_probes": Num(0, 6, integer=True),
    },
}

# Keys that used to exist, and where they went.
MOVED = {
    ("spi", "bus"): "spi.bus moved to zones.<zone>.spi_bus (each zone has its own SPI bus)",
}


def _walk(schema, data, path, problems):
    if not isinstance(data, dict):
        problems.append(f"{'.'.join(path) or 'config'}: expected a section, got {data!r}")
        return
    for key, rule in schema.items():
        where = ".".join(path + [key])
        if key not in data:
            problems.append(f"{where}: missing")
            continue
        if isinstance(rule, dict):
            _walk(rule, data[key], path + [key], problems)
        else:
            msg = rule.check(data[key], where)
            if msg:
                problems.append(msg)
    for key in data:
        if key not in schema:
            moved = MOVED.get(tuple(path + [key]))
            problems.append(moved or f"{'.'.join(path + [key])}: unknown setting (misspelled?)")


def _cross_checks(cfg, problems):
    z = cfg["zones"]
    z1, z2 = z["zone1"], z["zone2"]
    if z1["gpio_pin"] == z2["gpio_pin"]:
        problems.append("zones: both zones use the same gpio_pin")
    if (z1["spi_bus"], z1["spi_chip_select"]) == (z2["spi_bus"], z2["spi_chip_select"]):
        problems.append("zones: both zones use the same (spi_bus, spi_chip_select) pair")
    buses_in_use = {z[name]["spi_bus"] for name in ZONES}
    for name in ZONES:
        zc = z[name]
        if zc["spi_bus"] == 1 and zc["spi_chip_select"] not in SPI1_CHIP_SELECTS:
            problems.append(
                f"zones.{name}: SPI1 only has chip select 0 (dtoverlay=spi1-1cs); "
                f"got {zc['spi_chip_select']}")
        for bus in buses_in_use:
            owner = SPI_BUS_PINS[bus].get(zc["gpio_pin"])
            if owner:
                problems.append(
                    f"zones.{name}.gpio_pin: GPIO{zc['gpio_pin']} belongs to {owner}; "
                    f"pick a relay pin no SPI bus uses")
        s = cfg["safety"]
        if not s["min_setpoint_f"] <= zc["default_setpoint_f"] <= s["max_setpoint_f"]:
            problems.append(f"zones.{name}.default_setpoint_f: outside "
                            f"safety.min_setpoint_f..max_setpoint_f")
    s = cfg["safety"]
    if s["max_setpoint_f"] >= s["max_temp_f"]:
        problems.append("safety.max_setpoint_f must be below safety.max_temp_f")
    if s["min_setpoint_f"] >= s["max_setpoint_f"]:
        problems.append("safety.min_setpoint_f must be below safety.max_setpoint_f")
    r = cfg["relay"]
    if r["min_on_seconds"] * 2 >= r["window_seconds"]:
        problems.append("relay.min_on_seconds must be under half of relay.window_seconds")
    if cfg["loop"]["interval_seconds"] >= r["failsafe_timeout_s"]:
        problems.append("loop.interval_seconds must be shorter than relay.failsafe_timeout_s")
    if cfg["ssr_test"]["refresh_seconds"] >= r["failsafe_timeout_s"]:
        problems.append("ssr_test.refresh_seconds must be shorter than relay.failsafe_timeout_s")
    import safety  # local import: safety imports nothing from here
    if cfg["ssr_test"]["max_pulse_seconds"] > safety.ABSOLUTE_MAX_SSR_TEST_PULSE_S:
        problems.append("ssr_test.max_pulse_seconds is above the hard cap in safety.py")


def resolve_paths(cfg, base_dir):
    base = Path(base_dir)
    for key, value in cfg["paths"].items():
        p = Path(value).expanduser()
        cfg["paths"][key] = str(p if p.is_absolute() else (base / p).resolve())
    return cfg


def validate(cfg):
    """Return a list of problems (empty if the config is good)."""
    problems = []
    _walk(SCHEMA, cfg, [], problems)
    if not problems:
        _cross_checks(cfg, problems)
    return problems


def load_config(path=None):
    path = Path(path or CONFIG_PATH)
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found")
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: not valid YAML: {e}")
    if cfg is None:
        raise ConfigError(f"{path}: empty")
    problems = validate(cfg)
    if problems:
        raise ConfigError(f"{path} has {len(problems)} problem(s):\n  - " + "\n  - ".join(problems))
    return resolve_paths(cfg, path.parent)


if __name__ == "__main__":
    try:
        c = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    except ConfigError as e:
        print(e)
        sys.exit(1)
    print(yaml.safe_dump(c, sort_keys=False))
    print("config OK")
