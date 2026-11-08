# Parts list (dual zone)

Recovered from `weber_smoker_parts_list_v3_dualzone.xlsx` (lost in the crash).
"Paid" is what you recorded paying; the rest are rough estimates.

| Part | Product / model | Qty | Paid | Notes |
|---|---|---|---|---|
| Heating element, ~1100–1200W, 120V | Stanbroil / WADEO 1200W replacement element | 1 (+1 owned) | $21.99 | One already owned from the Inkbird tests; this matches it. |
| Solid state relay, 25A | SSR-25DA (yours are DMWD), 3–32VDC in, 24–380VAC out | 2 | $19.98 | ~9–10A per element; 25A rating leaves headroom. |
| Heat sink for SSR | aluminum, sized for SSR-25DA | 2 | | Skip if the relays came with one. Use thermal paste. |
| Thermocouple amplifier | MAX31855 breakout (K-type), SPI | 2 | $25.98 | One per zone, both on SPI0 (shared DO/CLK, separate CS). |
| K-type thermocouple probe | high-temp, rated well above 400 F | 2 | $29.96 | Grate level near each element, out of direct radiant heat. |
| Enclosure | stainless/weatherproof box, ~8x6x4 in | 1 | $15.99 | Metal: it must be grounded. Keep it out of the hot zone. |
| Wire | 12 AWG high-temp | 1 | $14.99 | Each zone gets its own fuse/breaker. |
| Circuit breaker | **see note** | 2 | $14.99 | The one bought is a 30A car-audio unit: check it's rated for 120V AC. 15A AC-rated per zone is the right size. |
| Terminal blocks | 30A, 4-position barrier strip (8 screws) | 1 | $5.99 | L / N / G distribution. |
| Jumper wires | female-to-female, Pi GPIO | 1 | $5.49 | |
| Raspberry Pi + 5V 5A USB supply | | 1 | | |
| Chef iQ CQ60 probes | | up to 3 | | Optional: logged only. |

Recorded total: $155.36.

## Electrical

Each element draws ~9.2–10A at 120V; both on together is ~18–20A. A 20A
circuit should only carry ~16A continuously, so either power each zone from a
separate circuit or cap combined duty in software
(`power.max_combined_duty: 1.6`). Use fused, properly rated wiring, and have
an electrician check the setup if unsure.
