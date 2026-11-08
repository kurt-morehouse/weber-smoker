# ~/smoke/.venv/bin/python - <<'EOF'
import spidev
for cs, name in ((0, "zone1 SPI0 CE0"), (1, "zone2 SPI0 CE1")):
    s = spidev.SpiDev(); s.open(0, cs); s.max_speed_hz = 500000; s.mode = 0
    frames = [int.from_bytes(bytes(s.xfer2([0]*4)), "big") for _ in range(3)]
    s.close()
    for raw in frames:
        tc = raw >> 18; tc = tc - 0x4000 if tc & 0x2000 else tc
        print(f"{name}: 0x{raw:08X}  probe {tc*0.25*9/5+32:6.1f}F  fault bits {raw & 7:03b}")
