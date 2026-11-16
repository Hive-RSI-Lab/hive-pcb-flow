"""check_fp_geometry: a footprint's pads against a drawing of its land
pattern (a cited land_patterns row, KiCad's stock footprint of its name,
its own name). Each kind has a planted fault and a quiet twin."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "lib"))
import check_fp_geometry as g  # noqa: E402

# KiCad's SOIC-8_3.9x4.9mm_P1.27mm: pads 1-4 down the left, 5-8 up the right
SOIC = {str(i + 1): (-2.475, -1.905 + 1.27 * i) for i in range(4)}
SOIC.update({str(8 - i): (2.475, -1.905 + 1.27 * i) for i in range(4)})
SOIC_NAME = "SOIC-8_3.9x4.9mm_P1.27mm"


def pad(num, x, y, w=1.95, h=0.6, rot=0.0, drill=None, kind="smd", shape="rect"):
    layers = '"*.Cu" "*.Mask"' if drill else '"F.Cu" "F.Paste" "F.Mask"'
    d = f" (drill {drill})" if drill else ""
    r = f" {rot:g}" if rot else ""
    return (f'\t\t(pad "{num}" {kind} {shape} (at {x:g} {y:g}{r}) '
            f'(size {w:g} {h:g}){d} (layers {layers}))\n')


def crt(side, x0, y0, x1, y1):
    return (f'\t\t(fp_rect (start {x0:g} {y0:g}) (end {x1:g} {y1:g}) '
            f'(stroke (width 0.05) (type solid)) (fill none) (layer "{side}.CrtYd"))\n')


def pads_of(centres, **kw):
    return "".join(pad(k, x, y, **kw) for k, (x, y) in centres.items())


def footprint(ref, fpid, body, at=(50, 50, 0), layer="F.Cu", props=None):
    props = {"Reference": ref, "Value": "X", **(props or {})}
    p = "".join(f'\t\t(property "{k}" "{v}" (at 0 0 0) (layer "F.Fab"))\n'
                for k, v in props.items())
    a = f"{at[0]:g} {at[1]:g}" + (f" {at[2]:g}" if len(at) > 2 and at[2] else "")
    return (f'\t(footprint "{fpid}"\n\t\t(layer "{layer}")\n\t\t(at {a})\n'
            f"{p}{body}\t)\n")


def board(tmp_path, *fps):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb\n\t(version 20240108)\n" + "".join(fps) + ")\n",
                   encoding="utf-8")
    return pcb


@pytest.fixture
def stock(tmp_path):
    d = tmp_path / "stock" / "Package_SO.pretty"
    d.mkdir(parents=True)
    (d / f"{SOIC_NAME}.kicad_mod").write_text(
        f'(footprint "{SOIC_NAME}"\n' + pads_of(SOIC) + crt("F", -3.7, -2.7, 3.7, 2.7)
        + ")\n", encoding="utf-8")
    t = tmp_path / "stock" / "Connector_PinHeader_2.54mm.pretty"
    t.mkdir()
    hdr = {str(i + 1): (0.0, 2.54 * i) for i in range(3)}
    (t / "PinHeader_1x03_P2.54mm_Vertical.kicad_mod").write_text(
        '(footprint "PinHeader_1x03_P2.54mm_Vertical"\n'
        + "".join(pad(k, x, y, 1.7, 1.7, drill=1.0, kind="thru_hole")
                  for k, (x, y) in hdr.items()) + ")\n", encoding="utf-8")
    return g.Stock(tmp_path / "stock")


def kinds(pcb, stock, rows=()):
    v, judged, nj = g.check(pcb, stock=stock, rows=list(rows))
    return sorted((x["refs"][0], x["kind"]) for x in v), judged, nj


SO = f"Package_SO:{SOIC_NAME}"


def test_stock_copy_is_quiet_at_any_turn_and_on_the_bottom(tmp_path, stock):
    flipped = {k: (x, -y) for k, (x, y) in SOIC.items()}  # KiCad's flip
    pcb = board(tmp_path,
                footprint("U1", SO, pads_of(SOIC)),
                footprint("U2", SO, pads_of(SOIC, rot=90), at=(60, 50, 90)),
                footprint("U3", SO, pads_of(flipped), layer="B.Cu"),
                # an origin moved off the pads' centre: only geometry counts
                footprint("U4", SO, pads_of({k: (x + 3, y - 1) for k, (x, y) in SOIC.items()})))
    k, judged, nj = kinds(pcb, stock)
    assert k == [] and judged == {"stock": 4} and nj == {}


def test_pin_order_vs_stock(tmp_path, stock):
    # numbered round the other way: pin 1 top right, 1-4 down the right
    cw = {str(i + 1): (2.475, -1.905 + 1.27 * i) for i in range(4)}
    cw.update({str(8 - i): (-2.475, -1.905 + 1.27 * i) for i in range(4)})
    pcb = board(tmp_path, footprint("U1", SO, pads_of(cw)))
    assert kinds(pcb, stock)[0] == [("U1", "fp_pin_order_vs_stock")]


def test_bottom_footprint_numbered_as_mirror_image_is_caught(tmp_path, stock):
    # stored without KiCad's flip: the part's mirror image on the bottom
    pcb = board(tmp_path, footprint("U1", SO, pads_of(SOIC), layer="B.Cu"))
    assert kinds(pcb, stock)[0] == [("U1", "fp_pin_order_vs_stock")]


def test_row_spacing_vs_stock(tmp_path, stock):
    wide = {k: (x * 1.45, y) for k, (x, y) in SOIC.items()}  # a 5.3 mm SO8's rows
    # an older library's land: shorter pads 0.4 mm further out, each centre
    # still on the stock pad's copper (fc980c U5's 2017 VSSOP-8): quiet
    older = {k: (x + (0.4 if x > 0 else -0.4), y) for k, (x, y) in SOIC.items()}
    pcb = board(tmp_path, footprint("U1", SO, pads_of(wide)),
                footprint("U2", SO, pads_of(older, w=1.2), at=(100, 50)))
    assert kinds(pcb, stock)[0] == [("U1", "fp_pads_vs_stock")]


def test_pad_size_and_numbers_vs_stock(tmp_path, stock):
    small = "".join(pad(k, x, y, w=0.8) for k, (x, y) in SOIC.items())  # 41 % area
    big = "".join(pad(k, x, y, w=2.4, h=0.7) for k, (x, y) in SOIC.items())
    extra = pads_of(SOIC) + pad("9", 0, 0, 2.4, 3.1)
    # an older library's shorter land (62 % area) and a pad stored turned
    # (w and h swapped) are the same land: quiet
    older = "".join(pad(k, x, y, w=1.2) for k, (x, y) in SOIC.items())
    turned = "".join(pad(k, x, y, w=0.6, h=1.95) for k, (x, y) in SOIC.items())
    pcb = board(tmp_path, footprint("U1", SO, small), footprint("U2", SO, big),
                footprint("U3", SO, extra), footprint("U4", SO, older),
                footprint("U5", SO, turned))
    assert kinds(pcb, stock)[0] == [("U1", "fp_pad_small_vs_stock"),
                                    ("U3", "fp_pad_numbers_vs_stock")]


def test_shield_pad_named_otherwise_is_quiet(tmp_path, stock):
    # stock SOIC with an exposed pad "EP"; the board names it 9: a rename
    d = stock.root / "Package_SO.pretty"
    (d / "SOIC-8-1EP_3.9x4.9mm_P1.27mm.kicad_mod").write_text(
        '(footprint "x"\n' + pads_of(SOIC) + pad("EP", 0, 0, 2.4, 3.1) + ")\n",
        encoding="utf-8")
    st = g.Stock(stock.root)
    fpid = "Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm"
    pcb = board(tmp_path,
                footprint("U1", fpid, pads_of(SOIC) + pad("9", 0, 0, 2.4, 3.1)),
                footprint("U2", fpid, pads_of({k: v for k, v in SOIC.items() if k != "3"})
                          + pad("EP", 0, 0, 2.4, 3.1)))
    assert kinds(pcb, st)[0] == [("U2", "fp_pad_numbers_vs_stock")]


def test_drill_vs_stock(tmp_path, stock):
    hdr = {str(i + 1): (0.0, 2.54 * i) for i in range(3)}
    fpid = "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical"

    def th(d):
        return "".join(pad(k, x, y, 1.7, 1.7, drill=d, kind="thru_hole")
                       for k, (x, y) in hdr.items())
    pcb = board(tmp_path, footprint("J1", fpid, th(0.8)), footprint("J2", fpid, th(1.1)))
    assert kinds(pcb, stock)[0] == [("J1", "fp_drill_small_vs_stock")]


ROW = {"part": "SiT1602B 3225", "source": "test drawing", "match": r"^SiT1602B",
       # pins 1 and 2 on one long side, 2.1 mm apart (top view)
       "pads": {"1": [-1.05, 0.825], "2": [1.05, 0.825],
                "3": [1.05, -0.825], "4": [-1.05, -0.825]}}


def _row():
    r = dict(ROW)
    r["_re"] = re.compile(r["match"], re.I)
    return r


def test_land_pattern_row(tmp_path, stock):
    good = {k: tuple(v) for k, v in ROW["pads"].items()}
    # a generic 4-pad crystal land: 1 and 2 on a short side
    swap = {"1": good["1"], "2": good["4"], "3": good["3"], "4": good["2"]}
    turned = {k: (-y, x) for k, (x, y) in good.items()}
    mpn = {"MPN": "SiT1602BI-73-33S-25.000000"}
    pcb = board(tmp_path,
                footprint("Y1", "x:Osc_3225", pads_of(good, w=0.9, h=1.2), props=mpn),
                footprint("Y2", "x:Osc_3225", pads_of(turned, w=0.9, h=1.2), props=mpn),
                footprint("Y3", "x:Osc_3225", pads_of(swap, w=0.9, h=1.2), props=mpn),
                footprint("Y4", "x:Osc_3225", pads_of(swap, w=0.9, h=1.2)))
    k, judged, nj = kinds(pcb, stock, [_row()])
    assert k == [("Y3", "fp_pin_order_vs_drawing")]
    assert judged == {"drawing": 3}
    assert list(nj.values()) == [["Y4"]]  # no MPN, no stock, no pitch: not judged


def test_committed_land_patterns_cite_a_drawing_and_judge_a_part(tmp_path, stock):
    rows = g.load_land_patterns()
    assert rows
    for r in rows:
        assert re.search(r"datasheet", r["source"], re.I) and r["pads"], r["part"]
        assert len(set(map(tuple, r["pads"].values()))) == len(r["pads"]), r["part"]
    (drv,) = [r for r in rows if r["part"].startswith("DRV8874")]
    good = {str(k): tuple(v) for k, v in drv["pads"].items()}
    # numbered clockwise (1-8 up the left): the bottom-view slip
    cw = {str(k): (x, -y) for k, (x, y) in good.items()}
    mpn = {"MPN": "DRV8874PWPR"}
    pcb = board(tmp_path,
                footprint("U5", "x:HTSSOP-16", pads_of(good, w=1.5, h=0.45), props=mpn),
                footprint("U6", "x:HTSSOP-16", pads_of(cw, w=1.5, h=0.45), props=mpn))
    k, judged, _ = kinds(pcb, stock, rows)
    assert k == [("U6", "fp_pin_order_vs_drawing")]
    assert judged["drawing"] == 2


def test_package_rows_judge_unnamed_chips_and_yield_to_a_part_row(tmp_path, stock):
    rows = g.load_land_patterns()
    two = lambda h: pads_of({"1": (-h, 0.0), "2": (h, 0.0)}, w=1.0, h=1.3)
    pcb = board(tmp_path,
                # the EasyEDA R0805 as drawn on the project boards: quiet
                footprint("R1", "lcsc:R0805", two(1.0), props={"Value": "10k 1% 0805"}),
                # a 1206's land under an 0805 name: 0.625 mm per pad off
                footprint("R2", "lcsc:R0805", two(1.6), at=(70, 50)),
                # the asymmetric PowerDI-123, numbered either way round
                footprint("D1", "lcsc:POWERDI-123_L2.8-W1.8-LS3.7-RD",
                          two(1.19), at=(90, 50)),
                # not a package name the rows know: not judged
                footprint("R3", "lcsc:R0805_custom", two(1.0), at=(110, 50)))
    k, judged, nj = kinds(pcb, stock, rows)
    assert k == [("R2", "fp_pads_vs_drawing")] and judged == {"drawing": 3}
    assert sum(nj.values(), []) == ["R3"]
    r = dict(_row(), footprint="R0805")
    r["_re"], r["_fp"] = re.compile(r["match"], re.I), re.compile("R0805")
    pkg = [x for x in rows if x.get("footprint") == "R0805"]
    fp = g.FP("U1", "lcsc:R0805", "top", (0, 0), "F.Cu", {"MPN": "SiT1602BI-x"})
    assert g.land_row(fp, pkg + [r]) is r  # a part row first, whatever the order


EC11 = {"A": (-2.54, 7.25), "B": (2.54, 7.25), "C": (0.0, 7.25),
        "D": (-2.54, -7.25), "E": (2.54, -7.25)}  # ultra-front SW4 as drawn


def _ec11(ref, centres, value, drill=1.2, at=(50, 50)):
    body = "".join(pad(k, x, y, 1.8, 1.8, drill=drill, kind="thru_hole", shape="circle")
                   for k, (x, y) in centres.items())
    return footprint(ref, "aiee:SW-TH_EC11E1820402", body, at=at,
                     props={"Value": value, "LCSC Part": "C361165"})


def _sev(pcb, stock, rows):
    v, _, _ = g.check(pcb, stock=stock, rows=rows)
    return sorted((x["refs"][0], x["kind"], x["severity"], x.get("result")) for x in v)


def test_name_vs_part_judged_by_the_parts_geometry(tmp_path, stock):
    rows = [r for r in g.load_land_patterns() if r["part"].startswith("ALPS EC11E1834403")]
    assert rows
    # planted: a footprint named for the EC11E1820402 whose push-switch
    # pins sit 2 mm off the EC11E1834403's drawing: the land is not the part's
    off = dict(EC11, D=(-2.54, -9.25), E=(2.54, -9.25))
    pcb = board(tmp_path, _ec11("SW1", off, "EC11E1834403"))
    assert _sev(pcb, stock, rows) == [
        ("SW1", "fp_name_vs_part", "warning", "pads differ"),
        ("SW1", "fp_pads_vs_drawing", "error", None)]
    # quiet twin (ultra-front SW4): the No.4 and No.5 drawings share their
    # hole pattern, the 2.54 grid copy of the 2.5 pitch drops each lead in
    # its 1.2 mm hole: the name only differs; a family Value (EC11, core
    # SW1) is no disagreement
    pcb = board(tmp_path, _ec11("SW4", EC11, "EC11E1834403"),
                _ec11("SW5", EC11, "EC11", at=(80, 50)))
    assert _sev(pcb, stock, rows) == [("SW4", "fp_name_vs_part", "info", "name only")]
    # the hole slack is the board's drill over the drawing's hole: drilled
    # at the drawing's 1.0 mm the 0.04 mm pitch slip is a finding, and the
    # name's disagreement with it a warning
    pcb = board(tmp_path, _ec11("SW4", EC11, "EC11E1834403", drill=1.0))
    assert _sev(pcb, stock, rows) == [("SW4", "fp_name_vs_part", "warning", "pads differ"),
                                      ("SW4", "fp_pitch_vs_drawing", "error", None)]
    # nothing of the part's own to judge the pads by: said, as info
    pcb = board(tmp_path, _ec11("SW4", EC11, "EC11E1834403"))
    assert _sev(pcb, stock, []) == [("SW4", "fp_name_vs_part", "info", "not judged")]


def test_name_vs_part_tokens():
    fp = lambda name, **f: g.FP("U1", f"x:{name}", "top", (0, 0), "F.Cu", f)
    assert g.name_vs_part(fp("SW-TH_EC11E1820402", Value="EC11E1834403")) == (
        "EC11E1820402", "EC11E1834403", "Value")
    assert g.name_vs_part(fp("SW-TH_EC11E1820402", Value="EC11")) is None
    assert g.name_vs_part(fp("TPS7A20_SOT-23-5", MPN="TPS7A2033PDBVR")) is None
    assert g.name_vs_part(fp("SOIC-8_3.9x4.9mm_P1.27mm", Value="LM358")) is None
    assert g.name_vs_part(fp("R0805", Value="10k 1% 0805")) is None
    # X stands for any one character in a family name (rf4's U1)
    assert g.name_vs_part(fp("HOPERF_RFM9XW_SMD", Value="RFM95W")) is None
    assert g.name_vs_part(fp("TO-220_LM78XX", Value="LM7805")) is None
    assert g.name_vs_part(fp("SW-TH_EC11E1820402", Value="EC11X1834403")) == (
        "EC11E1820402", "EC11X1834403", "Value")


def test_name_pitch_and_pins(tmp_path, stock):
    tight = {k: (x, y * 0.8) for k, (x, y) in SOIC.items()}  # 1.016 mm pitch
    seven = {k: v for k, v in SOIC.items() if k != "6"}
    pcb = board(tmp_path,
                footprint("U1", "Mine:SOIC-8_P1.27mm_custom", pads_of(SOIC)),
                footprint("U2", "Mine:SOIC-8_P1.27mm_custom", pads_of(tight)),
                footprint("U3", "Mine:SOIC-8_P1.27mm_custom", pads_of(seven)))
    k, judged, _ = kinds(pcb, stock)
    assert k == [("U2", "fp_pitch_vs_name"), ("U3", "fp_pins_vs_name")]
    assert judged == {"name": 3}


def test_courtyard_vs_stock(tmp_path, stock):
    flipped = {k: (x, -y) for k, (x, y) in SOIC.items()}
    moved = {k: (x + 3, y) for k, (x, y) in SOIC.items()}
    pcb = board(tmp_path,
                footprint("U1", SO, pads_of(SOIC) + crt("F", -3.7, -2.7, 3.7, 2.7)),
                footprint("U2", SO, pads_of(SOIC) + crt("F", -4.0, -3.0, 4.0, 3.0)),
                footprint("U3", SO, pads_of(flipped) + crt("B", -3.7, -2.7, 3.7, 2.7),
                          layer="B.Cu"),
                footprint("U4", SO, pads_of(moved) + crt("F", -0.7, -2.7, 6.7, 2.7)),
                # pads-only courtyard: the body's ends are left out, and a
                # resistor sits on the end it leaves out
                footprint("U5", SO, pads_of(SOIC) + crt("F", -3.7, -1.5, 3.7, 1.5),
                          at=(100, 50)),
                footprint("R1", "x:R", pad("1", -0.5, 0, 0.5, 0.5) + pad("2", 0.5, 0, 0.5, 0.5),
                          at=(100, 52.4)),
                # the same short courtyard with nothing in the strip: quiet
                footprint("U6", SO, pads_of(SOIC) + crt("F", -3.7, -1.5, 3.7, 1.5),
                          at=(150, 50)),
                # an older library's courtyard 0.25 mm in: quiet
                footprint("U7", SO, pads_of(SOIC) + crt("F", -3.45, -2.45, 3.45, 2.45),
                          at=(200, 50)),
                footprint("R2", "x:R", pad("1", -0.5, 0, 0.5, 0.5) + pad("2", 0.5, 0, 0.5, 0.5),
                          at=(200, 52.6)))
    k = [x for x in kinds(pcb, stock)[0] if x[0].startswith("U")]
    assert k == [("U5", "fp_courtyard_small_vs_stock")]


def test_courtyard_intruder_found_on_a_turned_part(tmp_path, stock):
    # U1 turned 90 deg: its body's ends lie along board x; R1 on one end
    rot = {k: (x, y) for k, (x, y) in SOIC.items()}
    pcb = board(tmp_path,
                footprint("U1", SO, pads_of(rot, rot=90) + crt("F", -3.7, -1.5, 3.7, 1.5),
                          at=(100, 50, 90)),
                footprint("R1", "x:R", pad("1", -0.5, 0, 0.5, 0.5) + pad("2", 0.5, 0, 0.5, 0.5),
                          at=(102.4, 50)),
                footprint("R2", "x:R", pad("1", -0.5, 0, 0.5, 0.5) + pad("2", 0.5, 0, 0.5, 0.5),
                          at=(100, 52.4)))
    v, _, _ = g.check(pcb, stock=stock, rows=[])
    c = [x for x in v if x["kind"] == "fp_courtyard_small_vs_stock"]
    assert len(c) == 1 and c[0]["intruders"] == ["R1"]


def test_t_shaped_module_courtyard_judged_by_its_outline(tmp_path, stock):
    # KiCad's RF modules: a T of courtyard lines, the body below and a wide
    # antenna keepout above. A copy whose courtyard is the body only, with a
    # resistor beside the body (in the T's box, not in the T): quiet; one in
    # the keepout: caught (minuet U4, ESP32-C3-WROOM-02)
    d = stock.root / "RF_Module.pretty"
    d.mkdir()
    body = {"1": (-4.0, 0.0), "2": (4.0, 0.0), "3": (-4.0, 4.0), "4": (4.0, 4.0)}
    t = [(-10, -12), (10, -12), (10, -4), (5, -4), (5, 7), (-5, 7), (-5, -4), (-10, -4)]
    line = "".join(f'\t\t(fp_line (start {a:g} {b:g}) (end {c:g} {e:g}) (stroke (width 0.05) '
                   f'(type solid)) (layer "F.CrtYd"))\n'
                   for (a, b), (c, e) in zip(t, t[1:] + t[:1]))
    (d / "MOD.kicad_mod").write_text('(footprint "MOD"\n' + pads_of(body) + line + ")\n",
                                     encoding="utf-8")
    st = g.Stock(stock.root)
    r = pad("1", -0.5, 0, 0.5, 0.5) + pad("2", 0.5, 0, 0.5, 0.5)
    own = pads_of(body) + crt("F", -5, -4, 5, 7)
    beside = board(tmp_path, footprint("U1", "Mine:MOD", own, at=(100, 50)),
                   footprint("R1", "x:R", r, at=(108, 54)))
    assert kinds(beside, st)[0] == []
    keep = board(tmp_path, footprint("U1", "Mine:MOD", own, at=(100, 50)),
                 footprint("R1", "x:R", r, at=(108, 54)),
                 footprint("R2", "x:R", r, at=(100, 41)))
    v, _, _ = g.check(keep, stock=st, rows=[])
    c = [x for x in v if x["kind"] == "fp_courtyard_small_vs_stock"]
    assert len(c) == 1 and c[0]["intruders"] == ["R2"]


def test_angled_header_drawn_body_the_other_way_is_quiet(tmp_path, stock):
    # fc980c J1: the stock 1x05 angled header's pads, its body (and so its
    # courtyard) drawn on the other side of the pad row, over the board edge.
    # The pad row is its own mirror image, so this is the stock land; a part
    # where the stock body would be is not in its courtyard. The same
    # courtyard cut to the pads only, with that part beside it: caught.
    d = stock.root / "Connector_PinHeader_2.54mm.pretty"
    fpn = "PinHeader_1x03_P2.54mm_Horizontal"

    def th(xx):
        return "".join(pad(str(i + 1), xx, 2.54 * i, 1.7, 1.7, drill=1.0, kind="thru_hole")
                       for i in range(3))
    (d / f"{fpn}.kicad_mod").write_text(f'(footprint "{fpn}"\n' + th(0) + crt("F", -1.77, -1.77, 10.54, 6.85)
                                        + ")\n", encoding="utf-8")
    st = g.Stock(stock.root)
    r = pad("1", -0.5, 0, 0.5, 0.5) + pad("2", 0.5, 0, 0.5, 0.5)
    J = f"Connector_PinHeader_2.54mm:{fpn}"
    other = board(tmp_path, footprint("J1", J, th(8.2) + crt("F", -2.35, -1.8, 10.0, 6.9), at=(100, 50)),
                  footprint("R1", "x:R", r, at=(114, 52.5)))
    assert kinds(other, st)[0] == []
    cut = board(tmp_path, footprint("J1", J, th(0) + crt("F", -1.77, -1.77, 1.77, 6.85), at=(100, 50)),
                footprint("R1", "x:R", r, at=(106, 52.5)))
    assert kinds(cut, st)[0] == [("J1", "fp_courtyard_small_vs_stock")]


def test_half_turn_symmetric_land_numbered_either_way_is_quiet(tmp_path, stock):
    # a 6 mm tact switch: 1,2 on the left, 2,1 on the right; numbered with
    # the pairs swapped it is the same land turned a half turn
    d = stock.root / "Button_Switch_THT.pretty"
    d.mkdir()
    sw = {"1": (0, 0), "2": (0, 4.5)}
    sw_r = {"1b": (6.5, 0), "2b": (6.5, 4.5)}

    def body(m):
        return "".join(pad(k.rstrip("b"), x, y, 2, 2, drill=1.1, kind="thru_hole")
                       for k, (x, y) in m.items())
    (d / "SW_PUSH_6mm.kicad_mod").write_text(
        '(footprint "SW_PUSH_6mm"\n' + body({**sw, **sw_r}) + ")\n", encoding="utf-8")
    st = g.Stock(stock.root)
    swapped = {"2": (0, 0), "1": (0, 4.5), "2b": (6.5, 0), "1b": (6.5, 4.5)}
    pcb = board(tmp_path, footprint("SW1", "Button_Switch_THT:SW_PUSH_6mm", body(swapped),
                                    layer="B.Cu"))
    assert kinds(pcb, st)[0] == []


def test_connector_mirror_goes_to_mate_check_and_own_library_numbering(tmp_path, stock):
    hdr = {"1": (0, 0), "2": (2.54, 0), "3": (0, 2.54), "4": (2.54, 2.54)}
    d = stock.root / "Connector_PinHeader_2.54mm.pretty"
    fpn = "PinHeader_2x02_P2.54mm_Vertical"

    def th(m):
        return "".join(pad(k, x, y, 1.7, 1.7, drill=1.0, kind="thru_hole") for k, (x, y) in m.items())
    (d / f"{fpn}.kicad_mod").write_text(f'(footprint "{fpn}"\n' + th(hdr) + ")\n",
                                        encoding="utf-8")
    st = g.Stock(stock.root)
    mirror = {k: (-x, y) for k, (x, y) in hdr.items()}
    cw = {str(i + 1): (2.475, -1.905 + 1.27 * i) for i in range(4)}
    cw.update({str(8 - i): (-2.475, -1.905 + 1.27 * i) for i in range(4)})
    pcb = board(tmp_path,
                footprint("J1", f"Connector_PinHeader_2.54mm:{fpn}", th(mirror)),
                footprint("U1", f"Mine:{SOIC_NAME}", pads_of(cw)))
    k, judged, nj = kinds(pcb, st)
    assert k == [] and judged == {}
    assert sorted(r for refs in nj.values() for r in refs) == ["J1", "U1"]


HDR8 = "PinHeader_1x08_P2.54mm_Vertical"
# KiCad's 1x08 header: pad 1 square, the rest round, a chamfered fab
# corner and a silk L by pad 1
HDR8_FAB = ('\t\t(fp_line (start -1.27 -0.635) (end -0.635 -1.27) '
            '(stroke (width 0.1) (type solid)) (layer "F.Fab"))\n'
            '\t\t(fp_line (start -0.635 -1.27) (end 1.27 -1.27) '
            '(stroke (width 0.1) (type solid)) (layer "F.Fab"))\n')


def _hdr8(m, shapes=True, fab=True):
    """A 1x08 header body: pad numbers at the centres in `m`; with
    `shapes`, the pad at the first position (0, 0) is the rect one."""
    return "".join(pad(k, x, y, 1.7, 1.7, drill=1.0, kind="thru_hole",
                       shape="rect" if not shapes or (x, y) == (0.0, 0.0) else "circle")
                   for k, (x, y) in m.items()) + (HDR8_FAB if fab else "")


@pytest.fixture
def stock8(stock):
    hdr = {str(i + 1): (0.0, 2.54 * i) for i in range(8)}
    (stock.root / "Connector_PinHeader_2.54mm.pretty" / f"{HDR8}.kicad_mod").write_text(
        f'(footprint "{HDR8}"\n' + _hdr8(hdr) + ")\n", encoding="utf-8")
    return g.Stock(stock.root), hdr


def test_header_numbered_from_the_marked_pins_other_end_is_caught(tmp_path, stock8):
    """#1253: a symmetric 1xN header with pads 1 and 8 swapped, or numbered
    from the other end, fits the stock pad set; the pin-1 cue (the square
    pad, the fab chamfer) says which end is pin 1."""
    st, hdr = stock8
    swap = dict(hdr)
    swap["1"], swap["8"] = hdr["8"], hdr["1"]
    rev = {str(8 - i): (0.0, 2.54 * i) for i in range(8)}
    cases = {
        "J1": ("aiee", swap, {}),                              # own copy, 1<->8
        "J2": ("Connector_PinHeader_2.54mm", rev, {}),         # stock, reversed
        "J3": ("Connector_PinHeader_2.54mm", rev, {"shapes": False}),  # fab only
        "J4": ("aiee", hdr, {}),                               # quiet twin
        "J5": ("Connector_PinHeader_2.54mm", hdr, {}),         # quiet twin
    }
    fps = [footprint(r, f"{lib}:{HDR8}", _hdr8(m, **kw), at=(10 * i, 50, 90 * i))
           for i, (r, (lib, m, kw)) in enumerate(cases.items())]
    k, judged, nj = kinds(board(tmp_path, *fps), st)
    assert k == [("J1", "fp_pin1_mark_vs_number"), ("J2", "fp_pin1_mark_vs_number"),
                 ("J3", "fp_pin1_mark_vs_number")]
    assert not {"J4", "J5"} & {r for refs in nj.values() for r in refs}
    v, _, _ = g.check(board(tmp_path, fps[0]), stock=st, rows=[])
    assert v[0]["pad"] == "8" and "renumber" in v[0]["msg"]


def test_header_with_no_pin1_mark_is_not_judged_and_cues_must_agree(tmp_path, stock8):
    st, hdr = stock8
    rev = {str(8 - i): (0.0, 2.54 * i) for i in range(8)}
    # the square pad says pad 8 is pin 1, the fab chamfer (moved to the
    # far end) says pad 1: the cues disagree, so no call
    far = _hdr8(rev, fab=False) + (
        '\t\t(fp_line (start -1.27 18.415) (end -0.635 19.05) '
        '(stroke (width 0.1) (type solid)) (layer "F.Fab"))\n')
    plain = "".join(pad(k, x, y, 1.7, 1.7, drill=1.0, kind="thru_hole", shape="circle")
                    for k, (x, y) in rev.items())
    pcb = board(tmp_path,
                footprint("J1", f"Connector_PinHeader_2.54mm:{HDR8}", plain),
                footprint("J2", f"Connector_PinHeader_2.54mm:{HDR8}", far, at=(70, 50)))
    k, judged, nj = kinds(pcb, st)
    assert k == []
    why = {r: w for w, refs in nj.items() for r in refs}
    assert "no pin-1 mark" in why["J1"] and "no pin-1 mark" in why["J2"]


def test_2xn_header_in_stock_order_is_quiet_and_marks_read_from_fab(tmp_path, stock):
    hdr = {}
    for i in range(4):
        hdr[str(2 * i + 1)] = (0.0, 2.54 * i)
        hdr[str(2 * i + 2)] = (2.54, 2.54 * i)
    fpn = "PinHeader_2x04_P2.54mm_Vertical"
    body = _hdr8(hdr)
    (stock.root / "Connector_PinHeader_2.54mm.pretty" / f"{fpn}.kicad_mod").write_text(
        f'(footprint "{fpn}"\n' + body + ")\n", encoding="utf-8")
    st = g.Stock(stock.root)
    pcb = board(tmp_path, footprint("J1", f"Connector_PinHeader_2.54mm:{fpn}", body,
                                    at=(50, 50, 180)))
    k, judged, nj = kinds(pcb, st)
    assert k == [] and nj == {} and judged
    assert g._marked(g.board_footprints(pcb)[0].pads,
                     g.board_footprints(pcb)[0].marks) == "1"


def _model(monkeypatch, pads, package):
    import easyeda
    m = easyeda.Model("C912768", "x", package,
                      [easyeda.Pad(k, x, y, 1.2, 1.7, "RECT") for k, (x, y) in pads.items()])
    monkeypatch.setattr(easyeda, "get", lambda *a, **k: m)


def test_model_compare_planted_and_kelvin_land_and_connector_quiet(tmp_path, stock, monkeypatch):
    # planted: a 1206 drawn with 0402 spacing, 1 mm short of LCSC's model of
    # the part at each end (cpl_verify's tolerance is half the pad gap)
    _model(monkeypatch, {"1": (-1.48, 0.0), "2": (1.48, 0.0)}, "R1206")
    lc = {"LCSC": "C912768"}
    wide = pad("1", -0.48, 0, 0.5, 0.6) + pad("2", 0.48, 0, 0.5, 0.6)
    k, judged, _ = kinds(board(tmp_path, footprint("R1", "Mine:R_1206", wide, props=lc)), stock)
    assert k == [("R1", "fp_pads_vs_model")] and judged == {"model": 1}
    # quiet twin: a Kelvin shunt land (pads 1-4) of the same 2-terminal part
    # is a different land, not a misplaced one (glasgow R90)
    kelvin = (pad("1", -1.46, -0.6, 1.1, 0.6) + pad("1", -1.46, 0.6, 1.1, 0.6)
              + pad("2", -1.46, 0, 1.1, 0.2) + pad("3", 1.46, 0, 1.1, 0.2)
              + pad("4", 1.46, -0.6, 1.1, 0.6) + pad("4", 1.46, 0.6, 1.1, 0.6))
    k, judged, nj = kinds(board(tmp_path, footprint("R2", "Mine:R_Shunt_Alt_1206", kelvin,
                                                    props=lc)), stock)
    assert k == [] and judged == {} and any("Kelvin" in w for w in nj)
    # quiet twin: a stock-named connector with an LCSC model numbered round
    # the body is judged against the stock footprint only (minuet J4)
    _model(monkeypatch, {"1": (0, 0), "2": (0, 5.08), "3": (0, 2.54)}, "HDR")
    hdr = "".join(pad(str(i + 1), 0, 2.54 * i, 1.7, 1.7, drill=1.0, kind="thru_hole")
                  for i in range(3))
    k, judged, _ = kinds(board(tmp_path, footprint(
        "J1", "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical", hdr, props=lc)), stock)
    assert k == [] and judged == {"stock": 1}


HDR = "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical"


def header(step):
    return "".join(pad(str(i + 1), 0, step * i, 1.7, 1.7, drill=1.0, kind="thru_hole")
                   for i in range(3))


def test_short_row_a_pitch_step_off_vs_stock(tmp_path, stock):
    # a 1x3 row at 2.0 mm where the stock is 2.54: its end pads are 0.54 mm
    # off, inside the quarter-pitch pad tolerance (0.635), so only the row's
    # pitch shows it; the stock pitch 0.02 mm off (a rounded copy) is quiet
    pcb = board(tmp_path, footprint("J1", HDR, header(2.0)),
                footprint("J2", HDR, header(2.56), at=(80, 50)),
                footprint("J3", HDR, header(2.54), at=(110, 50)))
    k, judged, _ = kinds(pcb, stock)
    assert k == [("J1", "fp_pitch_vs_stock")] and judged == {"stock": 3}


def test_row_pitch_reads_rows_not_any_two_pads(tmp_path):
    assert g.row_pitch(SOIC) == pytest.approx(1.27)
    # a 2x2 land has no row of three: no pitch to hold it to
    assert g.row_pitch({"1": (0, 0), "2": (1, 0), "3": (0, 1), "4": (1, 1)}) is None
    # rows moved apart (toe length) leave the pitch alone
    assert g.pitch_off(SOIC, {k: (x * 1.2, y) for k, (x, y) in SOIC.items()}) is None
    assert g.pitch_off(SOIC, {k: (x, y * 0.8) for k, (x, y) in SOIC.items()}) == \
        pytest.approx((1.27, 1.016))


def test_easyeda_name_pitch(tmp_path, stock):
    # EasyEDA names state the pitch as -P0.50- (no _P0.50mm)
    name = "lcsc:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL"
    tight = {k: (x, y * 0.8) for k, (x, y) in SOIC.items()}
    pcb = board(tmp_path, footprint("U1", name, pads_of(SOIC)),
                footprint("U2", name, pads_of(tight), at=(80, 50)))
    k, judged, _ = kinds(pcb, stock)
    assert k == [("U2", "fp_pitch_vs_name")] and judged == {"name": 2}



def test_name_pitch_of_a_row_with_its_middle_pin_left_out_is_not_judged(tmp_path, stock):
    # a SOT-23-3 names the 0.95 mm lead pitch, but pins 1 and 2 sit 1.9 mm
    # apart with pin 3 alone across: no row of three holds the pitch, so the
    # name judges nothing (PCB-0025 D201-D208 were flagged before)
    sot = {"1": (-1.1, -0.95), "2": (-1.1, 0.95), "3": (1.1, 0.0)}
    name = "aiee:SOT-23-3_L3.0-W1.7-P0.95-LS2.9-BR"
    pcb = board(tmp_path, footprint("D1", name, pads_of(sot, w=1.0, h=0.5)),
                footprint("TP1", "lib:TP", pads_of({"1": (0, 0)}), at=(80, 50)))
    k, judged, nj = kinds(pcb, stock)
    assert k == [] and judged == {} and sum(map(len, nj.values())) == 1
    trace = []  # a one-pad part is traced too
    g.check(pcb, stock=stock, rows=[], trace=trace)
    assert [t["why"] for t in trace if t["ref"] == "TP1"] == ["under two numbered pads"]

def qfn(step_x=0.5, ep=True):
    """A 16-pin quad, 4 a side at 0.5 mm, numbered anticlockwise from the
    top left, with its exposed pad 17 in the middle; the top and bottom
    sides at step_x."""
    c = {}
    for i in range(4):
        c[str(1 + i)] = (-2.0, -0.75 + 0.5 * i)
        c[str(5 + i)] = (-0.75 * step_x / 0.5 + step_x * i, 2.0)
        c[str(9 + i)] = (2.0, 0.75 - 0.5 * i)
        c[str(13 + i)] = (0.75 * step_x / 0.5 - step_x * i, -2.0)
    if ep:
        c["17"] = (0.0, 0.0)
    return c


def test_name_pitch_holds_on_every_side(tmp_path, stock):
    # two sides at 0.4 under a P0.50 name: other sides still hold 0.5 so
    # "any two pads at the pitch" passes; the numbered pin runs do not
    name = "lcsc:QFN-16_L4.0-W4.0-P0.50-TL-EP"
    pcb = board(tmp_path, footprint("U1", name, pads_of(qfn())),
                footprint("U2", name, pads_of(qfn(0.4)), at=(80, 50)),
                footprint("U3", name, pads_of(qfn(ep=False)), at=(110, 50)))
    k, judged, _ = kinds(pcb, stock)
    assert k == [("U2", "fp_pitch_vs_name")] and judged == {"name": 3}
    # the exposed pad sits in a column with pins 7 and 15 (wrong numbers,
    # far apart): not a run
    assert g.pin_run_gaps(qfn()) == [0.5]


def test_planted_faults_caught_and_a_cut_over_empty_board_quiet(tmp_path, stock, monkeypatch):
    """fp_geometry_faults on a clean board: each planted fault is caught on
    the footprint it was planted on, the board itself (the quiet twin) and
    a courtyard cut with nothing in the strip stay quiet."""
    import fp_geometry_faults as f
    pcb = board(tmp_path,
                footprint("U1", SO, pads_of(SOIC) + crt("F", -3.7, -2.7, 3.7, 2.7)),
                footprint("R1", "x:R", pad("1", -0.5, 0, 0.5, 0.5) + pad("2", 0.5, 0, 0.5, 0.5),
                          at=(70, 50)),
                footprint("J1", HDR, header(2.54), at=(90, 50)))
    assert kinds(pcb, stock)[0] == []
    monkeypatch.setattr(g, "Stock", lambda root: stock)
    monkeypatch.setattr(g, "load_land_patterns", lambda: [])
    res = f.run([pcb], tmp_path / "out", per=2)
    got = {(r["fault"], r["ref"]): (r["caught"], r["kinds"]) for r in res}
    assert got[("pitch", "U1")] == (True, ["fp_pads_vs_stock"])
    assert got[("pitch", "J1")] == (True, ["fp_pitch_vs_stock"])
    assert got[("shrink", "U1")] == (True, ["fp_pad_small_vs_stock"])
    assert got[("pin1", "U1")] == (True, ["fp_pin_order_vs_stock"])
    assert got[("courtyard", "U1")] == (True, ["fp_courtyard_small_vs_stock"])
    assert got[("crt_only", "U1")] == (False, [])


def test_cli_and_registered(tmp_path):
    assert g.main(["--pcb", str(tmp_path / "none.kicad_pcb")]) == 2
    reg = HERE.parent / "scripts" / "checks.d" / "check_fp_geometry.yaml"
    assert reg.is_file() and "check_fp_geometry" in reg.read_text()


def test_coverage_counts_an_uncached_model_and_fill_cache_waits_out_the_limit(
        tmp_path, stock, monkeypatch):
    # a custom 2-pad land with an LCSC number and no cached model is not
    # judged, and says why; --fill-cache fetches it (a 403 first: wait, go
    # on), and the recount judges it against the model
    import urllib.error
    import easyeda
    import fp_geometry_coverage as cov
    shared, cache = tmp_path / "shared", tmp_path / "cache"
    monkeypatch.setattr(easyeda, "shared_cache", lambda: shared)
    lc = {"LCSC": "C900777"}
    land = pad("1", -1.48, 0, 1.2, 1.7) + pad("2", 1.48, 0, 1.2, 1.7)
    pcb = board(tmp_path, footprint("R1", "Mine:Odd2", land, props=lc))
    c = cov.coverage([pcb], easyeda_cache=cache, stock=stock)
    assert c["judged"] == 0 and c["uncached_lcsc"] == {"C900777": 1}
    assert [k for k in c["not_judged"]][0].startswith(cov.UNCACHED)
    assert cov.share(c["per_board"][str(pcb)]) == 0.0
    raw = {"title": "X", "dataStr": {"shape": []},
           "packageDetail": {"title": "R1206", "dataStr": {
               "head": {"x": 4000, "y": 3000},
               "shape": ["PAD~RECT~3994.173~3000~4.7~6.7~1~~1~0~~0~a",
                         "PAD~RECT~4005.827~3000~4.7~6.7~1~~2~0~~0~b"]}}}
    calls, waits = [], []

    def fetcher(x):
        calls.append(x)
        if len(calls) == 1:
            raise urllib.error.HTTPError("u", 403, "Forbidden", None, None)
        if x == "C900778":
            return {}
        return raw
    try:
        f = cov.fill_cache(["C900777", "C900778"], pace_s=0, wait_min=0.01,
                           cache=cache, fetcher=fetcher, sleep=waits.append)
    finally:
        easyeda.reset_rate_limit()
    assert f == {"fetched": ["C900777"], "none": ["C900778"], "left": []}
    assert len(waits) == 1 and calls == ["C900777", "C900777", "C900778"]
    c = cov.coverage([pcb], easyeda_cache=cache, stock=stock)
    assert c["judged_by_source"]["model"] == 1 and c["uncached_lcsc"] == {}
    assert cov.share(c["per_board"][str(pcb)]) == 1.0
    # out of budget, the rest is left for a rerun, not dropped
    try:
        f = cov.fill_cache(["C900779"], pace_s=0, wait_min=1, budget_min=0.5, cache=cache,
                           fetcher=lambda x: (_ for _ in ()).throw(
                               urllib.error.HTTPError("u", 429, "Too many", None, None)),
                           sleep=waits.append)
    finally:
        easyeda.reset_rate_limit()
    assert f["left"] == ["C900779"] and len(waits) == 1


# ------------------------------------------------- part records (#1332)

def record(tmp_path, land, mpn="PARTA", **extra):
    """parts/<mpn>.json: a part record whose land_pattern is `land`."""
    d = tmp_path / "parts"
    d.mkdir(exist_ok=True)
    rec = {"mpn": mpn, "land_pattern": land, **extra,
           "source_pdf": "parts/datasheets/X.pdf (Maker doc DS1 Rev C, June 2020; p7)"}
    (d / f"{mpn}.json").write_text(json.dumps(rec), encoding="utf-8")
    return d


def rcheck(pcb, stock, parts):
    """(sorted (ref, kind, severity), judged, not_judged, violations)."""
    v, judged, nj = g.check(pcb, stock=stock, rows=[], parts=parts)
    return (sorted((x["refs"][0], x["kind"], x["severity"]) for x in v),
            judged, nj, v)


PART = {"MPN": "PARTA"}


def soic_land(**kw):
    return {"pad_count": 8, "pin1": "1", "pitch_mm": 1.27, "row_spacing_mm": 4.95,
            "pad_size_mm": [1.95, 0.6], "pads_source": "p7 land pattern",
            "pads": [{"number": k, "at_mm": [x, y]} for k, (x, y) in SOIC.items()],
            **kw}


def test_record_land_quiet_at_any_turn_and_on_the_bottom(tmp_path, stock):
    parts = record(tmp_path, soic_land())
    flipped = {k: (x, -y) for k, (x, y) in SOIC.items()}  # KiCad's flip
    pcb = board(tmp_path,
                footprint("U1", "Mine:SO8", pads_of(SOIC), props=PART),
                footprint("U2", "Mine:SO8", pads_of(SOIC, rot=90), at=(60, 50, 90),
                          props=PART),
                footprint("U3", "Mine:SO8", pads_of(flipped).replace('"F.', '"B.'),
                          layer="B.Cu", props=PART),
                # an origin moved off the pads' centre: only geometry counts
                footprint("U4", "Mine:SO8",
                          pads_of({k: (x + 3, y - 1) for k, (x, y) in SOIC.items()}),
                          props=PART))
    k, judged, nj, _ = rcheck(pcb, stock, parts)
    assert k == [] and judged == {"record": 4} and nj == {}
    # without --parts the same board has nothing to judge it by
    _, judged, nj = g.check(pcb, stock=stock, rows=[])
    assert judged == {} and sum(len(r) for r in nj.values()) == 4


def test_record_pin_order_and_pitch(tmp_path, stock):
    parts = record(tmp_path, soic_land())
    cw = {str(i + 1): (2.475, -1.905 + 1.27 * i) for i in range(4)}
    cw.update({str(8 - i): (-2.475, -1.905 + 1.27 * i) for i in range(4)})
    tight = {k: (x, y / 1.27) for k, (x, y) in SOIC.items()}  # a 1.0 mm pitch
    pcb = board(tmp_path, footprint("U1", "Mine:SO8", pads_of(cw), props=PART),
                footprint("U2", "Mine:SO8", pads_of(tight), at=(80, 50), props=PART))
    k, judged, _, v = rcheck(pcb, stock, parts)
    assert ("U1", "fp_pin_order_vs_record", "error") in k
    assert ("U2", "fp_pitch_vs_record", "error") in k
    assert judged == {"record": 2}
    order, = [x for x in v if x["kind"] == "fp_pin_order_vs_record"]
    assert order["record"] == "PARTA.json" and order["drawing"] == "parts/datasheets/X.pdf"
    # the cite is the source's first clause, not its every page
    assert "against its part record PARTA (Maker doc DS1 Rev C, p7 land pattern):" in order["msg"]
    assert order["hint"] == "bottom_view_numbering"


CHIP = {"pad_count": 2, "pin1": "1", "pitch_mm": 1.5, "pad_size_mm": [0.8, 0.9]}


def chip(half, along=0.8, across=0.9):
    return pad("1", -half, 0, along, across) + pad("2", half, 0, along, across)


def test_record_two_terminal_land_gap_and_toe(tmp_path, stock):
    """A chip's record gives its pitch and pad size: the gap is the pitch
    less the pad length (0.7 mm, +-0.1), info up to 25 % outside it, an
    error beyond; the toe a warning past 0.35 mm."""
    parts = record(tmp_path, CHIP)
    pcb = board(tmp_path,
                footprint("C1", "Mine:CHIP", chip(0.75), props=PART),
                footprint("C2", "Mine:CHIP", chip(0.825), at=(60, 50), props=PART),
                footprint("C3", "Mine:CHIP", chip(0.9), at=(70, 50), props=PART),
                footprint("C4", "Mine:CHIP", chip(0.6), at=(80, 50), props=PART),
                footprint("C5", "Mine:CHIP", chip(0.95, 1.2, 1.3), at=(90, 50),
                          props=PART))
    k, judged, nj, v = rcheck(pcb, stock, parts)
    assert judged == {"record": 5} and nj == {}
    assert k == [("C2", "fp_land_gap_vs_record", "info"),
                 ("C2", "fp_pitch_vs_record", "info"),
                 ("C3", "fp_land_gap_vs_record", "error"),
                 ("C3", "fp_pitch_vs_record", "info"),
                 ("C4", "fp_land_gap_vs_record", "error"),
                 ("C4", "fp_pitch_vs_record", "info"),
                 ("C5", "fp_land_edge_vs_record", "warning"),
                 ("C5", "fp_pad_size_vs_record", "info"),
                 ("C5", "fp_pitch_vs_record", "info")]
    by = {(x["refs"][0], x["kind"]): x for x in v}
    gap = by[("C2", "fp_land_gap_vs_record")]
    assert gap["measured_mm"] == pytest.approx(0.85)
    assert gap["stated_mm"] == [0.7, 0.7] and gap["expected_mm"] == [0.6, 0.8]
    assert "21% wider than the maker's" in gap["msg"] and "Fix:" not in gap["msg"]
    assert by[("C2", "fp_pitch_vs_record")]["basis"] == "nearest_pads"
    wide = by[("C3", "fp_land_gap_vs_record")]
    assert "43% wider" in wide["msg"] and "tombstone" in wide["msg"] and "Fix:" in wide["msg"]
    narrow = by[("C4", "fp_land_gap_vs_record")]
    assert "43% narrower" in narrow["msg"] and "bridge" in narrow["msg"]
    toe = by[("C5", "fp_land_edge_vs_record")]
    assert toe["toe_mm"] == pytest.approx(1.55)
    assert toe["toe_expected_mm"] == pytest.approx([1.15, 1.15])
    assert "beyond the maker's" in toe["msg"] and "float" in toe["msg"]
    assert by[("C5", "fp_pad_size_vs_record")]["basis"] == "sorted"


def test_record_gap_mm_severity_by_how_far_out(tmp_path, stock):
    parts = record(tmp_path, {"pad_count": 2, "pin1": "1", "gap_mm": 0.7,
                              "pad_size_mm": [0.8, 0.9]})
    pcb = board(tmp_path,
                footprint("C1", "Mine:CHIP", chip(0.75), props=PART),
                footprint("C2", "Mine:CHIP", chip(0.825), at=(60, 50), props=PART),
                footprint("C3", "Mine:CHIP", chip(0.9), at=(70, 50), props=PART))
    k, judged, _, v = rcheck(pcb, stock, parts)
    assert judged == {"record": 3}
    assert k == [("C2", "fp_land_gap_vs_record", "info"),
                 ("C3", "fp_land_gap_vs_record", "error")]
    info, = [x for x in v if x["refs"] == ["C2"]]
    assert info["stated_mm"] == [0.7, 0.7] and "(21% wider than the maker's" in info["msg"]


SOT23 = {"1": (-0.95, 1.0), "2": (0.95, 1.0), "3": (0.0, -1.0)}


def test_record_nearest_pad_pitch_is_info(tmp_path, stock):
    # SOT-23's e is 0.95 mm, but no two of its pads are 0.95 apart: a
    # nearest-pad pitch is a weak basis, so info
    parts = record(tmp_path, {"pad_count": 3, "pin1": "1", "pitch_mm": 0.95})
    pcb = board(tmp_path, footprint("Q1", "Mine:SOT", pads_of(SOT23, w=0.6, h=0.7),
                                    props=PART))
    k, judged, _, v = rcheck(pcb, stock, parts)
    assert k == [("Q1", "fp_pitch_vs_record", "info")] and judged == {"record": 1}
    assert v[0]["basis"] == "nearest_pads"


def test_record_with_nothing_to_judge_still_counts_pads(tmp_path, stock):
    parts = record(tmp_path, {"pad_count": 3, "pin1": "1"})
    pcb = board(tmp_path, footprint("C1", "Mine:CHIP", chip(0.75), props=PART))
    k, judged, nj, _ = rcheck(pcb, stock, parts)
    assert k == [("C1", "fp_pad_count_vs_record", "error")] and judged == {}
    assert nj == {"its part record states nothing to judge the land by (no pitch, "
                  "pad size, row spacing, land gap or hole)": ["C1"]}


def test_record_it_cannot_read_is_named_not_raised(tmp_path, stock):
    parts = record(tmp_path, {**CHIP, "pitch_mm": "abc"})
    pcb = board(tmp_path, footprint("C1", "Mine:CHIP", chip(0.75), props=PART))
    k, judged, nj, _ = rcheck(pcb, stock, parts)
    assert k == [] and judged == {}
    why, = nj
    assert why.startswith("its part record PARTA.json could not be read as a land "
                          "(ValueError: ") and nj[why] == ["C1"]


def test_load_records(tmp_path):
    assert g.load_records(None) == {} and g.load_records(tmp_path / "none") == {}
    d = record(tmp_path, CHIP, lcsc="C123")
    record(tmp_path, CHIP, mpn="PARTB")
    (d / "PARTA.grounding.json").write_text(json.dumps({"land_pattern": CHIP}))
    (d / "BAD.json").write_text("{not json")
    (d / "NOLAND.json").write_text(json.dumps({"mpn": "NOLAND", "land_pattern": {}}))
    # a second record claiming PARTA's mpn: the first file by name keeps it
    (d / "ZZ.json").write_text(json.dumps({"mpn": "PARTA", "land_pattern": {"pad_count": 9}}))
    got = g.load_records(d / "parts.json")   # a file in the dir, not there
    assert set(got) == {"parta", "c123", "partb", "zz"}
    assert got["parta"][0].name == "PARTA.json" and got["c123"][0].name == "PARTA.json"
    assert got["zz"][1]["land_pattern"] == {"pad_count": 9}
    assert g.load_records(d) == got


def test_cite_first_clause():
    fc = g._first_clause
    assert fc("Maker doc DS123 Rev C, June 2020, 8 pages; p1") == "Maker doc DS123 Rev C"
    assert fc("TI SLVS (Rev C, 2020), p3") == "TI SLVS (Rev C, 2020)"
    assert fc("'Package A, B' drawing; p3") == "'Package A, B' drawing"
    assert fc("Maker's doc, p3") == "Maker's doc"
    assert fc("Maker doc: land p7") == "Maker doc"
    assert fc(" ".join(["word"] * 20)) == " ".join(["word"] * 16) + "..."
    p = Path("parts/PARTA.json")
    assert g._cite({"source_pdf": "parts/datasheets/X.pdf (Maker doc, Rev C; p7)"}, p) == (
        "Maker doc", "parts/datasheets/X.pdf")
    assert g._cite({"source_pdf": "parts/datasheets/X.pdf"}, p) == (
        "parts/datasheets/X.pdf", "parts/datasheets/X.pdf")
    assert g._cite({}, p) == ("PARTA.json", "")
    # the land's pads_source (the page and figure the pads came from) follows
    assert g._cite({"source_pdf": "parts/datasheets/X.pdf (Maker doc, Rev C; p7)",
                    "land_pattern": {"pads_source": "p27 Table 2; reflow row"}}, p) == (
        "Maker doc, p27 Table 2", "parts/datasheets/X.pdf")
    assert g._cite({"land_pattern": {"pads_source": "p3 Fig 4"}}, p) == (
        "PARTA.json, p3 Fig 4", "")


def test_gap_rule():
    assert g.gap_allowed([0.7, 0.7]) == pytest.approx((0.6, 0.8))
    assert g.gap_allowed([0.2, 0.25]) == (0.2, 0.25)
    assert g.gap_off(0.7, [0.7, 0.7]) == 0.0
    assert g.gap_off(0.75, [0.7, 0.7]) == pytest.approx(0.05 / 0.7)
    assert g.gap_off(0.85, [0.7, 0.7]) == pytest.approx(0.15 / 0.7)
    assert g.gap_off(0.4, [0.7, 0.7]) == pytest.approx(-0.3 / 0.7)
    assert g.gap_off(-0.1, [0.0, 0.2]) is None
    # inside +-0.1 of one number: no finding, however far in fraction
    assert g.gap_severity(0.79, [0.7, 0.7]) is None
    assert g.gap_severity(0.85, [0.7, 0.7]) == "info"
    assert g.gap_severity(0.875, [0.7, 0.7]) == "info"     # 25 % exactly
    assert g.gap_severity(1.0, [0.7, 0.7]) == "error"
    assert g.gap_severity(0.4, [0.7, 0.7]) == "error"
    # a range is the maker's own: no +-0.1 round it
    assert g.gap_severity(0.26, [0.2, 0.25]) == "info"
    assert g.gap_severity(0.18, [0.2, 0.25]) == "info"
    assert g.gap_severity(0.14, [0.2, 0.25]) == "error"
    # a bound not above zero has no fraction: below it the pads overlap,
    # above it the record's own pads touch
    assert g.gap_severity(-0.5, [-0.2, -0.2]) == "error"
    assert g.gap_severity(0.3, [-0.2, -0.2]) == "warning"
    assert g.gap_severity(0.3, [0.0, 0.0]) == "warning"
    assert g.gap_note(0.7, [0.7, 0.7]) == ""
    assert g.gap_note(0.85, [0.7, 0.7]).startswith("21% wider than the maker's; lands")
    assert "likely a land for another part" in g.gap_note(1.0, [0.7, 0.7])
    assert g.gap_note(-0.5, [-0.2, -0.2]) == "the pads overlap"
    assert "check the record's pitch" in g.gap_note(0.3, [-0.2, -0.2])


def _uncached(nj):
    return sorted(r for why, refs in nj.items() if "not cached" in why for r in refs)


def test_a_test_board_is_judged_by_its_model_from_the_pinned_cache(monkeypatch):
    # #1254: CI has no shared model cache, so a test board's LCSC footprint
    # was judged on the tower and skipped on CI. The suite pins the models
    # the boards under tests/ use; without the pin, D1 is not judged (the
    # planted fault), with it, D1 is judged by its model (the quiet twin)
    pcb = HERE / "fixtures" / "teardrops" / "led.kicad_pcb"
    _, judged, nj = g.check(pcb)
    assert judged["model"] == 1 and _uncached(nj) == []
    monkeypatch.delenv("HWDE_EASYEDA_PINNED")
    _, judged, nj = g.check(pcb)
    assert "model" not in judged and _uncached(nj) == ["D1"]


@pytest.mark.slow
def test_every_test_board_has_its_lcsc_models_pinned():
    # CI and a host with a full shared cache judge the same footprints: no
    # board under tests/ has an LCSC model left uncached, bar fp_board's own
    # planted ones (fp_geometry_coverage's uncached rows)
    left = {}
    for pcb in sorted(HERE.rglob("*.kicad_pcb")):
        refs = _uncached(g.check(pcb)[2])
        if refs:
            left[str(pcb.relative_to(HERE))] = refs
    assert left == {"fixtures/fp_board/placed.kicad_pcb": ["U10", "U7", "U8", "U9"]}, (
        "a test board's LCSC model is not pinned: copy <LCSC>.json (or .none) "
        "from ~/.cache/hwde/easyeda into tests/fixtures/easyeda_pinned")
