"""fpwaivers (#813): a vendor footprint's own pads to its own NPTH pegs.

The MC-311D USB-C receptacle (pd-trigger J1) puts its GND and VBUS pads 0.18
and 0.21 mm from its locating pegs; KiCad 10.0.5+ holds them to its 0.25 mm
min_hole_clearance, so DRC 0 was out of reach with no waiver. Pure tests cover
the yaml validation, the rule text and merge_dru; smoke tests run kicad-cli on
a copy of the frozen routed pd-trigger to show the waiver clears exactly the
vendor geometry (quiet case) and nothing else (fault cases: a peg moved under
min_mm, a track that isn't J1's near the peg).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import fpwaivers  # noqa: E402

ROUTE = REPO / "tests" / "fixtures" / "stages" / "pd_trigger" / "route"
MC311D = "*:USB-C-SMD_MC-311D"
GOOD = {"footprint": MC311D, "constraint": "hole_clearance",
        "between": "own_npth", "min_mm": 0.18,
        "fab": "JLC 'NPTH to Track 0.2mm'", "reason": "vendor land pattern",
        "issue": 813}


def _load(tmp_path, *entries):
    p = tmp_path / "w.yaml"
    p.write_text(yaml.safe_dump({"waivers": list(entries)}))
    return fpwaivers.load(p)


# ------------------------------------------------------------------ pure

def test_shipped_file_has_the_mc311d_entry_cited():
    entries = fpwaivers.load()
    (e,) = [x for x in entries if x["footprint"] == MC311D]
    assert e["min_mm"] == pytest.approx(0.18)
    assert e["issue"] == 813
    assert "jlcpcb.com" in e["fab"] and "0.2mm" in e["fab"]
    # the floor sits under every measured gap, not under the fab's own figure
    # by more than the 0.02 mm the GND pads need
    assert min(e["measured_mm"].values()) >= e["min_mm"]
    assert e["min_mm"] >= 0.18


@pytest.mark.parametrize("bad, msg", [
    ({"footprint": "USB-C-SMD_MC-311D"}, "not lib:name"),
    ({"footprint": "aiee:x') || ('1"}, "not lib:name"),
    ({"constraint": "clearance"}, "constraint"),
    ({"between": "anything"}, "between"),
    ({"min_mm": None}, "min_mm"),
    ({"min_mm": 0}, "outside"),
    ({"min_mm": 2.5}, "outside"),
    ({"fab": ""}, "fab is required"),
    ({"reason": "  "}, "reason is required"),
])
def test_bad_entry_raises(tmp_path, bad, msg):
    with pytest.raises(fpwaivers.WaiverError, match=msg):
        _load(tmp_path, {**GOOD, **bad})


def test_rule_is_scoped_to_own_items_and_a_peg():
    (r,) = fpwaivers.rules([GOOD])
    assert r["name"] == "aiee_fpwaive_hole_clearance_USB_C_SMD_MC_311D"
    assert r["constraint"] == "hole_clearance" and r["min_mm"] == 0.18
    assert r["condition"] == (
        "A.memberOfFootprint('*:USB-C-SMD_MC-311D') && "
        "B.memberOfFootprint('*:USB-C-SMD_MC-311D') && "
        "(A.Pad_Type == 'NPTH, mechanical' || B.Pad_Type == 'NPTH, mechanical')")
    text = fpwaivers.render(r)
    assert "(constraint hole_clearance (min 0.1800mm))" in text
    assert text.startswith("# ") and "#813" in text


def test_footprints_filter_keeps_only_parts_on_the_board():
    assert fpwaivers.rules([GOOD], footprints=["aiee:C0603", "aiee:R0603"]) == []
    assert len(fpwaivers.rules([GOOD], footprints=["aiee:USB-C-SMD_MC-311D"])) == 1
    assert len(fpwaivers.rules([GOOD], footprints=["other:USB-C-SMD_MC-311D"])) == 1
    assert fpwaivers.rules([GOOD], footprints=["aiee:USB-C-SMD_MC-311DX"]) == []


DRU = """(version 1)
# fab floor
(rule "aiee_clearance_floor"
\t(constraint clearance (min 0.1524mm))
)
(rule "aiee_silk_width_floor"
\t(constraint silk_width (min 0.1524mm))
)
(rule "aiee_hv_VIN"
\t(constraint clearance (min 0.5mm))
\t(condition "A.NetName == 'VIN'")
)
"""


def test_merge_dru_places_after_floors_before_design_rules():
    out = fpwaivers.merge_dru(DRU, fpwaivers.rules([GOOD]))
    names = [n for _, _, n in fpwaivers._blocks(out)]
    assert names == ["aiee_clearance_floor", "aiee_silk_width_floor",
                     "aiee_fpwaive_hole_clearance_USB_C_SMD_MC_311D",
                     "aiee_hv_VIN"]
    assert out.startswith("(version 1)\n# fab floor\n")


def test_merge_dru_is_idempotent_and_replaces_only_its_own():
    rules = fpwaivers.rules([GOOD])
    once = fpwaivers.merge_dru(DRU, rules)
    assert fpwaivers.merge_dru(once, rules) == once
    # a changed floor replaces the old rule, never adds a second one
    twice = fpwaivers.merge_dru(once, fpwaivers.rules([{**GOOD, "min_mm": 0.19}]))
    assert twice.count("aiee_fpwaive_") == 1 and "0.1900mm" in twice
    # no entries for this board: the old waiver goes, everything else stays
    assert fpwaivers.merge_dru(once, []) == DRU


def test_merge_dru_without_floors_goes_after_version():
    out = fpwaivers.merge_dru("(version 1)\n", fpwaivers.rules([GOOD]))
    assert out.startswith("(version 1)\n# ")
    assert fpwaivers.merge_dru(None, fpwaivers.rules([GOOD])).startswith("(version 1)\n")


def test_rules_gen_emits_waiver_after_the_floors():
    import rules_gen
    cap = yaml.safe_load((REPO / "reference" / "jlc_capabilities.yaml")
                         .read_text("utf-8"))["design_rules"]["2layer_1oz"]
    stack = yaml.safe_load((REPO / "reference" / "stackups.yaml")
                           .read_text("utf-8"))["stackups"]["JLC2313_1.6"]
    cons = {"voltages": [{"net": "VIN", "voltage": 54}]}
    rules, report = rules_gen.build(cons, cap, stack, False)
    names = [r.name for r in rules]
    w = names.index("aiee_fpwaive_hole_clearance_USB_C_SMD_MC_311D")
    floors = [i for i, n in enumerate(names) if n.endswith("_floor")]
    assert floors and w > max(floors)
    assert all(not n.endswith("_floor") for n in names[w + 1:])
    assert w < min(i for i, n in enumerate(names) if n.startswith("aiee_hv_"))
    assert report["footprint_waivers"][0]["issue"] == 813


# ------------------------------------------------------------------ smoke

def _cli():
    import env
    try:
        cli = env.find_kicad_cli()
    except env.EnvError as e:
        pytest.skip(f"no kicad-cli ({e})")
    if not cli:
        pytest.skip("no kicad-cli")
    return cli


def _drc(tmp_path, edit=None, dru=None) -> dict:
    import kc
    cli = _cli()
    for f in ROUTE.iterdir():
        if f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            shutil.copy2(f, tmp_path / f.name)
    pcb = tmp_path / "pd-trigger.kicad_pcb"
    druf = tmp_path / "pd-trigger.kicad_dru"
    if edit:
        text = pcb.read_text()
        new = edit(text)
        assert new != text, "the fault edit did not apply"
        pcb.write_text(new)
    if dru:
        druf.write_text(dru(druf.read_text()))
    return kc.run_drc(cli, pcb)


def _strip(text: str) -> str:
    return fpwaivers.merge_dru(text, [])


@pytest.mark.smoke
def test_control_without_waiver_j1_fails_four(tmp_path):
    rep = _drc(tmp_path, dru=_strip)
    assert rep["counts"]["total"] == 4
    assert {v["check"] for v in rep["violations"]} == {"hole_clearance"}
    assert all("J1" in v["refs"] for v in rep["violations"])


@pytest.mark.smoke
def test_quiet_waiver_clears_j1_vendor_geometry(tmp_path):
    assert "aiee_fpwaive_" in (ROUTE / "pd-trigger.kicad_dru").read_text()
    rep = _drc(tmp_path)
    assert rep["counts"]["total"] == 0, rep["violations"]


@pytest.mark.smoke
def test_fault_peg_moved_under_the_floor_still_fails(tmp_path):
    # the +x peg 0.01 mm toward the pad row: GND goes 0.18 -> 0.17 mm
    rep = _drc(tmp_path, edit=lambda t: t.replace(
        "(at 2.89 -1.3 270)", "(at 2.89 -1.29 270)", 1))
    assert rep["counts"]["total"] >= 1
    assert {v["check"] for v in rep["violations"]} == {"hole_clearance"}
    assert all("J1" in v["refs"] for v in rep["violations"])


SEG = ('\t(segment\n\t\t(start 16.3 46.025)\n\t\t(end 16.8 46.025)\n'
       '\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net "GND")\n'
       '\t\t(uuid "0d0d0d0d-0000-4000-8000-000000000813")\n\t)\n')


@pytest.mark.smoke
def test_fault_foreign_track_near_peg_held_to_board_clearance(tmp_path):
    # a GND track 0.20 mm from J1's +x peg: above the waiver's 0.18 floor, but
    # not J1's own copper, so the board's 0.25 mm still holds it
    rep = _drc(tmp_path, edit=lambda t: t.replace(
        "\t(segment\n", SEG + "\t(segment\n", 1))
    (v,) = rep["violations"]
    assert v["check"] == "hole_clearance"
    assert "0.2500 mm" in v["msg"] and "0.2000 mm" in v["msg"]
    kinds = [i["msg"] for i in v["items"]]
    assert any(k.startswith("Track [GND]") for k in kinds), kinds


@pytest.mark.smoke
def test_fault_tighter_floor_fails_only_the_gnd_pads(tmp_path):
    rules = fpwaivers.rules([{**GOOD, "min_mm": 0.19}])
    rep = _drc(tmp_path, dru=lambda t: fpwaivers.merge_dru(t, rules))
    assert rep["counts"]["total"] == 2
    refs = sorted(tuple(sorted(v["refs"])) for v in rep["violations"])
    assert refs == [("A1", "B12", "J1"), ("A12", "B1", "J1")]
