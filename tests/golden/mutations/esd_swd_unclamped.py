"""Mutant: esd-swd-unclamped (blinky2).

SIDECAR fault (copper is the golden's, copied unchanged): constraints.json no
longer lists J2 (the SWD header) as an internal, bench-only connector, so it
counts as a user-facing port. The board has no clamp on it, so /SWDIO and
/SWCLK run from the header pins straight to the MCU. Must be caught by
check_esd (kind "esd_unclamped", net /SWDIO).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutlib


def surgery(text):
    cons = mutlib.golden_json("blinky2", "constraints.json")
    esd = cons["esd"]
    esd["internal"] = [ref for ref in esd["internal"] if ref != "J2"]
    return text, {"sidecars": {"constraints.json": mutlib.dump_json(cons)},
                  "group": "J2"}


if __name__ == "__main__":
    sys.exit(mutlib.run("esd-swd-unclamped", "blinky2", surgery))
