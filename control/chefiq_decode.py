"""Chef iQ (CQ60) BLE advertisement decoder. No Bluetooth needed here.

A probe broadcasts manufacturer data under company id 0x05CD, rotating three
packet types. The low nibble of byte 0 is the type; the protocol version is
major = byte1 >> 4, minor = byte1 & 0xF, patch = byte0 >> 4.

  type 0x0  name:        "Probe 1" etc, 12 bytes at [2:14], NUL padded
  type 0x1  temperature: V3 (major 5, verified on a CQ60):
                ambient [2:4], food [4:6], t1 [6:8], t2 [8:10],
                t3 [10:12], t4 [12:14]
            V2 (major 4): battery [2], ambient [4:6], food [6:8],
                t1 [8:10], t2 [10:12], t3 [12:14]
  type 0x3  status:      MAC [2:8]; on major 5 also battery % [8]

Temperatures are signed int16 little-endian, tenths of a degree Celsius. A
sensor with nothing to read reports a large sentinel (>= 32000 raw, e.g.
0x7FFF); it decodes to None. t1 is at the tip, t4 nearest the handle.

The layout follows the MIT-licensed chefiq-ble project
(https://github.com/Invader444/chefiq-ble), checked against its real CQ60
captures in tests/test_chefiq_decode.py.
"""
import struct

CHEFIQ_COMPANY_ID = 0x05CD
MAX_PROBE_PAYLOAD_LEN = 18     # the iQ Sense hub uses longer (>=20 byte) payloads

TYPE_NAME, TYPE_TEMPERATURE, TYPE_STATUS = 0x0, 0x1, 0x3
SENTINEL_MIN_RAW = 32000
TEMP_MIN_C, TEMP_MAX_C = -40.0, 600.0

# major version -> offsets
_LAYOUTS = {
    5: {"ambient": 2, "food": 4, "tips": (6, 8, 10, 12), "battery": None},
    4: {"ambient": 4, "food": 6, "tips": (8, 10, 12), "battery": 2},
}


def c_to_f(c):
    return None if c is None else round(c * 9.0 / 5.0 + 32.0, 1)


def version(msg):
    return (msg[1] >> 4, msg[1] & 0x0F, msg[0] >> 4)


def _temp_c(msg, offset):
    raw = struct.unpack_from("<h", msg, offset)[0]
    if raw >= SENTINEL_MIN_RAW:
        return None
    c = raw / 10.0
    return c if TEMP_MIN_C <= c <= TEMP_MAX_C else None


def decode(payload):
    """Decode one manufacturer-data payload (bytes after the company id).

    Returns a dict with "type" ("name", "temperature" or "status") and its
    fields, or None if the payload isn't a probe packet we understand.
    Temperatures are Celsius floats or None.
    """
    msg = bytes(payload)
    if not 2 <= len(msg) <= MAX_PROBE_PAYLOAD_LEN:
        return None
    ptype = msg[0] & 0x0F
    major = version(msg)[0]
    if major not in _LAYOUTS:
        return None
    if ptype == TYPE_NAME:
        if len(msg) < 14:
            return None
        try:
            name = msg[2:14].rstrip(b"\x00").decode()
        except UnicodeDecodeError:
            return None
        if not name or not name.isprintable():
            return None
        return {"type": "name", "name": name}
    if ptype == TYPE_TEMPERATURE:
        lay = _LAYOUTS[major]
        need = max([lay["ambient"], lay["food"], *lay["tips"]]) + 2
        if len(msg) < need:
            return None
        out = {
            "type": "temperature",
            "ambient_c": _temp_c(msg, lay["ambient"]),
            "food_c": _temp_c(msg, lay["food"]),
            "tips_c": [_temp_c(msg, o) for o in lay["tips"]],
        }
        if lay["battery"] is not None:
            out["battery_pct"] = msg[lay["battery"]]
        return out
    if ptype == TYPE_STATUS:
        if len(msg) < 8:
            return None
        out = {"type": "status", "mac": ":".join(f"{b:02X}" for b in msg[2:8])}
        if major >= 5 and len(msg) >= 10:
            out["battery_pct"] = msg[8]
        return out
    return None


def probe_slot_from_name(name):
    """'Probe 2' -> 2, anything else -> None."""
    parts = (name or "").strip().split()
    if len(parts) == 2 and parts[0].lower() == "probe" and parts[1].isdigit():
        return int(parts[1])
    return None
