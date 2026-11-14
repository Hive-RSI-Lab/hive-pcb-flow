"""check_bom_sync.py: DNP, BOM, CPL and board attributes agree (DFA-07,
DFA-04, DFA-08).

Each case copies the blinky2 golden (board + schematic) into its own tmp dir,
writes the fab files and parts.json it needs, and asserts both the ref that is
flagged and the refs that are not. The committed mutant dfa-07-dnp-populated
is checked as it is in the scorecard: caught at R2 by bom_dnp_populated.
"""
from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_bom_sync  # noqa: E402

GOLDEN = REPO / "tests" / "golden"
MUTANT = GOLDEN / "mutants" / "dfa-07-dnp-populated"
R2_POS = [127.1, 129.5]


def _mutation():
    spec = importlib.util.spec_from_file_location(
        "dfa_07_dnp_populated",
        GOLDEN / "mutations" / "dfa_07_dnp_populated.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MUT = _mutation()
GOLDEN_PCB = (GOLDEN / "blinky2" / "blinky2.kicad_pcb").read_text(
    encoding="utf-8")


def _workspace(tmp_path, pcb_text=GOLDEN_PCB, sch_dnp=None, fab=True):
    """A flat copy of blinky2 in tmp_path; fab=True writes the clean fab
    files built from the golden footprints. Returns the board path."""
    pcb = tmp_path / "blinky2.kicad_pcb"
    pcb.write_text(pcb_text, encoding="utf-8")
    sch = (GOLDEN / "blinky2" / "blinky2.kicad_sch").read_text(
        encoding="utf-8")
    if sch_dnp:
        sch = MUT.mark_schematic_dnp(sch, sch_dnp)
    (tmp_path / "blinky2.kicad_sch").write_text(sch, encoding="utf-8")
    if fab:
        for name, body in MUT.fab_files(GOLDEN_PCB).items():
            (tmp_path / name).write_text(body, encoding="utf-8")
    return pcb


def _run(pcb, parts=None):
    viols, legs = check_bom_sync.check(
        pcb, check_bom_sync.find_fab_dir(pcb, None), parts)
    return viols, legs


def _errors(viols, code=None):
    return [v for v in viols if v["severity"] == "error"
            and (code is None or v["kind"] == code)]


def _refs(viols, code):
    return {r for v in viols if v["kind"] == code for r in v["refs"]}


def _drop_ref(path, ref):
    """Remove one designator from a fab csv (whole CPL row, or one name
    from a BOM row's comma list)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        if line.startswith(f"{ref},"):
            continue
        out.append(line.replace(f"{ref},", "").replace(f",{ref}\"", "\""))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def test_golden_has_no_findings_and_says_which_legs_skipped():
    # a bare board is no fault: no finding, but the skipped legs are named
    viols, legs = _run(GOLDEN / "blinky2" / "blinky2.kicad_pcb")
    assert viols == []
    skipped = legs.pop("skipped")
    assert legs == {"schematic": True, "parts": False, "fab": False,
                    "packages": 0, "files": {}}
    assert [s.split(":")[0] for s in skipped] == ["fab", "parts"]


def test_clean_fab_files_pass(tmp_path):
    viols, legs = _run(_workspace(tmp_path))
    assert _errors(viols) == []
    assert legs["fab"] is True
    assert "fab" not in [s.split(":")[0] for s in legs["skipped"]]


def test_committed_mutant_is_caught_at_r2():
    viols, _ = _run(MUTANT / "blinky2.kicad_pcb")
    hits = _errors(viols, "bom_dnp_populated")
    assert {v["refs"][0] for v in hits} == {"R2"}
    assert all(v["pos"] == R2_POS for v in hits)
    # board attr and schematic agree, so nothing else is an error
    assert {v["kind"] for v in _errors(viols)} == {"bom_dnp_populated"}


def test_mutation_reproduces_committed_fab_files():
    for name, body in MUT.fab_files(GOLDEN_PCB).items():
        assert (MUTANT / name).read_text(encoding="utf-8") == body


def test_schematic_dnp_the_board_does_not_carry(tmp_path):
    # schematic says DNP, board attr and parts.json are silent: bom_cpl would
    # still place R2, and the stale BOM lists it
    viols, _ = _run(_workspace(tmp_path, sch_dnp="R2"))
    assert _refs(_errors(viols), "bom_dnp_mismatch") == {"R2"}
    assert _refs(_errors(viols), "bom_dnp_populated") == {"R2"}
    assert _refs(_errors(viols), "bom_ref_missing") == set()


def test_board_dnp_the_schematic_does_not_carry(tmp_path):
    pcb_text = MUT.mutlib.edit_footprint(
        GOLDEN_PCB, "R1", "(attr smd)", "(attr smd dnp)", "R1 dnp")
    viols, _ = _run(_workspace(tmp_path, pcb_text=pcb_text))
    assert _refs(_errors(viols), "bom_dnp_mismatch") == {"R1"}
    assert _refs(_errors(viols), "bom_dnp_populated") == {"R1"}


def test_placed_part_missing_from_cpl_and_bom_of_record(tmp_path):
    pcb = _workspace(tmp_path)
    _drop_ref(tmp_path / "CPL.csv", "C4")
    _drop_ref(tmp_path / "BOM-full.csv", "D1")
    viols, _ = _run(pcb)
    missing = _errors(viols, "bom_ref_missing")
    assert {(v["refs"][0], "CPL.csv" in v["msg"]) for v in missing} == {
        ("C4", True), ("D1", False)}


def test_unknown_designator_in_bom(tmp_path):
    pcb = _workspace(tmp_path)
    with (tmp_path / "BOM.csv").open("a", encoding="utf-8") as fh:
        fh.write("10k,R99,R_0603_1608Metric,\n")
    viols, _ = _run(pcb)
    assert _refs(_errors(viols), "bom_ref_unknown") == {"R99"}
    assert _errors(viols, "bom_ref_unknown")[0]["pos"] is None


def test_cpl_designators_match_whatever_their_case(tmp_path):
    # oasis: the board says boot1, the CPL it shipped to JLC says BOOT1. JLC
    # matches either way, so the case alone is no unknown or missing ref -
    # but a ref no footprint folds to still is
    pcb = _workspace(tmp_path)
    cpl = tmp_path / "CPL.csv"
    lines = cpl.read_text(encoding="utf-8").splitlines()
    body = [lines[0]] + [ln.lower() if ln.startswith("C4,") else ln
                         for ln in lines[1:]]
    cpl.write_text("\n".join(body) + "\nr99,1,1,0,top\n", encoding="utf-8")
    viols, _ = _run(pcb)
    assert _refs(_errors(viols), "bom_ref_unknown") == {"r99"}
    assert _errors(viols, "bom_ref_missing") == []


def test_parts_json_dnp_class_counts(tmp_path):
    pcb = _workspace(tmp_path)
    parts = tmp_path / "parts.json"
    parts.write_text(json.dumps({"parts": [
        {"refdes": ["C1", "C2"], "lcsc": "C14663",
         "refdes_class": {"C2": "dnp"}}]}), encoding="utf-8")
    viols, legs = _run(pcb, parts)
    assert legs["parts"] is True
    assert _refs(_errors(viols), "bom_dnp_populated") == {"C2"}
    # a DNP class from parts.json needs no schematic flag to agree with
    assert _refs(viols, "bom_dnp_mismatch") == set()


def test_pos_excluded_smd_part_warns_until_parts_json_names_it(tmp_path):
    pcb_text = MUT.mutlib.edit_footprint(
        GOLDEN_PCB, "C4", "(attr smd)", "(attr smd exclude_from_pos_files)",
        "C4 pos excluded")
    pcb = _workspace(tmp_path, pcb_text=pcb_text)
    _drop_ref(tmp_path / "BOM.csv", "C4")
    _drop_ref(tmp_path / "CPL.csv", "C4")
    viols, _ = _run(pcb)
    assert _refs(viols, "bom_pos_excluded") == {"C4"}
    assert _errors(viols) == []          # hand_install: BOM-full only
    parts = tmp_path / "parts.json"
    parts.write_text(json.dumps({"parts": [
        {"refdes": ["C4"], "lcsc": "C15849",
         "assembly_class": "hand_install"}]}), encoding="utf-8")
    viols, _ = _run(pcb, parts)
    assert _refs(viols, "bom_pos_excluded") == set()


def test_cli_exit_codes(tmp_path):
    pcb = _workspace(tmp_path)
    out = tmp_path / "r.json"
    # clean files, parts.json absent: exit 0, and the report names the skip
    assert check_bom_sync.main(["--pcb", str(pcb), "--out", str(out)]) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["legs"]["fab"] is True
    assert [s.split(":")[0] for s in rep["skipped"]] == ["parts"]
    assert check_bom_sync.main(["--pcb", str(MUTANT / "blinky2.kicad_pcb")]) \
        == 1
    assert check_bom_sync.main(["--pcb", str(tmp_path / "nope.kicad_pcb")]) \
        == 2


def test_board_feature_with_pos_excluded_is_no_part(tmp_path):
    # fiducials, printed coils and pogo pads carry exclude_from_pos_files
    # too, but exclude_from_bom makes them board_feature, not hand_install
    pcb_text = MUT.mutlib.edit_footprint(
        GOLDEN_PCB, "C4", "(attr smd)",
        "(attr smd exclude_from_pos_files exclude_from_bom)",
        "C4 board feature")
    pcb = _workspace(tmp_path, pcb_text=pcb_text)
    _drop_ref(tmp_path / "BOM.csv", "C4")
    _drop_ref(tmp_path / "CPL.csv", "C4")
    viols, _ = _run(pcb)
    assert _refs(viols, "bom_pos_excluded") == set()
    assert _errors(viols) == []


def _parts(tmp_path, lines):
    p = tmp_path / "parts" / "parts.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps({"parts": lines}), encoding="utf-8")
    return p


def test_0402_part_on_an_0603_pad(tmp_path):
    # oasis v0.2 Rfbb1 (docs/errata-corpus.md): JLC's review caught it
    pcb = _workspace(tmp_path, fab=False)
    parts = _parts(tmp_path, [
        {"refdes": ["R1"], "lcsc": "C25744", "package": "0402"},
        {"refdes": ["R2"], "lcsc": "C22775", "package": "0603"},
        {"refdes": ["U1"], "lcsc": "C8734", "package": "LQFP-48"}])
    viols, legs = _run(pcb, parts)
    hits = _errors(viols, "bom_package_mismatch")
    assert _refs(hits, "bom_package_mismatch") == {"R1"}
    assert "0402" in hits[0]["msg"] and "R_0603_1608Metric" in hits[0]["msg"]
    assert legs["packages"] == 2
    assert not [s for s in legs["skipped"] if s.startswith("packages")]


def test_package_from_parts_file_and_bom_csv(tmp_path):
    # the BOM buys C3 as a part whose parts/<LCSC>.json says 0402
    pcb = _workspace(tmp_path)
    parts = _parts(tmp_path, [])
    (parts.parent / "C1525.json").write_text(
        json.dumps({"lcsc": "C1525", "package": "0402"}), encoding="utf-8")
    bom = tmp_path / "BOM.csv"
    lines = bom.read_text(encoding="utf-8").splitlines()
    lines = [lines[0]] + [ln + "C1525" if ",C3" in ln or '"C3' in ln
                          else ln for ln in lines[1:]]
    bom.write_text("\n".join(lines) + "\n", encoding="utf-8")
    viols, _ = _run(pcb, parts)
    hits = _errors(viols, "bom_package_mismatch")
    assert "C3" in _refs(hits, "bom_package_mismatch")
    assert all("BOM.csv" in v["msg"] for v in hits)


def test_unknown_package_is_skipped_not_passed_and_live_asks_jlc(tmp_path):
    pcb = _workspace(tmp_path, fab=False)
    parts = _parts(tmp_path, [{"refdes": ["R1"], "lcsc": "C844504"}])
    # an empty table: the code is in neither the part data nor the table
    viols, legs = check_bom_sync.check(pcb, None, parts, table={})
    assert _errors(viols, "bom_package_mismatch") == []
    assert any(s.startswith("packages") and "C844504" in s
               for s in legs["skipped"])

    def fetch(method, url, body=None):
        return {"code": 200, "data": {"componentPageInfo": {"list": [{
            "componentCode": "C844504",
            "componentModelEn": "CRCW040222K1FKED",
            "componentSpecificationEn": "0402"}]}}}
    viols, legs = check_bom_sync.check(pcb, None, parts, live=True,
                                       fetch=fetch, table={})
    assert _refs(_errors(viols), "bom_package_mismatch") == {"R1"}
    assert "CRCW040222K1FKED" in viols[0]["msg"]


# ---- #879: the pinned package table judges a foreign board offline

def _lcsc_on(pcb_text, ref, code):
    return MUT.mutlib.edit_footprint(
        pcb_text, ref, '\t\t(property "Reference" "%s"' % ref,
        '\t\t(property "LCSC" "%s" (at 0 0 0) (layer "F.Fab") (hide yes))\n'
        '\t\t(property "Reference" "%s"' % (code, ref), f"{ref} LCSC")


def test_the_pinned_table_judges_a_board_with_no_part_data(tmp_path):
    # oasis v0.2's shape: the LCSC number is on the board, no parts.json,
    # no network - the table alone says C844504 is an 0402
    pcb = _workspace(tmp_path, _lcsc_on(GOLDEN_PCB, "R1", "C844504"),
                     fab=False)
    table = {"C844504": {"package": "0402", "model": "CRCW040222K1FKED",
                         "read": "2026-11-13"}}

    def no_network(*a, **k):
        raise AssertionError("the table must answer before the network")
    viols, legs = check_bom_sync.check(pcb, None, None, live=True,
                                       fetch=no_network, table=table)
    hits = _errors(viols, "bom_package_mismatch")
    assert _refs(hits, "bom_package_mismatch") == {"R1"}
    assert "reference/lcsc_packages.json" in hits[0]["msg"]
    assert "2026-11-13" in hits[0]["msg"]
    assert legs["packages"] == 1


def test_a_matching_table_part_passes_and_part_data_outranks_it(tmp_path):
    pcb = _workspace(tmp_path, _lcsc_on(GOLDEN_PCB, "R1", "C22843"),
                     fab=False)
    # the table says 0603, as the pad is: compared and quiet
    table = {"C22843": {"package": "0603", "model": "0603WAF1501T5E"}}
    viols, legs = check_bom_sync.check(pcb, None, None, table=table)
    assert _errors(viols, "bom_package_mismatch") == []
    assert legs["packages"] == 1
    # the workspace's own parts.json is read first: it says 0402 here
    parts = _parts(tmp_path, [{"refdes": ["R1"], "lcsc": "C22843",
                               "package": "0402"}])
    viols, _ = check_bom_sync.check(pcb, None, parts, table=table)
    assert _refs(_errors(viols), "bom_package_mismatch") == {"R1"}


def test_the_shipped_table_knows_the_oasis_rfbb1_part():
    import lcsc_packages
    assert lcsc_packages.load()["C844504"]["package"] == "0402"


def test_lcsc_packages_add_reads_jlc_and_records_a_missing_code(tmp_path):
    import lcsc_packages

    def fetch(method, url, body=None):
        if body["keyword"] == "C1":
            return {"code": 200, "data": {"componentPageInfo": {"list": [{
                "componentCode": "C1", "componentModelEn": "M1",
                "componentSpecificationEn": "0805"}]}}}
        return {"code": 200, "data": {"componentPageInfo": {"list": []}}}
    parts = {}
    assert lcsc_packages.add(["C1", "C2"], parts, fetch=fetch) == []
    assert parts["C1"]["package"] == "0805" and parts["C2"]["package"] == ""
    path = tmp_path / "t.json"
    lcsc_packages.save(parts, path)
    assert set(lcsc_packages.load(path)) == {"C1", "C2"}
    assert lcsc_packages.load(tmp_path / "absent.json") == {}


# ---- the fab leg reads the BOM and CPL a board ships under other names: a
# KiCad pos export and a distributor BOM in a folder of the fab dir were
# never read, so the leg was skipped as if there were no fab files

def _foreign_ws(tmp_path, pcb_text=GOLDEN_PCB, sch_dnp=None):
    """blinky2 as a workspace (kicad/, fab/) with no file under bom_cpl's
    names. Returns (board, fab dir)."""
    (tmp_path / "kicad").mkdir()
    pcb = _workspace(tmp_path / "kicad", pcb_text, sch_dnp, fab=False)
    (tmp_path / "fab").mkdir()
    return pcb, tmp_path / "fab"


def _golden_rows(name):
    return list(csv.DictReader(io.StringIO(MUT.fab_files(GOLDEN_PCB)[name])))


def _write_bom(path, drop=(), extra=()):
    """The golden BOM as a distributor BOM: a Designators column whose refs
    a space joins."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [[r["Comment"], " ".join(x for x in r["Designator"].split(",")
                                     if x not in drop), r["Footprint"]]
            for r in _golden_rows("BOM.csv")] + [list(e) for e in extra]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Value", "Designators", "Footprint"])
        w.writerows(r for r in rows if r[1])
    return path


def _write_pos(path, refs=None):
    """The golden CPL as a KiCad pos export (Ref, Val, ... Side), of refs
    (default: every one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"])
        for r in _golden_rows("CPL.csv"):
            if refs is None or r["Designator"] in refs:
                w.writerow([r["Designator"], "", "", r["Mid X"], r["Mid Y"],
                            r["Rotation"], "top"])
    return path


def _fab_lines(legs):
    return [s for s in legs["skipped"] if s.startswith("fab")]


def test_a_bom_and_pos_in_a_folder_of_the_fab_dir_are_read(tmp_path):
    pcb, fab = _foreign_ws(tmp_path)
    _write_bom(fab / "out" / "BOM-US.csv")
    _write_pos(fab / "out" / "blinky2-pos.csv")
    viols, legs = _run(pcb)
    assert legs["fab"] is True
    assert legs["files"] == {"BOM": ["out/BOM-US.csv"],
                             "CPL": ["out/blinky2-pos.csv"]}
    assert _errors(viols) == []
    assert _fab_lines(legs) == []


def test_foreign_files_still_catch_unknown_and_missing_refs(tmp_path):
    pcb, fab = _foreign_ws(tmp_path)
    _write_bom(fab / "out" / "BOM-US.csv", drop={"C4"},
               extra=[("10k", "R99", "R_0603_1608Metric")])
    every = {r["Designator"] for r in _golden_rows("CPL.csv")}
    _write_pos(fab / "out" / "blinky2-pos.csv", refs=every - {"D1"})
    viols, _ = _run(pcb)
    unknown = _errors(viols, "bom_ref_unknown")
    assert [(v["refs"][0], "out/BOM-US.csv" in v["msg"]) for v in unknown] \
        == [("R99", True)]
    missing = _errors(viols, "bom_ref_missing")
    assert {(v["refs"][0], v["msg"].rsplit(" ", 1)[1]) for v in missing} \
        == {("C4", "out/BOM-US.csv"), ("D1", "out/blinky2-pos.csv")}


def test_two_files_that_fit_a_role_leave_it_unread_and_say_which(tmp_path):
    pcb, fab = _foreign_ws(tmp_path)
    _write_bom(fab / "out" / "BOM-US.csv")
    _write_bom(fab / "out" / "BOM-EU.csv", drop={"C4"})
    _write_pos(fab / "out" / "blinky2-pos.csv")
    viols, legs = _run(pcb)
    assert legs["files"] == {"CPL": ["out/blinky2-pos.csv"]}
    [note] = _fab_lines(legs)
    assert "out/BOM-EU.csv, out/BOM-US.csv" in note and "--bom" in note
    assert _errors(viols) == []
    # --bom names the one to judge
    viols, legs = check_bom_sync.check(pcb, fab, None,
                                       bom=fab / "out" / "BOM-EU.csv")
    assert legs["files"]["BOM"] == ["out/BOM-EU.csv"]
    assert _refs(_errors(viols), "bom_ref_missing") == {"C4"}
    assert _fab_lines(legs) == []


def test_a_pos_export_split_by_side_is_read_as_one_cpl(tmp_path):
    pcb, fab = _foreign_ws(tmp_path)
    refs = [r["Designator"] for r in _golden_rows("CPL.csv")]
    _write_pos(fab / "blinky2-top-pos.csv", refs=set(refs[:9]))
    _write_pos(fab / "blinky2-bottom-pos.csv", refs=set(refs[9:]))
    viols, legs = _run(pcb)
    assert legs["files"] == {"CPL": ["blinky2-top-pos.csv",
                                     "blinky2-bottom-pos.csv"]}
    assert _errors(viols) == []
    # two pos files of different boards are no pair
    (fab / "blinky2-bottom-pos.csv").rename(fab / "other-bottom-pos.csv")
    _, legs = _run(pcb)
    assert legs["files"] == {}
    assert "2 files could be the CPL" in _fab_lines(legs)[0]


def test_a_kicad_grouped_bom_range_is_each_ref_in_it(tmp_path):
    split = check_bom_sync._split_refs
    assert split("C1-C4,C9 R1;R2") == ["C1", "C2", "C3", "C4", "C9", "R1",
                                       "R2"]
    # no range: a ref with a dash, a reversed or zero-padded pair
    assert split("TP-1 U1-A R4-R2 C01-C03 C1-D4") == [
        "TP-1", "U1-A", "R4-R2", "C01-C03", "C1-D4"]
    # kicad-cli sch export bom --group-by "Value,Footprint", as it writes it
    pcb, fab = _foreign_ws(tmp_path)
    (fab / "blinky2-bom.csv").write_text(
        '"Refs","Value","Footprint","Qty","DNP"\n'
        '"C1-C4,C9","100nF","C_0603_1608Metric","5",""\n'
        '"C5,C6","10uF","C_0805_2012Metric","2",""\n'
        '"C7,C8","22pF","C_0603_1608Metric","2",""\n'
        '"D1","LED_red","LED_0805_2012Metric","1",""\n'
        '"J1","PWR_5V","PinHeader_1x02_P2.54mm_Vertical","1",""\n'
        '"J2","SWD","PinHeader_1x04_P2.54mm_Vertical","1",""\n'
        '"R1","10k","R_0603_1608Metric","1",""\n'
        '"R2","470","R_0603_1608Metric","1",""\n'
        '"U1","STM32F103C8T6","LQFP-48_7x7mm_P0.5mm","1",""\n'
        '"U2","AMS1117-3.3","SOT-223-3_TabPin2","1",""\n'
        '"Y1","8MHz","Crystal_SMD_3225-4Pin_3.2x2.5mm","1",""\n',
        encoding="utf-8")
    viols, legs = _run(pcb)
    assert legs["files"] == {"BOM": ["blinky2-bom.csv"]}
    assert _errors(viols) == []


def test_dnp_in_a_foreign_file_is_named_not_an_error(tmp_path):
    # a KiCad pos or BOM export lists DNP parts unless told not to, and its
    # rows may say DNP in a column of their own: only bom_cpl's files are
    # held to the DNP rule
    pcb_text = MUT.mutlib.edit_footprint(
        GOLDEN_PCB, "R2", "(attr smd)", "(attr smd dnp)", "R2 dnp")
    pcb, fab = _foreign_ws(tmp_path, pcb_text, sch_dnp="R2")
    _write_bom(fab / "out" / "BOM-US.csv")
    _write_pos(fab / "out" / "blinky2-pos.csv")
    viols, legs = _run(pcb)
    assert _errors(viols) == []
    lines = [s for s in _fab_lines(legs) if "lists DNP R2" in s]
    assert len(lines) == 2
    # bom_cpl's own BOM.csv still is
    (fab / "out" / "BOM-US.csv").rename(fab / "BOM.csv")
    viols, legs = _run(pcb)
    assert legs["files"]["BOM"] == ["BOM.csv"]
    assert _refs(_errors(viols), "bom_dnp_populated") == {"R2"}
    assert all("BOM.csv" in v["msg"]
               for v in _errors(viols, "bom_dnp_populated"))


def test_a_file_with_no_designator_column_is_said_and_skipped(tmp_path):
    pcb, fab = _foreign_ws(tmp_path)
    (fab / "blinky2-bom.csv").write_text("Value,Qty\n10k,1\n",
                                         encoding="utf-8")
    _write_pos(fab / "blinky2-pos.csv")
    viols, legs = _run(pcb)
    assert legs["files"] == {"CPL": ["blinky2-pos.csv"]}
    [note] = _fab_lines(legs)
    assert note.startswith("fab: blinky2-bom.csv has no designator column")
    assert _errors(viols) == []


def test_cli_bom_and_cpl_name_the_files(tmp_path):
    pcb, _ = _foreign_ws(tmp_path)
    bom = _write_bom(tmp_path / "order" / "parts-list.csv", drop={"C4"})
    refs = [r["Designator"] for r in _golden_rows("CPL.csv")]
    top = _write_pos(tmp_path / "order" / "a.csv", refs=set(refs[:9]))
    bottom = _write_pos(tmp_path / "order" / "b.csv", refs=set(refs[9:]))
    out = tmp_path / "r.json"
    assert check_bom_sync.main(["--pcb", str(pcb), "--bom", str(bom),
                                "--cpl", str(top), str(bottom),
                                "--out", str(out)]) == 1
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["legs"]["fab"] is True
    assert rep["fab_files"] == {"BOM": [str(bom)],
                                "CPL": [str(top), str(bottom)]}
    assert {v["refs"][0] for v in rep["violations"]} == {"C4"}
    assert check_bom_sync.main(["--pcb", str(pcb), "--bom",
                                str(tmp_path / "nope.csv")]) == 2
