"""place_edit set_pad zone_connection: the per-pad solid zone connection the
boards scripted by hand (pinned core's kicad/gen/pad_zone_full.py, the USB-C
power pins carrying 5 A into the In2 plane; lite-main's j1_shell_solid.py).

The live test runs the op on a copy of pinned core, the board that needed the
script: it undoes the script (J1's eight power pins back to inherit), redoes it
with set_pad, and checks the board comes back the same as the pinned one: the
pads' saved zone_connect, the fill area of every zone layer (thermal spokes
fill less than a solid join) and the DRC counts.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import env  # noqa: E402
import place_edit  # noqa: E402
from checklib import CheckError  # noqa: E402

CORE = REPO / "pinned" / "core" / "kicad"
# the pads pad_zone_full.py made solid on core (J1's VBUS and GND pins)
J1_POWER = ["A1", "A4", "A9", "A12", "B1", "B4", "B9", "B12"]


def test_validate_zone_connection():
    ok = {"version": 1, "ops": [
        {"op": "set_pad", "ref": "J1", "pad": J1_POWER,
         "zone_connection": "solid"},
        {"op": "set_pad", "ref": "J1", "pad": "S1",
         "zone_connection": "inherit"}]}
    assert len(place_edit.validate_ops(ok)) == 2
    with pytest.raises(CheckError, match="zone_connection must be one of"):
        place_edit.validate_ops({"version": 1, "ops": [
            {"op": "set_pad", "ref": "J1", "pad": "A1",
             "zone_connection": "full"}]})
    with pytest.raises(CheckError, match="needs one of net, size, zone_connection"):
        place_edit.validate_ops({"version": 1, "ops": [
            {"op": "set_pad", "ref": "J1", "pad": "A1"}]})


def test_pad_zone_connects_reads_the_pinned_board():
    zc = place_edit._pad_zone_connects(CORE / "core.kicad_pcb", "J1")
    assert all(zc[n] == [2] for n in J1_POWER)
    assert zc["S1"] == [2, 2, 2, 2]          # four shell pads, one number
    assert all(v == [None] for n, v in zc.items()
               if n not in J1_POWER + ["S1"])


@pytest.fixture
def kicad():
    cli = env.find_kicad_cli()
    if cli is None or env.find_kicad_python(cli) is None:
        pytest.skip("KiCad bundled python not installed")
    return cli


def _fill_areas(pcb: Path) -> dict:
    import geom
    bg = geom.BoardGeom.from_file(pcb)
    areas: dict = {}
    for z in bg.zones_of():
        for layer in z.fills:
            k = (z.net, layer)
            areas[k] = areas.get(k, 0.0) + z.fill_area(layer)
    return {k: round(a, 3) for k, a in areas.items()}


def _set(pcb: Path, how: str) -> dict:
    payload, _ = place_edit.run([
        "--pcb", str(pcb), "--refill", "--op", json.dumps(
            {"op": "set_pad", "ref": "J1", "pad": J1_POWER,
             "zone_connection": how})])
    return payload


def test_live_pad_zone_full_on_a_copy_of_core(kicad, tmp_path):
    import kc
    work = tmp_path / "kicad"
    shutil.copytree(CORE, work)
    pcb = work / "core.kicad_pcb"
    base_drc = kc.run_drc(kicad, pcb)["counts"]
    base_fill = _fill_areas(pcb)

    # undo pad_zone_full.py: thermal spokes take less copper around J1
    assert _set(pcb, "inherit")["refilled"]
    zc = place_edit._pad_zone_connects(pcb, "J1")
    assert all(zc[n] == [None] for n in J1_POWER)
    spoked = _fill_areas(pcb)
    assert sum(spoked.values()) < sum(base_fill.values()) - 0.01

    # redo it with the hwde op: the pinned board's result again
    payload = _set(pcb, "solid")
    assert payload["verified"] and payload["refilled"]
    assert payload["results"][0]["pads"] == sorted(J1_POWER)
    zc = place_edit._pad_zone_connects(pcb, "J1")
    assert all(zc[n] == [2] for n in J1_POWER)
    again = _fill_areas(pcb)
    assert again.keys() == base_fill.keys()
    for k, area in base_fill.items():
        assert again[k] == pytest.approx(area, abs=0.05), k
    assert kc.run_drc(kicad, pcb)["counts"] == base_drc
