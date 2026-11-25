"""Extract a compact CSV from BYD Vehicle debug dumps: one row per dump file.

Columns: utc time, category, soc, power_gear, charge_state, gl (battery power, W).
Used for the before/after drain comparison. No location or identifiers are written.

Usage: python tests/extract_dumps.py DUMP_DIR SINCE OUT.csv [UNTIL]   (SINCE/UNTIL like 20261011)
DUMP_DIR is the BYD Vehicle debug dump folder, <config>/.storage/byd_vehicle_debug.
"""

import csv
import json
import os
import re
import sys

if len(sys.argv) < 4:
    sys.exit("Usage: " + __doc__.split("Usage: ")[1].strip())
D, since, out = sys.argv[1], sys.argv[2], sys.argv[3]
until = sys.argv[4] if len(sys.argv) > 4 else "99999999"

names = sorted(e.name for e in os.scandir(D) if since <= e.name[:8] <= until)
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["utc", "category", "soc", "power_gear", "charge_state", "gl"])
    for n in names:
        cat = re.sub(r"^[0-9T]+Z_", "", n).removesuffix(".json")
        soc = gear = charge = gl = ""
        if cat == "telemetry":
            try:
                with open(os.path.join(D, n), encoding="utf-8") as fh:
                    rt = json.load(fh)["sections"].get("realtime") or {}
                soc, gear = rt.get("elec_percent"), rt.get("power_gear")
                charge, gl = rt.get("charge_state"), rt.get("gl")
            except Exception:
                pass
        w.writerow([n[:15], cat, soc, gear, charge, gl])
print(f"{len(names)} rows -> {out}")
