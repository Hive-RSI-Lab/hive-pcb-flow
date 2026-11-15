"""check_fp_package: each part's footprint against the package of the part
it names (lib/pkgname.py reads both names), and a passive's Value against
its LCSC record (docs/miss-taxonomy.md class 1; flexgrid-v3-1/-2,
ice1usb-r01-1). No network: JLC's answers are injected."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import check_fp_package as cfp  # noqa: E402
import pkgname  # noqa: E402
import verify_all  # noqa: E402

SMD = '\t\t(pad "{n}" smd rect (at {x} 0) (size 1 1) (layers "F.Cu"))\n'
THT = ('\t\t(pad "{n}" thru_hole circle (at {x} 0) (size 1.6 1.6) '
       '(drill 0.8) (layers "*.Cu"))\n')


def footprint(ref, fpid, value, props=None, pads=2, kind=SMD, attr="smd"):
    p = "".join(f'\t\t(property "{k}" "{v}" (at 0 0 0) (layer "F.Fab"))\n'
                for k, v in {"Reference": ref, "Value": value,
                             **(props or {})}.items())
    pd = "".join(kind.format(n=i + 1, x=i) for i in range(pads))
    return (f'\t(footprint "{fpid}"\n\t\t(layer "F.Cu")\n\t\t(at 10 10)\n'
            f'{p}\t\t(attr {attr})\n{pd}\t)\n')


def board(tmp_path, *fps, parts=None):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text('(kicad_pcb\n\t(version 20240108)\n' + "".join(fps) + ")\n",
                   encoding="utf-8")
    if parts is not None:
        (tmp_path / "parts.json").write_text(json.dumps({"parts": parts}),
                                             encoding="utf-8")
    return pcb


def jlc(rows):
    """A fetch that answers JLC's search with `rows` filtered by keyword."""
    asked = []

    def fetch(method, url, body=None):
        asked.append(body["keyword"])
        kw = body["keyword"].upper()
        hit = [r for r in rows if kw in (r["componentCode"].upper(),)
               or kw in r["componentModelEn"].upper()]
        return {"code": 200, "data": {"componentPageInfo": {"list": hit}}}
    fetch.asked = asked
    return fetch


def row(code, model, package, describe=""):
    return {"componentCode": code, "componentModelEn": model,
            "componentSpecificationEn": package, "describe": describe}


def run(pcb, live=False, fetch=None, table=None, cache=None):
    codes, refs = cfp.parts_json_records(cfp.find_parts_json(pcb))
    recs = cfp.Records(codes, table or {}, live, cache, fetch, refs=refs)
    return cfp.check(pcb, recs)


# ---------------------------------------------------------------- pkgname

@pytest.mark.parametrize("part,fp,hit", [
    ("SOIC-8", "Package_SO:SOIC-8_5.23x5.23mm_P1.27mm", "150 mil"),
    ("SOIC-8", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", None),
    ("DO-41", "Diode_SMD:D_SOD-123F", "through hole"),
    ("SOD-123", "Diode_SMD:D_SOD-123F", None),          # one land pattern
    ("SOD-323", "Diode_SMD:D_SOD-123", "SOD-323"),
    ("0402", "Capacitor_SMD:C_0603_1608Metric", "0402"),
    ("SOT-23-5", "Package_TO_SOT_SMD:SOT-23", "5 pins"),
    ("SOT-23-5", "Package_TO_SOT_SMD:SOT-23-5", None),
    ("QFN-32(5x5)", "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm",
     None),
    ("QFN-32(4x4)", "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm",
     "4x4"),
    ("TSSOP-20", "Package_SO:SOIC-20W_7.5x12.8mm_P1.27mm", "pitch"),
    ("SO-8FL", "aiee:DFN-5_L4.9-W5.9-P1.27-LS6.2-BL", None),  # 5x6 power SON
    ("SMA(DO-214AC)", "Diode_SMD:D_SMA", None),
])
def test_pkgname_compare(part, fp, hit):
    why = pkgname.compare(pkgname.describe(part), pkgname.describe(fp), None)
    if hit is None:
        assert why == []
    else:
        assert why and hit in " ".join(why), why


def test_sma_coax_connector_is_not_the_diode_case():
    assert pkgname.describe("SMA_BAT_Wireless_BWSMA-KWE-Z001")["family"] \
        is None


# ---------------------------------------------------------------- check

def test_lcsc_code_package_against_footprint(tmp_path):
    # ice1usb-r01-1's shape: a 150 mil SOIC-8 flash on the 208 mil pads
    pcb = board(tmp_path, footprint(
        "U5", "Package_SO:SOIC-8_5.23x5.23mm_P1.27mm", "W25Q80",
        {"LCSC": "C12345"}, pads=8),
        parts=[{"lcsc": "C12345", "mpn": "W25Q80DVSNIG", "package": "SOIC-8"}])
    viols, facts = run(pcb)
    assert [v["kind"] for v in viols] == ["footprint_package_mismatch"]
    assert viols[0]["severity"] == "error" and viols[0]["refs"] == ["U5"]
    assert facts["judged"] == 1


def test_jlcpn_field_and_value_mismatch(tmp_path):
    # flexgrid-v3-2: "100 uF" with JLCPN C12530, a 2.2 uF 0402, on 0603
    fetch = jlc([row("C12530", "CL05A225MQ5NSNC", "0402",
                     "6.3V 2.2uF X5R ±20% 0402 MLCC")])
    pcb = board(tmp_path, footprint(
        "C9", "Capacitor_SMD:C_0603_1608Metric", "100 uF", {"JLCPN": "C12530"}))
    viols, facts = run(pcb, live=True, fetch=fetch)
    assert sorted(v["kind"] for v in viols) == [
        "footprint_package_mismatch", "lcsc_value_mismatch"]
    assert facts["values_judged"] == 1


def test_value_named_diode_judged_only_when_jlc_agrees(tmp_path):
    # flexgrid-v3-1: 1N5819 (a DO-41 part by that exact name) on SOD-123F
    fp = footprint("D1", "Diode_SMD:D_SOD-123F", "1N5819")
    pcb = board(tmp_path, fp)
    agree = jlc([row("C2480", "1N5819", "DO-41"), row("C8598", "1N5819",
                 "DO-41"), row("C64885", "B5819W", "SOD-123")])
    viols, _ = run(pcb, live=True, fetch=agree)
    assert [(v["kind"], v["severity"]) for v in viols] == [
        ("footprint_package_mismatch", "warning")]
    split = jlc([row("C2480", "1N5819", "DO-41"),
                 row("C9999", "1N5819", "SOD-123")])
    viols, facts = run(pcb, live=True, fetch=split)
    assert viols == [] and facts["judged"] == 0
    assert any("differ" in k for k in facts["not_judged"])


def test_quiet_cases(tmp_path):
    pcb = board(
        tmp_path,
        footprint("R1", "Resistor_SMD:R_0402_1005Metric", "10k",
                  {"LCSC": "C25744"}),
        footprint("U1", "Package_TO_SOT_SMD:SOT-23-5", "AP2112K",
                  {"LCSC": "C51118"}, pads=5),
        footprint("J1", "aiee:USC-C-SMD_USB4085-GF-A", "USB",
                  {"LCSC": "C7095263"}, pads=6, kind=THT, attr="through_hole"),
        footprint("H1", "MountingHole:MountingHole_3.2mm", "MH"),
        parts=[{"lcsc": "C25744", "package": "R0402",
                "description": "10kΩ 62.5mW 0402 Chip Resistor"},
               {"lcsc": "C51118", "package": "SOT-23-5"},
               {"lcsc": "C7095263", "package": "USC-C-SMD_USB4085-GF-A"}])
    viols, facts = run(pcb)
    assert viols == []
    # J1's package name says only "SMD" against THT pads: not judged
    assert facts["judged"] == 2 and facts["values_judged"] == 1
    assert facts["not_judged"] == {"the package names give only a mount": 1,
                                   "no part number on the footprint": 1}


def test_value_with_a_space_before_the_unit(tmp_path):
    pcb = board(tmp_path, footprint(
        "C5", "Capacitor_SMD:C_0402_1005Metric", "100 nF", {"LCSC": "C1525"}),
        footprint("C6", "Capacitor_SMD:C_0402_1005Metric", "100 pF",
                  {"LCSC": "C1525"}),
        parts=[{"lcsc": "C1525", "package": "0402",
                "description": "100nF 16V X7R ±10% 0402 MLCC"}])
    viols, facts = run(pcb)
    assert [v["refs"] for v in viols] == [["C6"]]
    assert facts["values_judged"] == 2


def test_parts_json_refdes_wins_over_a_stale_board_field(tmp_path):
    pcb = board(tmp_path, footprint(
        "R42", "Resistor_SMD:R_0402_1005Metric", "100k", {"LCSC": "C25744"}),
        parts=[{"lcsc": "C25744", "package": "R0402", "refdes": ["R1"],
                "description": "10kΩ 0402"},
               {"lcsc": "C25741", "package": "R0402", "refdes": ["R42"],
                "description": "100kΩ 0402"}])
    viols, facts = run(pcb)
    assert viols == [] and facts["values_judged"] == 1


def test_offline_names_what_it_could_not_judge(tmp_path):
    pcb = board(tmp_path, footprint("D1", "Diode_SMD:D_SOD-123F", "1N5819"),
                footprint("C1", "Capacitor_SMD:C_0603_1608Metric", "1u",
                          {"LCSC": "C99999999"}))
    viols, facts = run(pcb)
    assert viols == [] and facts["judged"] == 0
    assert set(facts["not_judged"]) == {
        "an MPN is looked up only with --live",
        "no record of the code (--live asks JLC)"}


def test_live_answers_are_cached(tmp_path):
    fetch = jlc([row("C2480", "1N5819", "DO-41")])
    pcb = board(tmp_path, footprint("D1", "Diode_SMD:D_SOD-123F", "1N5819"))
    run(pcb, live=True, fetch=fetch, cache=tmp_path / "c")
    run(pcb, live=True, fetch=fetch, cache=tmp_path / "c")
    assert fetch.asked == ["1N5819"]


def test_cli_exit_codes(tmp_path):
    pcb = board(tmp_path, footprint("C9", "Capacitor_SMD:C_0603_1608Metric",
                                    "2.2u", {"LCSC": "C12530"}),
                parts=[{"lcsc": "C12530", "package": "0402"}])
    assert cfp.main(["--pcb", str(pcb), "--cache", "",
                     "--out", str(tmp_path / "r.json")]) == 1
    assert cfp.main(["--pcb", str(tmp_path / "none.kicad_pcb")]) == 2


def test_registered():
    assert "check_fp_package" in [c["name"] for c in verify_all.load_checks()]


def test_greek_mu_and_melf_0204(tmp_path):
    # echoplayer writes "10 μF" with the Greek mu; glasgow's MELF-0204
    # parts sit on MiniMELF footprints, the same 0204 body
    pcb = board(
        tmp_path,
        footprint("C1", "Capacitor_SMD:C_0805_2012Metric", "10 μF",
                  {"LCSC": "C15850"}),
        footprint("R34", "Resistor_SMD:R_MiniMELF_MMA-0204", "1k",
                  {"LCSC": "C123"}),
        parts=[{"lcsc": "C15850", "package": "0805",
                "description": "10uF 25V X5R 0805 MLCC"},
               {"lcsc": "C123", "package": "MELF-0204",
                "description": "1kΩ MELF resistor"}])
    viols, facts = run(pcb)
    assert viols == []
    assert facts["judged"] == 2 and facts["values_judged"] == 2


def test_value_name_in_another_mount_not_judged(tmp_path):
    # sizif: BAT85 is a DO-35 part; JLC stocks only the LL-34 one
    pcb = board(tmp_path, footprint(
        "D4", "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal", "BAT85",
        kind=THT, attr="through_hole"))
    fetch = jlc([row("C82531", "BAT85", "LL-34")])
    viols, facts = run(pcb, live=True, fetch=fetch)
    assert viols == [] and facts["judged"] == 0
