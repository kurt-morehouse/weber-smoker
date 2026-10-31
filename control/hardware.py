"""Loads the Pi's hardware libraries, RPi.GPIO and spidev, and says which
mode a program is running in.

Nothing here ever falls back to a fake: if a library won't load,
HardwareUnavailable says why, and the program stops. Only --simulate fakes
hardware, and every program prints the banner from banner() first so the
mode is never in doubt.
"""
import importlib
import sys


class HardwareUnavailable(Exception):
    """A hardware library can't be imported in this Python."""


def _load(name, attr=None):
    try:
        module = importlib.import_module(name)
    except Exception as e:  # ImportError, RuntimeError ("not running on a Pi") ...
        raise HardwareUnavailable(
            f"{name} won't load in {sys.executable}: {type(e).__name__}: {e}") from e
    return module


def load_gpio():
    """The RPi.GPIO module, or raise HardwareUnavailable."""
    return _load("RPi.GPIO")


def load_spidev():
    """The spidev module, or raise HardwareUnavailable."""
    return _load("spidev")


def banner(simulate):
    if simulate:
        return "=== SIMULATED: no relays or thermocouples are touched ==="
    return f"=== REAL GPIO: relays and thermocouples are live ({sys.executable}) ==="


def mode_name(simulate):
    return "simulated" if simulate else "real"
