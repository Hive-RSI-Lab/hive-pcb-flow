"""A check that judged nothing, or judged against the wrong rule, must not
read as a pass (#590, #591; the eda-crossover cold review's cases)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from shapely.geometry import Point

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "tests"))
import check_decoupling  # noqa: E402
import check_mating  # noqa: E402
import check_pdn  # noqa: E402
import check_reset_drive  # noqa: E402
import check_straps  # noqa: E402
import check_usb_phy  # noqa: E402
import dfm_check  # noqa: E402
import gerblib  # noqa: E402
import matinglib  # noqa: E402
import verify_all  # noqa: E402
from test_antenna_straps import _by_kind, _netlist  # noqa: E402
from test_fab import RULES_2L, write_drill, write_gerber  # noqa: E402
from test_mating import _board, _fp, _pad, usbc  # noqa: E402


# ------------------------------------------------------------- RP2040 straps

def _rp(tmp_path, testen="GND", qspi="/QSPI_SS", comps=None, nets=None):
    c = {"U1": "RP2040", "U2": "W25Q128", "R1": "10k", **(comps or {})}
    n = {"+3V3": [("U1", "1", "IOVDD_1"), ("R1", "1", "")],
         "GND": [("U1", "57", "GND_57")],
         qspi: [("U1", "56", "QSPI_~{CS}_56"), ("U2", "1", "~{CS}_1")],
         "/R": [("R1", "2", "")]}
    n.setdefault(testen, []).append(("U1", "19", "TESTEN_19"))
    for name, nodes in (nets or {}).items():
        n.setdefault(name, []).extend(nodes)
    return _netlist(tmp_path, c, n)


def _straps(pcb, tmp_path):
    return check_straps.run(["--pcb", str(pcb), "--netlist",
                             str(tmp_path / "s.net")])[0]


def _drive(pcb, tmp_path):
    return check_reset_drive.run(["--pcb", str(pcb), "--netlist",
                                  str(tmp_path / "s.net")])[0]


def test_rp2040_is_known_and_its_straps_are_read(tmp_path):
    """eda-crossover: mcus was [] and both checks passed."""
    p = _straps(_rp(tmp_path), tmp_path)
    assert p["checked"][0]["mcus"] == ["U1"]
    assert "nothing_to_judge" not in p
    got = {s["strap"]: s for s in p["checked"][0]["straps"]}
    assert set(got) == {"QSPI_SS", "TESTEN"}      # QSPI_~{CS} reached by alias
    assert "low" in got["TESTEN"]["por"] and "high" in got["QSPI_SS"]["por"]
    assert "strap_level" not in _by_kind(p)
    assert "nothing_to_judge" not in _drive(_rp(tmp_path), tmp_path)


def test_rp2040_testen_not_grounded_fails(tmp_path):
    """Planted: TESTEN on the rail, the datasheet says tie it to GND."""
    k = _by_kind(_straps(_rp(tmp_path, testen="+3V3"), tmp_path))
    (v,) = k["strap_level"]
    assert v["strap"] == "TESTEN" and v["severity"] == "error"


def test_rp2040_flash_select_tied_low_fails(tmp_path):
    """Planted: the chip select grounded, so the ROM never boots the flash."""
    pcb = _rp(tmp_path, qspi="GND")
    (v,) = _by_kind(_straps(pcb, tmp_path))["strap_level"]
    assert v["strap"] == "QSPI_SS" and "boot ROM" in v["msg"]


def test_rp2040_gpio_pull_down_turns_an_active_low_load_on(tmp_path):
    """Planted: a ~{EN} on a GPIO: the chip's pull-down enables it at reset."""
    pcb = _rp(tmp_path, comps={"U3": "TPS22918"},
              nets={"/LOAD": [("U1", "2", "GPIO0_2"), ("U3", "3", "~{EN}_3")]})
    (v,) = _by_kind(_straps(pcb, tmp_path))["load_on_at_reset"]
    assert v["pin"] == "IO0" and v["refs"] == ["U1", "U3"]


def test_rp2040_gpio_leaving_the_board_is_not_a_finding(tmp_path):
    """Every GPIO has the pull-down: a breakout's 26 pins aren't 26 findings."""
    pcb = _rp(tmp_path, comps={"J1": "Conn"},
              nets={"/A": [("U1", "2", "GPIO0_2"), ("J1", "1", "1_1")]})
    assert "reset_pull_offboard" not in _by_kind(_straps(pcb, tmp_path))


def test_rp2040_row_matches_its_pad_table():
    """Known answers: RP2040 datasheet Reset State column and IO table."""
    row = next(r for r in check_straps.load_kb()["mcus"]
               if r["family"] == "RP2040")
    assert row["verified"] is True
    assert {f"IO{i}" for i in range(30)} <= set(row["reset_pulls"])
    assert all(row["reset_pulls"][f"IO{i}"]["pull"] == "down"
               for i in range(30))
    assert row["reset_pulls"]["SWCLK"]["pull"] == "up"
    assert row["straps"]["QSPI_SS"]["pull"] == "up"
    assert row["straps"]["TESTEN"]["pull"] == "down"
    assert row["vih"] * row["vdd"] == pytest.approx(2.0, abs=0.01)
    assert row["vil"] * row["vdd"] == pytest.approx(0.8, abs=0.01)
    assert row["pull_ohm"] == 65000            # the middle of 50-80 k


# -------------------------------------------------------- nothing to judge

def test_unknown_mcu_is_said_and_is_not_a_pass(tmp_path):
    pcb = _netlist(tmp_path, {"U1": "ATSAMD21G18A-AU"},
                   {"+3V3": [("U1", "1", "VDD_1")]})
    p = _straps(pcb, tmp_path)
    (v,) = _by_kind(p)["mcu_unknown"]
    assert v["refs"] == ["U1"] and v["severity"] == "warning"
    assert "U1 (ATSAMD21G18A-AU)" in p["nothing_to_judge"]
    d = _drive(pcb, tmp_path)
    assert d["status"] == "pass" and "U1 (ATSAMD21G18A-AU)" in d["nothing_to_judge"]


def test_no_mcu_is_nothing_to_judge(tmp_path):
    pcb = _netlist(tmp_path, {"U1": "LM358"},
                   {"+3V3": [("U1", "1", "VCC_1")]})
    for p in (_straps(pcb, tmp_path), _drive(pcb, tmp_path)):
        assert p["status"] == "pass"
        assert p["nothing_to_judge"] == "no MCU on the board"


def test_no_netlist_is_nothing_to_judge(tmp_path):
    pcb = _netlist(tmp_path, {}, {})
    (tmp_path / "s.net").unlink()
    for mod in (check_straps, check_reset_drive, check_usb_phy):
        p = mod.run(["--pcb", str(pcb)])[0]
        assert "no netlist" in p["nothing_to_judge"], mod.__name__


def _result(name, *severities, status=None, **kw):
    vs = [{"severity": s, "source": name} for s in severities]
    return {"name": name, "status": status or ("violations" if vs else "pass"),
            "counts": {"total": len(vs)}, "report": None, "violations": vs,
            **kw}


def test_coverage_says_nothing_to_judge_instead_of_passed():
    res = [_result("a"),
           _result("b", nothing_to_judge="no MCU on the board"),
           _result("c", "warning", nothing_to_judge="no row for U1"),
           _result("d", "error", nothing_to_judge="odd"),
           _result("e", not_judged=["J2 (X)"])]
    m = verify_all.merge("b.kicad_pcb", res)
    cov = m["coverage"]
    assert cov["passed"] == ["a", "e"] and cov["failed"] == ["d"]
    assert cov["ran"] == ["a", "b", "c", "d", "e"]
    assert cov["nothing_to_judge"] == {"b": "no MCU on the board",
                                       "c": "no row for U1"}
    assert cov["not_judged"] == {"e": ["J2 (X)"]}
    assert m["checks"]["b"]["status"] == "nothing_to_judge"
    assert m["checks"]["b"]["reason"] == "no MCU on the board"
    assert m["checks"]["c"]["status"] == "violations"
    assert m["status"] == "violations"           # c and d's findings


def test_strict_does_not_fail_a_board_with_nothing_to_judge():
    m = verify_all.merge("b.kicad_pcb",
                         [_result("b", nothing_to_judge="no MCU")], strict=True)
    assert m["status"] == "pass" and m["coverage"]["passed"] == []


def test_verify_all_reads_it_from_the_check(tmp_path):
    pcb = _netlist(tmp_path, {"U1": "LM358"}, {"+3V3": [("U1", "1", "VCC_1")]})
    check = next(c for c in verify_all.CHECKS if c["name"] == "check_straps")
    r = verify_all.run_one(check, {"pcb": str(pcb)}, tmp_path)
    assert r["status"] == "pass"
    assert r["nothing_to_judge"] == "no MCU on the board"


# ------------------------------------------------------------- check_pdn

class _Caps:
    """A geometry stand-in: nets, and each cap's footprint value; every cap
    sits across RAW and GND, and those in `dnp` are marked do-not-populate."""
    nets = {"RAW", "GND"}

    def __init__(self, values, dnp=()):
        self.footprints = {r: {"value": v,
                               "attrs": ("smd", "dnp") if r in dnp else ("smd",)}
                           for r, v in values.items()}

    def pads_of(self, ref=None, net=None):
        return [NS(net="RAW"), NS(net="GND")]


def test_pdn_reads_the_value_off_the_footprint():
    """eda-crossover's decoupling.json has no values: total_uf read 0.0 and
    RAW's 10 uF cap read 'no bulk reservoir'."""
    assocs = [{"ic": "U1", "pin": "1", "cap": "C7", "rail": "RAW"}]
    vs, facts = check_pdn.check_rail(_Caps({"C7": "10uF"}), "RAW", 0.35, assocs)
    assert vs == []
    assert facts["bulk_count"] == 1 and facts["total_uf"] == 10.0


def test_pdn_still_flags_a_rail_of_ceramics():
    """Planted: the same association, a 100 nF footprint."""
    assocs = [{"ic": "U1", "pin": "1", "cap": "C7", "rail": "RAW"}]
    vs, facts = check_pdn.check_rail(_Caps({"C7": "100nF"}), "RAW", 0.35, assocs)
    assert [v["kind"] for v in vs] == ["pdn_no_bulk"]
    assert facts["total_uf"] == pytest.approx(0.1)


def test_pdn_association_value_wins_over_the_footprint():
    assocs = [{"ic": "U1", "pin": "1", "cap": "C7", "rail": "RAW",
               "value": "22uF"}]
    _, facts = check_pdn.check_rail(_Caps({"C7": "100nF"}), "RAW", 0.35, assocs)
    assert facts["total_uf"] == 22.0


def test_pdn_unread_value_is_said_not_called_no_bulk():
    assocs = [{"ic": "U1", "pin": "1", "cap": "C7", "rail": "RAW"},
              {"ic": "U1", "pin": "2", "cap": "C8", "rail": "RAW"}]
    vs, facts = check_pdn.check_rail(_Caps({"C7": "CAP", "C8": "100nF"}),
                                     "RAW", 0.35, assocs)
    assert [v["kind"] for v in vs] == ["pdn_value_unread"]
    assert vs[0]["refs"] == ["C7"] and facts["caps_unread"] == ["C7"]
    # a readable bulk cap answers the question whatever else is unread
    vs, _ = check_pdn.check_rail(_Caps({"C7": "CAP", "C8": "10uF"}),
                                 "RAW", 0.35, assocs)
    assert vs == []


def test_pdn_board_bulk_cap_joins_the_recorded_ceramics():
    """The IC's 100 nF is recorded against its pin; the rail's 10 uF by its
    connector or regulator is on the board and not in decoupling.json. Read
    as 'no bulk reservoir' until the board's caps joined the recorded ones."""
    assocs = [{"ic": "U1", "pin": "1", "cap": "C7", "rail": "RAW"}]
    vs, facts = check_pdn.check_rail(_Caps({"C7": "100nF", "C9": "10uF"}),
                                     "RAW", 0.35, assocs)
    assert vs == [] and facts["caps_from_board"] == ["C9"]
    assert facts["cap_count"] == 2 and facts["bulk_count"] == 1
    assert facts["total_uf"] == pytest.approx(10.1)


def test_pdn_board_ceramic_is_still_no_bulk():
    assocs = [{"ic": "U1", "pin": "1", "cap": "C7", "rail": "RAW"}]
    vs, facts = check_pdn.check_rail(_Caps({"C7": "100nF", "C9": "100nF"}),
                                     "RAW", 0.35, assocs)
    assert [v["kind"] for v in vs] == ["pdn_no_bulk"]
    assert vs[0]["refs"] == ["C7", "C9"] and facts["caps_from_board"] == ["C9"]


def test_pdn_dnp_board_cap_is_no_reservoir():
    """A contingency footprint (a second bulk cap, DNP: first-spin.md) ships
    empty; counting it would pass a rail that has only ceramics."""
    assocs = [{"ic": "U1", "pin": "1", "cap": "C7", "rail": "RAW"}]
    vs, facts = check_pdn.check_rail(
        _Caps({"C7": "100nF", "C9": "10uF"}, dnp={"C9"}), "RAW", 0.35, assocs)
    assert [v["kind"] for v in vs] == ["pdn_no_bulk"]
    assert facts["caps_from_board"] == [] and facts["bulk_count"] == 0


def test_pdn_reads_dnp_off_the_board_file(tmp_path):
    """geom carries KiCad's (attr smd dnp): the fitted cap counts, the DNP
    one beside it doesn't."""
    def cap(ref, x, attr):
        return (f'  (footprint "t:C_0805" (layer "F.Cu") (at {x} 10)\n'
                f'    (property "Reference" "{ref}" (at 0 0 0))\n'
                f'    (property "Value" "10uF" (at 0 0 0))\n'
                f'    (attr {attr})\n'
                f'    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu")'
                f' (net "V"))\n'
                f'    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu")'
                f' (net "GND")))\n')
    pcb = tmp_path / "pdn.kicad_pcb"
    pcb.write_text(f"""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 40 20) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{cap("C1", 10, "smd")}{cap("C2", 20, "smd dnp")})
""", encoding="utf-8")
    import geom
    bg = geom.load_board(pcb)
    assert bg.footprints["C2"]["attrs"] == ("smd", "dnp")
    assert bg.footprints["C1"]["attrs"] == ("smd",)
    vs, facts = check_pdn.check_rail(bg, "V", 0.1, [])
    assert vs == [] and facts["caps_from_board"] == ["C1"]


def test_decoupling_class_reads_the_footprint_value_too():
    """The wrong rule: an unread 10 uF judged at the 'mid' class's limits."""
    a = {"ic": "U1", "pin": "1", "cap": "C7", "rail": "RAW"}
    f = check_decoupling.parse_farads(
        check_decoupling.association_value(_Caps({"C7": "10uF"}), a))
    assert check_decoupling.value_class(f) == "bulk"
    assert check_decoupling.value_class(
        check_decoupling.parse_farads(a.get("value"))) == "mid"


def test_pdn_with_no_rail_is_nothing_to_judge(tmp_path):
    pcb = _netlist(tmp_path, {}, {})
    con, dec = tmp_path / "c.json", tmp_path / "d.json"
    con.write_text(json.dumps({"power": [{"net": "GND", "current_a": 1},
                                         {"net": "V", "pdn": False}]}))
    dec.write_text(json.dumps({"associations": []}))
    p, _ = check_pdn.run(["--pcb", str(pcb), "--constraints", str(con),
                          "--decoupling", str(dec)])
    assert p["status"] == "pass" and "no power rail" in p["nothing_to_judge"]


# --------------------------------------------------------------- USB jumpers

RP = ("RP2040", ("47", "USB_DP_47"), ("46", "USB_DM_46"))


def _usb(tmp_path, jumper=None, breakout=False, r="27"):
    """J1 D+/D- to an RP2040 through series R. jumper: a 2-pad part in line
    on both lines (JP1/JP2); breakout: JP14's 4 solder pads on the line."""
    comps = {"J1": "USB_micro", "U1": RP[0], "R12": r, "R13": r}
    nets = {"+3V3": [("U1", "1", "VDD_1")],
            "GND": [("U1", "57", "GND_57"), ("J1", "5", "GND_5")],
            "VBUS": [("J1", "1", "VBUS_1")],
            "/DP_U": [("R13", "2", ""), ("U1", *RP[1])],
            "/DM_U": [("R12", "2", ""), ("U1", *RP[2])]}
    for line, jpin, ref, tag in (("D+", "3", "R13", "DP"),
                                 ("D-", "2", "R12", "DM")):
        net = f"/USB_{tag}"
        nets[net] = [("J1", jpin, f"{line}_{jpin}")]
        if jumper:
            jp = f"JP{jpin}"
            comps[jp] = jumper
            nets[net].append((jp, "1", "1"))
            nets[net + "_J"] = [(jp, "2", "2"), (ref, "1", "")]
        else:
            nets[net].append((ref, "1", ""))
        if breakout:
            comps["JP14"] = "USB_SOLDER_PADS"
            nets[net].append(("JP14", line, f"{jpin}_{line}"))
    if breakout:
        nets["VBUS"].append(("JP14", "VBUS", "1_VBUS"))
        nets["GND"].append(("JP14", "GND", "4_GND"))
    pcb = _netlist(tmp_path, comps, nets)
    return check_usb_phy.run(["--pcb", str(pcb), "--netlist",
                              str(tmp_path / "s.net")])[0]


def test_usb_solder_pads_on_the_line_are_not_a_phy(tmp_path):
    """eda-crossover: JP14 read as an unknown IC on D+ (usb_phy_unknown)."""
    p = _usb(tmp_path, breakout=True)
    assert p["violations"] == []
    (phy,) = p["checked"][0]["phys"]
    assert phy["series"] == {"D+": ["R13"], "D-": ["R12"]}


def test_usb_walks_through_a_solder_jumper_to_the_phy(tmp_path):
    p = _usb(tmp_path, jumper="SolderJumper_2_Bridged")
    assert p["violations"] == []
    (phy,) = p["checked"][0]["phys"]
    assert phy["series"]["D+"] == ["R13"] and phy["jumpers"] == ["JP3"]


def test_usb_wrong_series_r_behind_a_jumper_is_judged(tmp_path):
    """Planted: 100 ohm behind the jumper. It used to stop at the jumper."""
    p = _usb(tmp_path, jumper="SolderJumper_2_Bridged", r="100")
    kinds = {v["kind"] for v in p["violations"]}
    assert kinds == {"usb_series_r_value"}


def test_usb_unreadable_resistor_goes_in_not_judged(tmp_path):
    p = _usb(tmp_path, r="RES")
    assert any("R13 (RES)" in s for s in p["not_judged"])


def test_usb_with_no_connector_is_nothing_to_judge(tmp_path):
    pcb = _netlist(tmp_path, {"U1": "LM358"}, {"V": [("U1", "1", "VCC_1")]})
    p = check_usb_phy.run(["--pcb", str(pcb), "--netlist",
                           str(tmp_path / "s.net")])[0]
    assert p["status"] == "pass" and "USB-named" in p["nothing_to_judge"]


# ----------------------------------------------------------- second mate

def _jst(ref, x, y, angle=0.0):
    """A side-entry JST-SH, as SparkFun draws it: contacts at the back
    (-y here), mouth toward +y, two mechanical pads forward of them."""
    pads = "".join(_pad(n, -1.5 + i, -1.0, 0.6, 1.5)
                   for i, n in enumerate("1234"))
    pads += _pad("NC1", -3.0, 1.0, 1.2, 1.8) + _pad("NC2", 3.0, 1.0, 1.2, 1.8)
    return _fp(ref, "JST04_1MM_RA", x, y, angle, pads=pads,
               cy=(-3.6, -2.0, 3.6, 2.0))


def _mating(tmp_path, body):
    pcb = _board(tmp_path, body)
    (tmp_path / "constraints.json").write_text("{}")
    return check_mating.run(["--pcb", str(pcb)])[0]


def _kinds(p):
    return sorted(v["kind"] for v in p["violations"])


def test_side_entry_jst_sh_is_judged_at_the_edge(tmp_path):
    """eda-crossover: J5 was judged, J2 (the Qwiic socket) never was."""
    p = _mating(tmp_path, _jst("J2", 15.0, 17.7) + usbc("J5", 15.0, 3.0, 180))
    assert {c["ref"] for c in p["connectors"]} >= {"J2"}
    assert "J2" not in " ".join(v["refs"][0] for v in p["violations"])
    fact = next(c for c in p["connectors"] if c["ref"] == "J2")
    assert fact["family"] == "wire_to_board_side" and fact["mouth"] == "+y"


def test_side_entry_jst_sh_facing_inward_fails(tmp_path):
    """Planted: the same socket turned round, mouth into the board."""
    p = _mating(tmp_path, _jst("J2", 15.0, 17.7, 180.0))
    assert "mating_faces_inward" in _kinds(p)


def test_courtyard_margin_past_the_edge_beside_the_mouth_is_no_block(
        tmp_path, monkeypatch):
    """eda-crossover: S1/S2 flank J2, their courtyards 0.05 mm over the edge."""
    sw = _fp("S1", "SW_TACT", 21.0, 18.0, cy=(-1.5, -2.0, 1.5, 2.05))
    p = _mating(tmp_path, _jst("J2", 15.0, 17.7) + sw)
    assert "J2" in {c["ref"] for c in p["connectors"]}
    assert "mating_zone_blocked" not in _kinds(p)
    # the tolerance is what lets it through
    monkeypatch.setattr(matinglib, "GRIP_EDGE_TOL_MM", 0.0)
    assert "mating_zone_blocked" in _kinds(
        _mating(tmp_path, _jst("J2", 15.0, 17.7) + sw))


def test_part_well_past_the_edge_beside_the_mouth_blocks(tmp_path):
    """Planted: the same switch 1 mm over the edge, where the fingers go."""
    sw = _fp("S1", "SW_TACT", 21.0, 18.0, cy=(-1.5, -2.0, 1.5, 3.0))
    p = _mating(tmp_path, _jst("J2", 15.0, 17.7) + sw)
    assert "mating_zone_blocked" in _kinds(p)


def test_part_straight_in_front_of_the_mouth_still_blocks(tmp_path):
    """Planted: inside the tolerance, but in the plug's own path."""
    sw = _fp("S1", "SW_TACT", 15.0, 20.2, cy=(-1.5, -0.1, 1.5, 0.05))
    p = _mating(tmp_path, _jst("J2", 15.0, 17.7) + sw)
    assert "mating_zone_blocked" in _kinds(p)


def test_flanking_courtyard_across_the_mouth_margin_is_no_block(tmp_path):
    """eda-crossover: S1/S2's courtyards reach 0.12 mm across J2's own width
    on the ledge in front of it. Planted: 1 mm across is in the plug's way."""
    edge_sw = "SW_TACT"
    near = _fp("S1", edge_sw, 20.0, 18.0, cy=(-1.38, -1.0, 1.0, 2.05))
    assert "mating_zone_blocked" not in _kinds(
        _mating(tmp_path, _jst("J2", 15.0, 17.7) + near))
    far = _fp("S1", edge_sw, 20.0, 18.0, cy=(-2.5, -1.0, 1.0, 2.05))
    assert "mating_zone_blocked" in _kinds(
        _mating(tmp_path, _jst("J2", 15.0, 17.7) + far))


@pytest.mark.parametrize("name,family", [
    ("USC-C-SMD_USB4085-GF-A", "usb_c"),       # hifi core's J1, EasyEDA's typo
    ("TF-SMD_TF-PUSH", "card_slot"),           # linux-computer's J401
    ("JST04_1MM_RA", "wire_to_board_side"),    # eda-crossover's J2
])
def test_family_names_that_were_silently_unknown(name, family):
    assert matinglib.family_of(f"lib:{name}")["family"] == family


def test_flash_chip_select_is_a_known_input(tmp_path):
    """eda-crossover: U2's W25Q on the RP2040's select is no unknown driver."""
    k = _by_kind(_straps(_rp(tmp_path), tmp_path))
    assert "strap_unknown" not in k


def test_connector_no_family_knows_is_listed_not_passed(tmp_path):
    p = _mating(tmp_path, usbc("J5", 15.0, 3.0, 180)
                + _fp("J9", "WEIRD_SOCKET", 5.0, 10.0)
                + _fp("R1", "R0603", 9.0, 10.0))
    assert p["not_judged"] == ["J9 (WEIRD_SOCKET)"]
    assert "nothing_to_judge" not in p


def test_board_with_no_known_connector_is_nothing_to_judge(tmp_path):
    p = _mating(tmp_path, _fp("J9", "WEIRD_SOCKET", 5.0, 10.0))
    assert p["status"] == "pass" and p["nothing_to_judge"]
    assert p["not_judged"] == ["J9 (WEIRD_SOCKET)"]


# ---------------------------------------------------- component hole ring

R_4L = {"min_annular_ring_mm": 0.1, "abs_min_annular_ring_mm": 0.05,
        "min_pth_annular_ring_mm": 0.15}
H = (5.0, 5.0)
HOLE = NS(x=H[0], y=H[1], diameter=0.85, plated=True)


def _pad_disc(d):
    return Point(*H).buffer(d / 2.0, 64)


def _stack(layers):
    """{layer: (pads, pours)} as gerblib's FabStack shows them."""
    lg = {n: NS(pads=p, pours=q) for n, (p, q) in layers.items()}
    return NS(holes=[HOLE], copper_layer_names=lambda: list(lg),
              copper=lg.get)


def _ring(layers, rules=R_4L):
    vios: list = []
    dfm_check.check_annular_ring(_stack(layers), rules, vios)
    return vios


def _crossover(inner_d):
    """J3/J4 on eda-crossover: a custom SMD pad over the hole on both outer
    layers (ring 0.35 there), only the round barrel pad inside."""
    outer = [_pad_disc(1.016), _pad_disc(1.55)]
    return {"F.Cu": (outer, []), "In1.Cu": ([_pad_disc(inner_d)], []),
            "In2.Cu": ([_pad_disc(inner_d)], []), "B.Cu": (outer, [])}


def test_thin_inner_ring_under_an_outer_smd_pad_fails():
    """#590's case: 0.85 hole, 1.016 pad, 0.083 ring, 0.15 is the rule."""
    (v,) = _ring(_crossover(1.016))
    assert v["kind"] == "dfm_annular_ring" and v["severity"] == "error"
    assert v["hole"] == "PTH pad" and v["min_mm"] == 0.15
    assert v["ring_mm"] == pytest.approx(0.083, abs=1e-3)
    assert v["layer"] == "In1.Cu"


def test_the_boards_fix_passes():
    """eda-crossover's fix: 1.25 mm pads, a 0.2 ring."""
    assert _ring(_crossover(1.25)) == []


def test_two_layers_with_the_outer_pad_stay_fine():
    """No inner copper: the outer copper really is there."""
    assert _ring({n: ([_pad_disc(1.016), _pad_disc(1.55)], [])
                  for n in ("F.Cu", "B.Cu")}) == []


def test_thin_inner_pad_in_a_wide_pour_passes_and_in_a_thin_one_fails():
    plane = Point(*H).buffer(5.0)
    assert _ring({"F.Cu": ([_pad_disc(1.25)], []),
                  "In1.Cu": ([_pad_disc(1.016)], [plane])}) == []
    # Planted: a pour that doesn't reach round the pad
    sliver = Point(H[0] + 5.0, H[1]).buffer(4.6)
    (v,) = _ring({"F.Cu": ([_pad_disc(1.25)], []),
                  "In1.Cu": ([_pad_disc(1.016)], [sliver])})
    assert v["layer"] == "In1.Cu"


def test_no_inner_pad_is_not_judged_on_that_layer():
    """An inner layer the barrel has no pad on has no ring to hold."""
    assert _ring({"F.Cu": ([_pad_disc(1.25)], []), "In1.Cu": ([], []),
                  "B.Cu": ([_pad_disc(1.25)], [])}) == []


def test_real_gerbers_inner_ring_is_read(tmp_path):
    """End to end through gerblib: the inner layer's flash is measured."""
    d = tmp_path / "fab"
    d.mkdir()
    write_gerber(d / "s-F_Cu.gtl", flashes=[(1.55, 5.0, -5.0)])
    write_gerber(d / "s-In1_Cu.g1", flashes=[(1.016, 5.0, -5.0)])
    write_gerber(d / "s-In2_Cu.g2", flashes=[(1.016, 5.0, -5.0)])
    write_gerber(d / "s-B_Cu.gbl", flashes=[(1.55, 5.0, -5.0)])
    write_drill(d / "s.drl", holes=[(0.85, 5.0, -5.0)])
    fab = gerblib.open_fab(d)
    vios: list = []
    dfm_check.check_annular_ring(fab, RULES_2L | {
        "min_pth_annular_ring_mm": 0.15}, vios)
    (v,) = vios
    assert v["layer"] == "In1.Cu"
    assert v["ring_mm"] == pytest.approx(0.083, abs=5e-3)


# ------------------------------------- the survey's cheap ones: zero judged

@pytest.mark.parametrize("name,extra,reason", [
    ("check_model_paths", [], "no 3D model"),
    ("check_model_fit", [], "no 3D model"),
    ("check_courtyard", [], "courtyard"),
    ("check_si", [], "signal class"),
    ("check_diffpair", [], "differential pair"),
    ("check_cap_load", [], "op-amp"),
    ("check_rail_budget", [], "minimum supply"),
    ("check_antenna", [], "rf_modules"),
    ("check_route_style", [], "no tracks"),
    ("check_silk", [], "no silk"),
    ("check_vias", [], "no vias"),
    ("check_isolation", [], "isolation"),
    ("check_surge", [], "surge"),
    ("check_esd", [], "no external connector"),
    ("check_rf_match", [], "rf_match"),
    ("check_ddr_timing", [], "ddr"),
    ("check_length_groups", [], "length_groups"),
    ("check_net_tie", [], "net tie"),
])
def test_an_empty_board_is_nothing_to_judge(tmp_path, name, extra, reason):
    """Each of these read as passed on a board with nothing for it to see."""
    import importlib
    mod = importlib.import_module(name)
    pcb = _netlist(tmp_path, {}, {})
    (tmp_path / "s.net").unlink()
    p = mod.run(["--pcb", str(pcb), *extra])[0]
    assert p["status"] == "pass"
    assert reason in p["nothing_to_judge"]


def test_a_board_with_something_to_judge_is_not_nothing(tmp_path):
    """Planted the other way: the same check on a board with a track."""
    import check_route_style
    pcb = _board(tmp_path, '  (segment (start 1 1) (end 5 1) (width 0.2) '
                           '(layer "F.Cu") (net 0))\n')
    p = check_route_style.run(["--pcb", str(pcb)])[0]
    assert "nothing_to_judge" not in p


def test_gerber_td_of_an_unset_attribute_reads():
    """KiCad 10 writes `%TD.AperFunction*%` with no .AperFunction set (minuet
    v3's F.Cu); the file still reads, and a bad attribute still raises."""
    head = "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.100000*%\n"
    ok = head + "%TD.AperFunction*%\nD10*\nX0Y0D03*\nM02*\n"
    g = gerblib.GerberFile.from_string(ok)
    assert len(g.objects) == 1
    with pytest.raises(SyntaxError):
        gerblib.GerberFile.from_string(
            head + "%TF.Part,Single*%\n%TD.Part*%\nM02*\n")
