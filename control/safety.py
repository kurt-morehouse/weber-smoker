"""Watchdog rules: decides whether each zone's relay may be on at all.

Defaults built in:
- Open or disconnected thermocouple (any fault): that zone's relay off.
- Stale data (no good reading within stale_seconds): that zone's relay off.
- Over-temperature ceiling: either zone above max_temp_f -> BOTH relays off,
  and they stay off until the cook is resumed or restarted from the UI.
- Board (enclosure) too hot: both off, latched the same way.
- Chef iQ dropouts never affect this: probes are logged, not used here.

The relay driver adds its own failsafe on top (relay off if the loop stops
refreshing it), and power capping lives in main.py.
"""
from dataclasses import dataclass

# Hard limits no config value can raise.
ABSOLUTE_MAX_TEMP_F = 500.0
ABSOLUTE_MAX_SSR_TEST_PULSE_S = 30.0

FAULT_KINDS = ("open", "short_gnd", "short_vcc", "spi_error")


@dataclass
class ZoneVerdict:
    allowed: bool
    reason: str = ""


class SafetyMonitor:
    def __init__(self, max_temp_f, stale_seconds, max_board_temp_f):
        self.max_temp_f = min(max_temp_f, ABSOLUTE_MAX_TEMP_F)
        self.stale_seconds = stale_seconds
        self.max_board_temp_f = max_board_temp_f
        self.latched = None         # reason both relays are held off, or None

    def evaluate_zone(self, temp_f, fault_type, reading_age_s, board_f=None):
        """Verdict for one zone from its latest reading.

        temp_f: last good probe reading (None if never read)
        fault_type: None, or one of FAULT_KINDS for the latest read
        reading_age_s: seconds since the last good reading (None if never)
        """
        if self.latched:
            return ZoneVerdict(False, self.latched)
        if fault_type:
            return ZoneVerdict(False, f"thermocouple fault: {fault_type}")
        if temp_f is None or reading_age_s is None:
            return ZoneVerdict(False, "no reading yet")
        if reading_age_s > self.stale_seconds:
            return ZoneVerdict(False, f"stale reading ({reading_age_s:.1f} s old)")
        if temp_f > self.max_temp_f:
            self.latched = f"over-temperature: {temp_f:.0f} F > {self.max_temp_f:.0f} F"
            return ZoneVerdict(False, self.latched)
        if board_f is not None and board_f > self.max_board_temp_f:
            self.latched = (f"enclosure too hot: board {board_f:.0f} F > "
                            f"{self.max_board_temp_f:.0f} F")
            return ZoneVerdict(False, self.latched)
        return ZoneVerdict(True)

    def clear_latch(self):
        """Called when a cook is started or resumed by hand."""
        self.latched = None


def cap_combined_duty(duties, max_combined):
    """Scale duties down together so their sum stays at or under max_combined."""
    total = sum(duties)
    if total <= max_combined or total == 0:
        return list(duties)
    k = max_combined / total
    return [d * k for d in duties]
