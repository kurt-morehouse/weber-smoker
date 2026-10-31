"""Display names and units for the CSV columns, served at /api/labels."""

MAX_PROBES = 3


def _probe_labels():
    out = {}
    for n in range(1, MAX_PROBES + 1):
        out[f"probe{n}_food_f"] = {"label": f"Probe {n} food", "unit": "°F", "group": f"probe{n}"}
        for i in range(1, 5):
            where = "tip" if i == 1 else ("handle end" if i == 4 else f"sensor {i}")
            out[f"probe{n}_t{i}_f"] = {"label": f"Probe {n} t{i} ({where})", "unit": "°F",
                                      "group": f"probe{n}", "internal": True}
        out[f"probe{n}_ambient_f"] = {"label": f"Probe {n} ambient", "unit": "°F",
                                     "group": f"probe{n}"}
        out[f"probe{n}_battery_pct"] = {"label": f"Probe {n} battery", "unit": "%",
                                       "group": f"probe{n}"}
    return out


LABELS = {
    "columns": {
        "timestamp": {"label": "Time", "unit": ""},
        "zone1_temp_f": {"label": "Zone 1 grill", "unit": "°F", "group": "zones"},
        "zone2_temp_f": {"label": "Zone 2 grill", "unit": "°F", "group": "zones"},
        "zone1_target_f": {"label": "Zone 1 target", "unit": "°F", "group": "zones"},
        "zone2_target_f": {"label": "Zone 2 target", "unit": "°F", "group": "zones"},
        "zone1_relay_state": {"label": "Zone 1 relay duty", "unit": "", "group": "relays"},
        "zone2_relay_state": {"label": "Zone 2 relay duty", "unit": "", "group": "relays"},
        "zone1_board_f": {"label": "Zone 1 board (enclosure)", "unit": "°F"},
        "zone2_board_f": {"label": "Zone 2 board (enclosure)", "unit": "°F"},
        "zone1_fault": {"label": "Zone 1 fault", "unit": ""},
        "zone2_fault": {"label": "Zone 2 fault", "unit": ""},
        "cook_state": {"label": "Cook state", "unit": ""},
        "safety": {"label": "Safety", "unit": ""},
        **_probe_labels(),
    },
    "max_probes": MAX_PROBES,
    "battery_warn_pct": 20,
}
