"""check_thermal lists the power ICs it sees but can't judge (#884 follow-up).

On the training boards the check was blind on 17 of 18: no constraints.json,
and its one line was "no power IC thermal_parts.yaml knows", with no word
on which parts it saw. Now each power IC is either screened (an
exposed-pad package with a cited row) or a candidate with its reason.
Pure tests on synthetic boards; no toolchain.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_thermal_inferred import _board, _pour, _run, _vias  # noqa: E402

import check_thermal  # noqa: E402


def _fp(ref, value, lib, pads, mpn=None, at=(20, 20)):
    props = (f'    (property "MPN" "{mpn}" (at 0 0 0) (layer "F.Fab"))\n'
             if mpn else "")
    body = "".join(
        f'    (pad "{n}" smd rect (at {x} {y}) (size {w} {h}) '
        f'(layers "F.Cu") (net "{net}"))\n'
        for n, x, y, w, h, net in pads)
    return (f'  (footprint "{lib}" (at {at[0]} {at[1]}) (layer "F.Cu")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'    (property "Value" "{value}" (at 0 0 0) (layer "F.Fab"))\n'
            f'{props}{body})\n')


def _htssop(ref="U5", value="DRV8874PWPR"):
    """An HTSSOP-16 driver: a 3 x 5 mm GND exposed pad, two pins."""
    return _fp(ref, value, "Package_SO:HTSSOP-16-1EP_4.4x5mm",
               [("17", 0, 0, 3.0, 5.0, "GND"),
                ("1", -2.9, -2.1, 1.0, 0.3, "/m/OUT1"),
                ("2", -2.9, -1.5, 1.0, 0.3, "+12V")])


def _sot23_6(ref="U2", value="TPS561201DDCR", lib="Package_TO_SOT_SMD:"
             "SOT-23-6", mpn=None):
    return _fp(ref, value, lib,
               [(str(i + 1), -1.1 if i < 3 else 1.1, -0.95 + 0.95 * (i % 3),
                 1.0, 0.6, ["GND", "/sw", "+5V", "/fb", "+12V", "/en"][i])
                for i in range(6)], mpn=mpn)


def test_planted_ep_driver_on_its_bare_pad_fires(tmp_path):
    """Planted fault: a DRV8874 (cited row, PWP HTSSOP exposed pad) with
    only its own pad for copper."""
    p = _run(_board(tmp_path, _htssop()))
    vs = [v for v in p["violations"] if v["kind"] == "thermal_area"]
    assert len(vs) == 1, p
    assert vs[0]["refs"] == ["U5"] and vs[0]["inferred"] is True
    f = p["checked"][0]
    assert f["package"] == "PWP HTSSOP-16" and f["advisory"] is True
    assert f["theta_ratio"] > check_thermal.THETA_RATIO_MAX
    assert f["tj_max_c"] == 150


def test_quiet_twin_ep_driver_on_pours_with_vias(tmp_path):
    p = _run(_board(tmp_path, _htssop() + _pour("F.Cu") + _pour("B.Cu")
                    + _vias()))
    assert [v for v in p["violations"] if v["kind"] == "thermal_area"] == []
    f = p["checked"][0]
    assert f["inferred"] and f["theta_ratio"] <= check_thermal.THETA_RATIO_MAX


def test_a_no_pad_switcher_with_no_load_is_not_judged(tmp_path):
    """No exposed pad: its heat leaves by its leads, so it turns on its load,
    and none is stated. Not judged, with the screen ceiling given."""
    p = _run(_board(tmp_path, _sot23_6()))
    assert p["violations"] == [] and p["checked"] == []
    [c] = p["candidates"]
    assert c["reason"] == "no_load" and c["package"] == "DDC SOT-23-6"
    assert c["advisory"] is True
    # (150 - 25) / theta_JA, the most it sheds on the JEDEC board
    assert c["p_max_screen_w"] == pytest.approx(
        (c["tj_max_c"] - 25) / c["theta_ja_datasheet_cw"], rel=0.01)
    [line] = p["not_judged"]
    assert line.startswith("U2 (TPS561201DDCR): no_load") and "W" in line


def test_a_thermal_entry_takes_the_part_off_the_candidates(tmp_path):
    pcb = _board(tmp_path, _sot23_6())
    p = _run(pcb, {"thermal": [{"ref": "U2", "net": "GND",
                                "power_w": 0.2}]})
    assert "candidates" not in p


def test_the_mpn_suffix_beats_the_footprint_name():
    """TLV62569DBV drawn on a TSOT-25 footprint is the DBV package (188.2
    C/W), not the DDC thin SOT the footprint name suggests."""
    row = check_thermal.thermal_row({"value": "TLV62569DBV", "props": {}},
                                    check_thermal.thermal_rows())
    pkg, _ = check_thermal.row_package(
        {"value": "TLV62569DBV", "props": {}, "lib": "TSOT-25:TSOT-25"}, row)
    assert pkg["name"].startswith("DBV")
    pkg, _ = check_thermal.row_package(
        {"value": "TLV62569", "props": {}, "lib": "TSOT-25:TSOT-25"}, row)
    assert pkg["name"].startswith("DDC")


def test_an_ldo_is_listed_for_netlist_audit_not_left_unjudged(tmp_path):
    p = _run(_board(tmp_path, _sot23_6(value="AMS1117-3.3")))
    [c] = p["candidates"]
    assert c["reason"] == "ldo_heat" and "netlist_audit" in c["detail"]
    assert p.get("not_judged", []) == []


def test_an_ldo_with_no_theta_is_no_package_theta(tmp_path):
    p = _run(_board(tmp_path, _sot23_6(value="XC6206P332MR")))
    [c] = p["candidates"]
    assert c["reason"] == "no_package_theta"
    assert c["seen_in"] == "source_limits.yaml"


def test_every_row_package_has_a_cited_theta():
    for row in check_thermal.thermal_rows():
        for pkg in row.get("packages") or []:
            assert pkg["theta_ja_c_w"] > 0 and pkg["cite"], row["match"]
            assert isinstance(pkg["exposed_pad"], bool), row["match"]
        if row.get("packages"):
            assert row["tj_max_c"] and row["cite"], row["match"]
