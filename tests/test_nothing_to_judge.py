"""Each PCB check says so when a board gives it nothing to judge (#1295):
an empty board passes with a nothing_to_judge reason, so the coverage count
reads it as not judged rather than as a clean pass."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

EMPTY = """(kicad_pcb (version 20240108) (generator "pcbnew")
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user)
  (37 "F.SilkS" user) (38 "B.Mask" user) (39 "F.Mask" user)
  (44 "Edge.Cuts" user))
 (gr_rect (start 0 0) (end 20 20) (stroke (width 0.1) (type default))
  (fill none) (layer "Edge.Cuts")))
"""

CHECKS = {
    "check_esd": "no external connector",
    "check_mounting": "too small to need mounting holes",
    "check_wicking": "no via and no SMD pad",
    "check_fp_geometry": "no footprint could be compared",
    "check_fp_package": "no part whose footprint and package",
    "check_silk": "no silk and no reference text",
}


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_empty_board_has_nothing_to_judge(tmp_path, name):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(EMPTY, encoding="utf-8")
    payload, _ = importlib.import_module(name).run(["--pcb", str(pcb)])
    assert payload["status"] == "pass"
    assert CHECKS[name] in payload["nothing_to_judge"]
