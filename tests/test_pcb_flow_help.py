"""`pcb-flow --help` prints every usage entry in bin/pcb-flow's header.

The help used to print a fixed line range, which went stale twice (#894,
#1417) when a usage edit pushed the last entries out of it.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "pcb-flow"


def _usage_entries() -> list[str]:
    head = WRAPPER.read_text().split("\n#\n#   pcb-flow", 1)[1]
    head = "#   pcb-flow" + head.split("\n#\n", 1)[0]
    return re.findall(r"^#   (pcb-flow .*?)(?:  |$)", head, re.M)


def test_help_lists_every_usage_entry():
    entries = _usage_entries()
    assert len(entries) >= 10, entries
    out = subprocess.run(["sh", str(WRAPPER), "--help"], capture_output=True,
                         text=True, timeout=30, check=True).stdout
    missing = [e for e in entries if e not in out]
    assert not missing, missing
    # the help stops at the header's end, before the workaround notes
    assert "A shim here" not in out
    assert out.rstrip().endswith("(or the published copy's own)")
