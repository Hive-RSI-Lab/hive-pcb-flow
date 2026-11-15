"""fp_verify's pad-numbering checks (pcb-flow #1102) and the copper boxes
they stand on.

Which pad carries which pin: against the record's top view
(land_pattern.pads -> pad_numbering, pad_numbering_tied,
pad_positions_unmatched, pad_positions_off_axis, pad_names_unmatched), and, without per-pin data, by the way the numbers run
(pad_numbering_direction on perimeter packages, bga_numbering_mirrored on
ball grids). The geometry checks (pad count, pitch, size ...) are in
test_parts.py.

The board cases are two real faults found in a board review: the SiT1602
oscillator numbered round the short side (VDD on OE) and the T5848
microphone numbered from the bottom view. Each has its footprint as it was
(the `_rotated` / `_bottom_view` fixture) and as fixed, and a record with the per-pin positions
(tests/fixtures/parts/*_pads.json).

Hermetic: committed fixtures and footprints written into tmp_path.
"""
from __future__ import annotations

import copy
import json
import math
import random
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
FIXTURES = REPO / "tests" / "fixtures" / "parts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import datasheet_extract  # noqa: E402
import fp_verify  # noqa: E402
import fplib  # noqa: E402
import padnum  # noqa: E402

SIT_FOUND = FIXTURES / "sit1602_qfn4_rotated.kicad_mod"
SIT_GOOD = FIXTURES / "sit1602_qfn4.kicad_mod"
SIT_REC = FIXTURES / "sit1602_pads.json"
MIC_FOUND = FIXTURES / "t5848_lga8_bottom_view.kicad_mod"
MIC_GOOD = FIXTURES / "t5848_lga8.kicad_mod"
MIC_REC = FIXTURES / "t5848_pads.json"


# ------------------------------------------------------------------ helpers

def run_main(mod, argv, tmp_path, capsys, name="out"):
    """Run a script's main() in-process; return (exit_code, payload_dict)."""
    out = tmp_path / f"{name}.json"
    code = mod.main([*argv, "--out", str(out)])
    cap = capsys.readouterr()
    if out.exists():
        payload = json.loads(out.read_text(encoding="utf-8"))
    else:
        payload = json.loads(cap.out) if cap.out.strip() else None
    return code, payload


def write_ds(tmp_path, data, name="ds") -> Path:
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pad_text(num, x, y, w, h, kind="smd") -> str:
    if kind == "thru_hole":
        return (f'(pad "{num}" thru_hole circle (at {x} {y}) (size {w} {h}) '
                f'(drill 0.8) (layers *.Cu *.Mask))')
    return (f'(pad "{num}" smd rect (at {x} {y}) (size {w} {h}) '
            f'(layers F.Cu F.Paste F.Mask))')


def write_fp(tmp_path, name, pads, attr="smd", kind="smd") -> Path:
    """A legacy (module ..) footprint named `name`; `pads` are
    (number, x, y, w, h) tuples or raw (pad ..) text. attr=None writes no
    attr line (KiCad 5 wrote none for a through-hole part)."""
    body = [p if isinstance(p, str) else pad_text(*p, kind=kind) for p in pads]
    attr_line = f"(attr {attr}) " if attr else ""
    text = (f'(module "{name}" (layer F.Cu) {attr_line}' + " ".join(body) +
            " (fp_line (start -5 -5) (end 5 -5) (layer F.CrtYd) (width 0.05)))")
    p = tmp_path / (re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".kicad_mod")
    p.write_text(text, encoding="utf-8")
    return p


def verify(tmp_path, capsys, fp_path, ds, *extra):
    """fp_verify's CLI on `fp_path` against `ds` (a dict or a JSON path),
    the overlay always written into tmp_path (never next to a fixture)."""
    ds_path = ds if isinstance(ds, Path) else write_ds(tmp_path, ds)
    code, payload = run_main(
        fp_verify, ["--footprint", str(fp_path), "--datasheet-json", str(ds_path),
                    "--svg", str(tmp_path / "overlay.svg"), *extra],
        tmp_path, capsys)
    return code, payload


def numbering(payload) -> list[dict]:
    """The numbering findings (the record fit's warnings included)."""
    kinds = (*fp_verify.NUMBERING_KINDS, "pad_positions_unmatched",
             "pad_positions_off_axis", "pad_names_unmatched")
    return [v for v in payload["violations"] if v["kind"] in kinds]


def red_pads(svg: Path) -> set[str]:
    """Pad numbers the overlay draws red."""
    text = svg.read_text(encoding="utf-8")
    return set(re.findall(r'fill="#f8caca"[^>]*/>\s*<text[^>]*>([^<]*)</text>', text))


def without_pads(rec: dict) -> dict:
    out = copy.deepcopy(rec)
    out["land_pattern"].pop("pads", None)
    return out


def mirror_x(pads):
    return [(n, -x if x else 0.0, y, w, h) for n, x, y, w, h in pads]


# SOIC-8 3.9 x 4.9 mm P1.27, as KiCad draws it: top view, pin 1 top left,
# 1-4 down the left side and 5-8 up the right one: counter-clockwise
SOIC8 = "SOIC-8_3.9x4.9mm_P1.27mm"
SOIC8_CCW = [(str(i + 1), -2.7, y, 1.55, 0.6)
             for i, y in enumerate((-1.905, -0.635, 0.635, 1.905))] + \
            [(str(i + 5), 2.7, y, 1.55, 0.6)
             for i, y in enumerate((1.905, 0.635, -0.635, -1.905))]
SOIC8_CW = mirror_x(SOIC8_CCW)          # numbered from the bottom view


def soic_record(numbering_way=None, pads=None, package="SOIC-8") -> dict:
    land = {"package": package, "pad_count": 8, "pitch_mm": 1.27,
            "pad_size_mm": [1.55, 0.6], "pin1": "1"}
    if numbering_way:
        land["numbering"] = numbering_way
    if pads:
        land["pads"] = [{"number": n, "at_mm": [x, y], "size_mm": [w, h]}
                        for n, x, y, w, h in pads]
    return {"mpn": "TEST-SOIC8", "package": package,
            "pinout": [{"pin": str(i), "name": f"P{i}", "type": "passive"}
                       for i in range(1, 9)],
            "land_pattern": land}


def rec_pads(pads) -> list[padnum.RecPad]:
    return [padnum.RecPad(n, (x, y), (w, h)) for n, x, y, w, h in pads]


# ------------------------------------------------ two real board faults

def test_sit1602_found_flags_numbering_rotated(tmp_path, capsys):
    """The footprint as found numbers the oscillator round the short side
    (1 and 2 on a 1.9 mm side; the datasheet puts them 2.1 mm apart on a
    long one): VDD lands on OE. Its numbers still run counter-clockwise."""
    code, p = verify(tmp_path, capsys, SIT_FOUND, SIT_REC)
    assert code == 1 and p["status"] == "violations"
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering"]
    v = found[0]
    assert v["severity"] == "error"
    assert v["cause"] == "numbering_rotated" and v["turns"] == -1
    assert v["pads"] == ["1", "2", "3", "4"]
    assert "a quarter turn clockwise" in v["msg"]
    facts = p["pad_numbering"]
    assert facts["method"] == "record" and facts["result"] == "numbering"
    assert facts["family"] == "perimeter" and facts["record_direction"] == "ccw"


def test_sit1602_fixed_passes(tmp_path, capsys):
    code, p = verify(tmp_path, capsys, SIT_GOOD, SIT_REC)
    assert code == 0 and p["status"] == "pass"
    assert p["violations"] == []
    assert p["pad_numbering"]["method"] == "record"
    assert p["pad_numbering"]["result"] == "pass"


def test_t5848_found_flags_bottom_view_numbering(tmp_path, capsys):
    """The footprint as found is the microphone's bottom view, numbers and
    all, on top-view pads (the port ring, pad 3, stays put)."""
    code, p = verify(tmp_path, capsys, MIC_FOUND, MIC_REC)
    assert code == 1 and p["status"] == "violations"
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering"]
    v = found[0]
    assert v["severity"] == "error" and v["cause"] == "bottom_view_numbering"
    assert v["pads"] == ["1", "2", "4", "5", "6", "7"]
    assert "bottom view" in v["msg"]
    # the record's 0.3 mm corner pads are smaller than its 0.522 x 0.6 ones;
    # the port ring (pad 3) is a stroked gr_circle (#1133)
    assert {x["kind"] for x in p["violations"]} == {
        "pad_numbering", "pad_size", "stroked_pad_primitive"}


def test_t5848_fixed_passes(tmp_path, capsys):
    code, p = verify(tmp_path, capsys, MIC_GOOD, MIC_REC)
    assert code == 0 and p["status"] == "pass"
    assert [v["kind"] for v in p["violations"]] == ["pad_size"]
    assert p["pad_numbering"]["result"] == "pass"


def test_t5848_found_without_pads_warns_by_direction(tmp_path, capsys):
    """With no land_pattern.pads the bottom-view microphone still shows: its
    numbers run clockwise round the outline. A warning only, so exit 0."""
    code, p = verify(tmp_path, capsys, MIC_FOUND, without_pads(load(MIC_REC)))
    assert code == 0
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering_direction"]
    v = found[0]
    assert v["severity"] == "warning"
    assert v["direction"] == "cw" and v["expected"] == "ccw"
    assert "land_pattern.numbering: cw_top" in v["msg"]
    assert p["pad_numbering"]["method"] == "direction"


def test_sit1602_found_without_pads_is_missed(tmp_path, capsys):
    """The direction check can't see a wrong start corner: the
    oscillator's numbers as found run counter-clockwise like the fixed
    one's. Only land_pattern.pads catches it."""
    code, p = verify(tmp_path, capsys, SIT_FOUND, without_pads(load(SIT_REC)))
    assert code == 0 and numbering(p) == []
    facts = p["pad_numbering"]
    assert facts["method"] == "direction"
    assert facts["direction"]["direction"] == "ccw"


def test_record_pads_that_fit_nowhere_warn(tmp_path, capsys):
    """pad_positions_unmatched: the record's spots (here at twice the
    spacing) fit the pads at no turn or mirror; the direction decides."""
    rec = load(MIC_REC)
    for r in rec["land_pattern"]["pads"]:
        r["at_mm"] = [2 * c for c in r["at_mm"]]
    code, p = verify(tmp_path, capsys, MIC_GOOD, rec)
    assert code == 0
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_positions_unmatched"]
    assert found[0]["severity"] == "warning"
    assert found[0]["cost_mm"] > found[0]["tol_mm"]
    assert p["pad_numbering"]["method"] == "direction"
    assert p["pad_numbering"]["result"] == "unmatched"


def test_record_pads_named_by_function_warn(tmp_path, capsys):
    """pad_names_unmatched: a record that names its pins (VDD, GND ...)
    where the footprint numbers them has nothing to compare; the direction
    decides (and misses the rotated oscillator, as without pads)."""
    rec = load(SIT_REC)
    names = {"1": "OE", "2": "GND", "3": "OUT", "4": "VDD"}
    for r in rec["land_pattern"]["pads"]:
        r["number"] = names[r["number"]]
    code, p = verify(tmp_path, capsys, SIT_FOUND, rec)
    assert code == 0
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_names_unmatched"]
    assert found[0]["severity"] == "warning" and found[0]["shared_names"] == 0
    assert found[0]["msg"].endswith("the numbering was checked only by direction")
    assert p["pad_numbering"]["method"] == "direction"


def test_overlay_reddens_the_wrong_pads(tmp_path, capsys):
    """The SVG draws red exactly the pads the numbering finding names (the
    size tolerance opened so the pad_size warning doesn't redden them all)."""
    code, p = verify(tmp_path, capsys, MIC_FOUND, MIC_REC, "--size-tol-mm", "2")
    assert code == 1
    # (the port ring's stroked gr_circle warning, #1133, names no pin)
    assert [v["kind"] for v in p["violations"]
            if v["kind"] != "stroked_pad_primitive"] == ["pad_numbering"]
    assert red_pads(tmp_path / "overlay.svg") == {"1", "2", "4", "5", "6", "7"}
    code, p = verify(tmp_path, capsys, MIC_GOOD, MIC_REC, "--size-tol-mm", "2")
    assert code == 0 and red_pads(tmp_path / "overlay.svg") == set()


# ------------------------------------------- direction: perimeter packages

@pytest.mark.parametrize("pads, way, code, severity", [
    (SOIC8_CW, None, 0, "warning"),       # looks numbered from the bottom view
    (SOIC8_CW, "ccw_top", 1, "error"),    # the record says top view is CCW
    (SOIC8_CW, "cw_top", 0, None),        # the record says the part runs CW
    (SOIC8_CCW, "cw_top", 1, "error"),
    (SOIC8_CCW, None, 0, None),
], ids=["cw", "cw-declared-ccw", "cw-declared-cw", "ccw-declared-cw", "ccw"])
def test_perimeter_direction(tmp_path, capsys, pads, way, code, severity):
    fp = write_fp(tmp_path, SOIC8, pads)
    got, p = verify(tmp_path, capsys, fp, soic_record(way))
    assert got == code
    found = numbering(p)
    if severity is None:
        assert found == []
    else:
        assert [v["kind"] for v in found] == ["pad_numbering_direction"]
        v = found[0]
        assert v["severity"] == severity
        assert v["expected"] == ("cw" if way == "cw_top" else "ccw")
        assert v["direction"] != v["expected"]
        # every pad of the ring is named, in order round the outline
        assert sorted(v["pads"], key=int) == [str(i) for i in range(1, 9)]
    facts = p["pad_numbering"]
    assert facts["method"] == "direction" and facts["family"] == "perimeter"
    assert facts["declared"] == way


def test_direction_still_runs_after_a_record_pass(tmp_path, capsys):
    """A record copied from the same bottom view as the footprint passes
    spot for spot; the direction check still warns, and says both may be
    wrong."""
    fp = write_fp(tmp_path, SOIC8, SOIC8_CW)
    code, p = verify(tmp_path, capsys, fp, soic_record(pads=SOIC8_CW))
    assert code == 0
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering_direction"]
    assert found[0]["severity"] == "warning"
    assert "sit the same way" in found[0]["msg"]
    facts = p["pad_numbering"]
    assert facts["method"] == "record" and facts["result"] == "pass"
    assert facts["record_direction"] == "cw"


@pytest.mark.parametrize("way", [None, "ccw_top", "cw_top"])
def test_connector_is_left_alone(tmp_path, capsys, way):
    """A connector numbered clockwise is not judged, whatever the record
    declares: declaring the numbering puts no footprint in scope."""
    fp = write_fp(tmp_path, "TE_1-2337992-1", SOIC8_CW)
    code, p = verify(tmp_path, capsys, fp, soic_record(way))
    assert code == 0 and numbering(p) == []
    assert p["pad_numbering"]["why_not"] == "connector"
    assert p["pad_numbering"]["method"] is None


@pytest.mark.parametrize("way", [None, "ccw_top", "cw_top"])
@pytest.mark.parametrize("attr", ["through_hole", None, "smd"])
def test_through_hole_is_left_alone(tmp_path, capsys, attr, way):
    """A DIP numbered clockwise (as seen from the solder side) is not judged:
    through-hole parts are drawn from either side. Its plated-through pads
    make it one, whatever its attr line says (lib/fpattr.py's rule), and
    whatever the record declares."""
    pads = [(n, x * 3.81 / 2.7, y * 2, 1.6, 1.6) for n, x, y, _, _ in SOIC8_CW]
    fp = write_fp(tmp_path, "DIP-8_W7.62mm", pads, attr=attr, kind="thru_hole")
    rec = soic_record(way, package="PDIP-8 / SOIC-8")
    rec["land_pattern"].pop("pad_size_mm")
    rec["land_pattern"].pop("pitch_mm")
    code, p = verify(tmp_path, capsys, fp, rec)
    assert numbering(p) == []
    assert p["pad_numbering"]["why_not"] == "through_hole"
    assert p["pad_numbering"]["family"] is None


def test_unknown_package_is_left_alone(tmp_path, capsys):
    """No package family in the footprint's or the record's names: a
    clockwise numbering is not judged, even with ccw_top declared."""
    fp = write_fp(tmp_path, "custom8", SOIC8_CW)
    code, p = verify(tmp_path, capsys, fp, soic_record("ccw_top", package="custom"))
    assert code == 0 and numbering(p) == []
    facts = p["pad_numbering"]
    assert facts["why_not"] == "unknown" and facts["method"] is None
    assert facts["declared"] == "ccw_top"


# ------------------------------------------------- direction: ball grids

BGA9 = "BGA-9_1.5x1.5mm_Layout3x3_P0.5mm"
# top view: A1 top left, numbers run right (+x), letters run down (+y)
BGA9_TOP = [(f"{row}{col}", -0.5 + 0.5 * (col - 1), -0.5 + 0.5 * r, 0.25, 0.25)
            for r, row in enumerate("ABC") for col in (1, 2, 3)]


def bga_record(pads=None) -> dict:
    land = {"package": "BGA-9", "pad_count": 9, "pitch_mm": 0.5, "pin1": "A1"}
    if pads:
        land["pads"] = [{"number": n, "at_mm": [x, y], "size_mm": [w, h]}
                        for n, x, y, w, h in pads]
    return {"mpn": "TEST-BGA9", "package": "BGA-9",
            "pinout": [{"pin": n, "name": n, "type": "passive"}
                       for n, *_ in BGA9_TOP],
            "land_pattern": land}


@pytest.mark.parametrize("pads, mirrored", [
    (BGA9_TOP, False),
    (mirror_x(BGA9_TOP), True),                                  # bottom view
    ([(n, y, -x if x else 0.0, w, h) for n, x, y, w, h in BGA9_TOP], False),  # turned 90
], ids=["top", "mirrored", "turned"])
def test_bga_handedness(tmp_path, capsys, pads, mirrored):
    fp = write_fp(tmp_path, BGA9, pads)
    code, p = verify(tmp_path, capsys, fp, bga_record())
    assert code == 0
    found = numbering(p)
    facts = p["pad_numbering"]
    assert facts["family"] == "bga" and facts["method"] == "direction"
    if not mirrored:
        assert found == []
        assert facts["direction"]["mirrored"] is False
        return
    assert [v["kind"] for v in found] == ["bga_numbering_mirrored"]
    v = found[0]
    assert v["severity"] == "warning" and v["turn"] == pytest.approx(-1.0)
    assert sorted(v["pads"]) == sorted(n for n, *_ in BGA9_TOP)


def test_bga_record_copied_from_the_bottom_view(tmp_path, capsys):
    """A correct ball grid against a record whose rows run as in a bottom
    view: the footprint is checked against the record, so an error, but
    one that says to check the record first."""
    fp = write_fp(tmp_path, BGA9, BGA9_TOP)
    code, p = verify(tmp_path, capsys, fp, bga_record(mirror_x(BGA9_TOP)))
    assert code == 1
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering"]
    v = found[0]
    assert v["severity"] == "error" and v["cause"] == "bottom_view_numbering"
    assert "fix the record instead" in v["msg"]
    assert "check the record first" in v["msg"]
    facts = p["pad_numbering"]
    assert facts["record_direction"] == "mirrored"
    assert facts["direction"]["mirrored"] is False
    # the same record against a footprint drawn from the bottom view passes
    # spot for spot; the direction check still warns
    code, p = verify(tmp_path, capsys, write_fp(tmp_path, BGA9, mirror_x(BGA9_TOP)),
                     bga_record(mirror_x(BGA9_TOP)))
    assert code == 0
    assert [v["kind"] for v in numbering(p)] == ["bga_numbering_mirrored"]
    assert p["pad_numbering"]["result"] == "pass"


# ------------------------------------------ record and direction together

LGA8 = "LGA-8_2.0x1.95mm_P0.65mm"
LGA8_CCW = [(str(i + 1), -1.0, y, 0.5, 0.35)
            for i, y in enumerate((-0.975, -0.325, 0.325, 0.975))] + \
           [(str(i + 5), 1.0, y, 0.5, 0.35)
            for i, y in enumerate((0.975, 0.325, -0.325, -0.975))]


def lga_record(numbering_way=None) -> dict:
    """A record whose pins run clockwise (Bosch's LGA-8 does, top view)."""
    land = {"package": "LGA-8", "pad_count": 8, "pitch_mm": 0.65,
            "pad_size_mm": [0.5, 0.35], "pin1": "1",
            "pads": [{"number": n, "at_mm": [x, y], "size_mm": [w, h]}
                     for n, x, y, w, h in mirror_x(LGA8_CCW)]}
    if numbering_way:
        land["numbering"] = numbering_way
    return {"mpn": "TEST-LGA8", "package": "LGA-8",
            "pinout": [{"pin": str(i), "name": f"P{i}", "type": "passive"}
                       for i in range(1, 9)],
            "land_pattern": land}


def test_record_running_clockwise_is_still_an_error(tmp_path, capsys):
    """The record's pins run clockwise and the footprint's run
    counter-clockwise. The record may be the one copied from a bottom view,
    but Bosch's LGA-8 really numbers clockwise, so a wrong footprint must
    not pass: an error that says to check the record first."""
    fp = write_fp(tmp_path, LGA8, LGA8_CCW)
    code, p = verify(tmp_path, capsys, fp, lga_record())
    assert code == 1
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering"]
    v = found[0]
    assert v["severity"] == "error" and v["cause"] == "bottom_view_numbering"
    assert "check the record first" in v["msg"]
    assert p["pad_numbering"]["record_direction"] == "cw"


def test_record_declared_clockwise_makes_it_an_error(tmp_path, capsys):
    """land_pattern.numbering: cw_top says the part really numbers
    clockwise; then the counter-clockwise footprint is the fault."""
    fp = write_fp(tmp_path, LGA8, LGA8_CCW)
    code, p = verify(tmp_path, capsys, fp, lga_record("cw_top"))
    assert code == 1
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering"]
    assert found[0]["severity"] == "error"
    assert found[0]["cause"] == "bottom_view_numbering"


def test_footprint_drawn_mirrored(tmp_path, capsys):
    """mirrored_footprint: on a part with no symmetry the pads themselves
    only fit the record mirrored. Pins 1, 4 and 5 sit on the mirror line, so
    only pads 2 and 3 are off their pins and reddened. No package family in
    the names, so only the record can tell."""
    spots = [("1", 0.0, 0.0), ("2", 2.0, 0.0), ("3", 2.0, 1.0), ("4", 0.0, 3.0),
             ("5", 0.0, 1.5)]
    rec = {"mpn": "TEST-CHIRAL", "package": "custom",
           "pinout": [{"pin": n, "name": f"P{n}", "type": "passive"} for n, *_ in spots],
           "land_pattern": {"package": "custom", "pad_count": 5, "pin1": "1",
                            "pads": [{"number": n, "at_mm": [x, y],
                                      "size_mm": [0.5, 0.5]} for n, x, y in spots]}}
    fp = write_fp(tmp_path, "chiral5", [(n, -x if x else 0.0, y, 0.5, 0.5)
                                        for n, x, y in spots])
    code, p = verify(tmp_path, capsys, fp, rec)
    assert code == 1
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering"]
    v = found[0]
    assert v["severity"] == "error" and v["cause"] == "mirrored_footprint"
    assert v["pads"] == ["2", "3"] and v["mismatches"] == []
    facts = p["pad_numbering"]
    assert facts["family"] is None and facts["why_not"] == "unknown"
    assert facts["method"] == "record"


# ------------------------------------------------------- tied pins (CLI)

def sot223_record(tab) -> dict:
    nums = sorted({n for n, *_ in SOT223_ROW} | {tab})
    return {"mpn": "TEST-SOT223", "package": "SOT-223",
            "pinout": [{"pin": n, "name": f"P{n}", "type": "passive"} for n in nums],
            "land_pattern": sot223_land(tab)}


@pytest.mark.parametrize("rec_tab, fp_tab, rule, words", [
    ("4", "2", "shared", "the footprint ties it to pin 2"),
    ("2", "", "repeated", "this pad has no number, so it gets no net"),
    ("2", "4", "repeated", "the symbol has a pin 4"),
], ids=["shared", "repeated-unnumbered", "repeated"])
def test_tied_tab_warns(tmp_path, capsys, rec_tab, fp_tab, rule, words):
    """pad_numbering_tied: the fit passes with a record pin on a pad the
    footprint numbers otherwise. A warning (exit 0) that names the pin,
    the pad and the rule, and says what to check."""
    fp = write_fp(tmp_path, "SOT-223-3_TabPin2", SOT223_ROW + [(fp_tab, *SOT223_TAB)])
    code, p = verify(tmp_path, capsys, fp, sot223_record(rec_tab))
    assert code == 0
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering_tied"]
    v = found[0]
    assert v["severity"] == "warning" and v["rule"] == rule
    assert v["pin"] == rec_tab and v["pads"] == [fp_tab]
    assert words in v["msg"]
    facts = p["pad_numbering"]
    assert facts["method"] == "record" and facts["result"] == "pass"
    assert facts["ties"] == [{"pin": rec_tab, "pads": [fp_tab], "rule": rule}]


def test_names_unmatched_without_a_family_says_not_checked(tmp_path, capsys):
    """No package family to judge the direction by (an unnamed 4-pad part)
    and a record named by function: the numbering went unchecked, and the
    warning says so."""
    rec = load(SIT_REC)
    names = {"1": "OE", "2": "GND", "3": "OUT", "4": "VDD"}
    for r in rec["land_pattern"]["pads"]:
        r["number"] = names[r["number"]]
    rec["package"] = rec["land_pattern"]["package"] = "4-pad oscillator"
    code, p = verify(tmp_path, capsys, write_fp(tmp_path, "osc4", SIT_FP_GOOD), rec)
    assert code == 0
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_names_unmatched"]
    assert found[0]["msg"].endswith("the numbering was not checked")
    assert p["pad_numbering"]["method"] is None
    assert p["pad_numbering"]["why_not"] == "unknown"


# --------------------------------------------------------- record schema

@pytest.mark.parametrize("rec", [SIT_REC, MIC_REC], ids=["sit1602", "t5848"])
def test_record_with_pads_validates(tmp_path, capsys, rec):
    code, payload = run_main(datasheet_extract, ["--validate", str(rec)],
                             tmp_path, capsys)
    assert code == 0 and payload["status"] == "pass"


def test_record_numbering_enum(tmp_path, capsys):
    rec = load(SIT_REC)
    rec["land_pattern"]["numbering"] = "cw_top"
    code, payload = run_main(datasheet_extract,
                             ["--validate", str(write_ds(tmp_path, rec))],
                             tmp_path, capsys)
    assert code == 0 and payload["status"] == "pass"
    rec["land_pattern"]["numbering"] = "clockwise"
    code, payload = run_main(datasheet_extract,
                             ["--validate", str(write_ds(tmp_path, rec))],
                             tmp_path, capsys)
    assert code == 1 and payload["status"] == "invalid"


def test_record_pad_needs_number_and_spot(tmp_path, capsys):
    rec = load(SIT_REC)
    del rec["land_pattern"]["pads"][0]["at_mm"]
    code, payload = run_main(datasheet_extract,
                             ["--validate", str(write_ds(tmp_path, rec))],
                             tmp_path, capsys)
    assert code == 1 and payload["status"] == "invalid"


# ------------------------------------------------ the record fit (CLI)

def test_numbers_differ_is_an_error(tmp_path, capsys):
    """Pads 2 and 3 swapped: the numbers fit the record at no turn or
    mirror. An error naming the two pads."""
    swap = {"2": "3", "3": "2"}
    fp = write_fp(tmp_path, SOIC8, [(swap.get(n, n), x, y, w, h)
                                    for n, x, y, w, h in SOIC8_CCW])
    code, p = verify(tmp_path, capsys, fp, soic_record(pads=SOIC8_CCW))
    assert code == 1
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering"]
    v = found[0]
    assert v["severity"] == "error" and v["cause"] == "numbers_differ"
    assert v["pads"] == ["2", "3"]
    assert sorted(m["pin"] for m in v["mismatches"]) == ["2", "3"]
    assert p["pad_numbering"]["method"] == "record"
    assert red_pads(tmp_path / "overlay.svg") == {"2", "3"}


def test_straight_fit_passes_though_a_mirror_fits_closer(tmp_path, capsys):
    """A SOT-23 record with pin 3 drawn 0.2 mm off the pad's spot (no
    sizes): the record fits straight, every pin on its number, within the
    limit. The mirrored placement fits closer but is only a cause: no
    error."""
    fp = write_fp(tmp_path, "SOT-23", [("1", -1.1, -0.95, 1.3, 0.6),
                                       ("2", -1.1, 0.95, 1.3, 0.6),
                                       ("3", 1.1, 0.1, 1.3, 0.6)])
    rec = {"land_pattern": {"pad_count": 3, "pads": [
        {"number": "1", "at_mm": [-1.1, -0.95]},
        {"number": "2", "at_mm": [-1.1, 0.95]},
        {"number": "3", "at_mm": [1.1, -0.1]}]}}
    code, p = verify(tmp_path, capsys, fp, rec)
    assert code == 0 and numbering(p) == []
    facts = p["pad_numbering"]
    assert facts["method"] == "record" and facts["result"] == "pass"
    assert facts["record_mirrored"] is False


def test_size_tol_widens_the_fit(tmp_path, capsys):
    """The fit's limit is 0.4 x the record's closest pin spacing, at least
    --size-tol-mm. A record whose right-hand row sits 1.2 mm further out
    (each pin 0.6 mm off) fits nowhere at the default; with
    --size-tol-mm 1 it passes."""
    far = [(n, x + 1.2 if x > 0 else x, y, w, h) for n, x, y, w, h in SOIC8_CCW]
    fp = write_fp(tmp_path, SOIC8, SOIC8_CCW)
    code, p = verify(tmp_path, capsys, fp, soic_record(pads=far))
    assert code == 0
    assert [v["kind"] for v in numbering(p)] == ["pad_positions_unmatched"]
    assert p["pad_numbering"]["tol_mm"] == pytest.approx(0.4 * 1.27)
    code, p = verify(tmp_path, capsys, fp, soic_record(pads=far),
                     "--size-tol-mm", "1")
    assert numbering(p) == []
    facts = p["pad_numbering"]
    assert facts["tol_mm"] == pytest.approx(1.0) and facts["result"] == "pass"


def test_record_off_axis_warns(tmp_path, capsys):
    """The oscillator's record drawn 30 degrees round, against the
    footprint numbered round the short side: off_axis, a warning that says
    to redraw the record square."""
    rec = load(SIT_REC)
    for r in rec["land_pattern"]["pads"]:
        (_, x, y), = turned([("", *r["at_mm"])], 30)
        r["at_mm"] = [x, y]
    code, p = verify(tmp_path, capsys, SIT_FOUND, rec, "--size-tol-mm", "2")
    assert code == 0
    found = [v for v in numbering(p) if v["kind"] == "pad_positions_off_axis"]
    assert len(found) == 1 and found[0]["severity"] == "warning"
    assert "Redraw it square" in found[0]["msg"]
    assert p["pad_numbering"]["result"] == "off_axis"
    code, p = verify(tmp_path, capsys, SIT_GOOD, rec, "--size-tol-mm", "2")
    assert code == 0 and numbering(p) == []


def test_pad_over_twice_the_median_is_off_the_ring_and_a_big_land(tmp_path):
    """One limit (_BIG_PAD) for both: a pad 2.5 x the median pin's area is
    a tab for the direction check (off the ring) and for the fit's lands."""
    big = SOIC8_CCW[:7] + [("8", 2.7, -1.905, 1.55, 1.5)]
    assert 2.0 < (1.55 * 1.5) / (1.55 * 0.6) < 3.0
    pads = fplib.parse_footprint(write_fp(tmp_path, SOIC8, big)).copper_pads
    d = padnum.perimeter_direction(pads)
    # the ring walk may start anywhere; the tab (8) is not on it
    assert sorted(d["order"]) == [1, 2, 3, 4, 5, 6, 7]
    assert d["direction"] == "ccw"
    lands, limit = padnum._fp_lands(pads, {})
    assert [la["number"] for la in lands if la["area"] > limit] == ["8"]


# ----------------------------------------- a malformed record (CLI)

def _bad_first_pad(field, value):
    def edit(land):
        land["pads"][0][field] = value
    return edit


def _replace(field, value):
    def edit(land):
        land[field] = value
    return edit


def _first_pad_is(value):
    def edit(land):
        land["pads"][0] = value
    return edit


@pytest.mark.parametrize("edit, kept, why", [
    (_bad_first_pad("at_mm", [None, 1]), 7, "at_mm is not two finite numbers"),
    (_bad_first_pad("at_mm", "1,2"), 7, "at_mm is not two finite numbers"),
    (_bad_first_pad("at_mm", [1, 2, 3]), 7, "at_mm is not two finite numbers"),
    (_bad_first_pad("size_mm", 1.5), 8, "size ignored"),
    (_bad_first_pad("size_mm", ["a", "b"]), 8, "size ignored"),
    (_bad_first_pad("size_mm", "12"), 8, "size ignored"),
    (_bad_first_pad("size_mm", [1.0, -1.0]), 8, "size ignored"),
    (_bad_first_pad("number", "  "), 7, "no number"),
    (_bad_first_pad("number", True), 7, "no number"),
    (_first_pad_is("pin 1 at 0,0"), 7, "not an object"),
    (_replace("pads", {"1": [0, 0]}), 0, "land_pattern.pads is not a list"),
    (_replace("pads", "1 at 0,0"), 0, "land_pattern.pads is not a list"),
], ids=["at-null", "at-string", "at-three", "size-scalar", "size-strings",
        "size-string", "size-negative", "number-blank", "number-bool",
        "pad-string", "pads-object", "pads-string"])
def test_malformed_record_pads_warn(tmp_path, capsys, edit, kept, why):
    """A land_pattern.pads entry the check can't use is left out (a bad
    size_mm drops only the size) and named in a land_pattern_invalid
    warning; the rest are compared. No crash, no false error."""
    rec = soic_record(pads=SOIC8_CCW)
    edit(rec["land_pattern"])
    fp = write_fp(tmp_path, SOIC8, SOIC8_CCW)
    code, p = verify(tmp_path, capsys, fp, rec)
    assert code == 0 and p["status"] == "pass"
    bad = [v for v in p["violations"] if v["kind"] == "land_pattern_invalid"]
    assert len(bad) == 1 and bad[0]["severity"] == "warning"
    assert bad[0]["field"] == "pads" and why in bad[0]["msg"]
    assert [s["index"] for s in bad[0]["skipped"]] == [None if kept == 0 else 0]
    assert numbering(p) == []
    facts = p["pad_numbering"]
    if kept:
        assert facts["method"] == "record" and facts["result"] == "pass"
        assert f"{kept} of 8 record pins compared" in bad[0]["msg"]
    else:
        assert facts["method"] == "direction"
        assert "No record pin compared" in bad[0]["msg"]


def test_numbering_that_is_no_choice_is_ignored(tmp_path, capsys):
    """land_pattern.numbering "cw" (not cw_top): a land_pattern_invalid
    warning, and the numbering counts as not declared, so a clockwise
    SOIC-8 only warns."""
    fp = write_fp(tmp_path, SOIC8, SOIC8_CW)
    code, p = verify(tmp_path, capsys, fp, soic_record("cw"))
    assert code == 0
    bad = [v for v in p["violations"] if v["kind"] == "land_pattern_invalid"]
    assert len(bad) == 1 and bad[0]["field"] == "numbering"
    assert [(v["kind"], v["severity"]) for v in numbering(p)] == \
        [("pad_numbering_direction", "warning")]
    assert p["pad_numbering"]["declared"] is None


# --------------------------------------------------------------- classify

@pytest.mark.parametrize("names, family, why", [
    (("SOT-89-3",), "perimeter", None),
    (("MMICT5848-00-012_LGA-8_3.5x2.65mm",
      "bottom-port MEMS microphone, 8-terminal LGA"), "perimeter", None),
    (("LFCSP-16-1EP_3x3mm_P0.5mm_EP1.6x1.6mm",), "perimeter", None),
    (("SOT-363_SC-70-6_CounterClockwise",), "perimeter", None),
    (("SC-70-6",), "perimeter", None),
    (("TSSOP-20_4.4x6.5mm_P0.65mm",), "perimeter", None),
    # small-outline and no-lead names the first token list missed
    (("SOJ-24_7.62x15.875mm_P1.27mm",), "perimeter", None),
    (("VSO-40_7.6x15.4mm_P0.76mm",), "perimeter", None),
    (("Texas_S-PDSO-G8_3x3mm_P0.65mm",), "perimeter", None),
    (("Infineon_PG-TSDSO-14-22",), "perimeter", None),
    (("PowerSSO-36_7.5x10.3mm_P0.5mm",), "perimeter", None),
    (("DHVQFN-14-1EP_2.5x3mm_P0.5mm_EP1x1.5mm",), "perimeter", None),
    (("UFQFPN-32-1EP_5x5mm_P0.5mm_EP3.5x3.5mm",), "perimeter", None),
    (("Vishay_PowerPAK_1212-8_Single",), "perimeter", None),
    (("Diodes_PowerDI3333-8",), "perimeter", None),
    (("SC-82AA",), "perimeter", None),
    (("Maxim_WLP-12",), "bga", None),
    # power packages with a tab and leads in a row stay out
    (("TO-252-2",), None, "unknown"),
    (("D_PowerDI-123",), None, "unknown"),
    (("SOT-227",), None, "excluded"),
    (("LED_WS2812B_PLCC4_5.0x5.0mm_P3.2mm",), None, "excluded"),
    (("TerminalBlock_Degson_DG128-5.0-02P_1x02_P5.00mm_Horizontal",), None, "connector"),
    (("USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal",), None, "connector"),
    (("TE_1-2337992-1",), None, "connector"),
    (("WLCSP-6_1.4x1.0mm_P0.4mm",), "bga", None),
    (("BGA-9_1.5x1.5mm_Layout3x3_P0.5mm",), "bga", None),
    (("Bosch_LGA-8_2x2.5mm_P0.65mm_ClockwisePinNumbering",), None, "declared"),
    (("SOT-23-3_BR-CW",), None, "declared"),
    (("SOT-143R",), None, "declared"),
    (("TSOP-I-32_11.8x8mm_P0.5mm_Reverse",), None, "declared"),
    (("SOT-23-3-pinswapped-mosfet",), None, "declared"),
    (("SOT-23_Pin_Swap_GDS",), None, "declared"),
    (("R_0402_1005Metric",), None, "unknown"),
    (("Crystal_SMD_3225-4Pin_3.2x2.5mm", "4-pad crystal"), None, "unknown"),
])
def test_classify(names, family, why):
    got_family, got = padnum.classify(*names)
    assert got_family == family
    if why is not None:
        assert got == why


def test_classify_connector_words_count_in_the_footprint_name_only():
    # a record's prose says TERMINAL about an LGA's lands
    assert padnum.classify("LGA-8", "8-terminal LGA")[0] == "perimeter"
    assert padnum.classify("TerminalBlock_x", "LGA")[1] == "connector"
    # a library prefix comes off
    assert padnum.classify("Connector_USB:USB_C_Receptacle_x")[1] == "connector"


# ------------------------------------------------------ padnum on its own

def test_tab_and_row_of_three_is_not_judged():
    """SOT-89: three pins in a row and pin 2's tab. The tab is dropped (over
    twice the median pad's copper); three pads on a line have no
    way round."""
    pads = rec_pads([("1", -1.5, 1.7, 0.6, 1.2), ("2", 0.0, 1.7, 0.6, 1.2),
                     ("3", 1.5, 1.7, 0.6, 1.2), ("2", 0.0, -0.6, 1.8, 2.5)])
    assert padnum.judge(pads, "perimeter") is None


def test_zigzag_numbering_has_no_direction():
    """A 2 x 4 header numbered odd/even across: as many steps up as down."""
    pads = rec_pads([(str(2 * r + c + 1), 2.54 * c, 2.54 * r, 1.7, 1.7)
                     for r in range(4) for c in range(2)])
    res = padnum.perimeter_direction(pads)
    assert res is not None and res["direction"] is None
    assert res["up"] == res["down"]


def test_perimeter_direction_both_ways():
    ccw = padnum.perimeter_direction(rec_pads(SOIC8_CCW))
    cw = padnum.perimeter_direction(rec_pads(SOIC8_CW))
    # seven steps of one; the wrap from 8 back to 1 counts neither way
    assert ccw["direction"] == "ccw" and (ccw["up"], ccw["down"]) == (7, 0)
    assert cw["direction"] == "cw" and (cw["up"], cw["down"]) == (0, 7)
    # the walk starts wherever the outline does; it is 1..8 turned
    k = ccw["order"].index(1)
    assert ccw["order"][k:] + ccw["order"][:k] == [1, 2, 3, 4, 5, 6, 7, 8]


def _ring(nums, r=2.0):
    """Small pads evenly round a circle, numbered `nums` counter-clockwise
    on screen (y down)."""
    out = []
    for i, n in enumerate(nums):
        a = 2 * math.pi * i / len(nums)
        out.append((str(n), round(r * math.cos(a), 4), round(-r * math.sin(a), 4),
                    0.4, 0.4))
    return out


def test_staggered_row_has_no_direction():
    """TO-220-5 with staggered leads walks 1, 3, 5, 4, 2 round its outline:
    no single way round, so no verdict (the old step count called it cw)."""
    pads = rec_pads([(str(n), 1.7 * (n - 1), 0.0 if n % 2 else 3.7, 1.8, 1.8)
                     for n in range(1, 6)])
    res = padnum.perimeter_direction(pads)
    assert res is not None and res["direction"] is None
    k = res["order"].index(1)
    walk = res["order"][k:] + res["order"][:k]
    assert walk in ([1, 3, 5, 4, 2], [1, 2, 4, 5, 3])


def test_repeated_numbers_have_no_direction():
    """DirectFET-style: one terminal on pads on both sides of the outline
    (ring 1, 3, 1, 1, 3, 2, 1). A number met twice round the walk can't
    rise or fall all the way round: no verdict."""
    res = padnum.perimeter_direction(rec_pads(_ring([1, 3, 1, 1, 3, 2, 1])))
    assert res is not None and res["direction"] is None


def test_left_out_pin_still_runs_counter_clockwise():
    """SOIC-7 (pin 6 left out for creepage): the numbers still rise all
    the way round, with one gap."""
    pads = [p for p in SOIC8_CCW if p[0] != "6"]
    res = padnum.perimeter_direction(rec_pads(pads))
    assert res["direction"] == "ccw" and res["winding"] == "ccw"
    assert 6 not in res["order"] and (res["up"], res["down"]) == (5, 0)
    cw = padnum.perimeter_direction(rec_pads(mirror_x(pads)))
    assert cw["direction"] == "cw" and cw["winding"] == "cw"


def test_winding_that_disagrees_gives_no_direction():
    """Pin 1 drawn as five pads round two sides of a square, then 2, 3, 4:
    the numbers rise counter-clockwise, but from pin 1's first pad the turn
    1 -> 2 -> 3 about the centre is clockwise. The two tests disagree:
    no verdict."""
    res = padnum.perimeter_direction(rec_pads(_ring([1, 1, 1, 1, 1, 2, 3, 4])))
    assert (res["up"], res["down"]) == (3, 0)
    assert res["winding"] == "cw" and res["direction"] is None


def test_exposed_pad_and_centre_row_are_off_the_ring():
    """A QFN's exposed pad (pin 9) is neither a ring pin nor a reason for
    no verdict."""
    pads = rec_pads(SOIC8_CCW + [("9", 0.0, 0.0, 2.4, 3.1)])
    res = padnum.perimeter_direction(pads)
    assert res["direction"] == "ccw" and 9 not in res["order"]


def test_thermal_vias_numbered_like_the_exposed_pad_are_off_the_ring():
    """KiCad's _ThermalVias footprints: drilled vias in an SO-8's exposed
    pad carry its number (9), and the outer ones stand just past the pins'
    centres, out on the outline. Nine of them used to pull the size median
    down to a via's, so the pins looked big; and the ones on the outline
    broke the ring (4, 9, 9, 5 ...): no verdict on 60 stock footprints."""
    def pad(n, x, y, w, h, drill=None):
        return SimpleNamespace(number=n, copper_center=(x, y),
                               copper_size=(w, h), drill=drill)
    ep = [pad("9", 0.0, 0.0, 2.95, 4.9)] + [
        pad("9", x, y, 0.5, 0.5, 0.2)
        for x in (-0.65, 0.65) for y in (-1.95, -0.65, 0.65, 1.95)]
    pins = [pad(*q) for q in SOIC8_CCW]
    res = padnum.perimeter_direction(pins + ep)
    assert res["direction"] == "ccw" and 9 not in res["order"]
    cw = padnum.perimeter_direction([pad(*q) for q in SOIC8_CW] + ep)
    assert cw["direction"] == "cw"
    # an undrilled pad with the tab's number is a lead (SOT-89's pin 2, a
    # PowerPAK's drain leads): it stays on the ring
    lead = padnum.perimeter_direction(
        [q for q in pins if q.number != "8"] + [pad("8", 2.7, -1.905, 1.55, 0.6),
                                                 pad("8", 0.0, 0.0, 2.95, 4.9)])
    assert lead["direction"] == "ccw" and 8 in lead["order"]


def test_names_that_are_not_pin_numbers_give_no_direction():
    """A pulled SO08-E3 names its three lands 13, 4 and 58. Round the
    outline they fall twice and rise once, which read as clockwise and
    warned; a top number over twice the count of numbers means names, not
    pins: no verdict."""
    res = padnum.perimeter_direction(rec_pads(_ring([13, 4, 58])))
    assert res is not None and res["direction"] is None
    # the same three lands numbered 1, 2, 3 still have a way round
    assert padnum.perimeter_direction(rec_pads(_ring([1, 3, 2])))["direction"] == "cw"


def test_dual_ring_qfn_is_not_a_ball_grid():
    """A1-A12 round the outside and B1-B8 inside are named like balls but
    lie on no grid: no handedness."""
    outer = [(-2, -1), (-2, 0), (-2, 1), (-1, 2), (0, 2), (1, 2),
             (2, 1), (2, 0), (2, -1), (1, -2), (0, -2), (-1, -2)]
    inner = [(-1.2, -0.5), (-1.2, 0.5), (-0.5, 1.2), (0.5, 1.2),
             (1.2, 0.5), (1.2, -0.5), (0.5, -1.2), (-0.5, -1.2)]
    pads = rec_pads([(f"A{i + 1}", x, y, 0.25, 0.25) for i, (x, y) in enumerate(outer)] +
                    [(f"B{i + 1}", x, y, 0.25, 0.25) for i, (x, y) in enumerate(inner)])
    assert padnum.bga_handedness(pads) is None


def test_lga_module_named_by_grid_is_judged_as_a_grid():
    """A perimeter-family name whose pads are mostly A1, B7 ... (an LGA
    module) gets the ball-grid check."""
    res = padnum.judge(rec_pads(mirror_x(BGA9_TOP)), "perimeter")
    assert res["family"] == "bga" and res["mirrored"] is True


SIT_FP_GOOD = [("4", -1.1, -0.95, 1.4, 1.2), ("1", -1.1, 0.95, 1.4, 1.2),
               ("2", 1.1, 0.95, 1.4, 1.2), ("3", 1.1, -0.95, 1.4, 1.2)]


def test_fit_record_exposed_pad_renamed():
    """The record calls the exposed pad EP; the footprint numbers it 9:
    a rename, not a fault."""
    land = soic_record(pads=SOIC8_CCW + [("EP", 0.0, 0.0, 2.4, 3.1)])["land_pattern"]
    fit = padnum.fit_record(rec_pads(SOIC8_CCW + [("9", 0.0, 0.0, 2.4, 3.1)]), land)
    assert fit["verdict"] == "pass"
    assert [(r["pin"], r["pads"]) for r in fit["renames"]] == [("EP", ["9"])]


def test_fit_record_names_differ():
    land = load(SIT_REC)["land_pattern"]
    for r, name in zip(land["pads"], ("OE", "GND", "OUT", "VDD")):
        r["number"] = name
    fit = padnum.fit_record(rec_pads(SIT_FP_GOOD), land)
    assert fit["verdict"] == "names_differ" and fit["shared_names"] == 0


def test_fit_record_numbers_differ():
    """Two pads swapped: the numbers fit the record at no turn or mirror."""
    swapped = [(("3" if n == "2" else "2" if n == "3" else n), x, y, w, h)
               for n, x, y, w, h in SIT_FP_GOOD]
    land = load(SIT_REC)["land_pattern"]
    assert padnum.fit_record(rec_pads(SIT_FP_GOOD), land)["verdict"] == "pass"
    fit = padnum.fit_record(rec_pads(swapped), land)
    assert fit["verdict"] == "numbering" and fit["cause"] == "numbers_differ"
    assert sorted(m["pin"] for m in fit["mismatches"]) == ["2", "3"]


def test_fit_record_mirrored_misplaced():
    """mirrored_footprint names the fewest pads to move: with two pins on
    each of two lines the record is laid nearest the footprint as drawn
    (pins 1 and 4 on the mirror line stay); three pins on no line leave
    nothing to tell apart."""
    spots = [("1", 0.0, 0.0), ("2", 2.0, 0.0), ("3", 2.0, 1.0), ("4", 0.0, 3.0)]
    land = {"pads": [{"number": n, "at_mm": [x, y], "size_mm": [0.5, 0.5]}
                     for n, x, y in spots]}
    fit = padnum.fit_record(rec_pads([(n, -x, y, 0.5, 0.5) for n, x, y in spots]),
                            land)
    assert fit["cause"] == "mirrored_footprint" and fit["misplaced"] == ["2", "3"]
    for case in (padnum.fit_record(rec_pads([(n, x, y, 0.5, 0.5) for n, x, y in spots]),
                                   land),):
        assert case["verdict"] == "pass" and case["misplaced"] is None


def test_fit_record_without_pads_is_none():
    assert padnum.fit_record(rec_pads(SIT_FP_GOOD), {"pad_count": 4}) is None


def test_fit_record_one_shared_name():
    """One pin name in common and the footprint's other pads numbered with
    numbers the record lacks: any spot would pass with the rest renamed, so
    there is nothing to compare. With the rest unnumbered the one name is
    compared."""
    land = {"pads": [{"number": "1", "at_mm": [-0.5, 0.0]},
                     {"number": "2", "at_mm": [0.5, 0.0]}]}
    for other, verdict in (("3", "names_differ"), ("", "pass")):
        fit = padnum.fit_record(rec_pads([("1", -0.5, 0.0, 0.6, 0.6),
                                          (other, 0.5, 0.0, 0.6, 0.6)]), land)
        assert fit["verdict"] == verdict and fit["shared_names"] == 1


# ----------------------------------------- a record drawn at any angle

# Round cans as KiCad draws them (top view, through-hole): TO-39-8's eight
# leads 45 degrees apart round a 5.84 mm circle, TO-18's three at 90.
TO39_8 = [("1", 0.0, 0.0), ("2", 0.855, 2.065), ("3", 2.92, 2.92),
          ("4", 4.985, 2.065), ("5", 5.84, 0.0), ("6", 4.985, -2.065),
          ("7", 2.92, -2.92), ("8", 0.855, -2.065)]
TO18_3 = [("1", 0.0, 0.0), ("2", 1.27, 1.27), ("3", 2.54, 0.0)]


def turned(spots, deg, shift=(0.0, 0.0)):
    """(number, x, y) spots turned `deg` degrees counter-clockwise on
    screen (y down) about the origin, then shifted: a top view drawn at an
    angle."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [(n, round(x * c + y * s + shift[0], 4),
             round(-x * s + y * c + shift[1], 4)) for n, x, y in spots]


def can_record(spots, size=1.2) -> dict:
    return {"mpn": "TEST-CAN", "package": "TO-can",
            "pinout": [{"pin": n, "name": f"P{n}", "type": "passive"}
                       for n, *_ in spots],
            "land_pattern": {"package": "TO-can", "pad_count": len(spots),
                             "pin1": "1",
                             "pads": [{"number": n, "at_mm": [x, y],
                                       "size_mm": [size, size]}
                                      for n, x, y in spots]}}


def write_can(tmp_path, name, spots) -> Path:
    return write_fp(tmp_path, name, [(n, x, y, 1.2, 1.2) for n, x, y in spots],
                    attr="through_hole", kind="thru_hole")


@pytest.mark.parametrize("name,spots", [("TO-39-8", TO39_8), ("TO-18-3", TO18_3)])
@pytest.mark.parametrize("deg", [45, 30, 10])
def test_record_drawn_at_an_angle_passes(tmp_path, capsys, name, spots, deg):
    """A datasheet draws a can's leads at any angle. The pin numbers point
    to the turn that puts each pin on its pad, and the record passes there;
    tried only at quarter turns, TO-39-8 at 45 degrees fit its pads one
    lead round (every pin on the wrong pad) and failed."""
    fp = write_can(tmp_path, name, spots)
    code, p = verify(tmp_path, capsys, fp,
                     can_record(turned(spots, deg, (3.0, -1.0))))
    assert numbering(p) == []
    facts = p["pad_numbering"]
    assert facts["method"] == "record" and facts["result"] == "pass"
    assert facts["record_rotation_deg"] == pytest.approx(360 - deg, abs=0.01)
    assert facts["record_mirrored"] is False


@pytest.mark.parametrize("name,spots", [("TO-39-8", TO39_8), ("TO-18-3", TO18_3)])
def test_can_numbered_from_the_bottom_view_at_an_angle(tmp_path, capsys,
                                                       name, spots):
    """The same cans numbered round the other way (the bottom view's
    numbers on the top view's pads) against the record at 45 degrees: the
    numbers fit it only mirrored, still an error."""
    centre = sum(x for _, x, _ in spots) / len(spots)
    bottom = [(n, round(2 * centre - x, 4), y) for n, x, y in spots]
    fp = write_can(tmp_path, name, bottom)
    code, p = verify(tmp_path, capsys, fp, can_record(turned(spots, 45)))
    assert code == 1
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering"]
    assert found[0]["severity"] == "error"
    assert found[0]["cause"] == "bottom_view_numbering"


def test_record_at_an_angle_on_a_wrongly_numbered_footprint_is_not_a_pass():
    """The oscillator's record drawn 30 degrees round. The fixed footprint
    passes at the turn its numbers point to; the one numbered round the
    short side doesn't: its numbers point to a turn its pads don't fit, and
    the record, square to no axis, is judged at no other (off_axis, a
    warning)."""
    land = load(SIT_REC)["land_pattern"]
    for r in land["pads"]:
        (_, x, y), = turned([("", *r["at_mm"])], 30)
        r["at_mm"] = [x, y]
    good = fplib.parse_footprint(SIT_GOOD).copper_pads
    fit = padnum.fit_record(good, land)
    assert fit["verdict"] == "pass"
    assert fit["placement"]["k"] is None
    assert fit["placement"]["turn_deg"] % 90 == pytest.approx(60, abs=0.01)
    found = fplib.parse_footprint(SIT_FOUND).copper_pads
    assert padnum.fit_record(found, land)["verdict"] == "off_axis"


def test_numbers_turned_by_no_quarter_turn(tmp_path, capsys):
    """Six pins round a ring whose spots are not quite even, numbered one
    pin round: the numbers fit the record turned about 60 degrees, the pads
    only where they are. numbering_rotated, said in degrees."""
    angles = (0, 55, 120, 180, 235, 300)
    spots = [(str(i + 1), round(2 * math.cos(math.radians(a)), 4),
              round(-2 * math.sin(math.radians(a)), 4))
             for i, a in enumerate(angles)]
    one_round = [(n, *spots[i % 6][1:]) for i, (n, *_) in enumerate(spots, 1)]
    fp = write_fp(tmp_path, "ring6", [(n, x, y, 0.8, 0.8) for n, x, y in one_round])
    code, p = verify(tmp_path, capsys, fp, can_record(spots, size=0.8))
    assert code == 1
    found = numbering(p)
    assert [v["kind"] for v in found] == ["pad_numbering"]
    v = found[0]
    assert v["cause"] == "numbering_rotated" and v["turns"] is None
    assert v["turn_deg"] == pytest.approx(60, abs=1)
    assert "about 60 degrees counter-clockwise" in v["msg"]
    assert p["pad_numbering"]["turn_deg"] == v["turn_deg"]


@pytest.mark.parametrize("turns,deg,words", [
    (1, 90.0, "a quarter turn counter-clockwise"),
    (-1, -90.0, "a quarter turn clockwise"),
    (2, 180.0, "a half turn"),
    (None, 45.0, "about 45 degrees counter-clockwise"),
    (None, -132.6, "about 133 degrees clockwise"),
    (None, -0.4, "about 0.4 degrees clockwise"),
])
def test_turn_words(turns, deg, words):
    assert fp_verify._turn_words({"turns": turns, "turn_deg": deg}) == words


# --------------------------------------------- the record fit's matcher

def tht_offset_pad(num, y, ox) -> str:
    return (f'(pad "{num}" thru_hole oval (at 0 {y}) (size 2.4 1.4) '
            f'(drill 0.9 (offset {ox} 0)) (layers *.Cu *.Mask))')


def test_offset_drill_pad_meets_the_record_at_its_hole_or_its_shape(tmp_path):
    """A through-hole pad drawn off its hole ((drill .. (offset ..))): a
    record may give the pin at the lead (the hole) or at the land (the
    shape), and either fits at no cost. The offsets differ from pad to
    pad, so no shift of the record makes up for a view left out."""
    pads = [tht_offset_pad("1", 0, 0.6), tht_offset_pad("2", 2.54, -0.6),
            tht_offset_pad("3", 5.08, 0.6)]
    fp = fplib.parse_footprint(write_fp(tmp_path, "offset_drills", pads,
                                        attr="through_hole"))
    p1 = next(p for p in fp.copper_pads if p.number == "1")
    views = padnum._pad_views(p1)
    assert [v[:2] for v in views] == [pytest.approx((0.0, 0.0)),
                                      pytest.approx((0.6, 0.0))]
    holes = [("1", 0.0, 0.0), ("2", 0.0, 2.54), ("3", 0.0, 5.08)]
    lands = [("1", 0.6, 0.0), ("2", -0.6, 2.54), ("3", 0.6, 5.08)]
    for spots in (holes, lands):
        land = {"pads": [{"number": n, "at_mm": [x, y]} for n, x, y in spots]}
        fit = padnum.fit_record(fp.copper_pads, land)
        assert fit["verdict"] == "pass"
        assert fit["cost"] == pytest.approx(0.0, abs=1e-6)


# an exposed pad drawn as four touching pieces, every one pad 9
SPLIT_EP = [("9", sx * 0.6, sy * 0.775, 1.2, 1.55) for sx in (-1, 1) for sy in (-1, 1)]


def test_split_exposed_pad_is_one_land():
    """The record gives the exposed pad once, at its middle, where no piece
    is: the touching pieces are one land, met there at no cost. A number
    the record lists more than once keeps its pieces apart."""
    pads = rec_pads(SOIC8_CCW + SPLIT_EP)
    land = soic_record(pads=SOIC8_CCW + [("9", 0.0, 0.0, 2.4, 3.1)])["land_pattern"]
    fit = padnum.fit_record(pads, land)
    assert fit["verdict"] == "pass"
    assert fit["cost"] == pytest.approx(0.0, abs=1e-6)
    lands, _ = padnum._fp_lands(pads, {"9": 1})
    (ep,) = [g for g in lands if g["number"] == "9"]
    assert ep["views"][0] == pytest.approx((0.0, 0.0, 2.4, 3.1))
    assert ep["area"] == pytest.approx(2.4 * 3.1)
    lands, _ = padnum._fp_lands(pads, {"9": 2})
    assert len([g for g in lands if g["number"] == "9"]) == 4


def test_grid_near_matches_a_scan():
    """_Grid.near finds what scanning every view finds, for points among
    the pads and far outside them, with and without slack; stacked views
    come back together."""
    rng = random.Random(1102)
    views = [(rng.uniform(-5, 5), rng.uniform(-3, 3), 0.3, 0.3, i) for i in range(150)]
    views += [(x, y, 0.6, 0.6, 1000 + i) for x, y, *_, i in views[:10]]
    grid = padnum._Grid(views, 0.7)
    for _ in range(300):
        x, y = rng.uniform(-20, 20), rng.uniform(-15, 15)
        ds = [math.hypot(v[0] - x, v[1] - y) for v in views]
        for slack in (0.0, padnum._HERE_MM, 0.5):
            best, found = grid.near(x, y, slack)
            assert best == min(ds)
            assert sorted(found) == sorted(v for v, d in zip(views, ds)
                                           if d <= best + slack)


# a 0.5 mm connector: pins 1-6 in a row and four shell pads, all pad 7
SHELL_PINS = [(str(i + 1), -1.25 + 0.5 * i, 0.0, 0.3, 1.0) for i in range(6)]
SHELL_PADS = [("7", -2.6, -1.5, 1.0, 1.6), ("7", 2.6, -1.5, 1.0, 1.6),
              ("7", -2.9, 1.3, 1.0, 1.6), ("7", 2.9, 1.3, 1.0, 1.6)]


def test_vote_finds_the_fit_when_the_record_leaves_out_shell_pads(monkeypatch):
    """The record (in its own frame, origin off at a corner) gives one of
    the four shell pads: every centre of pads and pins is pulled off by
    more than half the pitch, and a fit refined from there lands a pin
    over. The pins' votes find the shift."""
    rec = [{"number": n, "x": x + 3.0, "y": y + 2.0, "w": w, "h": h}
           for n, x, y, w, h in SHELL_PINS + SHELL_PADS[:1]]
    pads = rec_pads(SHELL_PINS + SHELL_PADS)
    fit = padnum._Fit(pads, rec, 0.5, 0.2)
    pts = [(r["x"], r["y"]) for r in rec]
    for s in padnum._centre_shifts(fit.xy, pts):
        assert max(abs(s[0] + 3.0), abs(s[1] + 2.0)) > 0.25
    assert padnum._vote_shifts(fit, pts)[0] == pytest.approx((-3.0, -2.0))
    got = padnum.fit_record(pads, rec)
    assert got["verdict"] == "pass" and got["cost"] == pytest.approx(0.0, abs=1e-6)
    monkeypatch.setattr(padnum, "_vote_shifts", lambda *a, **k: [])
    assert padnum.fit_record(pads, rec)["verdict"] != "pass"


# SOT-223: pins 1-3 in a row, the tab across the top
SOT223_ROW = [(str(i + 1), 2.3 * (i - 1), 3.15, 0.95, 2.0) for i in range(3)]
SOT223_TAB = (0.0, -3.15, 3.25, 2.0)


def sot223_land(tab) -> dict:
    return {"package": "SOT-223", "pad_count": 4, "pin1": "1",
            "pads": [{"number": n, "at_mm": [x, y], "size_mm": [w, h]}
                     for n, x, y, w, h in SOT223_ROW + [(tab, *SOT223_TAB)]]}


@pytest.mark.parametrize("rec_tab, fp_tab, verdict, renames, ties, mismatches", [
    ("4", "4", "pass", [], [], []),
    ("TAB", "4", "pass", [("TAB", ["4"])], [], []),
    ("4", "2", "pass", [], [("4", ["2"], "shared")], []),
    ("2", "4", "pass", [], [("2", ["4"], "repeated")], []),
    ("2", "", "pass", [], [("2", [""], "repeated")], []),
    ("2", "1", "numbering", [], [], [("2", ["1"])]),
], ids=["same", "rename", "shared", "repeated", "repeated-unnumbered", "mismatch"])
def test_tab_rules(rec_tab, fp_tab, verdict, renames, ties, mismatches):
    """What the record's tab pin meets on the footprint's tab (_score's
    rules): a name neither side shares is a rename; the record's 4 on a
    tab numbered 2 (pin 2 right on the row) is tied to pin 2 (shared); the
    record's pin 2 listed at the tab as well, on a tab numbered 4 or left
    unnumbered, is tied too (repeated); on a tab numbered 1 it is a
    mismatch."""
    fit = padnum.fit_record(rec_pads(SOT223_ROW + [(fp_tab, *SOT223_TAB)]),
                            sot223_land(rec_tab))
    assert fit["verdict"] == verdict
    assert [(r["pin"], r["pads"]) for r in fit["renames"]] == renames
    assert [(t["pin"], t["pads"], t["rule"]) for t in fit["ties"]] == ties
    assert [(m["pin"], m["pads"]) for m in fit["mismatches"]] == mismatches


def test_small_pad_is_not_shared():
    """The shared rule needs a big land: the record's pin 4 on a pad the
    size of the others, numbered 2, is a mismatch."""
    pads = SOT223_ROW + [("2", 0.0, -3.15, 0.95, 2.0)]
    land = {"pads": [{"number": n, "at_mm": [x, y], "size_mm": [w, h]}
                     for n, x, y, w, h in SOT223_ROW + [("4", 0.0, -3.15, 0.95, 2.0)]]}
    fit = padnum.fit_record(rec_pads(pads), land)
    assert fit["verdict"] == "numbering"
    assert [(m["pin"], m["pads"]) for m in fit["mismatches"]] == [("4", ["2"])]


# ------------------------------------------------- fplib's copper boxes

def custom_pad(at, prims, num="1") -> str:
    return (f'(pad "{num}" smd custom (at {at}) (size 0.2 0.2) (layers F.Cu F.Mask) '
            f'(options (clearance outline) (anchor circle)) (primitives {prims}))')


def one_pad(tmp_path, text):
    fp = fplib.parse_footprint(write_fp(tmp_path, "custom_test", [text]))
    (pad,) = fp.copper_pads
    return pad


def test_ring_pad_copper_is_round_the_port():
    """The T5848's ground ring (pad 3) is anchored 0.66 mm off the port; its
    copper is centred on the port (0.708, 0), 1.625 mm across."""
    fp = fplib.parse_footprint(MIC_FOUND)
    (ring,) = [p for p in fp.copper_pads if p.number == "3"]
    assert ring.center == pytest.approx((1.3705, 0.0))
    assert ring.copper_center == pytest.approx((0.708, 0.0), abs=1e-3)
    assert ring.copper_size == pytest.approx((1.625, 1.625), abs=1e-3)
    # the fixed footprint draws the same ring as a polygon
    (ring,) = [p for p in fplib.parse_footprint(MIC_GOOD).copper_pads if p.number == "3"]
    assert ring.copper_center == pytest.approx((0.708, 0.0), abs=0.01)


def test_custom_pad_turns_with_the_pad(tmp_path):
    """KiCad's positive pad angle turns counter-clockwise on screen (y
    down): a primitive along +x ends up along -y."""
    pad = one_pad(tmp_path, custom_pad(
        "1 0 90", "(gr_rect (start 0.5 -0.1) (end 1.5 0.1) (width 0))"))
    assert pad.copper_center == pytest.approx((1.0, -0.7))
    assert pad.copper_size == pytest.approx((0.2, 1.6))


@pytest.mark.parametrize("start, end", [((-1, 0), (0.6, -0.8)), ((0.6, -0.8), (-1, 0))],
                         ids=["forward", "reversed"])
def test_custom_pad_arc_extent(tmp_path, start, end):
    """An arc's box takes the extremes its sweep passes (the top of the
    circle here), not just its three points; either direction."""
    pad = one_pad(tmp_path, custom_pad(
        "0 0", f"(gr_arc (start {start[0]} {start[1]}) (mid -0.6 -0.8) "
               f"(end {end[0]} {end[1]}) (width 0))"))
    assert pad.copper_size == pytest.approx((1.6, 1.1))
    assert pad.copper_center == pytest.approx((-0.2, -0.45))


def test_plain_pad_copper_box_turns(tmp_path):
    pad = one_pad(tmp_path, '(pad "1" smd rect (at 0 0 90) (size 2 1) '
                            '(layers F.Cu F.Paste F.Mask))')
    assert pad.copper_center == (0.0, 0.0)
    assert pad.copper_size == pytest.approx((1.0, 2.0))


@pytest.mark.parametrize("attr, kind, expected", [
    ("through_hole", "smd", False),     # the pads decide when all one kind
    ("smd", "thru_hole", True),
    (None, "thru_hole", True),
    (None, "smd", False),
])
def test_through_hole(tmp_path, attr, kind, expected):
    """lib/fpattr.py's rule: the pads, when they are all one kind; else the
    (attr)."""
    fp = fplib.parse_footprint(write_fp(tmp_path, "X", SOIC8_CCW, attr=attr, kind=kind))
    assert fp_verify._through_hole(fp) is expected


@pytest.mark.parametrize("attr, expected", [
    (None, False), ("smd", False), ("through_hole", True)])
def test_through_hole_mixed_pads_go_by_attr(tmp_path, attr, expected):
    pads = [pad_text(*SOIC8_CCW[0], kind="thru_hole")] + \
        [pad_text(*p) for p in SOIC8_CCW[1:]]
    fp = fplib.parse_footprint(write_fp(tmp_path, "X", pads, attr=attr))
    assert fp_verify._through_hole(fp) is expected


def test_through_hole_thermal_vias_stay_smd(tmp_path):
    """A plated hole inside an SMD pad is that pad's thermal via, not a pin
    (fpattr): a QFN with vias in its exposed pad is no through-hole part."""
    pads = [pad_text(*p) for p in SOIC8_CCW] + [
        pad_text("9", 0.0, 0.0, 2.4, 3.1),
        '(pad "9" thru_hole circle (at -0.5 -0.5) (size 0.6 0.6) (drill 0.3) '
        '(layers *.Cu))',
        '(pad "9" thru_hole circle (at 0.5 0.5) (size 0.6 0.6) (drill 0.3) '
        '(layers *.Cu))']
    fp = fplib.parse_footprint(write_fp(tmp_path, "X", pads, attr="through_hole"))
    assert fp_verify._through_hole(fp) is False


def test_back_copper_pads_are_no_pins(tmp_path, capsys):
    """A pad whose only copper is B.Cu (an exposed pad's back, a far-side
    test point) is no pin's land: B.Cu copies of a correct SOIC-8's pads,
    numbered clockwise, change neither the record's fit nor the direction
    read from the front pads (counted, they leave the direction unjudged)."""
    back = [f'(pad "{n}" smd rect (at {x} {y}) (size {w} {h}) '
            '(layers B.Cu B.Mask))' for n, x, y, w, h in SOIC8_CW]
    fp = write_fp(tmp_path, SOIC8, [pad_text(*p) for p in SOIC8_CCW] + back)
    for rec, method in ((soic_record(pads=SOIC8_CCW), "record"),
                        (soic_record(), "direction")):
        code, p = verify(tmp_path, capsys, fp, rec)
        assert numbering(p) == []
        facts = p["pad_numbering"]
        assert facts["method"] == method
        assert facts.get("result", "pass") == "pass"
        assert method == "record" or facts["direction"]["direction"] == "ccw"
