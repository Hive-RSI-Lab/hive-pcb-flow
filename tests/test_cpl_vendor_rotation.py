"""#1068: the CPL rotation is judged by the chosen assembler's convention.

JLC places by its LCSC model (test_cpl_verify.py has all of that). The KiCad
shops (PCBWay, Aisler, Seeed, Eurocircuits) take KiCad's own position, side
and rotation, so their CPL is judged row by row against the board footprint;
Eurocircuits' IEC zero leaves multi-pin parts unjudged; a fab with no assembly
table (OSH Park) judges nothing. The board is a hand-written SOIC-8, diode and
QFN-16, the pos file is what kicad-cli writes for it, and the vendor CPLs come
from vendor.py's own writer: nothing needs KiCad, a model or a network.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import cpl_verify as cv  # noqa: E402
import dfm_check  # noqa: E402
import vendor as vendor_py  # noqa: E402
import vendors  # noqa: E402

SOIC8 = [(str(i + 1), -2.7, -1.905 + 1.27 * i) for i in range(4)] + \
        [(str(8 - i), 2.7, -1.905 + 1.27 * i) for i in range(4)]
QFN16 = ([(str(i + 1), -1.5, -0.75 + 0.5 * i) for i in range(4)]
         + [(str(i + 5), -0.75 + 0.5 * i, 1.5) for i in range(4)]
         + [(str(i + 9), 1.5, 0.75 - 0.5 * i) for i in range(4)]
         + [(str(i + 13), 0.75 - 0.5 * i, -1.5) for i in range(4)])
DIODE = [("1", -1.65, 0.0), ("2", 1.65, 0.0)]
RES = [("1", -0.8, 0.0), ("2", 0.8, 0.0)]

# ref, fpid, x, y, rot, pads, layer
PARTS = [
    ("U1", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", 10.0, 12.0, 90.0, SOIC8, "F.Cu"),
    ("D1", "Diode_SMD:D_SMA", 20.0, 12.0, 180.0, DIODE, "F.Cu"),
    ("U2", "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm", 30.0, 12.0, 0.0, QFN16, "F.Cu"),
    ("D2", "Diode_SMD:D_SMA", 20.0, 22.0, 270.0, DIODE, "B.Cu"),
    ("R1", "Resistor_SMD:R_0603_1608Metric", 30.0, 22.0, 0.0, RES, "F.Cu"),
]


def _fp(ref, fpid, x, y, r, pads, layer):
    if layer == "B.Cu":
        pads = [(n, px, -py) for n, px, py in pads]
    pad_txt = "".join(
        f'(pad "{n}" smd rect (at {px} {py} {r}) (size 0.6 0.4) '
        f'(layers "{layer}"))' for n, px, py in pads)
    return (f'(footprint "{fpid}" (layer "{layer}") (at {x} {y} {r}) '
            f'(property "Reference" "{ref}") {pad_txt})')


BOARD = "(kicad_pcb (version 20240108) " + " ".join(
    _fp(*p) for p in PARTS) + ")"


def _pos_rows():
    """kicad-cli pcb export pos --format csv --units mm: Rot is the board
    file's own rotation on either side, y up."""
    return [{"Ref": ref, "Val": "x", "Package": fpid.split(":")[1],
             "PosX": f"{x:.4f}", "PosY": f"{-y:.4f}", "Rot": f"{r:.4f}",
             "Side": "bottom" if layer == "B.Cu" else "top"}
            for ref, fpid, x, y, r, _p, layer in PARTS]


def _write_pos(path: Path, rows) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, ["Ref", "Val", "Package", "PosX", "PosY",
                                "Rot", "Side"])
        w.writeheader()
        w.writerows(rows)
    return path


@pytest.fixture
def board(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(BOARD, encoding="utf-8")
    pos = _write_pos(tmp_path / "b-pos.csv", _pos_rows())
    return pcb, pos


def _vendor_cpl(vid: str, pos: Path, out: Path, turn: dict | None = None) -> Path:
    """The CPL vendor.py package writes for vid, rows turned by `turn`."""
    asm = vendors.load(vid)["assembly"]
    warn: list[str] = []
    text = vendor_py.vendor_cpl(asm, pos, {p[0] for p in PARTS}, None, warn)
    if turn:
        rd = list(csv.reader(io.StringIO(text)))
        head = [vendor_py.meaning(h) for h in rd[0]]
        for row in rd[1:]:
            ref = row[head.index("designator")]
            for col, val in (turn.get(ref) or {}).items():
                i = head.index(col)
                row[i] = f"{float(row[i]) + val:.4f}" if col != "side" else val
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(rd)
        text = buf.getvalue()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def _dfm(pcb, cpl, vid):
    vios: list = []
    facts = dfm_check.check_placement(pcb, cpl, None, None, None, False, vios,
                                      conv=cv.convention(vid))
    return facts, vios


# ----------------------------------------------------------- conventions

def test_each_profile_names_its_convention():
    got = {v: cv.convention(v)["rotation"] for v in vendors.ids()}
    assert got["jlcpcb"] == "jlc"
    assert cv.convention(None)["rotation"] == "jlc"   # the default
    for v in ("pcbway", "aisler", "seeed", "eurocircuits"):
        assert got[v] == "kicad", v
    for v in ("oshpark", "advancedpcb"):
        assert got[v] is None and "no assembly table" in cv.convention(v)["why"]
    assert cv.convention("eurocircuits")["zero_orientation"]
    assert not cv.convention("pcbway")["zero_orientation"]


# --------------------------------------------------- quiet: the false case

@pytest.mark.parametrize("vid", ["pcbway", "seeed"])
def test_kicad_vendor_cpl_from_the_pos_file_passes(board, tmp_path, vid):
    """The issue's false finding: a PCBWay CPL with KiCad's own rotations
    was judged against LCSC models (JLC's zero). Now it passes, bottom part
    and all, with no model and no network."""
    pcb, pos = board
    cpl = _vendor_cpl(vid, pos, tmp_path / "fab" / vid / "CPL.csv")
    rep = cv.verify_vendor(pcb, cv.read_vendor_cpl(cpl), cv.convention(vid))
    assert rep["status"] == "pass", rep["parts"]
    assert rep["n_checked"] == len(PARTS) and rep["basis"] == "kicad"
    assert {r["verdict"] for r in rep["parts"]} == {"ok"}
    facts, vios = _dfm(pcb, cpl, vid)
    assert vios == [] and facts["failed"] == [] and facts["vendor"] == vid
    assert cv.main(["--pcb", str(pcb), "--cpl", str(cpl), "--vendor", vid,
                    "--out", str(tmp_path / "r.json")]) == 0


def test_kicad_vendor_reads_the_raw_pos_file_too(board, tmp_path):
    pcb, pos = board
    rep = cv.verify_vendor(pcb, cv.read_vendor_cpl(pos),
                           cv.convention("pcbway"))
    assert rep["status"] == "pass"


def test_unpolarised_two_pad_part_either_way_round(board, tmp_path):
    pcb, pos = board
    cpl = _vendor_cpl("pcbway", pos, tmp_path / "pcbway" / "CPL.csv",
                      turn={"R1": {"rotation": 180.0}})
    rep = cv.verify_vendor(pcb, cv.read_vendor_cpl(cpl),
                           cv.convention("pcbway"))
    assert rep["status"] == "pass"


# ---------------------------------------------------- kept fault tests

@pytest.mark.parametrize("ref", ["U1", "D1", "U2", "D2"])
def test_a_row_turned_90_fails(board, tmp_path, ref):
    pcb, pos = board
    cpl = _vendor_cpl("pcbway", pos, tmp_path / "pcbway" / "CPL.csv",
                      turn={ref: {"rotation": 90.0}})
    rep = cv.verify_vendor(pcb, cv.read_vendor_cpl(cpl),
                           cv.convention("pcbway"))
    assert rep["failed"] == [ref]
    row = next(r for r in rep["parts"] if r["ref"] == ref)
    assert row["verdict"] == "wrong_rotation" and "KiCad's own" in row["why"]
    facts, vios = _dfm(pcb, cpl, "pcbway")
    assert [(v["kind"], v["refs"]) for v in vios] == [("cpl_rotation", [ref])]
    assert vios[0]["severity"] == "error"
    assert cv.main(["--pcb", str(pcb), "--cpl", str(cpl), "--vendor",
                    "pcbway", "--out", str(tmp_path / "r.json")]) == 1


def test_a_diode_turned_180_fails(board, tmp_path):
    """Two pads, but polarised: 180 is the cathode on the anode."""
    pcb, pos = board
    cpl = _vendor_cpl("pcbway", pos, tmp_path / "pcbway" / "CPL.csv",
                      turn={"D1": {"rotation": 180.0}})
    rep = cv.verify_vendor(pcb, cv.read_vendor_cpl(cpl),
                           cv.convention("pcbway"))
    assert rep["failed"] == ["D1"]


def test_moved_or_wrong_side_rows_fail(board, tmp_path):
    pcb, pos = board
    cpl = _vendor_cpl("pcbway", pos, tmp_path / "pcbway" / "CPL.csv",
                      turn={"U1": {"x": 0.5}, "D2": {"side": "Top"}})
    rep = cv.verify_vendor(pcb, cv.read_vendor_cpl(cpl),
                           cv.convention("pcbway"))
    got = {r["ref"]: r["verdict"] for r in rep["parts"]}
    assert got["U1"] == "wrong_position" and got["D2"] == "wrong_side"
    facts, vios = _dfm(pcb, cpl, "pcbway")
    assert sorted((v["kind"], v["refs"][0]) for v in vios) == [
        ("cpl_position", "D2"), ("cpl_position", "U1")]


# ------------------------------------------- IEC zero and no assembly

def test_eurocircuits_leaves_multi_pin_parts_not_judged(board, tmp_path):
    pcb, pos = board
    cpl = _vendor_cpl("eurocircuits", pos, tmp_path / "eurocircuits" / "CPL.csv",
                      turn={"U1": {"rotation": 90.0}})
    rep = cv.verify_vendor(pcb, cv.read_vendor_cpl(cpl),
                           cv.convention("eurocircuits"))
    got = {r["ref"]: r["verdict"] for r in rep["parts"]}
    assert got == {"U1": "not_judged", "U2": "not_judged", "D1": "ok",
                   "D2": "ok", "R1": "ok"}
    assert rep["status"] == "pass" and "IEC 61188-7" in rep["not_judged"]["note"]
    facts, vios = _dfm(pcb, cpl, "eurocircuits")
    assert [(v["kind"], v["severity"], v["refs"]) for v in vios] == [
        ("cpl_not_judged", "warning", ["U1", "U2"])]
    # a two-pad part is still judged under the IEC zero
    cpl = _vendor_cpl("eurocircuits", pos, tmp_path / "eurocircuits" / "CPL.csv",
                      turn={"D1": {"rotation": 90.0}})
    facts, vios = _dfm(pcb, cpl, "eurocircuits")
    assert ("cpl_rotation", ["D1"]) in [(v["kind"], v["refs"]) for v in vios]


def test_no_assembly_table_is_not_judged(board, tmp_path):
    pcb, pos = board
    rep = cv.verify_vendor(pcb, cv.read_placement(pos)[1],
                           cv.convention("oshpark"))
    assert rep["status"] == "not_judged" and rep["failed"] == []
    assert {r["verdict"] for r in rep["parts"]} == {"not_judged"}
    facts, vios = _dfm(pcb, pos, "oshpark")
    assert vios == [] and facts["status"] == "not_judged"
    out = tmp_path / "r.json"
    assert cv.main(["--pcb", str(pcb), "--cpl", str(pos), "--vendor",
                    "oshpark", "--out", str(out)]) == 3
    assert json.loads(out.read_text())["status"] == "not_judged"


# ------------------------------------------------- which CPL dfm reads

def test_dfm_reads_the_vendors_own_cpl(tmp_path):
    fab = tmp_path / "fab"
    (fab / "pcbway").mkdir(parents=True)
    jlc = fab / "CPL.csv"
    jlc.write_text("x", encoding="utf-8")
    assert dfm_check.vendor_cpl("pcbway", jlc, None) is None
    (fab / "pcbway" / "CPL.csv").write_text("x", encoding="utf-8")
    assert dfm_check.vendor_cpl("pcbway", jlc, None) == fab / "pcbway" / "CPL.csv"
    assert dfm_check.vendor_cpl("pcbway", None, fab / "gerbers") == \
        fab / "pcbway" / "CPL.csv"
    assert dfm_check.vendor_cpl("seeed", jlc, fab) is None
    own = fab / "pcbway" / "CPL.csv"
    assert dfm_check.vendor_cpl("pcbway", own, None) == own


def test_workspace_vendor_is_the_default(board, tmp_path, monkeypatch):
    """cpl_verify with no --vendor takes the workspace's chosen vendor."""
    pcb, pos = board
    import ledgerlib
    monkeypatch.setattr(ledgerlib, "workspace_of", lambda p: tmp_path)
    (tmp_path / "state.json").write_text(json.dumps({"vendor": "oshpark"}),
                                         encoding="utf-8")
    assert cv.main(["--pcb", str(pcb), "--cpl", str(pos),
                    "--out", str(tmp_path / "r.json")]) == 3
