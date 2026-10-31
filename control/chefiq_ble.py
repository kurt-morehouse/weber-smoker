"""Scans Chef iQ BLE probes (up to chefiq.max_probes) and keeps their latest
readings, via chefiq_decode.

Probe slots follow the probe's own name: "Probe 2" is always probe2 in the
CSV. A probe's readings start once its name packet arrives (up to ~30 s);
after that its address is remembered in paths.chefiq_slots_file, so later
runs place it straight away.

ProbeTracker does the bookkeeping and is fed advertisements by BleScanner
(real Bluetooth, via bleak) or FakeProbes (--simulate). Readings older than
chefiq.stale_seconds come back as None, so a dropout logs as blank cells.
Nothing here can affect the relays.
"""
import asyncio
import json
import threading
import time
from pathlib import Path

import chefiq_decode as dec

FIELDS = ("food", "t1", "t2", "t3", "t4", "ambient", "battery")
TIP_CAP_C = 100.0   # the probe's internal t1-t4 sensors stop at 212 F; blank there


class ProbeTracker:
    def __init__(self, max_probes, stale_seconds, slots_file=None, clock=time.monotonic):
        self.max_probes = max_probes
        self.stale_seconds = stale_seconds
        self.slots_file = Path(slots_file) if slots_file else None
        self._clock = clock
        self._lock = threading.Lock()
        self.slots = {}            # address -> slot number
        self.latest = {}           # slot -> {"values": {...}, "seen": t}
        self.heard = {}            # address -> local name (anything Chef iQ-like)
        self._load_slots()

    def _load_slots(self):
        if self.slots_file and self.slots_file.exists():
            try:
                data = json.loads(self.slots_file.read_text())
                self.slots = {a: int(s) for a, s in data.items()
                              if 1 <= int(s) <= self.max_probes}
            except (ValueError, OSError):
                self.slots = {}

    def _save_slots(self):
        if not self.slots_file:
            return
        try:
            self.slots_file.parent.mkdir(parents=True, exist_ok=True)
            self.slots_file.write_text(json.dumps(self.slots, indent=2, sort_keys=True))
        except OSError:
            pass

    def feed(self, address, payload, local_name=None):
        """Handle one advertisement's Chef iQ manufacturer data."""
        pkt = dec.decode(payload)
        if pkt is None:
            return None
        now = self._clock()
        with self._lock:
            self.heard[address] = local_name or self.heard.get(address) or "CQ60"
            if pkt["type"] == "name":
                slot = dec.probe_slot_from_name(pkt["name"])
                if slot and 1 <= slot <= self.max_probes and self.slots.get(address) != slot:
                    # One address per slot: a re-named probe takes the slot over.
                    self.slots = {a: s for a, s in self.slots.items() if s != slot}
                    self.slots[address] = slot
                    self._save_slots()
                return pkt
            slot = self.slots.get(address)
            if slot is None:
                return pkt         # wait for its name packet
            entry = self.latest.setdefault(slot, {"values": {}, "seen": {}})
            vals, seen = entry["values"], entry["seen"]
            if pkt["type"] == "temperature":
                vals["food"] = dec.c_to_f(pkt["food_c"])
                vals["ambient"] = dec.c_to_f(pkt["ambient_c"])
                tips = list(pkt["tips_c"]) + [None] * (4 - len(pkt["tips_c"]))
                for i, c in enumerate(tips[:4], start=1):
                    vals[f"t{i}"] = None if (c is None or c >= TIP_CAP_C) else dec.c_to_f(c)
                for k in ("food", "ambient", "t1", "t2", "t3", "t4"):
                    seen[k] = now
            if "battery_pct" in pkt:
                vals["battery"] = pkt["battery_pct"]
                seen["battery"] = now
            return pkt

    def snapshot(self):
        """{slot: {field: value or None}} for slots 1..max_probes, stale -> None."""
        now = self._clock()
        out = {}
        with self._lock:
            for slot in range(1, self.max_probes + 1):
                entry = self.latest.get(slot, {"values": {}, "seen": {}})
                row = {}
                for f in FIELDS:
                    t = entry["seen"].get(f)
                    fresh = t is not None and now - t <= self.stale_seconds
                    row[f] = entry["values"].get(f) if fresh else None
                out[slot] = row
        return out

    def heard_names(self):
        with self._lock:
            return sorted(set(self.heard.values()))


class BleScanner:
    """Runs a bleak scanner in its own thread and feeds a ProbeTracker."""

    def __init__(self, tracker, raw_log=None):
        self.tracker = tracker
        self.raw_log = raw_log          # callable(address, name, rssi, payload) or None
        self.error = None
        self._thread = None
        self._loop = None
        self._stop = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="chefiq-ble", daemon=True)
        self._thread.start()
        return self

    def _on_adv(self, device, adv):
        payload = adv.manufacturer_data.get(dec.CHEFIQ_COMPANY_ID)
        if payload is None:
            return
        name = adv.local_name or getattr(device, "name", None)
        if self.raw_log:
            self.raw_log(device.address, name, adv.rssi, bytes(payload))
        self.tracker.feed(device.address, payload, name)

    def _run(self):
        try:
            from bleak import BleakScanner
        except Exception as e:
            self.error = f"bleak won't load: {type(e).__name__}: {e}"
            return
        self._loop = asyncio.new_event_loop()
        self._stop = asyncio.Event()

        async def main():
            backoff = 2.0
            while not self._stop.is_set():
                try:
                    async with BleakScanner(detection_callback=self._on_adv):
                        self.error = None
                        backoff = 2.0
                        await self._stop.wait()
                except Exception as e:        # adapter busy, Bluetooth off ...
                    self.error = f"Bluetooth scan failed: {type(e).__name__}: {e}"
                    try:
                        await asyncio.wait_for(self._stop.wait(), backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, 60.0)

        try:
            self._loop.run_until_complete(main())
        finally:
            self._loop.close()

    def stop(self):
        if self._loop and self._stop:
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=5)


class FakeProbes:
    """--simulate: probe N sits in zone (N-1)%2+1; food creeps toward the zone."""

    def __init__(self, tracker, count, zone_temps_f, start_f=70.0):
        self.tracker = tracker
        self.count = min(count, tracker.max_probes)
        self.zone_temps_f = zone_temps_f      # callable -> [zone1_f, zone2_f]
        self.food_f = {n: start_f for n in range(1, self.count + 1)}
        for n in range(1, self.count + 1):
            self.tracker.feed(self._addr(n), self._name_packet(n), "CQ60")

    @staticmethod
    def _addr(n):
        return f"FA:KE:00:00:00:0{n}"

    @staticmethod
    def _name_packet(n):
        return bytes([0x00, 0x50]) + f"Probe {n}".encode().ljust(12, b"\x00") + b"\x0a\x2b"

    @staticmethod
    def _temp_packet(ambient_c, food_c, tips_c):
        vals = [ambient_c, food_c] + tips_c
        body = b"".join(int(round(v * 10)).to_bytes(2, "little", signed=True) for v in vals)
        return bytes([0x01, 0x50]) + body + b"\x00\x00"

    @staticmethod
    def _status_packet(battery):
        return bytes([0x03, 0x50]) + bytes(6) + bytes([battery, 30]) + bytes(6)

    def step(self, dt):
        zones = self.zone_temps_f()
        for n in range(1, self.count + 1):
            ambient_f = zones[(n - 1) % 2]
            self.food_f[n] += (ambient_f - self.food_f[n]) * 0.0006 * dt
            food_c = (self.food_f[n] - 32) / 1.8
            amb_c = (ambient_f - 32) / 1.8
            tips = [food_c + i * (amb_c - food_c) * 0.15 for i in range(4)]
            self.tracker.feed(self._addr(n), self._temp_packet(amb_c, food_c, tips), "CQ60")
            self.tracker.feed(self._addr(n), self._status_packet(88 - n), "CQ60")
