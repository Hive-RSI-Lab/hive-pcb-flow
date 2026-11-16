"""A symbol drawn as several units is placed as all of them (#378).

A dual op-amp or quad gate is one part drawn as two or more units. The
board generators placed unit 1 only, so the other units' pins were in no
net: KiCad's ERC said missing_unit and the footprint's pads for them
floated. lib_pull now flags such a symbol when it pulls it
(fplib.symbol_units), schlib's Sheet.add_component places every unit (each
pad wired on its own unit), and Sheet.save refuses a sheet that lacks one.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import fplib  # noqa: E402
import schlib  # noqa: E402

LM358 = "Amplifier_Operational:LM358"
LM358_PINS = {"1": "OUTA", "2": "INA-", "3": "INA+", "4": "GND",
              "5": "INB+", "6": "INB-", "7": "OUTB", "8": "VCC"}


def _pin(num, x, y, rot, kind="passive"):
    return (f'(pin {kind} line (at {x} {y} {rot}) (length 2.54) '
            f'(name "P{num}" (effects (font (size 1.27 1.27)))) '
            f'(number "{num}" (effects (font (size 1.27 1.27)))))')


def _props(name):
    return (f'(property "Reference" "U" (id 0) (at 0 6.35 0) (effects (font (size 1.27 1.27))))\n'
            f'(property "Value" "{name}" (id 1) (at 0 -6.35 0) (effects (font (size 1.27 1.27))))\n'
            f'(property "Footprint" "tlib:SOIC8" (id 2) (at 0 0 0) (effects (font (size 1.27 1.27)) hide))\n'
            f'(property "LCSC" "C7950" (id 3) (at 0 0 0) (effects (font (size 1.27 1.27)) hide))')


# DUAL: units 1 and 2 (three pins each), unit 0 the shared supply pins, a
# De Morgan body (style 2) repeating unit 1; DUAL_ALT `extends` it; PART1 is
# one unit only.
SYM = textwrap.dedent(f"""\
    (kicad_symbol_lib (version 20211014) (generator test)
      (symbol "DUAL" (in_bom yes) (on_board yes)
        {_props("DUAL")}
        (symbol "DUAL_0_1"
          {_pin(4, 0, -7.62, 90, "power_in")}
          {_pin(8, 0, 7.62, 270, "power_in")})
        (symbol "DUAL_1_1"
          (rectangle (start -2.54 2.54) (end 2.54 -2.54) (stroke (width 0)) (fill (type none)))
          {_pin(3, -5.08, 2.54, 0, "input")}
          {_pin(2, -5.08, -2.54, 0, "input")}
          {_pin(1, 5.08, 0, 180, "output")})
        (symbol "DUAL_1_2"
          {_pin(3, -5.08, 2.54, 0, "input")}
          {_pin(2, -5.08, -2.54, 0, "input")}
          {_pin(1, 5.08, 0, 180, "output")})
        (symbol "DUAL_2_1"
          (rectangle (start -2.54 2.54) (end 2.54 -2.54) (stroke (width 0)) (fill (type none)))
          {_pin(5, -5.08, 2.54, 0, "input")}
          {_pin(6, -5.08, -2.54, 0, "input")}
          {_pin(7, 5.08, 0, 180, "output")})
      )
      (symbol "DUAL_ALT" (extends "DUAL")
        {_props("DUAL_ALT")})
      (symbol "PART1" (in_bom yes) (on_board yes)
        {_props("PART1")}
        (symbol "PART1_1_1"
          {_pin(1, -5.08, 0, 0)}
          {_pin(2, 5.08, 0, 180)})
      )
    )
    """)

TABLE = textwrap.dedent("""\
    (sym_lib_table
      (version 7)
      (lib (name "tlib")(type "KiCad")(uri "${KIPRJMOD}/../lib/tlib.kicad_sym")(options "")(descr ""))
    )
    """)


def _lib(tmp: Path) -> Path:
    (tmp / "lib").mkdir()
    (tmp / "lib" / "tlib.kicad_sym").write_text(SYM)
    (tmp / "kicad" / "gen").mkdir(parents=True)
    (tmp / "kicad" / "sym-lib-table").write_text(TABLE)
    return tmp / "lib" / "tlib.kicad_sym"


def _symbols(text: str) -> list[tuple[str, int, tuple[float, float]]]:
    """(lib_id, unit, at) of every placed symbol on a saved sheet."""
    out = []
    for m in re.finditer(r'\n\t\(symbol\n\t\t\(lib_id "([^"]+)"\)\n\t\t\(at ([-\d.]+) ([-\d.]+)'
                         r'[^\n]*\n(?:\t\t[^\n]*\n)*?\t\t\(unit (\d+)\)', text):
        out.append((m.group(1), int(m.group(4)),
                    (float(m.group(2)), float(m.group(3)))))
    return out


def _wire_ends(text: str) -> set[tuple[float, float]]:
    pts = set()
    for m in re.finditer(r"\(wire\s+\(pts\s+\(xy ([-\d.]+) ([-\d.]+)\)\s+"
                         r"\(xy ([-\d.]+) ([-\d.]+)\)", text):
        pts.add((round(float(m.group(1)), 2), round(float(m.group(2)), 2)))
        pts.add((round(float(m.group(3)), 2), round(float(m.group(4)), 2)))
    return pts


# --------------------------------------------------------------- the library

def test_symbol_units_reads_units_and_aliases(tmp_path):
    units = fplib.symbol_units(_lib(tmp_path))
    # PART1 is one unit: not listed; the De Morgan body adds no pins
    assert set(units) == {"DUAL", "DUAL_ALT"}
    assert units["DUAL"] == {0: ["4", "8"], 1: ["3", "2", "1"],
                             2: ["5", "6", "7"]}
    assert units["DUAL_ALT"] == units["DUAL"]          # `extends` resolved
    assert fplib.symbol_units(tmp_path / "none.kicad_sym") == {}


def test_lib_pull_flags_a_multi_unit_symbol(tmp_path):
    """The planted fault: a pulled dual is flagged; the quiet twin, a
    single-unit part from the same library, is not."""
    import lib_pull
    sym = _lib(tmp_path)
    results = [{"lcsc": "C7950", "symbol": "DUAL", "status": "pulled"},
               {"lcsc": "C1", "symbol": "PART1", "status": "pulled"},
               {"lcsc": "C2", "symbol": "DUAL_ALT", "status": "failed"}]
    flagged = lib_pull._multi_unit(sym, results)
    assert [(f["symbol"], f["units"]) for f in flagged] == [("DUAL", 2)]
    assert flagged[0]["pins"] == {"0": ["4", "8"], "1": ["3", "2", "1"],
                                  "2": ["5", "6", "7"]}
    assert results[0]["multi_unit"] == 2
    assert any("2 units" in w for w in results[0]["warnings"])
    assert "multi_unit" not in results[1] and "warnings" not in results[1]
    assert "multi_unit" not in results[2]              # not pulled
    assert lib_pull._multi_unit(tmp_path / "none.kicad_sym", results[:1]) == []


# --------------------------------------------------------------- the sheet

def test_every_unit_is_placed_and_wired_on_its_own_unit(tmp_path):
    s = schlib.Sheet("mu")
    s.add_component(LM358, "U1", "LM358", (50.8, 50.8),
                    footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
    s.wire_pins("U1", LM358_PINS)
    anchors = {u: a for u, (_, a) in s._units["U1"].items()}
    assert sorted(anchors) == [1, 2, 3]
    xs = [anchors[u][0] for u in (1, 2, 3)]
    assert xs == sorted(xs) and len(set(xs)) == 3
    assert all(a[1] == 50.8 for a in anchors.values())
    for x in xs:
        schlib.assert_on_grid((x, 50.8), "unit")
    # each pad sits by its own unit's anchor, not unit 1's copy of the spot
    lib = schlib.lib_units(LM358)
    for u, pins in lib.items():
        for num, _, _ in pins:
            px, _ = s.pin_pos("U1", num)
            assert abs(px - anchors[max(u, 1)][0]) < 15, (num, u, px)
    # fit=False: fit_sheet centres the drawing on the paper, moving it all
    path = s.save(tmp_path, project=False, readable=False, fit=False)
    text = path.read_text(encoding="utf-8")
    placed = [(lid, u) for lid, u, _ in _symbols(text) if lid == LM358]
    assert sorted(placed) == [(LM358, 1), (LM358, 2), (LM358, 3)]
    assert text.count('(property "Reference" "U1"') == 3
    assert text.count('(property "Footprint" "Package_SO:SOIC-8') == 3
    assert schlib.missing_units(path) == {}
    # every stub starts on the pin it was wired to
    ends = _wire_ends(text)
    for num in LM358_PINS:
        x, y = s.pin_pos("U1", num)
        assert (round(x, 2), round(y, 2)) in ends, num


def test_a_single_unit_part_is_one_symbol(tmp_path):
    """The quiet twin: a resistor is placed once, as before."""
    s = schlib.Sheet("one")
    s.add_component("Device:R", "R1", "1k", (50.8, 50.8),
                    footprint="Resistor_SMD:R_0603_1608Metric")
    s.wire_pins("R1", {"1": "A", "2": "B"})
    assert "R1" not in s._units
    path = s.save(tmp_path, project=False, readable=False)
    text = path.read_text(encoding="utf-8")
    assert text.count('(property "Reference" "R1"') == 1
    assert schlib.missing_units(path) == {}


def test_a_sheet_with_one_unit_of_a_dual_is_refused(tmp_path):
    """The planted fault: unit 1 alone, placed past add_component."""
    s = schlib.Sheet("half")
    with schlib._quiet():
        s.sch.components.add(LM358, reference="U1", value="LM358",
                             position=(50.8, 50.8))
    with pytest.raises(ValueError, match=r"U1 lacks unit\(s\) 2, 3.*#378"):
        s.save(tmp_path, project=False, readable=False)
    assert schlib.missing_units(tmp_path / "half.kicad_sch") == {"U1": [2, 3]}


def test_unit_at_places_a_unit_where_asked(tmp_path):
    s = schlib.Sheet("at")
    s.add_component(LM358, "U1", "LM358", (50.8, 50.8),
                    unit_at={3: (50.8, 101.6)})
    assert s._units["U1"][3][1] == (50.8, 101.6)
    assert s._units["U1"][2][1][1] == 50.8
    with pytest.raises(ValueError, match="off the 1.27 mm grid"):
        schlib.Sheet("off").add_component(LM358, "U2", "LM358", (50.8, 50.8),
                                          unit_at={2: (51.0, 50.8)})


def test_dnp_marks_every_unit(tmp_path):
    s = schlib.Sheet("dnp")
    s.add_component(LM358, "U1", "LM358", (50.8, 50.8))
    s.add_component("Device:R", "R1", "1k", (50.8, 101.6))
    s.mark_dnp("U1")
    text = s.save(tmp_path, project=False, readable=False).read_text("utf-8")
    assert text.count("(dnp yes)") == 3 and text.count("(dnp no)") == 1


def _unit_instance(unit, lib="Amplifier_Operational:LM358", ref="U1"):
    return (f'\t(symbol\n\t\t(lib_id "{lib}")\n\t\t(at 100 100 0)\n'
            f'\t\t(unit {unit})\n\t\t(in_bom yes)\n\t\t(on_board yes)\n'
            f'\t\t(dnp no)\n\t\t(uuid "u-{unit}")\n'
            f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 1 2 0)\n\t\t)\n\t)\n')


@pytest.mark.parametrize("blocks, ok", [
    ((1, 2, 3), True),                 # one part, three units
    ((1, 1), False),                   # two parts sharing a ref
], ids=["units", "copied-ref"])
def test_native_dnp_takes_units_but_not_a_copied_ref(tmp_path, blocks, ok):
    sch = tmp_path / "d.kicad_sch"
    sch.write_text("(kicad_sch\n" + "".join(_unit_instance(u) for u in blocks)
                   + ")\n", encoding="utf-8")
    if ok:
        schlib.apply_native_dnp(["U1"], sch)
        assert sch.read_text(encoding="utf-8").count("(dnp yes)") == 3
    else:
        with pytest.raises(ValueError, match="not unique"):
            schlib.apply_native_dnp(["U1"], sch)
    # nor is a ref on two different symbols one part
    sch.write_text("(kicad_sch\n" + _unit_instance(1) + _unit_instance(
        2, lib="Device:R") + ")\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not unique"):
        schlib.apply_native_dnp(["U1"], sch)


def test_pin_table_lists_the_units():
    t = schlib.pin_table(LM358)
    assert t["units"] == 3
    by_num = {r["number"]: r["unit"] for r in t["pins"]}
    assert by_num["1"] == 1 and by_num["7"] == 2 and by_num["8"] == 3


def test_a_repeated_sheet_annotates_every_unit(tmp_path):
    ch = schlib.Sheet("ch", pwr_base=1)
    ch.add_component(LM358, "U1", "LM358", (50.8, 50.8))
    ch.wire_pins("U1", {"3": "IN", "1": "OUT"})
    ch.hier_pin("IN", ref="U1", pad="3", shape="input")
    root = schlib.Sheet("rep")
    p = schlib.Project(root)
    p.add_sheet(ch, size=(25.4, 12.7), instances=[
        {"at": (101.6, 50.8), "nets": [("IN", "IN1")]},
        {"at": (101.6, 76.2), "nets": [("IN", "IN2")]}])
    p.save(tmp_path)
    text = (tmp_path / "ch.kicad_sch").read_text(encoding="utf-8")
    got = sorted(re.findall(r'\(reference "(U\d+)"\)\s*\(unit (\d+)\)', text))
    assert got == sorted((r, u) for r in ("U101", "U201") for u in "123")


def test_a_project_library_dual_places_both_units(tmp_path):
    """A lib_pull'd dual (its own library, a unit-0 supply pair, a De
    Morgan body, an `extends` alias): both units placed, the shared supply
    pins wired on unit 1, the alias placed the same way."""
    _lib(tmp_path)
    code = textwrap.dedent("""
        import json, sys, schlib
        s = schlib.Sheet("pl")
        s.add_component("tlib:DUAL", "U1", "x", (50.8, 50.8))
        s.add_component("tlib:DUAL_ALT", "U2", "x", (50.8, 101.6))
        s.add_component("tlib:PART1", "R1", "x", (50.8, 152.4))
        s.wire_pins("U1", {"1": "A", "4": "GND", "7": "B", "8": "VCC"})
        path = s.save(sys.argv[1], project=False, readable=False)
        print(json.dumps({
            "units": {r: sorted(u) for r, u in s._units.items()},
            "pad_unit": s._pad_unit["U1"],
            "pos": {n: s.pin_pos("U1", n) for n in "1478"},
            "anchors": {u: a for u, (_, a) in s._units["U1"].items()},
            "missing": schlib.missing_units(path)}))
        """)
    script = tmp_path / "kicad" / "gen" / "pl.py"
    script.write_text(code)
    env = {**os.environ, "PYTHONPATH": f"{SCRIPTS}:{SCRIPTS / 'lib'}"}
    cp = subprocess.run([sys.executable, str(script), str(tmp_path / "kicad")],
                        cwd=tmp_path / "kicad" / "gen", env=env,
                        capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0, cp.stderr[-1500:]
    import json
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["units"] == {"U1": [1, 2], "U2": [1, 2]}
    assert r["pad_unit"] == {"5": 2, "6": 2, "7": 2}
    assert r["missing"] == {}
    x1, x2 = r["anchors"]["1"][0], r["anchors"]["2"][0]
    assert r["pos"]["1"] == pytest.approx([x1 + 5.08, 50.8])
    assert r["pos"]["7"] == pytest.approx([x2 + 5.08, 50.8])
    assert r["pos"]["4"][0] == x1 and r["pos"]["8"][0] == x1


# --------------------------------------------------------------- live KiCad

def _erc_net(sch: Path, netlist: bool = True):
    import kc
    from lib import env
    cli = env.find_kicad_cli()
    erc = kc.run_erc(cli, sch)
    if not netlist:
        return {v["check"] for v in erc["violations"]}, {}
    import board_init
    net = sch.with_suffix(".net")
    assert kc.export_netlist(cli, sch, net)["status"] == "pass"
    _, netmap = board_init.parse_netlist(net)
    return {v["check"] for v in erc["violations"]}, netmap


@pytest.mark.smoke
def test_smoke_erc_sees_every_unit(tmp_path):
    s = schlib.Sheet("mu")
    s.add_component(LM358, "U1", "LM358", (50.8, 50.8),
                    footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
    s.wire_pins("U1", LM358_PINS)
    checks, nets = _erc_net(s.save(tmp_path / "good"))
    assert "missing_unit" not in checks
    for num, net in LM358_PINS.items():
        assert nets.get(f"U1.{num}", "").lstrip("/") == net, num
    # the planted fault, written past Sheet.save's guard: KiCad agrees
    bad = schlib.Sheet("half")
    with schlib._quiet():
        bad.sch.components.add(LM358, reference="U1", value="LM358",
                               position=(50.8, 50.8))
        (tmp_path / "bad").mkdir()
        bad.sch.save(str(tmp_path / "bad" / "half.kicad_sch"))
    schlib.write_project("half", tmp_path / "bad")
    checks, _ = _erc_net(tmp_path / "bad" / "half.kicad_sch", netlist=False)
    assert "missing_unit" in checks
