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

import case_limits  # noqa: E402
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
    # sold in DO-41 and SOD-123: the footprint fits exactly one, so it is
    # judged and passes
    split = jlc([row("C2480", "1N5819", "DO-41"),
                 row("C9999", "1N5819", "SOD-123")])
    viols, facts = run(pcb, live=True, fetch=split)
    assert viols == [] and facts["judged"] == 1


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
        "a generic name sold in other packages too (--live asks JLC which)",
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


# ------------------------------------------------- MPN ordering codes (#1202)

import mpn_decode  # noqa: E402

SOIC16 = "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm"


@pytest.mark.parametrize("mpn,fpid,pads,hit", [
    # TI package designator: PW is TSSOP 0.65 mm, D the 3.9 mm SOIC
    ("SN74HC595PWR", SOIC16, 16, "pitch"),
    ("SN74HC595DR", SOIC16, 16, None),
    # Microchip -SU is the 8S2 208 mil SOIC, not the 150 mil one
    ("ATTINY85-20SU", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", 8, "208 mil"),
    ("ATTINY85-20SU", "Package_SO:SOIC-8_5.23x5.23mm_P1.27mm", 8, None),
    ("ATTINY85-20PU", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", 8, "through"),
    ("ATTINY85-20PU", "Package_DIP:DIP-8_W7.62mm", 8, None),
    # ST pin-count letter: C is 48 pins (LQFP 7x7), R 64
    ("STM32F103C8T6", "Package_QFP:LQFP-64_10x10mm_P0.5mm", 64, "48 pins"),
    ("STM32F103C8T6", "Package_QFP:LQFP-48_7x7mm_P0.5mm", 48, None),
    ("STM32F103C8U6", "Package_QFP:LQFP-48_7x7mm_P0.5mm", 48, "QFP"),
    # Nexperia: PW TSSOP, D SO
    ("74HC595PW,118", SOIC16, 16, "pitch"),
    ("74HC595D,653", SOIC16, 16, None),
    # PowerPAK 1212-8 is 3.3x3.3, not the 5x6 PowerPAK SO-8
    ("SIS412DN-T1-GE3", "Package_SO:Vishay_PowerPAK_SO-8_Single", 8,
     "POWER-SO8"),
    ("SIS412DN-T1-GE3", "Package_DFN_QFN:Vishay_PowerPAK_1212-8_Single", 8,
     None),
    # Microchip MCP1700 MB is SOT-89, TT SOT-23
    ("MCP1700T-3302E/MB", "Package_TO_SOT_SMD:SOT-23", 3, "SOT-89"),
    ("MCP1700T-3302E/TT", "Package_TO_SOT_SMD:SOT-23", 3, None),
])
def test_mpn_ordering_code_offline(tmp_path, mpn, fpid, pads, hit):
    tht = "DIP" in fpid
    pcb = board(tmp_path, footprint("U1", fpid, "x", {"MPN": mpn},
                                    pads=pads, kind=THT if tht else SMD,
                                    attr="through_hole" if tht else "smd"))
    viols, facts = run(pcb)
    assert facts["judged"] == 1, facts
    if hit is None:
        assert viols == []
    else:
        assert [v["kind"] for v in viols] == ["footprint_package_mismatch"]
        assert viols[0]["severity"] == "error"
        assert hit in viols[0]["msg"], viols[0]["msg"]
        assert "mpn_packages.yaml" in viols[0]["msg"]


def test_mpn_that_does_not_decode_is_not_judged(tmp_path):
    # no entry, or an entry's prefix with a code letter it has no row for
    pcb = board(tmp_path,
                footprint("U1", SOIC16, "x", {"MPN": "XYZ1234ABC"}, pads=16),
                footprint("U2", SOIC16, "x", {"MPN": "SN74HC595QQ"}, pads=16),
                # a little-logic part with a standard-logic letter (TI's DBV
                # cut short): not Nexperia's DB, so not decoded
                footprint("U3", "Package_TO_SOT_SMD:SOT-23-5", "x",
                          {"MPN": "74AHCT1G125DB"}, pads=5))
    viols, facts = run(pcb)
    assert viols == [] and facts["judged"] == 0
    assert facts["not_judged"] == {"an MPN is looked up only with --live": 3}


def test_another_makers_mpn_is_not_decoded(tmp_path):
    fp = footprint("U1", SOIC16, "x", {"MPN": "74HC595PW,118",
                                       "Manufacturer": "Toshiba"}, pads=16)
    viols, facts = run(board(tmp_path, fp))
    assert viols == [] and facts["judged"] == 0
    fp = footprint("U1", SOIC16, "x", {"MPN": "74HC595PW,118",
                                       "Manufacturer": "Nexperia"}, pads=16)
    viols, _ = run(board(tmp_path, fp))
    assert [v["kind"] for v in viols] == ["footprint_package_mismatch"]


def test_value_named_part_decoded_is_a_warning(tmp_path):
    pcb = board(tmp_path, footprint(
        "U1", "Package_QFP:LQFP-64_10x10mm_P0.5mm", "STM32F103C8T6", pads=64))
    viols, _ = run(pcb)
    assert [(v["kind"], v["severity"]) for v in viols] == [
        ("footprint_package_mismatch", "warning")]
    pcb = board(tmp_path, footprint(
        "U1", "Package_QFP:LQFP-48_7x7mm_P0.5mm", "STM32F103C8T6", pads=48))
    assert run(pcb)[0] == []


def test_code_without_a_readable_package_falls_back_to_the_mpn(tmp_path):
    # the LCSC code has no record offline; the MPN field still decodes
    fp = footprint("U1", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "x",
                   {"LCSC": "C89852", "MPN": "ATTINY85-20SU"}, pads=8)
    viols, facts = run(board(tmp_path, fp))
    assert [v["kind"] for v in viols] == ["footprint_package_mismatch"]
    assert "MPN field" in facts["parts"][0]["from"]
    # the record's own model decodes when its package name is not read
    pcb = board(tmp_path, footprint(
        "U1", "Package_SO:SOIC-8_5.23x5.23mm_P1.27mm", "x",
        {"LCSC": "C89852"}, pads=8),
        parts=[{"lcsc": "C89852", "mpn": "ATTINY85-20SU", "package": "?"}])
    viols, facts = run(pcb)
    assert viols == [] and facts["judged"] == 1


def test_mpn_table_rows_are_cited_and_regexes_compile():
    entries = mpn_decode.load()
    assert entries
    for e in entries:
        assert e["verified"] is True, e["name"]
        rows = [e.get("row")] + [r for g in (e.get("groups") or {}).values()
                                 for r in g.values()] + (e.get("combos") or [])
        for r in filter(None, rows):
            for c in r.get("cite") or []:
                assert c["doc"] and c["page"] and c["quote"] and c["sha256"]
            fam = r.get("family")
            assert fam is None or fam in {
                f for f, *_ in pkgname._FAMILIES} | {"NOLEAD"}, fam


@pytest.mark.parametrize("part,fp,hit", [
    ("SOT-563", "Package_TO_SOT_SMD:SOT-23-6", "SOT-563"),
    ("SOT-563", "Package_TO_SOT_SMD:SOT-563", None),
    ("PowerPAK 1212-8", "Package_SO:Vishay_PowerPAK_SO-8_Single", "NOLEAD"),
    ("PowerPAK 1212-8", "Package_DFN_QFN:Vishay_PowerPAK_1212-8_Single",
     None),
])
def test_pkgname_sot563_and_powerpak1212(part, fp, hit):
    why = pkgname.compare(pkgname.describe(part), pkgname.describe(fp), None)
    assert (why == []) if hit is None else (hit in " ".join(why)), why


# ------------------------------------------------- value against the case

CAP = "Capacitor_SMD:C_{}_{}Metric"
RES = "Resistor_SMD:R_{}_{}Metric"


@pytest.mark.parametrize("ref,fpid,value,props,hit", [
    # a capacitor over its case's largest part, and its quiet twin
    ("C1", CAP.format("0402", "1005"), "100uF", {}, True),
    ("C1", CAP.format("0402", "1005"), "22uF", {}, False),
    # the voltage column: 10 uF is sold in 0402 at 10 V, not at 25 V
    ("C2", CAP.format("0402", "1005"), "10uF 25V", {}, True),
    ("C2", CAP.format("0402", "1005"), "10uF 10V", {}, False),
    # the dielectric: X7R 0402 tops out well below X5R
    ("C3", CAP.format("0402", "1005"), "10uF",
     {"Dielectric": "X7R", "Voltage": "16V"}, True),
    ("C3", CAP.format("0402", "1005"), "10uF",
     {"Dielectric": "X5R", "Voltage": "6.3V"}, False),
    # a resistor's stated power over its case's rating, and its twin
    ("R1", RES.format("0603", "1608"), "10k 1W", {}, True),
    ("R1", RES.format("0603", "1608"), "10k", {"Power": "1/4W"}, False),
])
def test_passive_case_limit(tmp_path, ref, fpid, value, props, hit):
    viols, facts = run(board(tmp_path, footprint(ref, fpid, value, props)))
    assert facts["case_limits_judged"] == 1
    got = [v for v in viols if v["kind"] == "passive_case_limit"]
    assert len(got) == (1 if hit else 0), viols
    if hit:
        assert got[0]["severity"] == "warning"
        assert "reference/case_limits.yaml" in got[0]["msg"]
        assert ("Yageo" in got[0]["msg"]) and (" p" in got[0]["msg"])


@pytest.mark.parametrize("ref,fpid,value,props", [
    ("C1", "Capacitor_SMD:CP_Elec_6.3x5.4", "100uF", {}),     # electrolytic
    ("C1", "Capacitor_Tantalum_SMD:CP_EIA-3216-18_Kemet-A", "100uF", {}),
    ("C1", CAP.format("0402", "1005"), "100uF Y5V", {}),      # no chart rows
    ("C1", "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm", "100uF", {}),
    ("R1", RES.format("0603", "1608"), "10k", {}),            # no power stated
])
def test_passive_case_limit_not_judged(tmp_path, ref, fpid, value, props):
    viols, facts = run(board(tmp_path, footprint(ref, fpid, value, props)))
    assert facts["case_limits_judged"] == 0
    assert not [v for v in viols if v["kind"] == "passive_case_limit"]


def test_case_limit_table_is_cited():
    data = case_limits.load()
    assert data["caps"] and data["res"]
    for *_, cite in data["caps"]:
        assert cite["doc"] and cite["page"] and cite["quote"] \
            and cite["sha256"]
    for case, rows in data["res"].items():
        assert all(w > 0 and c["page"] for w, c in rows), case


# ------------------------------- flash and diode makers, generic names (#1206)

SO8N = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
SO8W = "Package_SO:SOIC-8_5.23x5.23mm_P1.27mm"


@pytest.mark.parametrize("mpn,fpid,pads,hit", [
    # Winbond SN is the 150 mil SOIC-8, SS the 208 mil one
    ("W25Q80DVSNIG", SO8W, 8, "150 mil"),
    ("W25Q80DVSNIG", SO8N, 8, None),
    ("W25Q32JVSSIQ", SO8N, 8, "208 mil"),
    ("W25Q32JVSSIQ", SO8W, 8, None),
    ("W25Q128JVSIQ", SO8N, 8, "208 mil"),
    # GigaDevice T is SOP8 150 mil, S 208 mil
    ("GD25Q16CTIG", SO8W, 8, "150 mil"),
    ("GD25Q16CTIG", SO8N, 8, None),
    # Macronix M2I is the 208 mil SOP
    ("MX25L3233FM2I-08G", SO8N, 8, "208 mil"),
    ("MX25L3233FM2I-08G", SO8W, 8, None),
    # ISSI B is the 208 mil SOIC
    ("IS25LP064A-JBLE", SO8N, 8, "208 mil"),
    ("IS25LP064A-JBLE", SO8W, 8, None),
    # Diodes Inc: B140B is the SMB body, B140 the SMA
    ("B140B-13-F", "Diode_SMD:D_SMA", 2, "SMB"),
    ("B140B-13-F", "Diode_SMD:D_SMB", 2, None),
    # 1N5819HW is the SOD-123 body, not the SOD-323 one
    ("1N5819HW-7-F", "Diode_SMD:D_SOD-323", 2, "SOD-123"),
    ("1N5819HW-7-F", "Diode_SMD:D_SOD-123", 2, None),
])
def test_flash_and_diode_ordering_codes(tmp_path, mpn, fpid, pads, hit):
    pcb = board(tmp_path, footprint("U1", fpid, "x", {"MPN": mpn},
                                    pads=pads))
    viols, facts = run(pcb)
    assert facts["judged"] == 1, facts
    if hit is None:
        assert viols == []
    else:
        assert [v["kind"] for v in viols] == ["footprint_package_mismatch"]
        assert hit in viols[0]["msg"], viols[0]["msg"]


def test_width_mil_rows_decode_to_mm():
    assert mpn_decode.decode("W25Q80DVSNIG")["desc"]["width"] == \
        pytest.approx(3.81)
    assert mpn_decode.decode("W25Q80DVSSIG")["desc"]["width"] == \
        pytest.approx(5.28)
    # a package letter the table has no row for is not decoded
    assert mpn_decode.decode("W25Q80DVSVIG") is None


def test_generic_jedec_name_offline(tmp_path):
    # 1N4007 is registered DO-41 but sold in SMA and SOD-123FL too: offline
    # a DO-41 footprint fits the one package known; anything else is not
    # judged, as another maker's body may fit
    pcb = board(tmp_path, footprint(
        "D1", "Diode_THT:D_DO-41_SOD81_P10.16mm_Horizontal", "1N4007",
        kind=THT, attr="through_hole"))
    viols, facts = run(pcb)
    assert viols == [] and facts["judged"] == 1
    pcb = board(tmp_path, footprint("D1", "Diode_SMD:D_SOD-123", "1N4007"))
    viols, facts = run(pcb)
    assert viols == [] and facts["judged"] == 0
    assert mpn_decode.decode("1N4007")["open"] is True


def test_generic_name_live_candidates(tmp_path):
    rows = [row("C2480", "1N5819", "DO-41"), row("C8598", "1N5819", "SMA")]
    # flexgrid D1: SOD-123F fits neither DO-41 nor SMA
    pcb = board(tmp_path, footprint("D1", "Diode_SMD:D_SOD-123F", "1N5819"))
    viols, facts = run(pcb, live=True, fetch=jlc(rows))
    assert [(v["kind"], v["severity"]) for v in viols] == [
        ("footprint_package_mismatch", "warning")]
    assert "DO-41" in viols[0]["msg"] and "SMA" in viols[0]["msg"]
    # its twin: SMA fits exactly one
    pcb = board(tmp_path, footprint("D1", "Diode_SMD:D_SMA", "1N5819"))
    viols, facts = run(pcb, live=True, fetch=jlc(rows))
    assert viols == [] and facts["judged"] == 1
    # a footprint that fits two of the packages sold is not judged
    rows2 = [row("C1", "1N5819", "SOD-123"), row("C2", "1N5819", "SOD-123F")]
    pcb = board(tmp_path, footprint("D1", "Diode_SMD:D_SOD-123F", "1N5819"))
    viols, facts = run(pcb, live=True, fetch=jlc(rows2))
    assert viols == [] and facts["judged"] == 0
    assert any("more than one" in k for k in facts["not_judged"])


def test_smaf_body_is_read_so_a_1n4007_is_judged(tmp_path):
    # flexgrid-v3 D4: JLC sells 1N4007 in DO-41, SMA, SMAF and SOD-123FL.
    # SMAF was taken for an SMA coax jack, so the name was never judged.
    assert pkgname.describe("SMAF")["family"] == "SMA"
    assert pkgname.describe("SMA-F")["family"] is None  # the coax jack
    rows = [row("C1", "1N4007", "DO-41"), row("C2", "1N4007", "SMA"),
            row("C3", "1N4007", "SMAF"), row("C4", "1N4007", "SOD-123FL")]
    # the fault: SOD-323 fits none of them
    pcb = board(tmp_path, footprint("D4", "Diode_SMD:D_SOD-323", "1N4007"))
    viols, facts = run(pcb, live=True, fetch=jlc(rows))
    assert [v["kind"] for v in viols] == ["footprint_package_mismatch"]
    assert "SOD-123FL" in viols[0]["msg"]
    # its twin: SOD-123F is the SOD-123FL land, the one body that fits
    pcb = board(tmp_path, footprint("D4", "Diode_SMD:D_SOD-123F", "1N4007"))
    viols, facts = run(pcb, live=True, fetch=jlc(rows))
    assert viols == [] and facts["judged"] == 1


def test_value_cut_short_of_its_ordering_code_is_not_judged(tmp_path):
    # #1211: TPS2552D is TPS2552DBVR with its ending dropped, not TI's D
    # SOIC; the full code on the same footprint is judged and passes
    sot = "Package_TO_SOT_SMD:SOT-23-6"
    pcb = board(tmp_path, footprint("U1", sot, "TPS2552D", pads=6))
    viols, facts = run(pcb)
    assert viols == [] and facts["judged"] == 0
    assert "the Value may be an ordering code cut short" in facts["not_judged"]
    assert mpn_decode.decode("TPS2552D")["cut"] is True
    pcb = board(tmp_path, footprint("U1", sot, "TPS2552DBVR", pads=6))
    viols, facts = run(pcb)
    assert viols == [] and facts["judged"] == 1
    # an MPN field naming the D part is still judged: TI sells SN74HC595D
    pcb = board(tmp_path, footprint("U1", SOIC16, "x",
                                    {"MPN": "SN74HC595D"}, pads=16))
    viols, facts = run(pcb)
    assert viols == [] and facts["judged"] == 1
