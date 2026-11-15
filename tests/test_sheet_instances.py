"""#563: one channel sheet instanced N times (schlib.Project.add_sheet
instances=), and board_init carrying each footprint's place in the
hierarchy ((path), (sheetname), (sheetfile)) from the netlist.

Hermetic tests cover the numbering, the guards and the netlist/board
readers; the smoke test is the generator round trip the issue asks for:
3 instances -> ERC clean, 3 sheetpaths sharing symbol uuids, refs per
instance, a board_init board with a path on every part and parity 0, and
the same refs and paths when the generator runs again.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import board_init  # noqa: E402
import replicatelib  # noqa: E402
import schlib  # noqa: E402


# ------------------------------------------------------------ numbering

def test_instance_ref_numbers_per_block():
    p = schlib.Project(schlib.Sheet("rep"))
    assert p.instance_ref("R1", 100) == "R101"
    assert p.instance_ref("C12", 300) == "C312"
    assert p.instance_ref("#PWR9001", 200) == "#PWR29001"
    assert p.instance_ref("#FLG01", 100) == "#FLG10001"


def test_instance_ref_overflow_raises():
    """Planted fault: R100 in a 100-wide range would become the next
    block's R200 (or collide with it); it must raise, not renumber."""
    p = schlib.Project(schlib.Sheet("rep"))
    assert p.instance_ref("R99", 100) == "R199"        # quiet twin
    with pytest.raises(ValueError, match="ref_step"):
        p.instance_ref("R100", 100)
    with pytest.raises(ValueError, match="no trailing number"):
        p.instance_ref("TP", 100)


# ------------------------------------------------------------ guards

def _proj():
    root = schlib.Sheet("rep")
    return schlib.Project(root), schlib.Sheet("ch")


def test_repeated_sheet_without_ref_base_raises():
    """Planted fault: the same child placed twice with no numbering would
    give two blocks of identical refs; the old add_sheet allowed it."""
    p, ch = _proj()
    p.add_sheet(ch, at=(101.6, 50.8), size=(25.4, 12.7))
    with pytest.raises(ValueError, match="without ref_base"):
        p.add_sheet(ch, at=(101.6, 76.2), size=(25.4, 12.7), name="ch2")


def test_instances_place_n_blocks_one_file():
    p, ch = _proj()
    p.add_sheet(ch, size=(25.4, 12.7), instances=[
        {"at": (101.6, 50.8)}, {"at": (101.6, 76.2)},
        {"at": (101.6, 101.6), "name": "right", "ref_base": 700}])
    assert p.children == [ch]                       # one child file
    assert p.instances == {"ch": [("ch1", 100), ("ch2", 200),
                                  ("right", 700)]}
    for n in ("ch1", "ch2", "right"):
        assert p.root.sch.sheets.get_sheet_by_name(n) is not None


@pytest.mark.parametrize("bad, match", [
    ([{"at": (101.6, 50.8), "ref_base": 100},
      {"at": (101.6, 76.2), "ref_base": 100}], "already used"),
    ([{"at": (101.6, 50.8), "name": "a"},
      {"at": (101.6, 76.2), "name": "a"}], "used twice"),
    ([{"at": (101.6, 50.8), "ref_base": 150}], "multiple of ref_step"),
    ([{"at": (101.6, 50.8), "colour": "red"}], "unknown keys"),
    ([], "empty"),
])
def test_instances_bad_entries_raise(bad, match):
    p, ch = _proj()
    with pytest.raises(ValueError, match=match):
        p.add_sheet(ch, size=(25.4, 12.7), instances=bad)


def test_instances_beside_at_raises():
    p, ch = _proj()
    with pytest.raises(ValueError, match="per entry"):
        p.add_sheet(ch, at=(101.6, 50.8), size=(25.4, 12.7),
                    instances=[{"at": (101.6, 76.2)}])


def test_decoupling_follows_instance_refs():
    p, ch = _proj()
    ch.decoupling.append({"cap": "C1", "ic": "U1", "pin": "4",
                          "rail": "+3V3", "value": "100n"})
    p.add_sheet(ch, size=(25.4, 12.7), instances=[
        {"at": (101.6, 50.8)}, {"at": (101.6, 76.2)}])
    assert [(a["cap"], a["ic"]) for a in p.decoupling] == [
        ("C101", "U101"), ("C201", "U201")]


# ------------------------------------------------------------ board side

NET = """(export (version "E")
 (components
  (comp (ref "J1") (value "x") (footprint "Lib:J")
   (property (name "Sheetname") (value "rep"))
   (property (name "Sheetfile") (value "rep.kicad_sch"))
   (sheetpath (names "/") (tstamps "/"))
   (tstamps "aaaaaaaa-0000-0000-0000-000000000001"))
  (comp (ref "R201") (value "1k") (footprint "Lib:R")
   (property (name "Sheetname") (value "ch2"))
   (property (name "Sheetfile") (value "ch.kicad_sch"))
   (sheetpath (names "/ch2/") (tstamps "/bbbbbbbb-0000-0000-0000-000000000002/"))
   (tstamps "cccccccc-0000-0000-0000-000000000003"))
  (comp (ref "R9") (value "1k") (footprint "Lib:R")))
 (nets))
"""


def test_parse_netlist_carries_sheet_path(tmp_path):
    f = tmp_path / "a.net"
    f.write_text(NET, encoding="utf-8")
    comps, _ = board_init.parse_netlist(f)
    by = {c["ref"]: c for c in comps}
    assert by["J1"]["path"] == "/aaaaaaaa-0000-0000-0000-000000000001"
    assert by["J1"]["sheetname"] == "/"
    assert by["J1"]["sheetfile"] == "rep.kicad_sch"
    assert by["R201"]["path"] == ("/bbbbbbbb-0000-0000-0000-000000000002/"
                                  "cccccccc-0000-0000-0000-000000000003")
    assert by["R201"]["sheetname"] == "/ch2/"
    assert by["R201"]["sheetfile"] == "ch.kicad_sch"
    # quiet twin: a hand-written netlist without tstamps gets no path
    assert "path" not in by["R9"]


def _fp(ref, path, sheetname):
    return (f'\t(footprint "Lib:R" (layer "F.Cu") (property "Reference" '
            f'"{ref}") (path "{path}") (sheetname "{sheetname}") '
            f'(sheetfile "ch.kicad_sch"))\n')


@pytest.mark.parametrize("sheetname", ["/ch2/", "ch2"])
def test_idents_from_board_reads_kicad_sheetname(tmp_path, sheetname):
    """KiCad writes (sheetname "/ch2/"); the reader made that "//ch2//"
    and matched no sheet. A bare "ch2" (older boards) still reads."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108)\n" + _fp(
        "R201", "/bbbb/cccc", sheetname) + ")\n", encoding="utf-8")
    idents = replicatelib.idents_from_board(pcb)
    assert idents["R201"].sheet == "/ch2/"


# ------------------------------------------------------------ smoke (live)

def _gen(out: Path) -> schlib.Project:
    ch = schlib.Sheet("ch", pwr_base=1)
    ch.add_component("Device:R", "R1", "1k", at=(50.8, 50.8),
                     footprint="Resistor_SMD:R_0603_1608Metric")
    ch.add_component("Device:C", "C1", "10n", at=(63.5, 50.8),
                     footprint="Capacitor_SMD:C_0603_1608Metric")
    ch.wire_pins("R1", {"2": "MID"})
    ch.wire_pins("C1", {"1": "MID", "2": "GND"})
    ch.hier_pin("IN", ref="R1", pad="1", shape="passive")
    ch.power_symbol_at_pin("C1", "2", "power:GND")
    root = schlib.Sheet("rep")
    root.add_component(
        "Connector_Generic:Conn_01x04", "J1", "x", at=(25.4, 76.2),
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical")
    root.wire_pins("J1", {"1": "IN1", "2": "IN2", "3": "IN3", "4": "GND"})
    root.power_symbol_at_pin("J1", "4", "power:GND")
    root.power_flag("GND", at=(25.4, 101.6), sym="power:GND")
    p = schlib.Project(root)
    p.add_sheet(ch, size=(25.4, 12.7), instances=[
        {"at": (101.6, 50.8 + 25.4 * k), "nets": [("IN", f"IN{k + 1}")]}
        for k in range(3)])
    p.save(out)
    return p


def _comps(net: Path) -> dict:
    """ref -> (sheet names, sheet tstamps, symbol uuid) from a netlist."""
    comps, _ = board_init.parse_netlist(net)
    out = {}
    for c in comps:
        stamps, _, sym = c["path"].rpartition("/")
        out[c["ref"]] = (c["sheetname"], stamps + "/", sym)
    return out


@pytest.mark.smoke
def test_smoke_three_instances_round_trip(tmp_path):
    import kc
    from lib import env
    cli = env.find_kicad_cli()
    out = tmp_path / "kicad"
    _gen(out)
    sch = out / "rep.kicad_sch"
    assert sorted(f.name for f in out.glob("*.kicad_sch")) == [
        "ch.kicad_sch", "rep.kicad_sch"]               # ONE child file
    erc = kc.run_erc(cli, sch)
    assert erc["counts"]["total"] == 0, erc
    net = out / "rep.net"
    assert kc.export_netlist(cli, sch, net)["status"] == "pass"
    comps = _comps(net)
    assert sorted(comps) == ["C101", "C201", "C301", "J1",
                             "R101", "R201", "R301"]
    sheets = {comps[f"R{k}01"][0] for k in (1, 2, 3)}
    assert sheets == {"/ch1/", "/ch2/", "/ch3/"}
    # 3 sheetpaths, the parts share their symbol uuids across them
    assert len({comps[f"R{k}01"][1] for k in (1, 2, 3)}) == 3
    assert len({comps[f"R{k}01"][2] for k in (1, 2, 3)}) == 1
    assert len({comps[f"C{k}01"][2] for k in (1, 2, 3)}) == 1

    rep = tmp_path / "bi.json"
    rc = board_init.main([
        "--netlist", str(net), "--name", "rep", "--out", str(out),
        "--layers", "2", "--mounting-holes", "0",
        "--schematic", str(sch), "--out-report", str(rep)])
    r = json.loads(rep.read_text("utf-8"))
    assert rc == 0 and r["status"] == "pass", r
    assert r["self_check"]["parity_count"] == 0
    pcb = out / "rep.kicad_pcb"
    idents = replicatelib.idents_from_board(pcb)
    assert set(idents) == set(comps)                   # a path on every part
    for ref, (names, stamps, sym) in comps.items():
        assert idents[ref].sheet == names
        assert idents[ref].sym == sym
    assert pcb.read_text("utf-8").count('(sheetfile "ch.kicad_sch")') == 6

    # re-running the generator keeps every ref and path
    again = tmp_path / "again"
    _gen(again)
    assert kc.export_netlist(cli, again / "rep.kicad_sch",
                             again / "rep.net")["status"] == "pass"
    assert _comps(again / "rep.net") == comps
