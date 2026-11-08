"""MAX31855 (K-type) thermocouple reader over SPI.

Both boards share SPI0: CLK (pin 23 / GPIO11) and DO (pin 21 / GPIO9) are
jumpered together; each board has its own chip select:
    zone 1: bus 0, device 0 -> SPI0 CE0 (/dev/spidev0.0), CS pin 24 / GPIO8
    zone 2: bus 0, device 1 -> SPI0 CE1 (/dev/spidev0.1), CS pin 26 / GPIO7
SPI0: sudo raspi-config nonint do_spi 0, then reboot.
Never add spi1-2cs or spi1-3cs: SPI1 CE1/CE2 are GPIO17/16.

read() returns (probe_f, board_f) in Fahrenheit, or raises ThermocoupleFault.
The control code treats a fault as "turn that zone's relay off".

simulate=True fakes the reading (for example on your Mac); it is never chosen
for you: without it, spidev failing to load raises
hardware.HardwareUnavailable. Set reader.sim_temp_f to control the fake
reading.
"""
import hardware


class ThermocoupleFault(Exception):
    """Probe disconnected, shorted, or the chip isn't responding.

    kind is one of "open", "short_gnd", "short_vcc" or "spi_error", the
    strings safety.SafetyMonitor.evaluate_zone() expects as fault_type.
    """

    def __init__(self, message, kind="spi_error"):
        super().__init__(message)
        self.kind = kind


def c_to_f(c):
    return c * 9.0 / 5.0 + 32.0


def decode_max31855(raw):
    """Decode one 32-bit MAX31855 frame into (probe_f, board_f).

    Bits 31..18: thermocouple, 14-bit signed, 0.25 C per count
    Bit 16:      fault
    Bits 15..4:  cold junction (the board), 12-bit signed, 0.0625 C per count
    Bits 2..0:   SCV (short to VCC), SCG (short to GND), OC (open circuit)
    """
    if raw == 0x00000000:
        # The board sent nothing. The Pi's MISO pin (GPIO9) has a
        # pull-down, so an unpowered board, a loose or misplaced DO, CS or CLK
        # wire, or unsoldered header pins all read as all zeros.
        raise ThermocoupleFault(
            "no response from MAX31855: read 0x00000000, all zeros (the board sent nothing: "
            "check its header pins are soldered, 3.3V across Vin/GND, and DO on MISO, "
            "CS and CLK on the right pins)", "spi_error")
    if raw == 0xFFFFFFFF:
        # DO stuck high: shorted to 3.3V, or a different chip answering.
        raise ThermocoupleFault("no response from MAX31855: read 0xFFFFFFFF, all ones "
                                "(DO stuck high: shorted to 3.3V?)", "spi_error")
    if raw & 0x00010000:
        if raw & 0x1:
            raise ThermocoupleFault("thermocouple open circuit (unplugged?)", "open")
        if raw & 0x2:
            raise ThermocoupleFault("thermocouple shorted to GND", "short_gnd")
        if raw & 0x4:
            raise ThermocoupleFault("thermocouple shorted to VCC", "short_vcc")
        raise ThermocoupleFault("MAX31855 fault bit set", "spi_error")
    probe = (raw >> 18) & 0x3FFF          # 14-bit signed, 0.25 C per count
    if probe & 0x2000:
        probe -= 0x4000
    board = (raw >> 4) & 0xFFF           # 12-bit signed, 0.0625 C per count
    if board & 0x800:
        board -= 0x1000
    return c_to_f(probe * 0.25), c_to_f(board * 0.0625)


def encode_max31855(probe_c, board_c, fault_bits=0):
    """Build a frame (used by tests and the simulator)."""
    p = int(round(probe_c / 0.25)) & 0x3FFF
    b = int(round(board_c / 0.0625)) & 0xFFF
    raw = (p << 18) | (b << 4) | (fault_bits & 0x7)
    if fault_bits:
        raw |= 0x00010000
    return raw


SPI_MODE = 0      # MAX31855: clock idles low, data read on the rising edge
FRAME_BYTES = 4   # one 32-bit frame per read


class ThermocoupleReader:
    def __init__(self, bus, device, max_speed_hz, sim_temp_f=70.0, sim_board_f=72.0,
                 simulate=False):
        self.bus = bus
        self.device = device
        self.simulate = simulate
        self.sim_temp_f = sim_temp_f
        self.sim_board_f = sim_board_f
        self.sim_fault = None     # set to a ThermocoupleFault kind to fake a fault
        self._spi = None
        if not self.simulate:
            self._spi = hardware.load_spidev().SpiDev()
            self._spi.open(bus, device)
            self._spi.max_speed_hz = max_speed_hz
            self._spi.mode = SPI_MODE

    @property
    def device_path(self):
        return f"/dev/spidev{self.bus}.{self.device}"

    def read(self):
        """Return (probe_temp_f, board_temp_f). Raises ThermocoupleFault."""
        if self.simulate:
            if self.sim_fault:
                raise ThermocoupleFault(f"simulated {self.sim_fault} fault", self.sim_fault)
            return self.sim_temp_f, self.sim_board_f
        try:
            data = self._spi.xfer2([0] * FRAME_BYTES)
        except OSError as e:
            raise ThermocoupleFault(f"SPI read failed on {self.device_path}: {e}", "spi_error")
        return decode_max31855(int.from_bytes(bytes(data), "big"))

    def close(self):
        if self._spi is not None:
            self._spi.close()
            self._spi = None
