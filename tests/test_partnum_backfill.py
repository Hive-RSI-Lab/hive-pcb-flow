"""partnum_backfill: part numbers from a board's own sources, added only
where a footprint has none, without touching the source checkout."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import partnum_backfill as pnb  # noqa: E402


def _fp(ref: str, *props: tuple[str, str], value: str = "10k",
        name: str = "R_0603") -> str:
    extra = "".join(f'\n    (property "{k}" "{v}" (at 0 0 0) (layer "F.Fab"))'
                    for k, v in props)
    return (f'  (footprint "{name}" (layer "F.Cu") (at 1 1)\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'    (property "Value" "{value}" (at 0 0 0) (layer "F.Fab")){extra}\n'
            f'    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
            f'  )\n')


def _board(tmp: Path, *fps: str, name: str = "b.kicad_pcb") -> Path:
    p = tmp / name
    p.write_text('(kicad_pcb (version 20240108) (generator "pcbnew")\n'
                 + "".join(fps) + ")\n", encoding="utf-8")
    return p


def _run(tmp: Path, pcb: Path, *extra: str) -> tuple[dict, Path]:
    out, rep = tmp / "out.kicad_pcb", tmp / "r.json"
    rc = pnb.main(["--pcb", str(pcb), "--src-pcb", str(pcb), "--repo",
                   str(tmp), "--out", str(out), "--out-report", str(rep),
                   *extra])
    assert rc == 0
    return json.loads(rep.read_text()), out


def _props(pcb: Path) -> dict:
    return {fp["ref"]: {k: v[0] for k, v in fp["props"].items()}
            for fp in pnb.footprints(pcb.read_text())}


def test_bom_fills_only_footprints_without_a_part_number(tmp_path):
    pcb = _board(tmp_path, _fp("R1"), _fp("R2", ("LCSC", "C9999")),
                 _fp("C1", value="100n"))
    (tmp_path / "board-bom.csv").write_text(
        "Designator,Comment,LCSC\nR1 R2,10k,C25804\nC1,100n,C14663\n")
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    rep, out = _run(tmp_path, pcb)
    assert rep["status"] == "written"
    got = _props(out)
    assert got["R1"]["LCSC"] == "C25804"
    assert got["R2"]["LCSC"] == "C9999"        # had one: left alone
    assert got["C1"]["LCSC"] == "C14663"
    assert rep["counts"]["given_part_number"] == 2
    assert rep["counts"]["added_from"]["bom"] == 2
    for p, data in before.items():             # sources untouched
        assert p.read_bytes() == data
    # the rest of the file stays byte for byte
    stripped = out.read_text()
    for chunk in pcb.read_text().split("\n"):
        assert chunk in stripped


def test_conflicting_values_in_one_source_give_none(tmp_path):
    pcb = _board(tmp_path, _fp("R1"), _fp("R2"))
    (tmp_path / "bom.csv").write_text(
        "Ref,LCSC\nR1,C25804\nR1,C25805\nR2,C21190\n")
    rep, out = _run(tmp_path, pcb)
    assert rep["counts"]["conflicting"] == 1
    got = _props(out)
    assert "LCSC" not in got["R1"]
    assert got["R2"]["LCSC"] == "C21190"


def test_a_bom_mostly_naming_other_refs_is_skipped(tmp_path):
    pcb = _board(tmp_path, _fp("R1"))
    (tmp_path / "bom.csv").write_text(
        "Designator,LCSC\nR1,C21190\nR7,C21191\nR8,C21192\nR9,C21193\n")
    rep, _ = _run(tmp_path, pcb)
    assert rep["status"] == "nothing_to_add"
    assert rep["boms_skipped"][0][1] == "1 of 4 refs on this board"


def test_a_bom_without_designators_is_skipped(tmp_path):
    pcb = _board(tmp_path, _fp("R1"))
    (tmp_path / "bom.csv").write_text("Mouser No,Mfr. No\n1-R,RC0603\n")
    rep, _ = _run(tmp_path, pcb)
    assert rep["status"] == "nothing_to_add"
    assert rep["boms_skipped"][0][1] == "no designator column"


def test_empty_property_is_filled_not_duplicated(tmp_path):
    pcb = _board(tmp_path, _fp("U1", ("MPN", "")))
    (tmp_path / "bom.csv").write_text("Reference,MPN\nU1,TPS62162DSGR\n")
    rep, out = _run(tmp_path, pcb)
    assert rep["status"] == "written"
    assert out.read_text().count('(property "MPN"') == 1
    assert _props(out)["U1"]["MPN"] == "TPS62162DSGR"


def test_xlsx_bom_is_read(tmp_path):
    pcb = _board(tmp_path, _fp("C1"), _fp("C2"))
    shared = ["Designator", "LCSC", "C1,C2", "C14663"]
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sst = (f'<sst xmlns="{ns}">' + "".join(f"<si><t>{s}</t></si>"
                                            for s in shared) + "</sst>")
    sheet = (f'<worksheet xmlns="{ns}"><sheetData>'
             '<row r="1"><c r="A1" t="s"><v>0</v></c>'
             '<c r="C1" t="s"><v>1</v></c></row>'
             '<row r="2"><c r="A2" t="s"><v>2</v></c>'
             '<c r="C2" t="s"><v>3</v></c></row></sheetData></worksheet>')
    with zipfile.ZipFile(tmp_path / "board-bom.xlsx", "w") as z:
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    rows = pnb.xlsx_rows(tmp_path / "board-bom.xlsx")
    assert rows[0] == ["Designator", "", "LCSC"]
    rep, out = _run(tmp_path, pcb)
    assert rep["counts"]["codes_added"] == 2
    assert _props(out)["C2"]["LCSC"] == "C14663"


def test_bom_of_a_board_in_a_subfolder_is_not_this_boards(tmp_path):
    pcb = _board(tmp_path, _fp("R1"), name="ctrl.kicad_pcb")
    sub = tmp_path / "other"
    sub.mkdir()
    _board(sub, _fp("R1"), name="other.kicad_pcb")
    (sub / "bom.csv").write_text("Ref,LCSC\nR1,C21190\n")
    rep, _ = _run(tmp_path, pcb)
    assert rep["status"] == "nothing_to_add"
    assert rep["boms_read"] == []


def test_netlist_fields_without_footprints_still_read(tmp_path):
    net = tmp_path / "b.net"
    net.write_text(
        '(export (version "E")\n (components\n'
        '  (comp (ref "R1") (value "10k") (footprint "R_0603")\n'
        '   (fields (field (name "LCSC") "C25804")))\n'
        '  (comp (ref "C9") (value "1u")\n'
        '   (fields (field (name "LCSC") "C21190"))))\n (nets))\n')
    pcb = _board(tmp_path, _fp("R1"))
    rep, out = _run(tmp_path, pcb, "--netlist", str(net))
    assert rep["counts"]["added_from"]["schematic"] == 1
    assert _props(out)["R1"]["LCSC"] == "C25804"


def test_error_report_goes_to_out_report(tmp_path):
    rep = tmp_path / "r.json"
    rc = pnb.main(["--pcb", str(tmp_path / "missing.kicad_pcb"),
                   "--out", str(tmp_path / "o.kicad_pcb"),
                   "--out-report", str(rep)])
    assert rc == 2
    assert json.loads(rep.read_text())["status"] == "error"


def _fp_named(ref: str, name: str, value: str) -> str:
    return (f'  (footprint "{name}" (layer "F.Cu") (at 1 1)\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'    (property "Value" "{value}" (at 0 0 0) (layer "F.Fab"))\n'
            f'    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
            f'  )\n')


def _two_revs(tmp: Path, b_fp: str) -> tuple[dict, Path]:
    """rev_a ships a BOM, rev_b (in its own folder) does not."""
    a, b = tmp / "rev_a", tmp / "rev_b"
    a.mkdir()
    b.mkdir()
    pa = _board(a, _fp_named("R1", "Resistor_SMD:R_0603", "10k"),
                _fp_named("C1", "Capacitor_SMD:C_0402", "100n"))
    (a / "bom.csv").write_text("Designator,LCSC\nR1,C25804\nC1,C1525\n")
    pb = _board(b, _fp_named("R1", "Resistor_SMD:R_0603", "10k"), b_fp)
    out, rep = tmp / "out.kicad_pcb", tmp / "r.json"
    assert pnb.main(["--pcb", str(pb), "--src-pcb", str(pb), "--repo",
                     str(b), "--other-pcb", str(pa), "--other-src-pcb",
                     str(pa), "--other-repo", str(a), "--out", str(out),
                     "--out-report", str(rep)]) == 0
    return json.loads(rep.read_text()), out


def test_other_revision_bom_fills_the_matching_footprint(tmp_path):
    rep, out = _two_revs(
        tmp_path, _fp_named("C1", "Capacitor_SMD:C_0402", "100n"))
    got = _props(out)
    assert got["R1"]["LCSC"] == "C25804"
    assert got["C1"]["LCSC"] == "C1525"
    assert rep["counts"]["added_from"]["other_revision"] == 2
    assert rep["counts"]["other_revision_unmatched"] == 0
    assert rep["other_revision"]["boms_read"] == ["bom.csv"]


def test_other_revision_does_not_fill_a_changed_footprint(tmp_path):
    # the quiet twin: C1 moved to 0603 on this revision, so rev_a's number
    # is not this part's
    rep, out = _two_revs(
        tmp_path, _fp_named("C1", "Capacitor_SMD:C_0603", "100n"))
    got = _props(out)
    assert got["R1"]["LCSC"] == "C25804"
    assert "LCSC" not in got["C1"]
    assert rep["counts"]["added_from"]["other_revision"] == 1
    assert rep["counts"]["other_revision_unmatched"] == 1


def test_own_sources_win_over_the_other_revision(tmp_path):
    a, b = tmp_path / "rev_a", tmp_path / "rev_b"
    a.mkdir()
    b.mkdir()
    pa = _board(a, _fp_named("R1", "R_0603", "10k"))
    (a / "bom.csv").write_text("Designator,LCSC\nR1,C25804\n")
    pb = _board(b, _fp_named("R1", "R_0603", "10k"))
    (b / "bom.csv").write_text("Designator,LCSC\nR1,C21190\n")
    out, rep = tmp_path / "o.kicad_pcb", tmp_path / "r.json"
    pnb.main(["--pcb", str(pb), "--repo", str(b), "--other-pcb", str(pa),
              "--other-repo", str(a), "--out", str(out),
              "--out-report", str(rep)])
    assert _props(out)["R1"]["LCSC"] == "C21190"
    assert json.loads(rep.read_text())["counts"]["added_from"] == {
        "schematic": 0, "bom": 1, "parts_json": 0, "other_revision": 0}


def _ics(tmp: Path) -> Path:
    return _board(
        tmp,
        _fp("U1", ("MPN", "AD8232ACPZ-R7"), value="AD8232ACPZ-R7",
            name="Package_DFN_QFN:QFN-20-1EP_4x4mm_P0.5mm"),
        _fp("U2", value="TPS7A2033PDBVR", name="Package_TO_SOT_SMD:SOT-23-5"),
        _fp("U3", value="TPS7A2033PDBVR", name="Package_TO_SOT_SMD:SOT-23-5"),
        _fp("R1"), _fp("C1", value="100n", name="C_0603"))


def test_a_bom_swapping_two_parts_fills_neither(tmp_path):
    """Planted swap: the BOM puts the LDO's row on the amplifier and the
    amplifier's on an LDO (and a capacitor's on a resistor). Each part it
    names plainly disagrees with the footprint's Value: nothing is filled
    there, and each is counted and listed."""
    pcb = _ics(tmp_path)
    (tmp_path / "board-bom.csv").write_text(
        "Comment,Designator,LCSC,MPN\n"
        "TPS7A2028PDBVR,U1 U3,C2862740,TPS7A2028PDBVR\n"
        "AD8232ACPZ-R7,U2,C43216,AD8232ACPZ-R7\n"
        "100n,R1,C14663,\nGRM188R71H104KA93D,C1,C14663,\n")
    rep, out = _run(tmp_path, pcb)
    got = _props(out) if out.exists() else _props(pcb)
    assert "LCSC" not in got["U1"] and "LCSC" not in got["U2"]
    assert "LCSC" not in got["R1"]
    assert got["U3"]["LCSC"] == "C2862740"     # one family: filled
    assert got["C1"]["LCSC"] == "C14663"       # an MPN against 100n: not judged
    assert rep["counts"]["part_conflicts"] == 3
    assert sorted(c["ref"] for c in rep["part_conflicts"]) == ["R1", "U1", "U2"]


def test_the_same_bom_in_order_fills_every_part(tmp_path):
    """Quiet twin: the same rows on the right refs fill every footprint and
    count no conflict."""
    pcb = _ics(tmp_path)
    (tmp_path / "board-bom.csv").write_text(
        "Comment,Designator,LCSC,MPN\n"
        "TPS7A2028PDBVR,U2 U3,C2862740,TPS7A2028PDBVR\n"
        "AD8232ACPZ-R7,U1,C43216,AD8232ACPZ-R7\n"
        "10k,R1,C25804,\nGRM188R71H104KA93D,C1,C14663,\n")
    rep, out = _run(tmp_path, pcb)
    got = _props(out)
    assert got["U1"]["LCSC"] == "C43216"
    assert got["U2"]["LCSC"] == got["U3"]["LCSC"] == "C2862740"
    assert got["R1"]["LCSC"] == "C25804" and got["C1"]["LCSC"] == "C14663"
    assert rep["counts"]["part_conflicts"] == 0 and rep["part_conflicts"] == []


def test_generic_values_are_not_judged():
    """Only values it can place are compared: a symbol name, a bare number
    or a frequency is never called a disagreement."""
    for board, bom in (("Conn_01x04", "B4B-PH-K-S"), ("LED", "XL-1608UBC"),
                       ("16MHz", "X322516MLB4SI"), ("100", "AD8232")):
        assert not pnb.disagree({board}, {bom}), (board, bom)
    assert pnb.disagree({"AD8232ACPZ-R7"}, {"TPS7A2028PDBVR"})
    assert not pnb.disagree({"AMS1117-3.3"}, {"LM1117MPX-3.3"})
    assert pnb.disagree({"10k"}, {"100nF"})
    assert not pnb.disagree({"4k7"}, {"10K"})


def _borrow(tmp: Path, rows: str, parts: str = "") -> tuple[dict, Path]:
    """rev_a ships the BOM, rev_b (this board) borrows from it; both carry
    the amplifier as U1 and an LDO as U2 on matching footprints."""
    a, b = tmp / "rev_a", tmp / "rev_b"
    a.mkdir()
    b.mkdir()
    fps = (_fp("U1", value="AD8232ACPZ-R7",
               name="Package_DFN_QFN:QFN-20-1EP_4x4mm_P0.5mm"),
           _fp("U2", value="TPS7A2033PDBVR",
               name="Package_TO_SOT_SMD:SOT-23-5"))
    pa, pb = _board(a, *fps), _board(b, *fps)
    (a / "bom.csv").write_text("Comment,Designator,LCSC\n" + rows)
    if parts:
        (a / "parts.json").write_text(parts)
    out, rep = tmp / "out.kicad_pcb", tmp / "r.json"
    assert pnb.main(["--pcb", str(pb), "--src-pcb", str(pb), "--repo",
                     str(b), "--other-pcb", str(pa), "--other-src-pcb",
                     str(pa), "--other-repo", str(a), "--out", str(out),
                     "--out-report", str(rep)]) == 0
    rep = json.loads(rep.read_text())
    return rep, out if out.exists() else pb


def test_a_swap_in_the_other_revisions_bom_is_not_borrowed(tmp_path):
    """Planted swap in the other revision's BOM: its rows put the LDO's
    number on the amplifier and the amplifier's on the LDO. The footprints
    match across revisions, but the borrowed rows plainly disagree with this
    board's Values: nothing is filled and both are counted."""
    rep, out = _borrow(tmp_path, "TPS7A2028PDBVR,U1,C2862740\n"
                                 "AD8232ACPZ-R7,U2,C43216\n")
    got = _props(out)
    assert "LCSC" not in got["U1"] and "LCSC" not in got["U2"]
    assert rep["counts"]["part_conflicts"] == 2
    assert {c["source"] for c in rep["part_conflicts"]} == {"other_revision"}
    assert rep["counts"]["added_from"]["other_revision"] == 0


def test_the_other_revisions_bom_in_order_is_borrowed(tmp_path):
    """Quiet twin: the same rows on the right refs are borrowed."""
    rep, out = _borrow(tmp_path, "AD8232ACPZ-R7,U1,C43216\n"
                                 "TPS7A2028PDBVR,U2,C2862740\n")
    got = _props(out)
    assert got["U1"]["LCSC"] == "C43216" and got["U2"]["LCSC"] == "C2862740"
    assert rep["counts"]["part_conflicts"] == 0
    assert rep["counts"]["added_from"]["other_revision"] == 2


def test_a_borrowed_swap_is_refused_even_where_another_source_agrees(
        tmp_path):
    """The other revision's parts.json names the right MPNs while its BOM
    swaps the LCSC codes: each code is judged by its own BOM row, so the
    agreeing MPN does not let the swapped code through."""
    parts = ('[{"refdes": ["U1"], "mpn": "AD8232ACPZ-R7"},'
             ' {"refdes": ["U2"], "mpn": "TPS7A2033PDBVR"}]')
    rep, out = _borrow(tmp_path, "TPS7A2028PDBVR,U1,C2862740\n"
                                 "AD8232ACPZ-R7,U2,C43216\n", parts)
    got = _props(out)
    assert "LCSC" not in got["U1"] and "LCSC" not in got["U2"]
    assert rep["counts"]["part_conflicts"] == 2
