"""S6 acceptance tests: parts / library / datasheet tooling.

Plan S6 accept criteria (5 test parts: MCU, buck, USB connector, crystal, 0603 R):
  - search returns in-stock LCSC hits          -> test_net_search_* (net)
  - pulled footprints load in KiCad            -> test_net_lib_pull_loads (net)
  - fp_verify flags a corrupted fp, passes good ones
                                               -> test_fp_verify_* (hermetic)
  - datasheet JSON validates                   -> test_datasheet_validate_* (hermetic)

Hermetic tests (no marker) use committed footprint fixtures + pure venv (jsonschema,
pypdf, sexpdata) and monkeypatched search results - no toolchain, no network. The
live tests carry the `net` marker and SKIP when the JLCPCB endpoint is unreachable,
so an offline run of check.cmd does not hard-fail on a third-party API.
"""
from __future__ import annotations

import json
import sqlite3
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
import lib_pin_types  # noqa: E402
import lib_pull  # noqa: E402
import partslib  # noqa: E402
import parts_search  # noqa: E402

LEGACY_FP = FIXTURES / "cap0402_legacy.kicad_mod"
MODERN_FP = FIXTURES / "cap0402_modern.kicad_mod"

# Correct land pattern for the committed 0402 cap fixtures.
CAP_DS = {
    "mpn": "CL05B104KO5NNNC", "lcsc": "C1525", "package": "0402",
    "pinout": [{"pin": "1", "name": "P1", "type": "passive"},
               {"pin": "2", "name": "P2", "type": "passive"}],
    "land_pattern": {"package": "0402", "pad_count": 2, "pitch_mm": 0.84,
                     "pad_size_mm": [0.5, 0.54], "pin1": "1"},
}


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


def write_ds(tmp_path, data) -> Path:
    p = tmp_path / "ds.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def make_pdf_pages(path: Path, texts: list[str]) -> None:
    """A minimal multi-page text PDF (no authoring lib needed; see LEARNINGS)."""
    n = len(texts)
    font_obj = 3 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (f"<< /Type /Pages /Kids [{kids}] /Count {n} >>").encode("latin-1"),
    ]
    for i, text in enumerate(texts):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                     f"/Resources << /Font << /F1 {font_obj} 0 R >> >> "
                     f"/Contents {4 + 2 * i} 0 R >>").encode("latin-1"))
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream"
                    % (len(content), content))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (i, o)
    xref_at = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
            % (len(objs) + 1, xref_at))
    path.write_bytes(pdf)


def make_mini_pdf(path: Path, text: str) -> None:
    make_pdf_pages(path, [text])


# ============================================================ fplib (parsing)

def test_fplib_parses_both_formats_identically():
    leg = fplib.parse_footprint(LEGACY_FP)
    mod = fplib.parse_footprint(MODERN_FP)
    assert leg.name == mod.name == "C0402"
    for fp in (leg, mod):
        assert len(fp.copper_pads) == 2
        assert fp.has_courtyard and fp.has_layer_kind("SilkS")
        centers = sorted(round(p.center[0], 3) for p in fp.copper_pads)
        assert centers == [-0.42, 0.42]
        assert all(tuple(p.size) == (0.5, 0.54) for p in fp.copper_pads)


def test_fplib_npth_pad_not_copper(tmp_path):
    fp_text = ('(footprint "H" (layer "F.Cu")\n'
               '  (pad "" np_thru_hole circle (at 0 0) (size 2 2) (layers "*.Cu" "*.Mask"))\n'
               '  (pad "1" smd rect (at 1 0) (size 0.5 0.5) (layers "F.Cu"))\n)')
    p = tmp_path / "h.kicad_mod"
    p.write_text(fp_text, encoding="utf-8")
    fp = fplib.parse_footprint(p)
    assert len(fp.pads) == 2 and len(fp.copper_pads) == 1
    assert fp.copper_pads[0].number == "1"


def test_fplib_symbol_names(tmp_path):
    sym = ('(kicad_symbol_lib (version 20211014) (generator test)\n'
           '  (symbol "MyPart" (property "Reference" "U" (at 0 0 0))\n'
           '    (symbol "MyPart_1_1")))')
    p = tmp_path / "l.kicad_sym"
    p.write_text(sym, encoding="utf-8")
    assert fplib.symbol_names(p) == ["MyPart"]


# ============================================================ fp_verify

@pytest.mark.parametrize("fixture", [LEGACY_FP, MODERN_FP])
def test_fp_verify_passes_good(fixture, tmp_path, capsys):
    ds = write_ds(tmp_path, CAP_DS)
    code, payload = run_main(
        fp_verify, ["--footprint", str(fixture), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    assert code == 0 and payload["status"] == "pass"
    assert payload["copper_pads"] == 2
    assert payload["measured_pitch_mm"] == pytest.approx(0.84, abs=1e-3)
    assert payload["violations"] == []
    assert (tmp_path / "o.svg").read_text(encoding="utf-8").startswith("<svg")


def test_fp_verify_flags_pad_count(tmp_path, capsys):
    text = LEGACY_FP.read_text(encoding="utf-8")
    corrupt = "\n".join(l for l in text.splitlines() if "(pad 2 smd" not in l)
    fp = tmp_path / "c.kicad_mod"
    fp.write_text(corrupt, encoding="utf-8")
    ds = write_ds(tmp_path, CAP_DS)
    code, payload = run_main(
        fp_verify, ["--footprint", str(fp), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    assert code == 1 and payload["status"] == "violations"
    kinds = {v["kind"] for v in payload["violations"]}
    assert "pad_count" in kinds
    assert payload["copper_pads"] == 1


def _tht_annulus(tmp_path, capsys, pad, drill):
    """fp_verify one THT pad: (pad 1 thru_hole <shape> (size W H) (drill ...))."""
    fp = tmp_path / "t.kicad_mod"
    fp.write_text(
        '(footprint "T" (layer "F.Cu")\n'
        f'  (pad "1" thru_hole {pad} (at 0 0 0) {drill} (layers "*.Cu" "*.Mask")))\n',
        encoding="utf-8")
    ds = write_ds(tmp_path, {"mpn": "X", "package": "T",
                             "land_pattern": {"package": "T"}})
    _, payload = run_main(
        fp_verify, ["--footprint", str(fp), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    return [v for v in payload["violations"] if v["kind"] == "annulus_floor"]


def test_fp_verify_annulus_oval_pad_oval_drill(tmp_path, capsys):
    # C165948 USB-C shell: 1.2x2.0 oval pad, 0.8x1.5 oval drill -> 0.20 mm
    assert _tht_annulus(tmp_path, capsys, "oval (size 1.2 2.0)",
                        "(drill oval 0.8 1.5)") == []


def test_fp_verify_annulus_round_regression(tmp_path, capsys):
    assert _tht_annulus(tmp_path, capsys, "circle (size 1.6 1.6)", "(drill 1.0)") == []
    bad = _tht_annulus(tmp_path, capsys, "circle (size 1.2 1.2)", "(drill 1.0)")
    assert [v["measured_mm"] for v in bad] == [pytest.approx(0.10)]


def test_fp_verify_annulus_drill_rotated_vs_pad(tmp_path, capsys):
    # long drill axis across the short pad axis: x = (1.2-1.5)/2 = -0.15
    bad = _tht_annulus(tmp_path, capsys, "oval (size 1.2 2.0)",
                       "(drill oval 1.5 0.8)")
    assert [v["measured_mm"] for v in bad] == [pytest.approx(-0.15)]


def test_fp_verify_annulus_thin_oval_fails(tmp_path, capsys):
    bad = _tht_annulus(tmp_path, capsys, "oval (size 1.2 2.0)",
                       "(drill oval 1.0 1.5)")
    assert [v["measured_mm"] for v in bad] == [pytest.approx(0.10)]


def test_fp_verify_flags_wrong_pitch(tmp_path, capsys):
    text = LEGACY_FP.read_text(encoding="utf-8")
    corrupt = text.replace("(at 0.42 0.00 0.00)", "(at 1.42 0.00 0.00)")
    assert corrupt != text
    fp = tmp_path / "c.kicad_mod"
    fp.write_text(corrupt, encoding="utf-8")
    ds = write_ds(tmp_path, CAP_DS)
    code, payload = run_main(
        fp_verify, ["--footprint", str(fp), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    assert code == 1
    pitch = next(v for v in payload["violations"] if v["kind"] == "pad_pitch")
    assert pitch["severity"] == "error"
    assert pitch["measured_mm"] == pytest.approx(1.84, abs=1e-3)


def test_fp_verify_flags_missing_pin1(tmp_path, capsys):
    text = LEGACY_FP.read_text(encoding="utf-8")
    corrupt = text.replace("(pad 1 smd", "(pad 3 smd")
    fp = tmp_path / "c.kicad_mod"
    fp.write_text(corrupt, encoding="utf-8")
    ds = write_ds(tmp_path, CAP_DS)
    code, payload = run_main(
        fp_verify, ["--footprint", str(fp), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    assert code == 1
    assert any(v["kind"] == "pin1_missing" for v in payload["violations"])



# #1127: a depopulated A1 ball, and a pin1 that is prose, are not a missing
# pin 1. Infineon S28HS512T's BGA-24: a 5 x 5 grid at 1.0 mm, A1 left off.
def _bga24(tmp_path, mark: str = "", depop=("A1",)) -> Path:
    pads = [f'  (pad "{r}{c}" smd circle (at {(c - 3) * 1.0:g} {(i - 2) * 1.0:g}) '
            '(size 0.4 0.4) (layers "F.Cu" "F.Paste" "F.Mask"))'
            for i, r in enumerate("ABCDE") for c in range(1, 6)
            if f"{r}{c}" not in depop]
    fp = tmp_path / "bga24.kicad_mod"
    fp.write_text('(footprint "BGA-24_5x5" (layer "F.Cu")\n' + "\n".join(pads)
                  + ("\n" + mark if mark else "") + "\n)\n", encoding="utf-8")
    return fp


def _dot(x, y):
    return (f'  (fp_circle (center {x} {y}) (end {x + 0.1} {y}) '
            '(stroke (width 0.1) (type solid)) (layer "F.SilkS"))')


def _bga_ds(tmp_path, pin1="A1", pinout_a1=False):
    pins = [{"pin": f"{r}{c}", "name": f"{r}{c}", "type": "passive"}
            for r in "ABCDE" for c in range(1, 6) if pinout_a1 or (r, c) != ("A", 1)]
    return write_ds(tmp_path, {"mpn": "S28HS512T", "pinout": pins,
                               "land_pattern": {"pad_count": 24, "pin1": pin1}})


def _fpv(tmp_path, capsys, fp, ds):
    return run_main(fp_verify, ["--footprint", str(fp), "--datasheet-json",
                                str(ds), "--svg", str(tmp_path / "o.svg")],
                    tmp_path, capsys)


def test_fp_verify_depopulated_a1_mark_at_corner(tmp_path, capsys):
    code, payload = _fpv(tmp_path, capsys, _bga24(tmp_path, _dot(-2.8, -2.8)),
                         _bga_ds(tmp_path))
    assert code == 0, payload["violations"]
    v, = [v for v in payload["violations"] if v["kind"].startswith("pin1")]
    assert v["kind"] == "pin1_depopulated" and v["severity"] == "info"
    assert v["site_mm"] == [-2.0, -2.0] and v["mark"] == "ok"


def test_fp_verify_depopulated_a1_no_mark_is_info(tmp_path, capsys):
    code, payload = _fpv(tmp_path, capsys, _bga24(tmp_path), _bga_ds(tmp_path))
    assert code == 0
    v, = [v for v in payload["violations"] if v["kind"].startswith("pin1")]
    assert v["kind"] == "pin1_depopulated" and v["mark"] == "none"


def test_fp_verify_depopulated_a1_mark_off_corner_warns(tmp_path, capsys):
    code, payload = _fpv(tmp_path, capsys, _bga24(tmp_path, _dot(2.8, 2.8)),
                         _bga_ds(tmp_path))
    assert code == 0
    v, = [v for v in payload["violations"] if v["kind"].startswith("pin1")]
    assert v["kind"] == "pin1_mark_off_corner" and v["severity"] == "warning"
    assert v["marks_off"] == [[2.8, 2.8]]


def test_fp_verify_depopulated_corners_large_grid(tmp_path, capsys):
    """OSD62x-PM class: all four corner balls left off; A1's site still found."""
    fp = _bga24(tmp_path, _dot(-2.8, -2.8), depop=("A1", "A5", "E1", "E5"))
    ds = write_ds(tmp_path, {"mpn": "OSD", "pinout": [{"pin": "B2", "type": "passive"}],
                             "land_pattern": {"pad_count": 21, "pin1": "A1"}})
    code, payload = _fpv(tmp_path, capsys, fp, ds)
    assert code == 0
    v, = [v for v in payload["violations"] if v["kind"].startswith("pin1")]
    assert v["kind"] == "pin1_depopulated" and v["site_mm"] == [-2.0, -2.0]


def test_fp_verify_a1_in_pinout_but_no_pad_is_still_missing(tmp_path, capsys):
    """The quiet twin: the record says ball A1 exists, so its absence is real."""
    code, payload = _fpv(tmp_path, capsys, _bga24(tmp_path, _dot(-2.8, -2.8)),
                         _bga_ds(tmp_path, pinout_a1=True))
    assert code == 1
    assert any(v["kind"] == "pin1_missing" for v in payload["violations"])


def test_fp_verify_pin1_prose_not_judged(tmp_path, capsys):
    """MGM240P class: the record's pin1 is a sentence, not a pad name."""
    prose = "top-left in land pattern view (Fig 8.3); pins 1-14 left column"
    ds = write_ds(tmp_path, {**CAP_DS, "land_pattern": {
        **CAP_DS["land_pattern"], "pin1": prose}})
    code, payload = _fpv(tmp_path, capsys, LEGACY_FP, ds)
    assert code == 0
    assert not any(v["kind"].startswith("pin1") for v in payload["violations"])
    nj, = [n for n in payload["not_judged"] if n["check"] == "pin1"]
    assert "not a pad name" in nj["why"]


# #1102: which pad carries which pin. Each wrong footprint is the issue's.
def _numbered_fp(tmp_path, name, pads, npth=()) -> Path:
    """pads: (number, x, y, w, h) in KiCad's y-down frame."""
    lines = [f'  (pad "{n}" smd rect (at {x:g} {y:g}) (size {w:g} {h:g}) '
             '(layers "F.Cu" "F.Paste" "F.Mask"))' for n, x, y, w, h in pads]
    lines += [f'  (pad "" np_thru_hole circle (at {x:g} {y:g}) (size {d:g} {d:g}) '
              f'(drill {d:g}) (layers "*.Cu" "*.Mask"))' for x, y, d in npth]
    fp = tmp_path / f"{name}.kicad_mod"
    fp.write_text(f'(footprint "{name}" (layer "F.Cu")\n' + "\n".join(lines)
                  + "\n)\n", encoding="utf-8")
    return fp


SIT_RIGHT = {"1": (-1.1, 0.95), "2": (1.1, 0.95), "3": (1.1, -0.95),
             "4": (-1.1, -0.95)}
SIT_WRONG = {"1": (-1.1, -0.95), "2": (-1.1, 0.95), "3": (1.1, 0.95),
             "4": (1.1, -0.95)}


def _sit_ds(tmp_path, pads=SIT_RIGHT, **extra):
    return write_ds(tmp_path, {"mpn": "SiT1602B", "pinout": [
        {"pin": str(i), "name": n, "type": "passive"}
        for i, n in enumerate(["OE", "GND", "OUT", "VDD"], 1)],
        "land_pattern": {"pad_count": 4, "pin1": "1", **extra, "pads": [
            {"number": k, "at_mm": list(xy)} for k, xy in pads.items()]}})


def _sit_fp(tmp_path, pads, turn=False):
    return _numbered_fp(tmp_path, "SiT", [
        (k, -y if turn else x, x if turn else y, 1.4, 1.2)
        for k, (x, y) in pads.items()])


def test_fp_verify_sit1602_numbered_round_the_short_side_fails(tmp_path, capsys):
    code, payload = _fpv(tmp_path, capsys, _sit_fp(tmp_path, SIT_WRONG),
                         _sit_ds(tmp_path))
    assert code == 1
    v, = [v for v in payload["violations"] if v["kind"] == "pad_numbering"]
    assert v["severity"] == "error" and v["hint"] == "numbering_rotated"
    assert {"pad": "1", "drawing_pin": "4"} in v["misplaced"]
    assert payload["pad_numbering"]["fit"] is False


def test_fp_verify_sit1602_right_numbering_passes_turned(tmp_path, capsys):
    for turn in (False, True):
        code, payload = _fpv(tmp_path, capsys,
                             _sit_fp(tmp_path, SIT_RIGHT, turn), _sit_ds(tmp_path))
        assert code == 0, payload["violations"]
        f = payload["pad_numbering"]
        assert f["fit"] is True and f["turn_deg"] in ((90, 270) if turn else (0,))


T5848_RIGHT = {"7": (-1.364, -0.9), "6": (-0.544, -0.9), "2": (-0.544, 0.9),
               "1": (-1.364, 0.9), "8": (-1.364, 0.0), "5": (1.473, -1.05),
               "4": (1.473, 1.05)}


def _t5848(tmp_path, swap: bool):
    pos = dict(T5848_RIGHT)
    if swap:
        for a, b in (("1", "7"), ("2", "6"), ("4", "5")):
            pos[a], pos[b] = pos[b], pos[a]
    pads = [(k, x, y, 0.3 if k in "45" else 0.522, 0.3 if k in "45" else 0.6)
            for k, (x, y) in pos.items()]
    pads.append(("3", 0.708, 0.0, 1.0, 1.0))
    return _numbered_fp(tmp_path, "T5848", pads, npth=[(0.708, 0.0, 0.5)])


def test_fp_verify_t5848_bottom_view_numbering_fails(tmp_path, capsys):
    ref = {**T5848_RIGHT, "3": (0.708, 0.0)}
    ds = write_ds(tmp_path, {"mpn": "T5848", "pinout": [
        {"pin": k, "type": "passive"} for k in ref],
        "land_pattern": {"pad_count": 8, "pin1": "1", "pads": [
            {"number": k, "at_mm": list(xy)} for k, xy in ref.items()]}})
    code, payload = _fpv(tmp_path, capsys, _t5848(tmp_path, True), ds)
    assert code == 1
    v, = [v for v in payload["violations"] if v["kind"] == "pad_numbering"]
    assert v["hint"] == "bottom_view_numbering"
    assert {"pad": "1", "drawing_pin": "7"} in v["misplaced"]
    code, payload = _fpv(tmp_path, capsys, _t5848(tmp_path, False), ds)
    assert code == 0, payload["violations"]
    assert payload["pad_numbering"]["fit"] is True


def test_fp_verify_ball_grid_numbered_from_the_bottom(tmp_path, capsys):
    ds = _bga_ds(tmp_path, pinout_a1=True)
    code, payload = _fpv(tmp_path, capsys, _bga24(tmp_path, depop=()), ds)
    assert not [v for v in payload["violations"] if v["kind"] == "pad_numbering"]
    assert payload["pad_numbering"] == {"from": "ball_names", "fit": True}
    fp = _bga24(tmp_path, depop=())
    fp.write_text(fp.read_text().replace("(at -", "(at M").replace(
        "(at ", "(at -").replace("(at -M", "(at ").replace("(at -0 ", "(at 0 "))
    code, payload = _fpv(tmp_path, capsys, fp, ds)
    assert code == 1
    v, = [v for v in payload["violations"] if v["kind"] == "pad_numbering"]
    assert v["hint"] == "bottom_view_numbering"


def test_fp_verify_ccw_numbering_and_not_judged(tmp_path, capsys):
    ccw = {"1": (-1.1, -0.95), "2": (-1.1, 0.95), "3": (1.1, 0.95),
           "4": (1.1, -0.95)}  # down the left, back up the right: CCW on screen
    cw = {k: (-x, y) for k, (x, y) in ccw.items()}
    lp = {"pad_count": 4, "pin1": "1"}
    for pads, code_want in ((ccw, 0), (cw, 1)):
        ds = write_ds(tmp_path, {"mpn": "X", "pinout": [{"pin": "1", "type": "passive"}],
                                 "land_pattern": {**lp, "numbering": "ccw_top"}})
        code, payload = _fpv(tmp_path, capsys, _sit_fp(tmp_path, pads), ds)
        assert code == code_want, payload["violations"]
    ds = write_ds(tmp_path, {"mpn": "X", "pinout": [{"pin": "1", "type": "passive"}],
                             "land_pattern": lp})
    code, payload = _fpv(tmp_path, capsys, _sit_fp(tmp_path, cw), ds)
    assert code == 0 and payload["pad_numbering"] is None
    assert [n for n in payload["not_judged"] if n["check"] == "pad_numbering"]


def test_datasheet_validate_land_pattern_pads(tmp_path, capsys):
    ds = {"mpn": "X", "pinout": [], "land_pattern": {
        "pad_count": 2, "numbering": "ccw_top", "pads": [
            {"number": "1", "at_mm": [0, 0], "size_mm": [1, 1],
             "source": "POD fig 3, mirrored"},
            {"number": "2", "at_mm": [1, 0]}]}}

    def check(name):
        return run_main(datasheet_extract, ["--validate", str(write_ds(tmp_path, ds))],
                        tmp_path, capsys, name=name)[0]
    assert check("ok") == 0
    ds["land_pattern"]["pads"][1]["at_mm"] = [1]
    assert check("short") == 1
    ds["land_pattern"]["pads"][1]["at_mm"] = [1, 0]
    ds["land_pattern"]["numbering"] = "cw"
    assert check("dir") == 1


# #1066: a footprint with two pitches. A micro-SD socket: eight contacts at
# 1.1 mm, a card-detect pad of another size 0.95 mm past contact 8, and four
# shield tabs. The nearest pair of pads (contact 8 to the switch) is no pitch.
def _microsd(tmp_path, move=None, extra=()) -> Path:
    def pad(n, x, y, w, h):
        return (f'  (pad "{n}" smd rect (at {x:g} {y:g}) (size {w:g} {h:g}) '
                '(layers "F.Cu" "F.Paste" "F.Mask"))')
    pads = [pad(i, (i - 1) * 1.1 + (move[1] if move and move[0] == i else 0),
                0, 0.7, 1.6) for i in range(1, 9)]
    pads.append(pad("CD", 7 * 1.1 + 0.95, 0, 0.5, 1.0))
    pads += [pad(f"S{i}", x, y, 1.2, 2.2) for i, (x, y) in
             enumerate([(-2.5, 4), (10.2, 4), (-2.5, 9), (10.2, 9)], 1)]
    pads += [pad(n, x, y, 0.7, 1.6) for n, x, y in extra]
    lib = tmp_path / "lib.pretty"
    lib.mkdir(exist_ok=True)
    fp = lib / "microSD.kicad_mod"
    fp.write_text('(footprint "microSD" (layer "F.Cu")\n' + "\n".join(pads)
                  + "\n)\n", encoding="utf-8")
    return fp


def _microsd_ds(tmp_path, n=13, **land):
    return write_ds(tmp_path, {"mpn": "TF-01A", "land_pattern": {
        "pad_count": n, "pitch_mm": 1.1, "pin1": "1", **land}})


def test_fp_verify_two_pitch_microsd_passes(tmp_path, capsys):
    code, payload = _fpv(tmp_path, capsys, _microsd(tmp_path), _microsd_ds(tmp_path))
    assert code == 0, payload["violations"]
    assert not any(v["kind"] == "pad_pitch" for v in payload["violations"])
    assert payload["measured_pitch_mm"] == pytest.approx(1.1, abs=1e-3)
    row, = payload["pitch_rows"]
    assert row["pads"] == "1..8 (8 pads)" and row["ok"]


def test_fp_verify_contact_moved_along_row_fails(tmp_path, capsys):
    code, payload = _fpv(tmp_path, capsys, _microsd(tmp_path, move=(5, 0.1)),
                         _microsd_ds(tmp_path))
    assert code == 1
    v, = [v for v in payload["violations"] if v["kind"] == "pad_pitch"]
    assert v["severity"] == "error" and "moved" in v["msg"]
    assert set(v["pads"]) == {"4", "5", "6"}


def test_fp_verify_pitches_mm_judges_each_run(tmp_path, capsys):
    """A second row at 2.0 mm: unstated it is not judged; stated, it is."""
    fp = _microsd(tmp_path, extra=[("9", 0, 6), ("10", 2, 6), ("11", 4, 6)])
    code, payload = _fpv(tmp_path, capsys, fp, _microsd_ds(tmp_path, n=16))
    assert code == 0, payload["violations"]
    nj, = [n for n in payload.get("not_judged", []) if n["check"] == "pitch"]
    assert "9..11" in nj["why"] and "pitches_mm" in nj["why"]
    code, payload = _fpv(tmp_path, capsys, fp, _microsd_ds(
        tmp_path, n=16, pitches_mm=[{"pads": "9-11", "pitch_mm": 2.0}]))
    assert code == 0, payload["violations"]
    assert not [n for n in payload.get("not_judged", []) if n["check"] == "pitch"]
    code, payload = _fpv(tmp_path, capsys, fp, _microsd_ds(
        tmp_path, n=16, pitches_mm=[{"pads": ["9", "10", "11"], "pitch_mm": 2.5}]))
    assert code == 1
    v, = [v for v in payload["violations"] if v["kind"] == "pad_pitch"]
    assert v["expected_mm"] == 2.5 and v["pads"] == ["9", "10", "11"]


def test_fp_verify_svg_stays_out_of_the_library(tmp_path, capsys):
    fp = _microsd(tmp_path)
    ds = _microsd_ds(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    out = reports / "fp_verify_J1.json"
    assert fp_verify.main(["--footprint", str(fp), "--datasheet-json",
                           str(ds), "--out", str(out)]) == 0
    capsys.readouterr()
    assert [p.name for p in fp.parent.iterdir()] == ["microSD.kicad_mod"]
    svg = reports / "fp_verify_J1.overlay.svg"
    assert svg.read_text(encoding="utf-8").startswith("<svg")
    assert json.loads(out.read_text(encoding="utf-8"))["svg"] == str(svg)
    # to stdout: no report folder, so no SVG at all
    assert fp_verify.main(["--footprint", str(fp), "--datasheet-json", str(ds)]) == 0
    assert "svg" not in json.loads(capsys.readouterr().out)
    assert [p.name for p in fp.parent.iterdir()] == ["microSD.kicad_mod"]


def test_datasheet_validate_pitches_mm(tmp_path, capsys):
    ds = {"mpn": "X", "pinout": [], "land_pattern": {"pad_count": 9, "pitches_mm": [
        {"pads": "1-8", "pitch_mm": 1.1}, {"pads": ["CD", "SW"], "pitch_mm": 0.9}]}}
    code, _ = run_main(datasheet_extract, ["--validate", str(write_ds(tmp_path, ds))],
                       tmp_path, capsys)
    assert code == 0
    ds["land_pattern"]["pitches_mm"] = [{"pads": "1-8"}]
    code, _ = run_main(datasheet_extract, ["--validate", str(write_ds(tmp_path, ds))],
                       tmp_path, capsys, name="bad")
    assert code == 1


def test_datasheet_validate_rejects_prose_pin1(tmp_path, capsys):
    ds = {**CAP_DS, "land_pattern": {**CAP_DS["land_pattern"],
                                     "pin1": "top-left in land pattern view"}}
    code, payload = run_main(datasheet_extract, ["--validate", str(write_ds(tmp_path, ds))],
                             tmp_path, capsys)
    assert code == 1 and payload["errors"][0]["path"] == "land_pattern/pin1"
    for name in ("1", "A1", "EP", "SH2"):
        ds["land_pattern"]["pin1"] = name
        code, _ = run_main(datasheet_extract, ["--validate", str(write_ds(tmp_path, ds))],
                           tmp_path, capsys, name=name)
        assert code == 0, name

def _custom_pad(tmp_path, capsys, prims):
    """fp_verify one custom SMD pad with the given primitives."""
    fp = tmp_path / "m.kicad_mod"
    fp.write_text(
        '(footprint "M" (layer "F.Cu")\n'
        '  (fp_rect (start -1 -1) (end 1 1) (layer "F.CrtYd") (width 0.05))\n'
        '  (pad "1" smd custom (at 0 0 0) (size 0.3 0.3) (layers "F.Cu" "F.Mask")\n'
        f'    (primitives {prims})))\n', encoding="utf-8")
    ds = write_ds(tmp_path, {"mpn": "X", "package": "M",
                             "land_pattern": {"package": "M"}})
    code, payload = run_main(
        fp_verify, ["--footprint", str(fp), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    return code, [v for v in payload["violations"]
                  if v["kind"] == "stroked_pad_primitive"]


def test_fp_verify_warns_stroked_pad_ring(tmp_path, capsys):
    # #1133: the T5848 GND ring, r 0.6625 with a 0.3 stroke; DRC sees only r 0.6625
    code, w = _custom_pad(tmp_path, capsys,
                          "(gr_circle (center 0 0) (end 0.6625 0) (width 0.3) (fill no))")
    assert code == 0 and len(w) == 1 and w[0]["severity"] == "warning"
    assert w[0]["primitives"] == ["gr_circle (width 0.3)"]
    # the new stroke form, an explicit (fill none), and a circle with no fill token
    _, w = _custom_pad(tmp_path, capsys,
                       "(gr_rect (start -0.5 -0.5) (end 0.5 0.5) (stroke (width 0.2)) (fill no))"
                       " (gr_poly (pts (xy 0 0) (xy 1 0) (xy 0 1)) (width 0.1) (fill none))"
                       " (gr_circle (center 0 0) (end 0.5 0) (width 0.2))")
    assert w[0]["primitives"] == ["gr_rect (width 0.2)", "gr_poly (width 0.1)",
                                  "gr_circle (width 0.2)"]


def test_fp_verify_filled_or_open_pad_primitives_pass(tmp_path, capsys):
    # the fix (filled polys), a filled circle, a zero-width outline and a line
    for prims in ("(gr_poly (pts (xy 0 0) (xy 1 0) (xy 0 1)) (width 0) (fill yes))",
                  "(gr_circle (center 0 0) (end 0.5 0) (width 0.2) (fill solid))",
                  "(gr_circle (center 0 0) (end 0.5 0) (width 0) (fill no))",
                  "(gr_line (start 0 0) (end 1 0) (width 0.2))",
                  # KiCad 5 / easyeda2kicad: no fill token, and KiCad fills it
                  "(gr_poly (pts (xy 0 0) (xy 1 0) (xy 0 1)) (width 0.1))",
                  "(gr_rect (start -0.5 -0.5) (end 0.5 0.5) (width 0.1))"):
        assert _custom_pad(tmp_path, capsys, prims) == (0, [])


def test_fp_verify_courtyard_is_warning_not_failure(tmp_path, capsys):
    text = LEGACY_FP.read_text(encoding="utf-8")
    stripped = "\n".join(l for l in text.splitlines() if "F.CrtYd" not in l)
    fp = tmp_path / "nc.kicad_mod"
    fp.write_text(stripped, encoding="utf-8")
    ds = write_ds(tmp_path, CAP_DS)
    code, payload = run_main(
        fp_verify, ["--footprint", str(fp), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    # a warning-only finding must NOT fail the gate
    assert code == 0 and payload["status"] == "pass"
    warns = [v for v in payload["violations"] if v["kind"] == "no_courtyard"]
    assert len(warns) == 1 and warns[0]["severity"] == "warning"
    assert payload["has_courtyard"] is False


def test_fp_verify_pad_size_warning(tmp_path, capsys):
    ds = dict(CAP_DS)
    ds["land_pattern"] = dict(CAP_DS["land_pattern"], pad_size_mm=[0.9, 0.9])
    dsp = write_ds(tmp_path, ds)
    code, payload = run_main(
        fp_verify, ["--footprint", str(LEGACY_FP), "--datasheet-json", str(dsp),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    assert code == 0  # size mismatch is a warning only
    sz = [v for v in payload["violations"] if v["kind"] == "pad_size"]
    assert len(sz) == 1 and sz[0]["severity"] == "warning"


def test_fp_verify_bad_json_exit2(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code, payload = run_main(
        fp_verify, ["--footprint", str(LEGACY_FP), "--datasheet-json", str(bad)],
        tmp_path, capsys)
    assert code == 2 and payload["status"] == "error"


# ============================================================ datasheet_extract

def test_datasheet_schema_is_valid():
    import jsonschema
    code = datasheet_extract.main(["--schema"])
    assert code == 0
    jsonschema.Draft202012Validator.check_schema(datasheet_extract.DATASHEET_SCHEMA)


def test_datasheet_validate_good(tmp_path, capsys):
    p = write_ds(tmp_path, CAP_DS)
    code, payload = run_main(datasheet_extract, ["--validate", str(p)], tmp_path, capsys)
    assert code == 0 and payload["status"] == "pass"
    assert payload["mpn"] == "CL05B104KO5NNNC" and payload["pins"] == 2


def test_datasheet_validate_bad(tmp_path, capsys):
    bad = {"lcsc": "C1", "pinout": [{"pin": "1", "type": "nonsense"}],
           "land_pattern": {"pitch_mm": -1}}
    p = write_ds(tmp_path, bad)
    code, payload = run_main(datasheet_extract, ["--validate", str(p)], tmp_path, capsys)
    assert code == 1 and payload["status"] == "invalid"
    msgs = " ".join(e["msg"] for e in payload["errors"])
    assert "mpn" in msgs and "pad_count" in msgs and "nonsense" in msgs


def test_datasheet_validate_unreadable_exit2(tmp_path, capsys):
    code, payload = run_main(
        datasheet_extract, ["--validate", str(tmp_path / "nope.json")], tmp_path, capsys)
    assert code == 2 and payload["status"] == "error"


def test_datasheet_pdf_text_extraction(tmp_path, capsys):
    pdf = tmp_path / "d.pdf"
    make_mini_pdf(pdf, "STM32F103 VDD VSS 100nF decoupling LQFP-48 0.5mm pitch")
    code, payload = run_main(
        datasheet_extract, ["--pdf", str(pdf), "--lcsc", "C8734"], tmp_path, capsys)
    assert code == 0 and payload["status"] == "extracted"
    assert payload["n_pages"] == 1 and payload["lcsc"] == "C8734"
    assert "STM32F103" in payload["text_by_page"][0]["text"]
    # the grounding payload carries the schema + a template for the agent
    assert "schema" in payload and payload["template"]["land_pattern"]["pad_count"] == 0


# ============================================================ partslib / parts_search (hermetic)

_CANNED = [
    {"lcsc": "C1", "model": "EXT", "type": "Extended", "stock": 100, "price": 0.5,
     "package": "0603", "brand": "ACME", "description": "ext part"},
    {"lcsc": "C2", "model": "BAS_LO", "type": "Basic", "stock": 50, "price": 0.2,
     "package": "0603", "brand": "ACME", "description": "basic low stock"},
    {"lcsc": "C3", "model": "BAS_HI", "type": "Basic", "stock": 9999, "price": 0.9,
     "package": "0805", "brand": "OTHER", "description": "basic high stock"},
]


def test_partslib_normalize_and_rank():
    parts = [partslib.normalize(x) for x in _CANNED]
    parts.sort(key=partslib.rank_key)
    # Basic first, then higher stock, then cheaper -> C3, C2, C1
    assert [p["lcsc"] for p in parts] == ["C3", "C2", "C1"]
    assert parts[0]["basic"] is True and parts[-1]["basic"] is False


def test_partslib_apply_filters():
    parts = [partslib.normalize(x) for x in _CANNED]
    assert {p["lcsc"] for p in partslib.apply_filters(parts, basic_only=True)} == {"C2", "C3"}
    assert {p["lcsc"] for p in partslib.apply_filters(parts, package="0603")} == {"C1", "C2"}
    assert {p["lcsc"] for p in partslib.apply_filters(parts, min_stock=1000)} == {"C3"}
    assert {p["lcsc"] for p in partslib.apply_filters(parts, max_price=0.3)} == {"C2"}
    assert {p["lcsc"] for p in partslib.apply_filters(parts, brand="acme")} == {"C1", "C2"}


def test_parts_search_ranks_and_filters(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(partslib, "live_search", lambda *a, **k: (list(_CANNED), 3))
    code, payload = run_main(
        parts_search, ["--query", "x", "--limit", "5"], tmp_path, capsys)
    assert code == 0 and payload["status"] == "pass"
    assert payload["source"] == "live"
    assert [r["lcsc"] for r in payload["results"]] == ["C3", "C2", "C1"]
    assert payload["results"][0]["rank"] == 0


def test_parts_search_basic_only_filter(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(partslib, "live_search", lambda *a, **k: (list(_CANNED), 3))
    code, payload = run_main(
        parts_search, ["--query", "x", "--basic-only", "--min-stock", "1000"],
        tmp_path, capsys)
    assert code == 0
    assert [r["lcsc"] for r in payload["results"]] == ["C3"]


def test_parts_search_bad_filter_key_exit2(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(partslib, "live_search", lambda *a, **k: (list(_CANNED), 3))
    code, payload = run_main(
        parts_search, ["--query", "x", "--filters", "bogus=1"], tmp_path, capsys)
    assert code == 2 and payload["status"] == "error"


def test_parts_search_offline_no_db_exit2(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(partslib, "live_search", lambda *a, **k: ([], 0))
    monkeypatch.setattr(partslib, "endpoint_reachable", lambda *a, **k: False)
    code, payload = run_main(parts_search, ["--query", "x"], tmp_path, capsys)
    assert code == 2 and payload["status"] == "error"
    assert "unreachable" in payload["error"]


def _block_import(monkeypatch, name):
    import builtins
    real = builtins.__import__

    def fake(n, *a, **k):
        if n == name or n.startswith(name + "."):
            raise ImportError(f"No module named '{name}'")
        return real(n, *a, **k)
    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(builtins, "__import__", fake)


def test_parts_search_missing_httpx_names_cause(tmp_path, capsys, monkeypatch):
    called = []
    monkeypatch.setattr(partslib, "live_search",
                        lambda *a, **k: called.append(1) or ([], 0))
    _block_import(monkeypatch, "httpx")
    code, payload = run_main(parts_search, ["--query", "x"], tmp_path, capsys)
    assert code == 2 and payload["status"] == "error"
    assert "httpx" in payload["error"] and sys.executable in payload["error"]
    # inside the module the hint names its venv or runner (94ec4b7), both
    # built from requirements.lock; standalone it names the hwde venv
    assert partslib._venv_hint() in payload["error"]
    assert "requirements.lock" in payload["error"]
    assert "unreachable" not in payload["error"]
    assert not called


def test_parts_search_missing_easyeda_names_cause(tmp_path, capsys, monkeypatch):
    _block_import(monkeypatch, "easyeda2kicad")
    code, payload = run_main(parts_search, ["--query", "x"], tmp_path, capsys)
    assert code == 2 and "easyeda2kicad" in payload["error"]
    assert "unreachable" not in payload["error"]


def test_parts_search_unexpected_error_not_unreachable(tmp_path, capsys, monkeypatch):
    def boom(*a, **k):
        raise KeyError("oops")
    monkeypatch.setattr(partslib, "live_search", boom)
    code, payload = run_main(parts_search, ["--query", "x"], tmp_path, capsys)
    assert code == 2 and "KeyError" in payload["error"]
    assert "unreachable" not in payload["error"]


def test_endpoint_reachable_network_error_is_false(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "get", boom)
    assert partslib.endpoint_reachable() is False


def test_parts_search_real_network_error_says_unreachable(tmp_path, capsys, monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(partslib, "live_search", lambda *a, **k: ([], 0))
    monkeypatch.setattr(httpx, "get", boom)
    code, payload = run_main(parts_search, ["--query", "x"], tmp_path, capsys)
    assert code == 2 and "unreachable" in payload["error"]


def test_parts_search_empty_but_reachable_exit0(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(partslib, "live_search", lambda *a, **k: ([], 0))
    monkeypatch.setattr(partslib, "endpoint_reachable", lambda *a, **k: True)
    code, payload = run_main(parts_search, ["--query", "zzz"], tmp_path, capsys)
    assert code == 0 and payload["status"] == "empty" and payload["count"] == 0


# ------------------------------------------------ jlcparts SQLite fallback

def _make_jlcparts_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE components (lcsc INTEGER PRIMARY KEY, mfr TEXT, "
                "package TEXT, basic INTEGER, description TEXT, datasheet TEXT, "
                "stock INTEGER, price TEXT)")
    con.executemany(
        "INSERT INTO components VALUES (?,?,?,?,?,?,?,?)",
        [(25804, "RC0603FR-0710KL", "0603", 1, "10kOhm 0603 1% resistor",
          "http://d/r.pdf", 1_000_000, '[{"qFrom":1,"price":0.001}]'),
         (100, "CAPX", "0402", 0, "100nF 0402 X7R cap", "http://d/c.pdf",
          5000, '[{"qFrom":1,"price":0.01}]')])
    con.commit()
    con.close()


def test_db_search_reads_jlcparts_schema(tmp_path):
    db = tmp_path / "cache.sqlite3"
    _make_jlcparts_db(db)
    res = partslib.db_search(db, "10k")
    assert len(res) == 1
    r = res[0]
    assert r["lcsc"] == "C25804" and r["basic"] is True
    assert r["stock"] == 1_000_000 and r["price"] == pytest.approx(0.001)


def test_db_search_missing_file_raises():
    with pytest.raises(partslib.PartsError):
        partslib.db_search("no/such.sqlite3", "x")


def test_parts_search_db_fallback(tmp_path, capsys, monkeypatch):
    db = tmp_path / "cache.sqlite3"
    _make_jlcparts_db(db)
    monkeypatch.setattr(partslib, "live_search", lambda *a, **k: ([], 0))
    code, payload = run_main(
        parts_search, ["--query", "10k", "--db", str(db)], tmp_path, capsys)
    assert code == 0 and payload["source"] == "db"
    assert payload["results"][0]["lcsc"] == "C25804"


@pytest.mark.parametrize("module", ["httpx", "easyeda2kicad"])
def test_parts_search_db_works_without_live_deps(tmp_path, capsys, monkeypatch, module):
    db = tmp_path / "cache.sqlite3"
    _make_jlcparts_db(db)
    _block_import(monkeypatch, module)
    code, payload = run_main(
        parts_search, ["--query", "10k", "--db", str(db)], tmp_path, capsys)
    assert code == 0 and payload["source"] == "db"
    assert payload["results"][0]["lcsc"] == "C25804"
    assert module in payload["note"] and sys.executable in payload["note"]


def test_lib_pull_registration_idempotent(tmp_path):
    """Lib-table registration writes portable URIs and is idempotent (no live pull)."""
    project = tmp_path / "kicad"
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "aiee.kicad_sym").write_text("(kicad_symbol_lib)", encoding="utf-8")
    pretty = lib_dir / "aiee.pretty"
    pretty.mkdir()
    r1 = lib_pull._register_project(project, "aiee",
                                    lib_dir / "aiee.kicad_sym", pretty)
    assert r1 == {"fp": "created", "sym": "created"}
    r2 = lib_pull._register_project(project, "aiee",
                                    lib_dir / "aiee.kicad_sym", pretty)
    assert r2 == {"fp": "present", "sym": "present"}
    fp_table = (project / "fp-lib-table").read_text(encoding="utf-8")
    assert fp_table.count('(name "aiee")') == 1
    assert "${KIPRJMOD}/../lib/aiee.pretty" in fp_table


# ============================================ lib_pull per-part gate (T6 batch C)
#
# Ground truth (LEARNINGS 2026-07-28): a part is present iff aiee.kicad_sym
# holds a top-level (symbol ...) with (property "LCSC Part" "<id>") whose
# Footprint property resolves in aiee.pretty. The old footprint-grep gate
# failed BOTH ways (shared footprint -> false error; 403 + id in some file ->
# false pass) and substring-matched C2580 against C25804.

_MOD_TEXT = ('(module {name} (layer F.Cu)\n'
             '  (descr "first puller {lcsc}")\n'
             '  (pad 1 smd rect (at -0.5 0) (size 0.5 0.5) '
             '(layers F.Cu F.Paste F.Mask))\n'
             '  (pad 2 smd rect (at 0.5 0) (size 0.5 0.5) '
             '(layers F.Cu F.Paste F.Mask))\n'
             ')\n')


def _sym_block(name: str, lcsc: str, fp: str) -> str:
    return (f'  (symbol "{name}" (property "Reference" "U" (at 0 0 0))\n'
            f'    (property "Footprint" "aiee:{fp}" (at 0 0 0))\n'
            f'    (property "LCSC Part" "{lcsc}" (at 0 0 0))\n'
            f'    (symbol "{name}_1_1"))')


def _write_sym_lib(path: Path, symbols: list[tuple[str, str, str]]) -> None:
    blocks = "\n".join(_sym_block(*s) for s in symbols)
    path.write_text("(kicad_symbol_lib (version 20211014) (generator test)\n"
                    + blocks + "\n)\n", encoding="utf-8")


def _fake_lib(tmp_path: Path, symbols, footprints) -> Path:
    """lib/aiee.{kicad_sym,pretty} with the given symbol + footprint content."""
    lib_dir = tmp_path / "lib"
    pretty = lib_dir / "aiee.pretty"
    pretty.mkdir(parents=True)
    _write_sym_lib(lib_dir / "aiee.kicad_sym", symbols)
    for name, lcsc in footprints:
        (pretty / f"{name}.kicad_mod").write_text(
            _MOD_TEXT.format(name=name, lcsc=lcsc), encoding="utf-8")
    return lib_dir


def _ok_run(*a, **k):
    return SimpleNamespace(stdout="Created Kicad symbol\nCreated Kicad footprint",
                           stderr="", returncode=0)


def test_fplib_symbol_index_reads_lcsc_and_footprint(tmp_path):
    lib_dir = _fake_lib(tmp_path, [("PartA", "C999", "C0603")],
                        [("C0603", "C999")])
    idx = fplib.symbol_index(lib_dir / "aiee.kicad_sym")
    assert idx == [{"name": "PartA", "lcsc": "C999", "footprint": "aiee:C0603",
                    "reference": "U"}]
    assert fplib.symbol_index(tmp_path / "missing.kicad_sym") == []


def test_pull_gate_shared_footprint_reports_pulled(tmp_path, monkeypatch):
    """A part whose footprint was first pulled by ANOTHER part must verify:
    the .kicad_mod records only the first puller's id."""
    lib_dir = _fake_lib(tmp_path, [("PartA", "C999", "C0603")],
                        [("C0603", "C999")])       # file text lacks C111

    def run(*a, **k):                              # the pull adds PartB only
        _write_sym_lib(lib_dir / "aiee.kicad_sym",
                       [("PartA", "C999", "C0603"), ("PartB", "C111", "C0603")])
        return _ok_run()
    monkeypatch.setattr(lib_pull, "_run_easyeda", run)
    r = lib_pull._pull_one("C111", lib_dir / "aiee", True, False)
    assert r["status"] == "pulled", r
    assert r["symbol_verified"] and r["footprint_verified"]
    assert r["footprints"][0]["name"] == "C0603"


def test_pull_gate_403_reports_error_even_when_id_sits_in_a_footprint(tmp_path, monkeypatch):
    lib_dir = _fake_lib(tmp_path, [("PartA", "C999", "C0603")],
                        [("C0603", "C222")])       # raw text carries C222
    monkeypatch.setattr(
        lib_pull, "_run_easyeda",
        lambda *a, **k: SimpleNamespace(stdout="",
                                        stderr="HTTP Error 403: Forbidden",
                                        returncode=1))
    r = lib_pull._pull_one("C222", lib_dir / "aiee", True, False)
    assert r["status"] == "error"
    assert r["symbol_verified"] is False
    assert "no symbol carrying" in r["detail"] and "403" in r["detail"]


def test_pull_gate_lcsc_match_is_exact_not_substring(tmp_path, monkeypatch):
    lib_dir = _fake_lib(tmp_path, [("PartA", "C25804", "R0603")],
                        [("R0603", "C25804")])
    monkeypatch.setattr(lib_pull, "_run_easyeda", _ok_run)
    assert lib_pull._pull_one("C2580", lib_dir / "aiee", True, False)["status"] == "error"
    # already in the library: not re-pulled (snag 15), and found by its exact id
    assert lib_pull._pull_one("C25804", lib_dir / "aiee", True, False)["status"] == "exists"


def test_pull_gate_missing_footprint_file_is_an_error(tmp_path, monkeypatch):
    lib_dir = _fake_lib(tmp_path, [("PartA", "C999", "GONE")], [])
    monkeypatch.setattr(lib_pull, "_run_easyeda", _ok_run)
    r = lib_pull._pull_one("C999", lib_dir / "aiee", True, False)
    assert r["status"] == "error" and r["symbol_verified"] is True
    assert r["footprint_verified"] is False and "no such file" in r["detail"]


def test_dedupe_symbols_keeps_first_and_is_stable(tmp_path):
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    sym = lib_dir / "aiee.kicad_sym"
    blocks = [_sym_block("A", "C1", "F1"), _sym_block("B", "C2", "F2"),
              _sym_block("A", "C1", "F1")]
    sym.write_text("(kicad_symbol_lib (version 20211014) (generator test)\n"
                   + "\n".join(blocks) + "\n)\n", encoding="utf-8")
    rep = lib_pull._dedupe_symbols(sym)
    assert rep["removed"] == 1 and rep["names"] == ["A"]
    assert fplib.symbol_names(sym) == ["A", "B"]
    assert lib_pull._dedupe_symbols(sym)["removed"] == 0     # idempotent


# ------------------------------------------- batch mode + pacing (T6 batch C)

def test_auto_pace_thresholds():
    assert lib_pull._auto_pace(10) == 0.0
    assert lib_pull._auto_pace(11) == 15.0


def test_pull_all_paces_between_parts(monkeypatch):
    sleeps = []
    monkeypatch.setattr(lib_pull.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(lib_pull, "_pull_one",
                        lambda l, b, n, o, **k: {"lcsc": l, "status": "pulled"})
    res, retried = lib_pull._pull_all(["C1", "C2", "C3"], Path("x"), True, False,
                                       lib_pull._Pacer(15.0))
    assert sleeps == [15.0, 15.0] and retried == 0 and len(res) == 3


def _batch_fake_run(tmp_path, fail_first_for=(), always_fail=()):
    """_run_easyeda stand-in that maintains a growing on-disk lib."""
    pulled: set[str] = set()
    calls: dict[str, int] = {}

    def fake(lcsc, base, no_3d, overwrite, **k):
        calls[lcsc] = calls.get(lcsc, 0) + 1
        if lcsc in always_fail or (lcsc in fail_first_for and calls[lcsc] == 1):
            return SimpleNamespace(stdout="", stderr="HTTP Error 403: Forbidden",
                                   returncode=1)
        pulled.add(lcsc)
        _write_sym_lib(base.with_suffix(".kicad_sym"),
                       [(f"P{l}", l, f"FP{l}") for l in sorted(pulled)])
        pretty = Path(str(base) + ".pretty")
        pretty.mkdir(parents=True, exist_ok=True)
        (pretty / f"FP{lcsc}.kicad_mod").write_text(
            _MOD_TEXT.format(name=f"FP{lcsc}", lcsc=lcsc), encoding="utf-8")
        return _ok_run()

    return fake, calls


def test_lib_pull_parts_batch_retries_403_and_passes(tmp_path, monkeypatch):
    fake, calls = _batch_fake_run(tmp_path, fail_first_for={"C2"})
    sleeps = []
    monkeypatch.setattr(lib_pull, "_run_easyeda", fake)
    monkeypatch.setattr(lib_pull.time, "sleep", lambda s: sleeps.append(s))
    parts = tmp_path / "parts.json"
    parts.write_text(json.dumps(
        {"parts": [{"lcsc": "C1"}, {"lcsc": "C2"}, {"lcsc": "C3"}]}),
        encoding="utf-8")
    out = tmp_path / "r.json"
    code = lib_pull.main(["--parts", str(parts), "--out-dir", str(tmp_path / "lib"),
                          "--no-autofix", "--no-refdes-norm", "--out", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0 and payload["status"] == "pass", payload
    assert [r["status"] for r in payload["results"]] == ["pulled"] * 3
    assert payload["results"][1]["retried"] is True
    assert payload["retried"] == 1 and 90.0 in sleeps
    assert payload["paced_s"] == 0.0        # auto: 3 parts <= 10
    assert calls["C2"] == 2                 # one retry, not more


def test_lib_pull_parts_batch_reports_hard_failure(tmp_path, monkeypatch):
    fake, calls = _batch_fake_run(tmp_path, always_fail={"C2"})
    monkeypatch.setattr(lib_pull, "_run_easyeda", fake)
    monkeypatch.setattr(lib_pull.time, "sleep", lambda s: None)
    parts = tmp_path / "parts.json"
    parts.write_text(json.dumps({"parts": [{"lcsc": "C1"}, {"lcsc": "C2"}]}),
                     encoding="utf-8")
    out = tmp_path / "r.json"
    code = lib_pull.main(["--parts", str(parts), "--out-dir", str(tmp_path / "lib"),
                          "--no-autofix", "--no-refdes-norm", "--out", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert code == 1 and payload["status"] == "fail"
    st = {r["lcsc"]: r["status"] for r in payload["results"]}
    assert st == {"C1": "pulled", "C2": "error"}


class _Clock:
    """time.monotonic/sleep stand-ins: each easyeda call costs `pull_s`."""

    def __init__(self, pull_s: float):
        self.t, self.pull_s = 0.0, pull_s

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_lib_pull_keeps_to_its_budget_and_resumes(tmp_path, monkeypatch):
    """21 parts at the 15 s pace overran a 10-minute call (lite-main snag 5):
    the batch stops starting pulls at its budget, says what is left, and a
    re-run skips what is already in the library."""
    fake, calls = _batch_fake_run(tmp_path)
    clock = _Clock(pull_s=10.0)

    def timed(*a, **k):
        clock.t += clock.pull_s
        return fake(*a, **k)

    monkeypatch.setattr(lib_pull, "_run_easyeda", timed)
    monkeypatch.setattr(lib_pull.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(lib_pull.time, "sleep", clock.sleep)
    ids = [f"C{i}" for i in range(1, 21)]
    parts = tmp_path / "parts.json"
    parts.write_text(json.dumps({"parts": [{"lcsc": l} for l in ids]}), encoding="utf-8")
    argv = ["--parts", str(parts), "--out-dir", str(tmp_path / "lib"), "--no-3d",
            "--no-autofix", "--no-refdes-norm", "--budget-s", "200",
            "--out", str(tmp_path / "r.json")]
    code = lib_pull.main(argv)
    p1 = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    # 15 s pace + 10 s pull: 8 parts fit in 200 s (the 8th ends at 185 s)
    assert code == 1 and p1["status"] == "partial"
    assert [r["lcsc"] for r in p1["results"] if r["status"] == "pulled"] == ids[:8]
    assert p1["deferred"] == ids[8:] and "re-run" in p1["resume"]
    assert clock.t <= 200

    clock.t = 0.0
    lib_pull.main(argv)
    p2 = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    st = {r["lcsc"]: r["status"] for r in p2["results"]}
    assert all(st[l] == "exists" for l in ids[:8])          # no network for these
    assert all(calls[l] == 1 for l in ids[:8])
    assert [l for l in ids if st[l] == "pulled"] == ids[8:16]

    clock.t = 0.0
    assert lib_pull.main(argv[:-4] + ["--budget-s", "0", "--out", str(tmp_path / "r.json")]) == 0
    p3 = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert p3["status"] == "pass" and p3["deferred"] == [] and p3["resume"] is None


def test_lib_pull_requires_exactly_one_of_lcsc_or_parts(tmp_path):
    with pytest.raises(SystemExit) as e:
        lib_pull.main(["--out-dir", str(tmp_path / "lib")])
    assert e.value.code == 2


# ==================================== datasheet URL + PDF guard (T6 batch C)

_WWW_URL = ("https://www.lcsc.com/datasheet/lcsc_datasheet_2304140030_"
            "STMicroelectronics-LM2901DT_C142961.pdf")
_WMSC_URL = ("https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/lcsc/2304140030_"
             "STMicroelectronics-LM2901DT_C142961.pdf")


def test_partslib_rewrites_www_datasheet_urls():
    assert partslib.fix_datasheet_url(_WWW_URL) == _WMSC_URL
    assert partslib.fix_datasheet_url(_WMSC_URL) == _WMSC_URL      # idempotent
    assert partslib.fix_datasheet_url("https://x.com/a.pdf") == "https://x.com/a.pdf"
    assert partslib.fix_datasheet_url("") == ""
    # applied inside normalize so no agent ever sees the unfetchable form
    row = partslib.normalize({"lcsc": "C142961", "datasheet": _WWW_URL})
    assert row["datasheet"] == _WMSC_URL


def test_datasheet_pdf_rejects_html_shell(tmp_path, capsys):
    fake = tmp_path / "shell.pdf"
    fake.write_text("<!doctype html><html>viewer shell</html>", encoding="utf-8")
    code, payload = run_main(datasheet_extract, ["--pdf", str(fake)], tmp_path, capsys)
    assert code == 2 and payload["status"] == "error"
    assert "wmsc" in payload["error"] and "not a PDF" in payload["error"]


def test_datasheet_pdf_trims_irrelevant_pages(tmp_path, capsys):
    pdf = tmp_path / "d.pdf"
    make_pdf_pages(pdf, ["Pin Functions VDD VSS pinout table",
                         "Electrical characteristics typical values at 25C"])
    code, payload = run_main(datasheet_extract, ["--pdf", str(pdf)], tmp_path, capsys)
    assert code == 0 and payload["n_pages"] == 2
    p0, p1 = payload["text_by_page"]
    assert "Pin Functions" in p0["text"]
    assert "text" not in p1 and p1["chars"] > 0
    assert "Electrical" in p1["first_line"]
    assert payload["pages_trimmed"] == 1
    assert "schema" in payload and payload["template"]["land_pattern"]["pad_count"] == 0
    code2, full = run_main(datasheet_extract, ["--pdf", str(pdf), "--full-text"],
                           tmp_path, capsys, name="full")
    assert code2 == 0 and full["pages_trimmed"] == 0
    assert "Electrical" in full["text_by_page"][1]["text"]


# ============================== parts_search hardening (T6 batch C + T6-P1-3)

def test_normalize_value_query_forms():
    f = parts_search._normalize_value_query
    assert f("10K 0603") == "10 kohm 0603"
    assert f("4R7") == "4.7 ohm"
    assert f("2.2K") == "2.2 kohm"
    assert f("1M") == "1 Mohm"
    assert f("4K7 0402") == "4.7 kohm 0402"
    assert f("STM32F103C8T6") is None
    assert f("100nF 0603 X7R") is None


def test_parts_search_value_token_retry(tmp_path, capsys, monkeypatch):
    calls = []

    def fake_live(q, **kw):
        calls.append(q)
        return (list(_CANNED), 3) if "kohm" in q else ([], 0)

    monkeypatch.setattr(partslib, "live_search", fake_live)
    code, payload = run_main(parts_search, ["--query", "10K 0603"], tmp_path, capsys)
    assert code == 0 and payload["status"] == "pass"
    assert payload["query_retried"] == "10 kohm 0603"
    assert calls == ["10K 0603", "10 kohm 0603"]
    assert payload["count"] == 3


def test_parts_search_empty_carries_hint(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(partslib, "live_search", lambda *a, **k: ([], 0))
    monkeypatch.setattr(partslib, "endpoint_reachable", lambda *a, **k: True)
    code, payload = run_main(parts_search, ["--query", "zzz"], tmp_path, capsys)
    assert code == 0 and payload["status"] == "empty"
    assert "stock-out" in payload["hint"]
    assert "query_retried" not in payload      # no value token in "zzz"


_PLACEHOLDER_ROWS = [
    {"lcsc": "C90001", "model": "", "brand": "JLCPCB Assembly", "type": "Basic",
     "stock": 5000, "price": 0.04, "package": "0603",
     "description": "resistor", "datasheet": ""},
    {"lcsc": "C90002", "model": "LM5069", "brand": "Tokmas", "type": "Extended",
     "stock": 100, "price": 1.0, "package": "SOIC-10",
     "description": "hot swap controller", "datasheet": ""},
]


def test_parts_search_filters_placeholder_rows(tmp_path, capsys, monkeypatch):
    """Placeholder = (no/JLCPCB brand) AND (no datasheet); a real-brand row
    with a missing datasheet URL survives (S14 finding, T6-P1-3)."""
    monkeypatch.setattr(partslib, "live_search",
                        lambda *a, **k: (list(_PLACEHOLDER_ROWS), 2))
    code, payload = run_main(parts_search, ["--query", "x"], tmp_path, capsys)
    assert code == 0
    assert [r["lcsc"] for r in payload["results"]] == ["C90002"]
    assert payload["placeholders_filtered"] == 1
    code2, restored = run_main(
        parts_search, ["--query", "x", "--include-placeholders"],
        tmp_path, capsys, name="restored")
    assert code2 == 0 and restored["placeholders_filtered"] == 0
    assert {r["lcsc"] for r in restored["results"]} == {"C90001", "C90002"}


_PASSIVE_ROWS = [
    {"lcsc": "CA", "model": "R1", "type": "Basic", "stock": 8000, "price": 0.002,
     "package": "0603", "brand": "UNI", "description": "10 kohm 1%",
     "datasheet": "http://d/a.pdf"},
    {"lcsc": "CB", "model": "R2", "type": "Basic", "stock": 400, "price": 0.001,
     "package": "0603", "brand": "UNI", "description": "10 kohm 1%",
     "datasheet": "http://d/b.pdf"},
    {"lcsc": "CC", "model": "R3", "type": "Extended", "stock": 90000,
     "price": 0.0005, "package": "0603", "brand": "UNI",
     "description": "10 kohm 1%", "datasheet": "http://d/c.pdf"},
    {"lcsc": "CD", "model": "R4", "type": "Basic", "stock": 90000,
     "price": 0.0008, "package": "1206", "brand": "UNI",
     "description": "10 kohm 1%", "datasheet": "http://d/d.pdf"},
    {"lcsc": "CE", "model": "R5", "type": "Basic", "stock": 90000, "price": 0.003,
     "package": "0805", "brand": "UNI", "description": "10 kohm 1%",
     "datasheet": "http://d/e.pdf"},
]


def test_parts_search_pick_basic_passive(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(partslib, "live_search",
                        lambda *a, **k: (list(_PASSIVE_ROWS), 5))
    code, payload = run_main(
        parts_search, ["--query", "10 kohm 0603", "--pick", "basic-passive",
                       "--qty", "100"], tmp_path, capsys)
    # CB fails stock (400 < 500), CC is Extended, CD nonstandard package ->
    # cheapest conforming is CA (0.002) over CE (0.003)
    assert code == 0 and payload["status"] == "pass"
    assert payload["pick"] == "basic-passive" and payload["count"] == 1
    assert payload["results"][0]["lcsc"] == "CA"


def test_parts_search_pick_no_candidate_is_distinct(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(partslib, "live_search",
                        lambda *a, **k: (list(_PASSIVE_ROWS), 5))
    code, payload = run_main(
        parts_search, ["--query", "10 kohm 0603", "--pick", "basic-passive",
                       "--qty", "100000"], tmp_path, capsys)
    assert code == 0                       # a completed query, not an error
    assert payload["status"] == "no_candidate"
    assert payload["results"] == [] and "stock-out" in payload["hint"]



def test_parts_search_pick_keeps_the_asked_value(tmp_path, capsys, monkeypatch):
    # issue #384: JLC's fuzzy search for 4.7 ohm returns cheaper 47 ohm rows
    rows = [
        {"lcsc": "C47", "model": "R47", "type": "Basic", "stock": 90000,
         "price": 0.0005, "package": "0603", "brand": "UNI",
         "description": "47\u03a9 \u00b11% 100mW 0603",
         "datasheet": "http://d/47.pdf",
         "attributes": [{"name": "Resistance", "value": "47\u03a9"}]},
        {"lcsc": "C4K7", "model": "R4K7", "type": "Basic", "stock": 90000,
         "price": 0.0006, "package": "0603", "brand": "UNI",
         "description": "4.7k\u03a9 \u00b11% 100mW 0603",
         "datasheet": "http://d/4k7.pdf"},
        {"lcsc": "C4R7", "model": "R4R7", "type": "Basic", "stock": 90000,
         "price": 0.0009, "package": "0603", "brand": "UNI",
         "description": "4.7\u03a9 \u00b11% 100mW 0603",
         "datasheet": "http://d/4r7.pdf",
         "attributes": [{"name": "Resistance", "value": "4.7\u03a9"}]},
    ]
    monkeypatch.setattr(partslib, "live_search",
                        lambda *a, **k: (list(rows), 3))
    for q, want in (("4.7 ohm 0603", "C4R7"), ("4R7 0603", "C4R7"),
                    ("4.7k 0603", "C4K7"), ("47 ohm", "C47")):
        code, payload = run_main(
            parts_search, ["--query", q, "--pick", "basic-passive"],
            tmp_path, capsys, name=want + q.replace(" ", "_"))
        assert code == 0 and payload["status"] == "pass", q
        assert payload["results"][0]["lcsc"] == want, q
    code, payload = run_main(
        parts_search, ["--query", "2.2 ohm 0603", "--pick", "basic-passive"],
        tmp_path, capsys, name="none")
    assert payload["status"] == "no_candidate"


# ================================= fp_verify geometry (T6 batch C, rows 111/115)

def _mk_fp(tmp_path, pads: list[str], name: str = "T") -> Path:
    text = f"(module {name} (layer F.Cu)\n" + "\n".join(pads) + "\n)\n"
    p = tmp_path / f"{name}.kicad_mod"
    p.write_text(text, encoding="utf-8")
    return p


def test_fplib_parses_drill(tmp_path):
    fp = _mk_fp(tmp_path, [
        '  (pad 1 thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.05) '
        '(layers *.Cu *.Mask))',
        '  (pad 2 thru_hole oval (at 3 0) (size 1.8 2.4) (drill oval 0.9 1.2) '
        '(layers *.Cu *.Mask))',
        '  (pad 3 smd rect (at 6 0) (size 1 1) (layers F.Cu F.Paste F.Mask))',
    ])
    pads = {p.number: p for p in fplib.parse_footprint(fp).pads}
    assert pads["1"].drill == pytest.approx(1.05)
    assert pads["2"].drill == pytest.approx(1.2)     # largest oval dimension
    assert pads["3"].drill is None


def test_fp_verify_min_max_pad_size_catches_tied_asymmetry(tmp_path, capsys):
    """SSOP-20 class miss: a 50/50 size tie let the mode hide the 2.0 column."""
    pads = [f'  (pad {i + 1} smd rect (at {i * 0.65:.2f} 0) (size 0.4 1.8) '
            '(layers F.Cu F.Paste F.Mask))' for i in range(2)]
    pads += [f'  (pad {i + 3} smd rect (at {i * 0.65:.2f} 3) (size 0.4 2.0) '
             '(layers F.Cu F.Paste F.Mask))' for i in range(2)]
    fp = _mk_fp(tmp_path, pads)
    ds = write_ds(tmp_path, {"land_pattern": {"pad_count": 4,
                                              "pad_size_mm": [0.4, 1.8]}})
    code, payload = run_main(
        fp_verify, ["--footprint", str(fp), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    assert code == 0                                  # still warning severity
    sz = [v for v in payload["violations"] if v["kind"] == "pad_size"]
    assert len(sz) == 1 and sz[0]["severity"] == "warning"
    assert [0.4, 2.0] in sz[0]["measured_mm"]
    assert [0.4, 1.8] not in sz[0]["measured_mm"]     # the in-tol size


def test_fp_verify_tht_annulus_floor_error(tmp_path, capsys):
    fp = _mk_fp(tmp_path, [
        '  (pad 1 thru_hole circle (at 0 0) (size 0.8 0.8) (drill 0.6) '
        '(layers *.Cu *.Mask))',
        # an NPTH peg (mechanical) must NOT trip the copper annulus check
        '  (pad "" np_thru_hole circle (at 3 0) (size 0.65 0.65) (drill 0.65) '
        '(layers *.Cu *.Mask))',
    ])
    ds = write_ds(tmp_path, {"land_pattern": {"pad_count": 1}})
    code, payload = run_main(
        fp_verify, ["--footprint", str(fp), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    assert code == 1 and payload["status"] == "violations"
    ann = [v for v in payload["violations"] if v["kind"] == "annulus_floor"]
    assert len(ann) == 1 and ann[0]["severity"] == "error"
    assert ann[0]["measured_mm"] == pytest.approx(0.1)


def test_fp_verify_declared_drill_and_annulus(tmp_path, capsys):
    pads = ['  (pad 1 thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.05) '
            '(layers *.Cu *.Mask))']
    fp = _mk_fp(tmp_path, pads)
    ok = write_ds(tmp_path, {"land_pattern": {"pad_count": 1, "drill_mm": 1.1}})
    code, payload = run_main(
        fp_verify, ["--footprint", str(fp), "--datasheet-json", str(ok),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    # 0.05 within tol: no errors (a no_courtyard WARNING is fixture noise)
    assert code == 0
    assert [v for v in payload["violations"] if v["severity"] == "error"] == []

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"land_pattern": {"pad_count": 1, "drill_mm": 1.3,
                                                "annulus_mm": 0.9}}),
                   encoding="utf-8")
    code2, payload2 = run_main(
        fp_verify, ["--footprint", str(fp), "--datasheet-json", str(bad),
                    "--svg", str(tmp_path / "o2.svg")], tmp_path, capsys,
        name="bad")
    kinds = {v["kind"] for v in payload2["violations"]
             if v["severity"] == "error"}
    assert code2 == 1 and kinds == {"drill_mismatch", "annulus_mismatch"}


def test_datasheet_schema_accepts_drill_and_annulus(tmp_path, capsys):
    ds = dict(CAP_DS)
    ds["land_pattern"] = dict(CAP_DS["land_pattern"], drill_mm=1.1,
                              annulus_mm=0.325)
    p = write_ds(tmp_path, ds)
    code, payload = run_main(datasheet_extract, ["--validate", str(p)],
                             tmp_path, capsys)
    assert code == 0 and payload["status"] == "pass"


# ======================================= lib_pin_types (T6 batch C, row 60)

def _pin(num: str, name: str, etype: str = "unspecified") -> str:
    return (f'      (pin {etype} line (at 0 {num} 0) (length 2.54)\n'
            f'        (name "{name}" (effects (font (size 1.27 1.27))))\n'
            f'        (number "{num}" (effects (font (size 1.27 1.27)))))')


def _retype_lib(tmp_path: Path) -> tuple[Path, Path]:
    lib = tmp_path / "aiee.kicad_sym"
    reg_pins = "\n".join([_pin("1", "GND"), _pin("2", "VOUT"),
                          _pin("3", "VIN"), _pin("4", "VOUT")])
    res_pins = "\n".join([_pin("1", "R1", "input"), _pin("2", "R2", "input")])
    lib.write_text(
        '(kicad_symbol_lib (version 20211014) (generator test)\n'
        '  (symbol "REG" (property "Reference" "U" (at 0 0 0))\n'
        '    (property "Footprint" "aiee:SOT-223" (at 0 0 0))\n'
        '    (property "LCSC Part" "C6186" (at 0 0 0))\n'
        '    (symbol "REG_1_1"\n' + reg_pins + '))\n'
        '  (symbol "RES" (property "Reference" "R" (at 0 0 0))\n'
        '    (property "LCSC Part" "C999" (at 0 0 0))\n'
        '    (symbol "RES_1_1"\n' + res_pins + ')))\n',
        encoding="utf-8")
    ds = tmp_path / "C6186.json"
    ds.write_text(json.dumps({
        "mpn": "AMS1117-3.3", "lcsc": "C6186",
        "pinout": [{"pin": "1", "name": "GND", "type": "ground"},
                   {"pin": "2", "name": "VOUT", "type": "power_out"},
                   {"pin": "3", "name": "VIN", "type": "power_in"},
                   {"pin": "4", "name": "VOUT", "type": "power_out"}],
    }), encoding="utf-8")
    return lib, ds


def test_lib_pin_types_retypes_from_datasheet(tmp_path, capsys):
    import re as _re
    lib, ds = _retype_lib(tmp_path)
    code, payload = run_main(
        lib_pin_types, ["--lib", str(lib), "--datasheet-json", str(ds)],
        tmp_path, capsys)
    assert code == 0 and payload["status"] == "pass"
    assert payload["extracts_matched"] == ["C6186"]
    types = _re.findall(r"\(pin (\w+) line", lib.read_text(encoding="utf-8"))
    # REG: GND->power_in, VOUT->power_out, VIN->power_in, tab VOUT dup->passive
    # RES (no extract): blanket passive kills the unspecified/input junk
    assert types == ["power_in", "power_out", "power_in", "passive",
                     "passive", "passive"]
    # idempotent second run
    code2, again = run_main(
        lib_pin_types, ["--lib", str(lib), "--datasheet-json", str(ds)],
        tmp_path, capsys, name="again")
    assert code2 == 0 and again["changed"] == 0


def test_lib_pin_types_dry_run_and_stale_extract(tmp_path, capsys):
    lib, ds = _retype_lib(tmp_path)
    stale = tmp_path / "C777.json"
    stale.write_text(json.dumps({"mpn": "GONE", "lcsc": "C777", "pinout": []}),
                     encoding="utf-8")
    before = lib.read_text(encoding="utf-8")
    code, payload = run_main(
        lib_pin_types, ["--lib", str(lib), "--datasheet-json", str(ds),
                        str(stale), "--dry-run"], tmp_path, capsys)
    assert code == 0 and payload["changed"] > 0
    assert payload["extracts_unmatched"] == ["C777"]   # removed-part JSON: not fatal
    assert lib.read_text(encoding="utf-8") == before   # dry-run wrote nothing
    assert lib_pin_types.main(["--lib", str(tmp_path / "nope.kicad_sym")]) == 2


# ============================================================ live (net) tests

def _skip_if_offline():
    if not partslib.endpoint_reachable(timeout=8.0):
        pytest.skip("JLCPCB endpoint unreachable (offline)")


@pytest.mark.net
@pytest.mark.parametrize("query", [
    "STM32F103C8T6",      # MCU
    "AP63203",            # buck regulator
    "USB Micro B connector",  # USB connector
    "8MHz crystal SMD",   # crystal
    "10k 0603 1%",        # 0603 resistor
])
def test_net_search_returns_in_stock_hits(query, tmp_path):
    _skip_if_offline()
    out = tmp_path / "pn.json"
    code = parts_search.main(["--query", query, "--min-stock", "1", "--limit", "5",
                              "--out", str(out)])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0, data
    assert data["count"] >= 1, f"no hits for {query!r}"
    assert all(r["stock"] >= 1 for r in data["results"])
    assert all(r["lcsc"].startswith("C") for r in data["results"])


@pytest.mark.net
def test_net_lib_pull_loads_in_kicad(tmp_path):
    _skip_if_offline()
    out = tmp_path / "r.json"
    code = lib_pull.main(["--lcsc", "C1525", "--out-dir", str(tmp_path / "lib"),
                          "--verify-load", "--out", str(out)])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0, data
    assert data["results"][0]["status"] in ("pulled", "exists")
    assert data["load_check"]["ok"] is True, data["load_check"]
    # the pulled footprint is parseable and has copper pads
    fp_file = data["results"][0]["footprints"][0]["file"]
    assert len(fplib.parse_footprint(fp_file).copper_pads) >= 2


def _two_rows(tmp_path, half_span: float, per_row: int = 4,
              pitch: float = 1.27) -> Path:
    """A SOIC-like footprint: two pad columns at x = +-half_span."""
    pads = []
    for i in range(per_row):
        y = (i - (per_row - 1) / 2) * pitch
        pads.append(f'  (pad {i + 1} smd rect (at {-half_span} {y:.3f}) '
                    '(size 1.5 0.6) (layers F.Cu F.Paste F.Mask))')
        pads.append(f'  (pad {2 * per_row - i} smd rect (at {half_span} '
                    f'{y:.3f}) (size 1.5 0.6) (layers F.Cu F.Paste F.Mask))')
    return _mk_fp(tmp_path, pads, name=f"R{int(half_span * 1000)}")


def test_fp_verify_flags_a_wrong_row_spacing(tmp_path, capsys):
    """SOP-20 class miss: rows 6.00 mm apart against a 5.75 mm land passed
    pad count and pitch - row spacing was never compared."""
    ds = write_ds(tmp_path, {"land_pattern": {
        "pad_count": 8, "pitch_mm": 1.27, "row_spacing_mm": 5.75}})
    bad = _two_rows(tmp_path, 3.0)
    code, payload = run_main(
        fp_verify, ["--footprint", str(bad), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    rows = [v for v in payload["violations"] if v["kind"] == "row_spacing"]
    assert code == 1 and len(rows) == 1 and rows[0]["severity"] == "error"
    assert 6.0 in rows[0]["measured_mm"]
    assert "pad_pitch" not in {v["kind"] for v in payload["violations"]}

    good = _two_rows(tmp_path, 2.875)
    code, payload = run_main(
        fp_verify, ["--footprint", str(good), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o2.svg")], tmp_path, capsys,
        name="good")
    assert code == 0
    assert payload["measured_row_spacing_mm"] == [5.75]
    assert not [v for v in payload["violations"]
                if v["kind"].startswith("row_spacing")]


def test_fp_verify_row_spacing_on_a_quad_and_unmeasurable(tmp_path, capsys):
    """A QFP's rows run both ways: either axis matching passes. A SOT-23 has
    no row pair: the datasheet value is then a warning to check by hand,
    never a silent pass."""
    pads, n = [], 1
    for i in range(4):
        o = (i - 1.5) * 0.5
        for at in (f"-4.25 {o}", f"{o} 4.25", f"4.25 {-o}", f"{-o} -4.25"):
            pads.append(f'  (pad {n} smd rect (at {at}) (size 0.3 1.2) '
                        '(layers F.Cu F.Paste F.Mask))')
            n += 1
    quad = _mk_fp(tmp_path, pads, name="Q")
    ds = write_ds(tmp_path, {"land_pattern": {"pad_count": 16,
                                              "row_spacing_mm": 8.5}})
    code, payload = run_main(
        fp_verify, ["--footprint", str(quad), "--datasheet-json", str(ds),
                    "--svg", str(tmp_path / "o.svg")], tmp_path, capsys)
    assert payload["measured_row_spacing_mm"] == [8.5, 8.5]
    assert code == 0

    sot = _mk_fp(tmp_path, [
        '  (pad 1 smd rect (at -1 -0.95) (size 0.6 0.7) (layers F.Cu))',
        '  (pad 2 smd rect (at -1 0.95) (size 0.6 0.7) (layers F.Cu))',
        '  (pad 3 smd rect (at 1.2 0) (size 0.6 0.7) (layers F.Cu))'],
        name="S")
    ds2 = tmp_path / "sot.json"
    ds2.write_text(json.dumps({"land_pattern": {"pad_count": 3,
                                                "row_spacing_mm": 2.2}}),
                   encoding="utf-8")
    code, payload = run_main(
        fp_verify, ["--footprint", str(sot), "--datasheet-json", str(ds2),
                    "--svg", str(tmp_path / "o3.svg")], tmp_path, capsys,
        name="sot")
    un = [v for v in payload["violations"]
          if v["kind"] == "row_spacing_unmeasured"]
    assert code == 0 and len(un) == 1 and un[0]["severity"] == "warning"


SO8_PINS = {str(i): ((-2.5 if i <= 4 else 2.5),
                     (-1.905 + 1.27 * (i - 1) if i <= 4 else 1.905 - 1.27 * (i - 5)))
            for i in range(1, 9)}


def _so8_ds(tmp_path, rows=3.85):
    """The maker's land: rows `rows` apart, not the footprint's 5 mm."""
    k = rows / 5.0
    return write_ds(tmp_path, {"mpn": "SO8", "pinout": [
        {"pin": str(i), "name": f"P{i}", "type": "passive"} for i in range(1, 9)],
        "land_pattern": {"pad_count": 8, "pin1": "1",
                         "pads_source": "p.33 example board layout",
                         "pads": [{"number": n, "at_mm": [x * k, y]}
                                  for n, (x, y) in SO8_PINS.items()]}})


def test_fp_verify_pads_judged_by_nearest_pin_not_tolerance(tmp_path, capsys):
    ds = _so8_ds(tmp_path)
    right = _numbered_fp(tmp_path, "SO8", [(n, x, y, 1.5, 0.6)
                                           for n, (x, y) in SO8_PINS.items()])
    code, payload = _fpv(tmp_path, capsys, right, ds)
    assert code == 0, payload["violations"]
    assert payload["pad_numbering"]["fit"] is True
    assert payload["coverage"]["pad_numbering"] == "judged"
    mirror = _numbered_fp(tmp_path, "SO8", [(n, -x, y, 1.5, 0.6)
                                            for n, (x, y) in SO8_PINS.items()])
    code, payload = _fpv(tmp_path, capsys, mirror, ds)
    assert code == 1
    v, = [v for v in payload["violations"] if v["kind"] == "pad_numbering"]
    assert v["hint"] == "bottom_view_numbering"
    assert v["drawing"] == "p.33 example board layout"


def test_fp_verify_pads_two_pad_land_and_other_shape_not_judged(tmp_path, capsys):
    ds = write_ds(tmp_path, {"mpn": "D", "pinout": [], "land_pattern": {
        "pad_count": 2, "pads": [{"number": "1", "at_mm": [-1.6, 0]},
                                 {"number": "2", "at_mm": [1.6, 0]}]}})
    fp = _numbered_fp(tmp_path, "D", [("1", 1.6, 0, 1, 1), ("2", -1.6, 0, 1, 1)])
    code, payload = _fpv(tmp_path, capsys, fp, ds)
    assert code == 0 and payload["pad_numbering"] is None
    assert "mirror" in [n for n in payload["not_judged"]
                        if n["check"] == "pad_numbering"][0]["why"]
    # three pads in a line on the drawing, a triangle on the footprint
    ds = write_ds(tmp_path, {"mpn": "Q", "pinout": [], "land_pattern": {
        "pad_count": 3, "pads": [{"number": str(i), "at_mm": [i - 2.0, 0]}
                                 for i in (1, 2, 3)]}})
    fp = _numbered_fp(tmp_path, "Q", [("1", -0.95, 1, 1, 1), ("2", 0.95, 1, 1, 1),
                                      ("3", 0, -1, 1, 1)])
    code, payload = _fpv(tmp_path, capsys, fp, ds)
    assert code == 0 and payload["coverage"]["pad_numbering"] == "not_judged"


def test_fp_verify_pairs_coverage_counts(tmp_path, capsys):
    ds = _so8_ds(tmp_path)
    fp = _numbered_fp(tmp_path, "SO8", [(n, x, y, 1.5, 0.6)
                                        for n, (x, y) in SO8_PINS.items()])
    (tmp_path / "b").mkdir()
    bare = write_ds(tmp_path / "b", {"mpn": "B", "pinout": [],
                                     "land_pattern": {"pad_count": 8, "pin1": "1"}})
    pairs = tmp_path / "pairs.tsv"
    pairs.write_text(f"{fp}\t{ds}\n{fp}\t{bare}\n", encoding="utf-8")
    code, payload = run_main(fp_verify, ["--pairs", str(pairs)], tmp_path, capsys)
    assert code == 0
    c = payload["coverage"]["pad_numbering"]
    assert (c["judged"], c["not_judged"], payload["pairs"]) == (1, 1, 2)
    assert payload["errors"] == []
    # a one-step shift on a judged pair is listed with its kind and message
    shifted = _numbered_fp(tmp_path / "b", "SO8s", [
        (str(int(n) % 8 + 1), x, y, 1.5, 0.6) for n, (x, y) in SO8_PINS.items()])
    pairs.write_text(f"{shifted}\t{ds}\n", encoding="utf-8")
    code, payload = run_main(fp_verify, ["--pairs", str(pairs)], tmp_path, capsys)
    assert [e["kind"] for e in payload["errors"]] == ["pad_numbering"]
    assert code == 1 and payload["errors"][0]["message"]


# #1331: pad size axis by axis, and a two-terminal land's gap. The sorted
# pad_size_mm check passed a pad turned a quarter, and row spacing (centre
# to centre) never saw the gap that turn moves under the body.
def _chip_ds(tmp_path, **land):
    return write_ds(tmp_path, {"mpn": "CHIP", "pinout": [], "land_pattern": {
        "pad_count": 2, "pin1": "1", **land}})


def _chip_fp(tmp_path, along, across, half, name="CHIP"):
    """Two pads on the x axis at +-half, `along` x `across` mm."""
    return _numbered_fp(tmp_path, name, [("1", -half, 0, along, across),
                                         ("2", half, 0, along, across)])


def _kinds(payload):
    return sorted(v["kind"] for v in payload["violations"])


def test_fp_verify_turned_chip_pad_fails_on_gap_and_axes(tmp_path, capsys):
    ds = _chip_ds(tmp_path, pad_length_mm=0.3, pad_width_mm=0.5, gap_mm=0.3,
                  pad_size_mm=[0.3, 0.5])
    code, payload = _fpv(tmp_path, capsys, _chip_fp(tmp_path, 0.3, 0.5, 0.3), ds)
    assert code == 0 and _kinds(payload) == ["no_courtyard"], payload["violations"]
    assert payload["two_terminal_land"]["gap_mm"] == pytest.approx(0.3)
    assert payload["coverage"]["pad_size"] == payload["coverage"]["land_gap"] == "judged"
    # the same pads turned a quarter: the sorted size still matches, the
    # centres don't move, and the gap shrinks to 0.1 mm
    code, payload = _fpv(tmp_path, capsys, _chip_fp(tmp_path, 0.5, 0.3, 0.3), ds)
    assert code == 1 and _kinds(payload) == ["land_gap", "no_courtyard", "pad_turned"]
    gap, = [v for v in payload["violations"] if v["kind"] == "land_gap"]
    assert gap["measured_mm"] == pytest.approx(0.1)
    assert gap["expected_mm"] == pytest.approx([0.2, 0.4])
    assert "bridge" in gap["msg"]
    turned, = [v for v in payload["violations"] if v["kind"] == "pad_turned"]
    assert turned["severity"] == "error" and turned["pads"] == ["1", "2"]


def test_fp_verify_stock_0201_misses_murata_land_ranges(tmp_path, capsys):
    """KiCad's C_0201_0603Metric (0.46 x 0.40 pads at +-0.32, a 0.18 mm gap)
    against Murata GRM033's land, given as ranges."""
    ds = _chip_ds(tmp_path, gap_mm=[0.2, 0.25], pad_length_mm=[0.2, 0.3],
                  pad_width_mm=[0.25, 0.35])
    code, payload = _fpv(tmp_path, capsys, _chip_fp(tmp_path, 0.46, 0.40, 0.32), ds)
    assert code == 1 and _kinds(payload) == ["land_gap", "no_courtyard", "pad_size"]
    gap, = [v for v in payload["violations"] if v["kind"] == "land_gap"]
    assert gap["measured_mm"] == pytest.approx(0.18)
    assert gap["expected_mm"] == [0.2, 0.25] and "0.2..0.25" in gap["msg"]
    size, = [v for v in payload["violations"] if v["kind"] == "pad_size"]
    assert size["severity"] == "warning" and size["pads"] == ["1", "2"]
    assert size["expected_mm"] == {"length": [0.2, 0.3], "width": [0.25, 0.35]}
    # the middle of each range passes, and so does the land turned upright
    code, payload = _fpv(tmp_path, capsys, _chip_fp(tmp_path, 0.25, 0.3, 0.2375), ds)
    assert code == 0 and _kinds(payload) == ["no_courtyard"], payload["violations"]
    upright = _numbered_fp(tmp_path, "UP", [("1", 0, -0.2375, 0.3, 0.25),
                                            ("2", 0, 0.2375, 0.3, 0.25)])
    code, payload = _fpv(tmp_path, capsys, upright, ds)
    assert code == 0 and _kinds(payload) == ["no_courtyard"], payload["violations"]
    t = payload["two_terminal_land"]
    assert t["gap_mm"] == pytest.approx(0.225)
    assert [(p["along_mm"], p["across_mm"]) for p in t["pads"]] == [(0.25, 0.3)] * 2


def _so8_sized_ds(tmp_path):
    return write_ds(tmp_path, {"mpn": "SO8", "pinout": [], "land_pattern": {
        "pad_count": 8, "pin1": "1", "pad_size_mm": [1.5, 0.6],
        "pads_source": "p.33 example board layout",
        "pads": [{"number": n, "at_mm": [x, y], "size_mm": [[1.4, 1.6], 0.6]}
                 for n, (x, y) in SO8_PINS.items()]}})


def test_fp_verify_per_pad_size_in_the_drawings_frame(tmp_path, capsys):
    ds = _so8_sized_ds(tmp_path)
    # the footprint turned a quarter against the drawing, pads and all
    turned_fp = _numbered_fp(tmp_path, "SO8", [(n, -y, x, 0.6, 1.5)
                                               for n, (x, y) in SO8_PINS.items()])
    code, payload = _fpv(tmp_path, capsys, turned_fp, ds)
    assert code == 0 and _kinds(payload) == ["no_courtyard"], payload["violations"]
    f = payload["pad_size_by_axis"]
    assert f["turn_deg"] in (90, 270) and len(f["pads"]) == 8
    assert payload["coverage"]["pad_size"] == "judged"
    # pad 3 alone turned within the footprint: sorted, its size still
    # matches pad_size_mm, so only the per-pad check sees it
    pads = [(n, x, y, 0.6 if n == "3" else 1.5, 1.5 if n == "3" else 0.6)
            for n, (x, y) in SO8_PINS.items()]
    code, payload = _fpv(tmp_path, capsys, _numbered_fp(tmp_path, "SO8", pads), ds)
    assert code == 1 and _kinds(payload) == ["no_courtyard", "pad_turned"]
    v, = [v for v in payload["violations"] if v["kind"] == "pad_turned"]
    assert v["pads"] == ["3"] and v["drawing"] == "p.33 example board layout"
    assert "1.4..1.6 x 0.6+-0.1" in v["msg"]
    # pad 5 too long: a warning, not a turn
    pads = [(n, x, y, 2.0 if n == "5" else 1.5, 0.6) for n, (x, y) in SO8_PINS.items()]
    code, payload = _fpv(tmp_path, capsys, _numbered_fp(tmp_path, "SO8", pads), ds)
    assert code == 0 and _kinds(payload) == ["no_courtyard", "pad_size"]
    v, = [v for v in payload["violations"] if v["kind"] == "pad_size"]
    assert v["pads"] == ["5"] and v["expected_mm"] == [[[1.4, 1.6], [0.5, 0.7]]]


def test_fp_verify_land_size_and_gap_not_judged(tmp_path, capsys):
    ds = _chip_ds(tmp_path, gap_mm=0.3, pad_length_mm=0.3, pad_width_mm=0.5)
    # three pads: no two-terminal land to measure
    fp = _numbered_fp(tmp_path, "Q", [("1", -0.95, 1, 0.6, 0.7), ("2", 0.95, 1, 0.6, 0.7),
                                      ("3", 0, -1, 0.6, 0.7)])
    code, payload = _fpv(tmp_path, capsys, fp, ds)
    assert payload["coverage"]["land_gap"] == payload["coverage"]["pad_size"] == "not_judged"
    assert "3 copper pads" in [n for n in payload["not_judged"]
                               if n["check"] == "land_gap"][0]["why"]
    assert "land_gap" not in _kinds(payload)
    # a custom pad has no one length
    fp = _mk_fp(tmp_path, [
        '  (pad 1 smd custom (at -0.3 0 0) (size 0.3 0.3) (layers F.Cu F.Mask)\n'
        '    (primitives (gr_poly (pts (xy -0.15 -0.25) (xy 0.15 -0.25) (xy 0.15 0.25)'
        ' (xy -0.15 0.25)) (width 0) (fill yes))))',
        '  (pad 2 smd rect (at 0.3 0) (size 0.3 0.5) (layers F.Cu F.Mask))'])
    code, payload = _fpv(tmp_path, capsys, fp, ds)
    assert payload["coverage"]["land_gap"] == "not_judged"
    assert payload["two_terminal_land"] is None
    # a pad at 45 degrees to the line through both
    fp = _mk_fp(tmp_path, [
        '  (pad 1 smd rect (at -0.3 0 45) (size 0.3 0.5) (layers F.Cu F.Mask))',
        '  (pad 2 smd rect (at 0.3 0) (size 0.3 0.5) (layers F.Cu F.Mask))'])
    code, payload = _fpv(tmp_path, capsys, fp, ds)
    assert "odd angle" in [n for n in payload["not_judged"]
                           if n["check"] == "pad_size"][0]["why"]
    # nothing stated: not_stated, not judged and not a pass
    code, payload = _fpv(tmp_path, capsys, _chip_fp(tmp_path, 0.3, 0.5, 0.3),
                         _chip_ds(tmp_path))
    assert payload["coverage"]["land_gap"] == payload["coverage"]["pad_size"] == "not_stated"
    assert payload["two_terminal_land"] is None and payload["pad_size_by_axis"] is None


def test_datasheet_schema_land_ranges(tmp_path, capsys):
    rec = {**CAP_DS, "land_pattern": {**CAP_DS["land_pattern"], "gap_mm": [0.2, 0.25],
                                      "pad_length_mm": 0.25, "pad_width_mm": [0.25, 0.35]}}
    code, payload = run_main(datasheet_extract, ["--validate", str(write_ds(tmp_path, rec))],
                             tmp_path, capsys)
    assert code == 0, payload
    rec["land_pattern"]["gap_mm"] = [0.2, 0.25, 0.3]
    code, payload = run_main(datasheet_extract, ["--validate", str(write_ds(tmp_path, rec))],
                             tmp_path, capsys)
    assert code == 1 and "gap_mm" in " ".join(e["msg"] + str(e.get("path", ""))
                                              for e in payload["errors"])
