"""#469: IPC-D-356 netlist in the fab package, and the Gerber netlist compare.

Hermetic: the pd_trigger stage fixture's committed gerbers + its kicad-cli
IPC-D-356 (tests/fixtures/netlist/pd-trigger.ipc), planted faults written
into copies of the gerbers, and synthetic 4-layer stacks for blind vias,
slots and net ties. Slow: the pinned boards exported live (fab_export ships
the .ipc; dfm_check reads it back) give 0 shorts / 0 opens, and the gerber
connectivity agrees with netconn's wherever netconn joins.
"""
from __future__ import annotations

import re
import shutil
import sys
import warnings
import zipfile
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import dfm_check  # noqa: E402
import geom  # noqa: E402
import gerblib  # noqa: E402
import ipcd356  # noqa: E402

STAGES = REPO / "tests" / "fixtures" / "stages"
PD_PCB = STAGES / "pd_trigger" / "route" / "pd-trigger.kicad_pcb"
PD_GERBERS = STAGES / "pd_trigger" / "fab" / "gerbers"
PD_IPC = REPO / "tests" / "fixtures" / "netlist" / "pd-trigger.ipc"

warnings.simplefilter("ignore")


# ------------------------------------------------------------ helpers

def rec(code, net, ref, pin, x, y, access, hole=None, size=(0.6, 0.6)):
    """One IPC-D-356A fixed-column record, UNITS CUST 0 (0.0001 in)."""
    def n(v):
        return round(v / 0.00254)
    line = f"{code}{net:<14.14}   {ref:<6.6}{'-' if pin else ' '}{pin:<4.4}"
    line += "M" if ref == "VIA" else " "
    line += f"D{n(hole):04d}P" if hole else "      "
    line += (f"A{access:02d}X{n(x):+07d}Y{-n(y):+07d}"
             f"X{n(size[0]):04d}Y{n(size[1]):04d}R000S0")
    return line


def d356(*records) -> str:
    return "\n".join(["P  CODE 00", "P  UNITS CUST 0", *records, "999"]) + "\n"


def gx(v: float) -> int:
    return round(v * 1e6)


def gerber(*features: str) -> str:
    return "\n".join(["%FSLAX46Y46*%", "%MOMM*%", "%LPD*%", "G01*",
                      "%ADD10C,0.600000*%", "%ADD11C,0.200000*%",
                      *features, "M02*"]) + "\n"


def flash(x, y, d="D10"):
    return f"{d}*\nX{gx(x)}Y{gx(-y)}D03*"


def region(poly) -> str:
    pts = list(poly.exterior.coords)
    out = ["G36*", f"X{gx(pts[0][0])}Y{gx(-pts[0][1])}D02*"]
    out += [f"X{gx(x)}Y{gx(-y)}D01*" for x, y in pts[1:]]
    return "\n".join(out + ["G37*"])


def drill(holes, span=None, slots=()) -> str:
    fn = (f"; #@! TF.FileFunction,Plated,{span[0]},{span[1]},Blind\n"
          if span else "; #@! TF.FileFunction,Plated,1,4,PTH\n")
    body = ["T1"] + [f"X{x:.3f}Y{-y:.3f}" for x, y in holes]
    body += [f"X{x1:.3f}Y{-y1:.3f}G85X{x2:.3f}Y{-y2:.3f}" for (x1, y1), (x2, y2) in slots]
    return ("M48\n" + fn + "FMAT,2\nMETRIC\n"
            "; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT1C0.300\n%\nG90\nG05\n"
            + "\n".join(body) + "\nM30\n")


def stack(tmp: Path, layers: dict, drills: dict) -> gerblib.FabStack:
    ext = {"F.Cu": "gtl", "In1.Cu": "g1", "In2.Cu": "g2", "B.Cu": "gbl"}
    tmp.mkdir(parents=True, exist_ok=True)
    for name, feats in layers.items():
        (tmp / f"s-{name.replace('.', '_')}.{ext[name]}").write_text(
            gerber(*feats))
    for name, text in drills.items():
        (tmp / name).write_text(text)
    return gerblib.open_fab(tmp)


ALL4 = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def pd_copy(tmp: Path) -> Path:
    dst = tmp / "gerbers"
    shutil.copytree(PD_GERBERS, dst)
    return dst


def plant(gdir: Path, layer_file: str, feature: str) -> None:
    p = gdir / layer_file
    text = p.read_text()
    assert text.rstrip().endswith("M02*")
    p.write_text(text.rstrip()[:-4] + feature + "\nM02*\n")


_XY = re.compile(r"^(?:G0[123])?X(-?\d+)Y(-?\d+)(?:I-?\d+J-?\d+)?D0([123])\*$")


def pen_up(gdir: Path, layer_file: str, a, b) -> int:
    """Turn the drawn segment a->b (either way) into a move: the trace is
    gone from the gerber, everything else byte-identical."""
    p = gdir / layer_file
    want = {(gx(a[0]), gx(-a[1])), (gx(b[0]), gx(-b[1]))}
    cur, out, hits = None, [], 0
    for line in p.read_text().splitlines():
        m = _XY.match(line)
        if m:
            pt = (int(m.group(1)), int(m.group(2)))
            if m.group(3) == "1" and {cur, pt} == want:
                line = line[:-4] + "D02*"
                hits += 1
            cur = pt
        out.append(line)
    p.write_text("\n".join(out) + "\n")
    return hits


def pd_compare(gdir: Path) -> dict:
    bg = geom.load_board(PD_PCB)
    d = ipcd356.parse(PD_IPC.read_text())
    ipcd356.resolve(d, bg)
    return ipcd356.compare(gerblib.open_fab(gdir), d,
                           ipcd356.net_tie_classes(PD_PCB.read_text(), bg))


# ------------------------------------------------------------ parser

def test_parse_kicad_records():
    d = ipcd356.parse(PD_IPC.read_text())
    assert d.units == "CUST 0"
    codes = {p.code for p in d.points}
    assert {"317", "327"} <= codes
    via = next(p for p in d.points if p.ref == "VIA")
    assert via.pin == "" and via.access == 0 and via.plated
    # coordinates come back in board space (mm, Y down): every point sits on
    # a pad or via of the board
    bg = geom.load_board(PD_PCB)
    assert ipcd356.resolve(d, bg) == []
    assert all(p.board_net for p in d.points if p.net != "N/C"
               and p.code != "367")
    # the pin field is 4 characters: J1's "A1-B12" comes back "A1-B"
    assert any(p.ref == "J1" and p.pin == "A1-B" and p.board_net == "GND"
               for p in d.points)
    smd = next(p for p in d.points if p.code == "327" and p.ref == "D5")
    pad = next(q for q in bg.pads_of(ref=smd.ref) if q.number == smd.pin)
    assert abs(pad.center[0] - smd.x) < 0.003
    assert abs(pad.center[1] - smd.y) < 0.003


def test_parse_alias_units_and_unplated():
    text = ("P  CODE 00\nP  UNITS CUST 1\nP  NNAME1  /A_VERY_LONG_NET_NAME_X\n"
            "327NNAME1            U1    -1          A01X+010000Y-020000"
            "X0500Y0500R000S0\n"
            "367N/C              H1          D3200UA00X+001000Y-001000"
            "X3200Y0000R000S0\n999\n")
    d = ipcd356.parse(text)
    smd, hole = d.points
    assert smd.net == "/A_VERY_LONG_NET_NAME_X"
    assert (smd.x, smd.y) == pytest.approx((10.0, 20.0))
    assert hole.code == "367" and hole.plated is False
    assert hole.hole_mm == pytest.approx(3.2)
    with pytest.raises(ipcd356.D356Error):
        ipcd356.parse("P  UNITS CUST 0\n999\n")


def test_drill_span_reads_the_x2_header(tmp_path):
    p = tmp_path / "b-1-2.drl"
    p.write_text("M48\n; #@! TF.FileFunction,Plated,1,2,Blind\n")
    assert ipcd356.drill_span(p, 4) == (1, 2)
    p.write_text("M48\n; #@! TF.FileFunction,MixedPlating,1,4\n")
    assert ipcd356.drill_span(p, 4) == (1, 4)


# ------------------------------------------------------------ known answer

def test_pd_trigger_shipped_gerbers_match_the_netlist():
    res = pd_compare(PD_GERBERS)
    assert res["shorts"] == [] and res["opens"] == []
    assert res["n_points"] > 100 and res["n_plated_holes"] > 40
    assert set(res["fragments_per_net"].values()) == {1}


def test_planted_short_is_one_short(tmp_path):
    """A 0.1 mm copper sliver from D5-2 (/D5_A) to the nearest F.Cu pad of
    another net it can reach without touching a third net's copper."""
    bg = geom.load_board(PD_PCB)
    a = next(p for p in bg.pads_of(ref="D5") if p.number == "2")
    nets = {p.net for p in bg.pads_of() if p.net}
    sliver = b = None
    for cand in sorted((p for p in bg.pads_of(layer="F.Cu")
                        if p.net and p.net != a.net),
                       key=lambda p: p.poly.distance(a.poly)):
        line = LineString(nearest_points(a.poly, cand.poly)).buffer(0.05)
        if not any(bg.net_copper(n, "F.Cu").intersects(line)
                   for n in nets - {a.net, cand.net}):
            sliver, b = line, cand
            break
    gdir = pd_copy(tmp_path)
    plant(gdir, "pd-trigger-F_Cu.gtl", region(sliver))
    res = pd_compare(gdir)
    assert len(res["shorts"]) == 1, res["shorts"]
    assert res["shorts"][0]["nets"] == sorted({a.net, b.net})
    assert res["opens"] == []


def test_removed_track_is_one_open(tmp_path):
    """/D5_A is one F.Cu segment D5-2 -> R12-2; pen it up."""
    bg = geom.load_board(PD_PCB)
    (t,) = bg.tracks_of("/D5_A")
    a, b = t.shape.coords[0], t.shape.coords[-1]
    gdir = pd_copy(tmp_path)
    assert pen_up(gdir, "pd-trigger-F_Cu.gtl", a, b) == 1
    res = pd_compare(gdir)
    assert res["shorts"] == []
    assert [o["net"] for o in res["opens"]] == ["/D5_A"]
    assert sorted(map(sorted, res["opens"][0]["fragments"])) == [
        ["D5-2"], ["R12-2"]]


def test_stale_fill_reaching_a_pad_is_a_short(tmp_path):
    """The #243 shape: a pour region (G36) that still reaches a pad the
    board has since moved next to it - the gerber GND fill touches D5-2."""
    bg = geom.load_board(PD_PCB)
    fill = bg.zone_fill("GND", "F.Cu")
    pad = min((p for p in bg.pads_of(layer="F.Cu")
               if p.net not in (None, "GND") and p.kind == "smd"),
              key=lambda p: p.poly.distance(fill))
    pa, pb = nearest_points(pad.poly, fill)
    gdir = pd_copy(tmp_path)
    plant(gdir, "pd-trigger-F_Cu.gtl",
          region(LineString([pa, pb]).buffer(0.05)))
    res = pd_compare(gdir)
    assert [s["nets"] for s in res["shorts"]] == [sorted({"GND", pad.net})]


def test_missing_pad_copper_is_an_open(tmp_path):
    """A test point with no copper under it (a pad the export dropped)."""
    fab = stack(tmp_path, {"F.Cu": [flash(1, 1)], "B.Cu": []}, {})
    d = ipcd356.parse(d356(rec("327", "A", "U1", "1", 1, 1, 1),
                           rec("327", "A", "U1", "2", 5, 1, 1)))
    res = ipcd356.compare(fab, d)
    (op,) = res["opens"]
    assert op["no_copper"]["pin"] == "2"


# ------------------------------------------------------------ layers

def _blind_case(tmp, span):
    """Net A on F.Cu, net B on B.Cu, stacked at (2, 2) on all four layers;
    the one hole there joins only its span."""
    layers = {n: [flash(2, 2)] for n in ALL4}
    return stack(tmp, layers, {"s.drl": drill([(2, 2)], span=span)})


def test_through_hole_joins_every_layer(tmp_path):
    d = ipcd356.parse(d356(rec("327", "A", "U1", "1", 2, 2, 1),
                           rec("327", "B", "U2", "1", 2, 2, 4)))
    res = ipcd356.compare(_blind_case(tmp_path, None), d)
    assert [s["nets"] for s in res["shorts"]] == [["A", "B"]]


def test_blind_via_joins_only_its_span(tmp_path):
    d = ipcd356.parse(d356(rec("327", "A", "U1", "1", 2, 2, 1),
                           rec("327", "B", "U2", "1", 2, 2, 4)))
    res = ipcd356.compare(_blind_case(tmp_path, (1, 2)), d)
    assert res["shorts"] == [] and res["opens"] == []


def test_zero_length_slot_is_a_plated_hole(tmp_path):
    """KiCad writes an oval drill (pinned core's USB-C J1, 0.3998 x 0.4) as a
    G85 slot; read_drill keeps flashes only, so the compare reads slots too."""
    layers = {"F.Cu": [flash(3, 3)], "B.Cu": [flash(3, 3)]}
    fab = stack(tmp_path, layers,
                {"s.drl": drill([], slots=[((3, 3), (3, 3))])})
    assert len(gerblib.read_slots(fab.drill_files[0])) == 1
    d = ipcd356.parse(d356(rec("327", "A", "U1", "1", 3, 3, 1),
                           rec("327", "A", "U2", "1", 3, 3, 2)))
    res = ipcd356.compare(fab, d)
    assert res["opens"] == [] and res["n_plated_holes"] == 1


# ------------------------------------------------------------ board side

class _Pad:
    def __init__(self, ref, number, net, center=(0, 0)):
        self.ref, self.number, self.net, self.center = ref, number, net, center


class _BG:
    def __init__(self, pads, vias=()):
        self._pads, self._vias = pads, list(vias)

    def pads_of(self, net=None, layer=None, ref=None):
        return [p for p in self._pads if ref is None or p.ref == ref]

    def vias_of(self, net=None, layer=None):
        return self._vias


TIE_PCB = ('(kicad_pcb (footprint "NetTie-2"\n'
           '  (property "Reference" "NT1")\n'
           '  (net_tie_pad_groups "1, 2")\n'
           '  (pad "1" smd) (pad "2" smd)))\n')


def _tie_case(tmp_path, gap=0.4, eol="\n"):
    """NT1 ties GND_A (pad 1) to GND_B (pad 2); C1 sits on GND_B. The two
    pads' flashes touch at a 0.4 mm pitch and stand apart at a larger one
    (the tie's copper cut)."""
    bg = _BG([_Pad("NT1", "1", "GND_A"), _Pad("NT1", "2", "GND_B"),
              _Pad("C1", "1", "GND_B")])
    fab = stack(tmp_path, {"F.Cu": [flash(1, 1), flash(1 + gap, 1),
                                    flash(1 + gap, 3), "D11*",
                                    f"X{gx(1 + gap)}Y{gx(-1)}D02*",
                                    f"X{gx(1 + gap)}Y{gx(-3)}D01*"],
                           "B.Cu": []}, {})
    text = d356(rec("327", "GND_A", "NT1", "1", 1, 1, 1),
                rec("327", "GND_B", "NT1", "2", 1 + gap, 1, 1),
                rec("327", "GND_B", "C1", "1", 1 + gap, 3, 1))
    return fab, text.replace("\n", eol), bg


def test_net_tie_short_is_the_design_once_its_nets_share_a_name(tmp_path):
    fab, text, bg = _tie_case(tmp_path)
    ties = ipcd356.net_tie_classes(TIE_PCB, bg)
    assert ties["GND_A"] == ties["GND_B"]
    d = ipcd356.parse(text)
    ipcd356.resolve(d, bg)
    (sh,) = ipcd356.compare(fab, d)["shorts"]
    assert not sh.get("tie")
    # the copper is the design, but a tester knows the island by its D-356
    # names, and reads two names in one island as a short
    res = ipcd356.compare(fab, d, ties)
    (sh,) = res["shorts"]
    assert sh["tie"] and sh["mergeable"]
    assert sh["d356_names"] == ["GND_A", "GND_B"]
    (j,) = res["tie_joins"]
    assert j["nets"] == ["GND_A", "GND_B"] and j["n_points"] == 3
    assert res["tie_opens"] == []

    new, (r,) = ipcd356.merge_net_ties(text, res["tie_joins"])
    # GND_B has the most records, so it stays
    assert (r["from"], r["to"], r["points"]) == ("GND_A", "GND_B", 1)
    assert r["nets"] == ["GND_A", "GND_B"]
    assert len(new.splitlines()) == len(text.splitlines())
    d2 = ipcd356.parse(new)
    assert [p.net for p in d2.points] == ["GND_B"] * 3
    (clash,) = ipcd356.resolve(d2, bg)
    assert clash == {"d356_net": "GND_B", "board_nets": ["GND_A", "GND_B"]}
    res2 = ipcd356.compare(fab, d2, ties)
    assert res2["shorts"] == [] and res2["opens"] == []
    assert res2["tie_joins"][0]["d356_names"] == ["GND_B"]
    # merging again changes nothing
    assert ipcd356.merge_net_ties(new, res2["tie_joins"]) == (new, [])


def test_a_cut_tie_keeps_its_names_and_is_a_tie_open(tmp_path):
    fab, text, bg = _tie_case(tmp_path, gap=2)
    d = ipcd356.parse(text)
    ipcd356.resolve(d, bg)
    res = ipcd356.compare(fab, d, ipcd356.net_tie_classes(TIE_PCB, bg))
    assert res["shorts"] == [] and res["opens"] == [] and res["tie_joins"] == []
    assert ipcd356.merge_net_ties(text, res["tie_joins"]) == (text, [])
    # the board ties the nets and this copper doesn't: the netlist names
    # them apart, so a tester passes it, and only the tie open tells
    (to,) = res["tie_opens"]
    assert to["nets"] == ["GND_A", "GND_B"]
    assert to["groups"] == [["GND_A"], ["GND_B"]]
    assert [(p["ref"], p["pin"]) for p in to["near"]] == [("NT1", "1"),
                                                         ("NT1", "2")]
    assert to["at"] == pytest.approx([2.0, 1.0], abs=0.01)   # D-356 rounds
    # R1 spans the two nets closer than the tie's pads: the open is still
    # placed at the tie, where the bridge is missing
    plant(tmp_path, "s-F_Cu.gtl", flash(1, 6) + "\n" + flash(1.8, 6))
    text_r = text.replace("999", rec("327", "GND_A", "R1", "1", 1, 6, 1) + "\n"
                          + rec("327", "GND_B", "R1", "2", 1.8, 6, 1) + "\n999")
    bg_r = _BG(bg._pads + [_Pad("R1", "1", "GND_A"), _Pad("R1", "2", "GND_B")])
    d = ipcd356.parse(text_r)
    ipcd356.resolve(d, bg_r)
    fab_r = gerblib.open_fab(tmp_path)
    ties = ipcd356.net_tie_classes(TIE_PCB, bg_r)
    assert ipcd356.net_tie_refs(TIE_PCB) == {"NT1"}
    (to,) = ipcd356.compare(fab_r, d, ties)["tie_opens"]
    assert {p["ref"] for p in to["near"]} == {"R1"}
    (to,) = ipcd356.compare(fab_r, d, ties, tie_refs={"NT1"})["tie_opens"]
    assert [p["ref"] + p["pin"] for p in to["near"]] == ["NT11", "NT12"]
    # a three-net tie that two islands hold, one joining two of its nets
    pcb3 = TIE_PCB.replace('"1, 2"', '"1, 2, 3"').replace(
        '(pad "2" smd)', '(pad "2" smd) (pad "3" smd)')
    bg._pads.append(_Pad("NT1", "3", "GND_C"))
    plant(tmp_path, "s-F_Cu.gtl", flash(3.4, 1))
    text3 = text.replace("999", rec("327", "GND_C", "NT1", "3", 3.4, 1, 1)
                         + "\n999")
    d = ipcd356.parse(text3)
    ipcd356.resolve(d, bg)
    res = ipcd356.compare(gerblib.open_fab(tmp_path), d,
                          ipcd356.net_tie_classes(pcb3, bg))
    (to,) = res["tie_opens"]
    assert to["nets"] == ["GND_A", "GND_B", "GND_C"]
    assert to["groups"] == [["GND_A"], ["GND_B", "GND_C"]]
    assert [p["ref"] + p["pin"] for p in to["near"]] == ["NT11", "NT12"]


def test_the_tie_name_with_most_records_stays_and_aliases_resolve():
    lines = ["P  CODE 00", "P  UNITS CUST 0", "P  NNAME1     /BOOT5_SEL",
             "P  NNAME2     /BOOT9_SEL",
             rec("327", "+1V8", "JP1", "1", 1, 1, 1),
             rec("327", "NNAME1", "JP1", "2", 2, 1, 1),
             rec("327", "NNAME1", "R1", "1", 3, 1, 1),
             rec("327", "GND", "JP2", "1", 1, 5, 1),
             rec("317", "GND", "VIA", "", 1, 6, 0, hole=0.3),
             rec("327", "NNAME2", "JP2", "2", 2, 5, 1), "999"]
    text = "\r\n".join(lines) + "\r\n"
    joins = [{"nets": ["+1V8", "/BOOT5_SEL"], "d356_names": ["+1V8", "/BOOT5_SEL"],
              "mergeable": True},
             {"nets": ["/BOOT9_SEL", "GND"], "d356_names": ["/BOOT9_SEL", "GND"],
              "mergeable": True}]
    new, renames = ipcd356.merge_net_ties(text, joins)
    # /BOOT5_SEL has 2 records to +1V8's 1, though +1V8 sorts first; the
    # winner's own field (its alias) goes into the loser's record
    assert [(r["from"], r["to"], r["points"]) for r in renames] == \
        [("+1V8", "/BOOT5_SEL", 1), ("/BOOT9_SEL", "GND", 1)]
    assert [p.net for p in ipcd356.parse(new).points] == \
        ["/BOOT5_SEL"] * 3 + ["GND"] * 3
    assert new.splitlines()[4][:17] == "327NNAME1        "
    assert new.count("\r\n") == text.count("\r\n") and "\n\n" not in new
    # a three-net tie seen as two islands that share a name is one group
    text = d356(rec("327", "A", "U1", "1", 1, 1, 1),
                rec("327", "B", "U1", "2", 2, 1, 1),
                rec("327", "B", "U1", "3", 3, 1, 1),
                rec("327", "C", "U1", "4", 4, 1, 1))
    joins = [{"nets": ["A", "B"], "d356_names": ["A", "B"], "mergeable": True},
             {"nets": ["B", "C"], "d356_names": ["B", "C"], "mergeable": True}]
    new, renames = ipcd356.merge_net_ties(text, joins)
    assert [(r["from"], r["to"], r["nets"]) for r in renames] == \
        [("A", "B", ["A", "B", "C"]), ("C", "B", ["A", "B", "C"])]
    assert {p.net for p in ipcd356.parse(new).points} == {"B"}


def test_a_tie_name_that_also_stands_for_another_net_is_left(tmp_path):
    # kicad-cli cut GND_A and C9's OTHER to one name, X: renaming X would
    # put OTHER on the tie too
    fab, text, bg = _tie_case(tmp_path)
    bg._pads.append(_Pad("C9", "1", "OTHER"))
    plant(tmp_path, "s-F_Cu.gtl", flash(8, 8))
    text = text.replace("327GND_A", "327X    ").replace(
        "999", rec("327", "X", "C9", "1", 8, 8, 1) + "\n999")
    d = ipcd356.parse(text)
    (clash,) = ipcd356.resolve(d, bg)
    assert clash["board_nets"] == ["GND_A", "OTHER"]
    res = ipcd356.compare(gerblib.open_fab(tmp_path), d,
                          ipcd356.net_tie_classes(TIE_PCB, bg))
    (sh,) = res["shorts"]
    assert sh["tie"] and not sh["mergeable"] and sh["d356_names"] == ["GND_B", "X"]
    assert ipcd356.merge_net_ties(text, res["tie_joins"]) == (text, [])


def test_dfm_check_passes_a_tie_once_export_names_it_once(tmp_path, monkeypatch):
    import fab_export
    pcb = tmp_path / "t.kicad_pcb"
    pcb.write_text(TIE_PCB)
    gdir = tmp_path / "g"
    fab, text, bg = _tie_case(gdir, eol="\r\n")
    ipc = gdir / "t.ipc"
    ipc.write_bytes(text.encode())
    vios: list = []
    facts, skip = dfm_check.check_netlist(fab, pcb, bg, gdir, tmp_path, vios)
    assert skip is None and facts["netlist"]["net_ties"] == 1
    (v,) = vios
    assert v["kind"] == "dfm_net_short" and v["tie"] and v["severity"] == "error"
    assert "a net tie joins these nets on purpose" in v["msg"]
    assert "(GND_A, GND_B)" in v["msg"] and v["refs"] == ["NT1"]

    monkeypatch.setattr(geom, "load_board", lambda p: bg)
    (r,) = fab_export.name_net_ties(pcb, ipc, gdir)
    assert (r["from"], r["to"]) == ("GND_A", "GND_B")
    raw = ipc.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n") and b"GND_A" not in raw
    assert not list(gdir.glob("*.part"))
    vios = []
    facts, _ = dfm_check.check_netlist(fab, pcb, bg, gdir, tmp_path, vios)
    # GND_B now stands for both of the tie's nets: that is no name clash
    assert vios == [] and facts["netlist"]["shorts"] == 0
    assert facts["netlist"]["name_clashes"] == 0
    assert facts["netlist"]["net_ties"] == 1
    # and naming again changes nothing
    assert fab_export.name_net_ties(pcb, ipc, gdir) == []
    assert ipc.read_bytes() == raw


def test_dfm_check_reports_a_cut_tie_the_tester_cant_see(tmp_path):
    pcb = tmp_path / "t.kicad_pcb"
    pcb.write_text(TIE_PCB)
    gdir = tmp_path / "g"
    fab, text, bg = _tie_case(gdir, gap=2)
    (gdir / "t.ipc").write_text(text)
    vios: list = []
    facts, skip = dfm_check.check_netlist(fab, pcb, bg, gdir, tmp_path, vios)
    assert skip is None and facts["netlist"]["net_tie_opens"] == 1
    assert facts["netlist"]["shorts"] == facts["netlist"]["opens"] == 0
    (v,) = vios
    assert v["kind"] == "dfm_net_tie_open" and v["severity"] == "error"
    assert v["refs"] == ["NT1"] and v["nets"] == ["GND_A", "GND_B"]
    assert v["groups"] == [["GND_A"], ["GND_B"]]
    assert "leaves them in 2 pieces" in v["msg"]
    assert "NT1-1 / NT1-2" in v["msg"] and "re-export the package" in v["msg"]
    assert "netlist" in dfm_check.DFM_FAMILIES and \
        "dfm_net_tie_open" in dfm_check.DFM_FAMILIES["netlist"]


def test_dfm_check_names_a_failed_tie_rename(tmp_path, monkeypatch):
    import fab_export
    import kc
    pcb = tmp_path / "t.kicad_pcb"
    pcb.write_text(TIE_PCB)
    fab, text, bg = _tie_case(tmp_path / "g")

    def export(cli, pcb, f, gdir, bg):
        f.write_text(text)
        return {"status": "pass", "tie_rename_error": "D356Error: bad"}
    monkeypatch.setattr(fab_export, "export_ipc_absolute", export)
    monkeypatch.setattr(kc, "resolve_cli", lambda: Path("cli"))
    vios: list = []
    facts, skip = dfm_check.check_netlist(fab, pcb, bg, tmp_path / "g",
                                          tmp_path, vios)
    assert skip is None and facts["netlist"]["source"] == "board"
    (v,) = vios
    assert v["tie"] and "naming itself failed (D356Error: bad)" in v["msg"]


def test_dfm_check_tells_a_tie_whose_name_clashes_to_rename_nets(tmp_path):
    pcb = tmp_path / "t.kicad_pcb"
    pcb.write_text(TIE_PCB)
    gdir = tmp_path / "g"
    fab, text, bg = _tie_case(gdir)
    bg._pads.append(_Pad("C9", "1", "OTHER"))
    plant(gdir, "s-F_Cu.gtl", flash(8, 8))
    (gdir / "t.ipc").write_text(text.replace("327GND_A", "327X    ").replace(
        "999", rec("327", "X", "C9", "1", 8, 8, 1) + "\n999"))
    vios: list = []
    facts, _ = dfm_check.check_netlist(gerblib.open_fab(gdir), pcb, bg, gdir,
                                       tmp_path, vios)
    assert sorted(v["kind"] for v in vios) == ["dfm_net_short",
                                               "dfm_netlist_name_clash"]
    (sh,) = [v for v in vios if v["kind"] == "dfm_net_short"]
    assert "also stands for a net outside the tie" in sh["msg"]
    assert facts["netlist"]["name_clashes"] == 1


def test_export_names_ties_after_jumpers_and_survives_a_failed_tie_rename(
        tmp_path, monkeypatch):
    import fab_export
    pcb = tmp_path / "t.kicad_pcb"
    pcb.write_text(TIE_PCB.replace("net_tie_pad_groups", "x"))
    _, text, _ = _tie_case(tmp_path / "g")
    ipc = tmp_path / "t.ipc"
    ipc.write_text(text)
    # no tie on the board: nothing to read, nothing written
    assert fab_export.name_net_ties(pcb, ipc, tmp_path / "g") == []
    assert ipc.read_text() == text

    calls = []
    monkeypatch.setattr(fab_export, "_export_ipc_absolute",
                        lambda cli, pcb, f: {"status": "pass"})
    monkeypatch.setattr(fab_export, "name_jumper_islands",
                        lambda *a: calls.append("jumpers") or [])
    def boom(*a, **k):
        calls.append("ties")
        raise ipcd356.D356Error("no gerbers")
    monkeypatch.setattr(fab_export, "name_net_ties", boom)
    res = fab_export.export_ipc_absolute(Path("cli"), pcb, ipc, tmp_path)
    assert calls == ["jumpers", "ties"] and res["status"] == "pass"
    assert res["tie_rename_error"] == "D356Error: no gerbers"
    assert "jumper_rename_error" not in res
    assert ipc.read_text() == text


def test_kicad_cli_keeps_the_last_14_characters_upper_cased():
    """What the name clash and the tie checks lean on: kicad-cli writes a
    net's name upper-cased, cut to its last 14 characters."""
    bg = geom.load_board(PD_PCB)
    d = ipcd356.parse(PD_IPC.read_text())
    ipcd356.resolve(d, bg)
    pairs = {(p.net, p.board_net) for p in d.points if p.board_net}
    assert any(len(b) > 14 and b != b.upper() for _, b in pairs)
    assert all(name == board[-14:].upper() for name, board in pairs)


# #962: a flow-through ESD part (USBLC6-2SC6) joins pins 1-6 and 3-4 inside
# the package. Pin 6 goes to the connector, pin 1 to the MCU: two islands of
# USB_DP on the bare board.
JUMPER_PCB = ('(kicad_pcb (footprint "SOT-23-6"\n'
              '  (property "Reference" "U1")\n'
              '  (jumper_pad_groups ("1" "6") ("3" "4"))\n'
              '  (pad "1" smd) (pad "3" smd) (pad "4" smd) (pad "6" smd)))\n')


def _jumper_case(tmp_path, ipc_net_6="USB_DP"):
    # U1-1 (1, 1) is joined by a track to J1-1 (4, 1); U1-6 (1, 2) is alone
    # with J2-1 at (4, 2): two islands, one net.
    fab = stack(tmp_path, {"F.Cu": [flash(1, 1), flash(4, 1), flash(1, 2),
                                    flash(4, 2), "D11*",
                                    f"X{gx(1)}Y{gx(-1)}D02*",
                                    f"X{gx(4)}Y{gx(-1)}D01*",
                                    f"X{gx(1)}Y{gx(-2)}D02*",
                                    f"X{gx(4)}Y{gx(-2)}D01*"],
                            "B.Cu": []}, {})
    text = d356(rec("327", "USB_DP", "U1", "1", 1, 1, 1),
                rec("327", "USB_DP", "J1", "1", 4, 1, 1),
                rec("327", ipc_net_6, "U1", "6", 1, 2, 1),
                rec("327", ipc_net_6, "J2", "1", 4, 2, 1))
    bg = _BG([_Pad("U1", "1", "USB_DP"), _Pad("U1", "6", "USB_DP"),
              _Pad("J1", "1", "USB_DP"), _Pad("J2", "1", "USB_DP")])
    return fab, text, bg


def test_jumper_groups_read_both_forms():
    assert ipcd356.jumper_groups(JUMPER_PCB) == [("U1", ("1", "6")),
                                                ("U1", ("3", "4"))]
    dup = ('(kicad_pcb (footprint "SW"\n  (property "Reference" "SW1")\n'
           '  (duplicate_pad_numbers_are_jumpers yes)\n'
           '  (pad "1" smd) (pad "1" smd) (pad "2" smd)))\n'
           '(footprint "R" (property "Reference" "R1")\n'
           '  (duplicate_pad_numbers_are_jumpers no) (pad "1" smd) (pad "1" smd))')
    assert ipcd356.jumper_groups(dup) == [("SW1", ("1",))]


def test_jumper_split_with_one_name_is_an_open_and_renaming_clears_it(tmp_path):
    fab, text, bg = _jumper_case(tmp_path)
    jumpers = ipcd356.jumper_groups(JUMPER_PCB)
    d = ipcd356.parse(text)
    ipcd356.resolve(d, bg)
    assert [o["net"] for o in ipcd356.compare(fab, d)["opens"]] == ["USB_DP"]
    res = ipcd356.compare(fab, d, jumpers=jumpers)
    (op,) = res["opens"]
    assert op["jumper"] and op["shared_names"] == ["USB_DP"]
    (sp,) = res["jumper_splits"]
    assert not sp["own_names"]

    new, renames = ipcd356.split_jumper_islands(text, res["jumper_splits"])
    assert [(r["from"], r["to"], r["points"]) for r in renames] == \
        [("USB_DP", "USB_DP~J1", 2)]
    d2 = ipcd356.parse(new)
    assert sorted({p.net for p in d2.points}) == ["USB_DP", "USB_DP~J1"]
    assert len(new.splitlines()) == len(text.splitlines())
    ipcd356.resolve(d2, bg)
    res2 = ipcd356.compare(fab, d2, jumpers=jumpers)
    assert res2["opens"] == [] and res2["shorts"] == []
    assert res2["jumper_splits"][0]["own_names"]
    # renaming again changes nothing
    assert ipcd356.split_jumper_islands(new, res2["jumper_splits"])[1] == []


def test_a_real_open_on_a_jumper_net_stays_an_open(tmp_path):
    # J2 loses its track: three islands, one of them not at a jumper pad
    fab = stack(tmp_path, {"F.Cu": [flash(1, 1), flash(4, 1), flash(1, 2),
                                    flash(4, 2), "D11*",
                                    f"X{gx(1)}Y{gx(-1)}D02*",
                                    f"X{gx(4)}Y{gx(-1)}D01*"],
                           "B.Cu": []}, {})
    d = ipcd356.parse(d356(rec("327", "USB_DP", "U1", "1", 1, 1, 1),
                           rec("327", "USB_DP", "J1", "1", 4, 1, 1),
                           rec("327", "USB_DP", "U1", "6", 1, 2, 1),
                           rec("327", "USB_DP", "J2", "1", 4, 2, 1)))
    res = ipcd356.compare(fab, d, jumpers=ipcd356.jumper_groups(JUMPER_PCB))
    (op,) = res["opens"]
    assert not op.get("jumper") and op["n_fragments"] == 3
    assert res["jumper_splits"] == []


def test_long_names_are_cut_to_the_field():
    text = d356(rec("327", "_USB_D_P_LONG", "U1", "1", 1, 1, 1),
                rec("327", "_USB_D_P_LONG", "U1", "6", 1, 2, 1))
    sp = {"net": "x", "own_names": False, "points": [[2], [3]]}
    new, (r,) = ipcd356.split_jumper_islands(text, [sp])
    assert r["to"] == "SB_D_P_LONG~J1" and len(r["to"]) == 14
    assert ipcd356.parse(new).points[1].net == r["to"]


def test_dfm_check_passes_a_jumper_split_named_apart(tmp_path, monkeypatch):
    pcb = tmp_path / "j.kicad_pcb"
    pcb.write_text(JUMPER_PCB)
    fab, text, bg = _jumper_case(tmp_path / "g")
    gdir = tmp_path / "g"
    (gdir / "j.ipc").write_text(text)
    vios: list = []
    facts, skip = dfm_check.check_netlist(fab, pcb, bg, gdir, tmp_path, vios)
    assert skip is None and facts["netlist"]["opens"] == 1
    (v,) = vios
    assert v["kind"] == "dfm_net_open" and v["jumper"]

    # fab_export's rename, on the same gerbers
    import fab_export
    monkeypatch.setattr(geom, "load_board", lambda p: bg)
    renames = fab_export.name_jumper_islands(pcb, gdir / "j.ipc", gdir)
    assert [r["to"] for r in renames] == ["USB_DP~J1"]
    vios = []
    facts, _ = dfm_check.check_netlist(fab, pcb, bg, gdir, tmp_path, vios)
    assert vios == [] and facts["netlist"]["opens"] == 0
    assert facts["netlist"]["jumper_splits"] == 1

def test_the_rename_keeps_a_crlf_file_crlf(tmp_path, monkeypatch):
    import fab_export
    pcb = tmp_path / "j.kicad_pcb"
    pcb.write_text(JUMPER_PCB)
    fab, text, bg = _jumper_case(tmp_path / "g")
    ipc = tmp_path / "g" / "j.ipc"
    ipc.write_bytes(text.replace("\n", "\r\n").encode())
    monkeypatch.setattr(geom, "load_board", lambda p: bg)
    (r,) = fab_export.name_jumper_islands(pcb, ipc, tmp_path / "g")
    raw = ipc.read_bytes()
    assert b"USB_DP~J1" in raw and raw.count(b"\n") == raw.count(b"\r\n")
    assert not list((tmp_path / "g").glob("*.part"))


def test_dfm_check_names_a_failed_rename(tmp_path, monkeypatch):
    import fab_export
    import kc
    pcb = tmp_path / "j.kicad_pcb"
    pcb.write_text(JUMPER_PCB)
    fab, text, bg = _jumper_case(tmp_path / "g")

    def export(cli, pcb, f, gdir, bg):
        f.write_text(text)
        return {"status": "pass", "jumper_rename_error": "D356Error: bad"}
    monkeypatch.setattr(fab_export, "export_ipc_absolute", export)
    monkeypatch.setattr(kc, "resolve_cli", lambda: Path("cli"))
    vios: list = []
    facts, skip = dfm_check.check_netlist(fab, pcb, bg, tmp_path / "g",
                                          tmp_path, vios)
    assert skip is None and facts["netlist"]["source"] == "board"
    (v,) = vios
    assert v["jumper"] and "naming itself failed (D356Error: bad)" in v["msg"]


def test_dnp_and_cut_pin_clashes_are_no_jumpers():
    # nothing joins a do-not-populate part's pads on the assembled board
    for dnp in ('(attr smd dnp)', '(dnp yes)'):
        assert ipcd356.jumper_groups(JUMPER_PCB.replace(
            "(jumper_pad_groups", dnp + " (jumper_pad_groups")) == []
    # D-356 keeps 4 characters: SHLD1/SHLD2 would also catch SHLD3
    shld = ('(kicad_pcb (footprint "USB" (property "Reference" "J1")\n'
            '  (jumper_pad_groups ("SHLD1" "SHLD2") ("A1" "B1"))\n'
            '  (pad "SHLD1" smd) (pad "SHLD2" smd) (pad "SHLD3" smd)\n'
            '  (pad "A1" smd) (pad "B1" smd)))\n')
    assert ipcd356.jumper_groups(shld) == [("J1", ("A1", "B1"))]
    assert ipcd356.jumper_groups('(kicad_pcb (footprint "R"))') == []
    # and a duplicated pin cut to 4 characters would catch its neighbour
    dup = ('(kicad_pcb (footprint "SW" (property "Reference" "S1")\n'
           '  (duplicate_pad_numbers_are_jumpers yes)\n'
           '  (pad "ABCD1" smd) (pad "ABCD1" smd) (pad "ABCD2" smd)\n'
           '  (pad "7" smd) (pad "7" smd)))\n')
    assert ipcd356.jumper_groups(dup) == [("S1", ("7",))]


def test_islands_named_apart_but_joined_in_copper_are_a_short(tmp_path):
    # a stale rename: the copper now joins U1-1's island and U1-6's
    fab = stack(tmp_path, {"F.Cu": [flash(1, 1), flash(4, 1), flash(1, 2),
                                    flash(4, 2), "D11*",
                                    f"X{gx(1)}Y{gx(-1)}D02*",
                                    f"X{gx(4)}Y{gx(-1)}D01*",
                                    f"X{gx(1)}Y{gx(-2)}D02*",
                                    f"X{gx(4)}Y{gx(-2)}D01*",
                                    f"X{gx(4)}Y{gx(-1)}D02*",
                                    f"X{gx(4)}Y{gx(-2)}D01*"],
                            "B.Cu": []}, {})
    _, text, bg = _jumper_case(tmp_path / "x", ipc_net_6="USB_DP~J1")
    d = ipcd356.parse(text)
    ipcd356.resolve(d, bg)
    res = ipcd356.compare(fab, d, jumpers=ipcd356.jumper_groups(JUMPER_PCB))
    (sh,) = res["shorts"]
    assert sh["nets"] == ["USB_DP", "USB_DP~J1"] and sh["jumper"]
    assert res["opens"] == []


def test_duplicate_number_jumpers_split_and_rename(tmp_path):
    pcb = ('(kicad_pcb (footprint "SW" (property "Reference" "U1")\n'
           '  (duplicate_pad_numbers_are_jumpers yes)\n'
           '  (pad "1" smd) (pad "1" smd)))\n')
    fab, text, bg = _jumper_case(tmp_path)
    text = text.replace("U1    -6   ", "U1    -1   ")
    jumpers = ipcd356.jumper_groups(pcb)
    assert jumpers == [("U1", ("1",))]
    d = ipcd356.parse(text)
    ipcd356.resolve(d, bg)
    res = ipcd356.compare(fab, d, jumpers=jumpers)
    assert [o.get("jumper") for o in res["opens"]] == [True]
    new, (r,) = ipcd356.split_jumper_islands(text, res["jumper_splits"])
    d2 = ipcd356.parse(new)
    ipcd356.resolve(d2, bg)
    assert ipcd356.compare(fab, d2, jumpers=jumpers)["opens"] == []


def test_a_jumper_island_joined_through_a_via(tmp_path):
    # U1-6's island runs F.Cu -> via at (3, 2) -> B.Cu to J2-1
    fab = stack(tmp_path, {"F.Cu": [flash(1, 1), flash(4, 1), flash(1, 2),
                                    flash(3, 2), "D11*",
                                    f"X{gx(1)}Y{gx(-1)}D02*",
                                    f"X{gx(4)}Y{gx(-1)}D01*",
                                    f"X{gx(1)}Y{gx(-2)}D02*",
                                    f"X{gx(3)}Y{gx(-2)}D01*"],
                           "In1.Cu": [], "In2.Cu": [],
                           "B.Cu": [flash(3, 2), flash(4, 2), "D11*",
                                    f"X{gx(3)}Y{gx(-2)}D02*",
                                    f"X{gx(4)}Y{gx(-2)}D01*"]},
                {"s.drl": drill([(3, 2)])})
    text = d356(rec("327", "USB_DP", "U1", "1", 1, 1, 1),
                rec("327", "USB_DP", "J1", "1", 4, 1, 1),
                rec("327", "USB_DP", "U1", "6", 1, 2, 1),
                rec("327", "USB_DP", "J2", "1", 4, 2, 4))
    jumpers = ipcd356.jumper_groups(JUMPER_PCB)
    res = ipcd356.compare(fab, ipcd356.parse(text), jumpers=jumpers)
    (sp,) = res["jumper_splits"]
    assert sp["n_fragments"] == 2 and [o["jumper"] for o in res["opens"]] == [True]
    new, (r,) = ipcd356.split_jumper_islands(text, res["jumper_splits"])
    assert r["points"] == 2
    res2 = ipcd356.compare(fab, ipcd356.parse(new), jumpers=jumpers)
    assert res2["opens"] == [] and res2["shorts"] == []


def test_renames_resolve_aliases_stay_unique_and_keep_crlf():
    lines = ["P  CODE 00", "P  UNITS CUST 0", "P  NNAME1     /USB_DP",
             rec("327", "NNAME1", "U1", "1", 1, 1, 1),
             rec("327", "NNAME1", "U1", "6", 1, 2, 1),
             rec("327", "/USB_DP~J1", "U9", "1", 9, 9, 1), "999"]
    text = "\r\n".join(lines) + "\r\n"
    sp = {"net": "/USB_DP", "own_names": False, "points": [[3], [4]]}
    new, (r,) = ipcd356.split_jumper_islands(text, [sp])
    assert (r["from"], r["to"]) == ("/USB_DP", "/USB_DP~J2")
    assert new.count("\r\n") == text.count("\r\n") and "\n\n" not in new
    assert [p.net for p in ipcd356.parse(new).points] == \
        ["/USB_DP", "/USB_DP~J2", "/USB_DP~J1"]


def test_export_leaves_a_board_without_jumpers_and_survives_a_failed_rename(
        tmp_path, monkeypatch):
    import fab_export
    pcb = tmp_path / "r.kicad_pcb"
    pcb.write_text('(kicad_pcb (footprint "R" (property "Reference" "R1")))\n')
    ipc = tmp_path / "r.ipc"
    text = d356(rec("327", "A", "R1", "1", 1, 1, 1))
    ipc.write_text(text)
    assert fab_export.name_jumper_islands(pcb, ipc, tmp_path) == []
    assert ipc.read_text() == text

    monkeypatch.setattr(fab_export, "_export_ipc_absolute",
                        lambda cli, pcb, f: {"status": "pass"})
    def boom(*a, **k):
        raise ipcd356.D356Error("no gerbers")
    monkeypatch.setattr(fab_export, "name_jumper_islands", boom)
    res = fab_export.export_ipc_absolute(Path("cli"), pcb, ipc, tmp_path)
    assert res["status"] == "pass"
    assert res["jumper_rename_error"] == "D356Error: no gerbers"
    assert ipc.read_text() == text


def test_truncated_names_are_told_apart_and_reported():
    """kicad-cli keeps the last 14 characters: two unconnected-(...-PAD5)
    nets can share one D-356 name."""
    d = ipcd356.parse(d356(rec("327", "D-(U1-NC-PAD5)", "U1", "5", 1, 1, 1),
                           rec("327", "D-(U1-NC-PAD5)", "U9", "5", 9, 1, 1)))
    bg = _BG([_Pad("U1", "5", "unconnected-(U1-NC-PAD5)"),
              _Pad("U9", "5", "unconnected-(U9A-NC-PAD5)")])
    (clash,) = ipcd356.resolve(d, bg)
    assert len(clash["board_nets"]) == 2
    assert {p.key for p in d.points} == set(clash["board_nets"])


# ------------------------------------------------------------ dfm_check

def test_dfm_check_reports_the_netlist_family(tmp_path):
    gdir = pd_copy(tmp_path)
    shutil.copy(PD_IPC, gdir / "pd-trigger.ipc")
    rep = dfm_check.run(PD_PCB, fab_dir=gdir, polarity=False)
    assert rep["netlist"]["source"] == "package"
    assert rep["netlist"]["shorts"] == 0 and rep["netlist"]["opens"] == 0
    assert "netlist" in rep["coverage"]["passed"]

    bg = geom.load_board(PD_PCB)
    (t,) = bg.tracks_of("/D5_A")
    pen_up(gdir, "pd-trigger-F_Cu.gtl", t.shape.coords[0], t.shape.coords[-1])
    rep = dfm_check.run(PD_PCB, fab_dir=gdir, polarity=False)
    opens = [v for v in rep["violations"] if v["kind"] == "dfm_net_open"]
    assert [v["net"] for v in opens] == ["/D5_A"]
    assert opens[0]["severity"] == "error"
    assert "netlist" in rep["coverage"]["failed"]


def test_dfm_check_without_a_netlist_or_kicad_is_a_coverage_hole(
        tmp_path, monkeypatch):
    import kc
    monkeypatch.setattr(kc, "resolve_cli",
                        lambda: (_ for _ in ()).throw(RuntimeError("no cli")))
    rep = dfm_check.run(PD_PCB, fab_dir=pd_copy(tmp_path), polarity=False,
                        skip=("zone_fill",))
    assert "netlist" in rep["coverage"]["skipped_error"]


def test_order_submit_names_the_zipped_netlist(tmp_path):
    import order_submit
    z = tmp_path / "b_gerbers.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("b-F_Cu.gtl", "")
        zf.writestr("b.ipc", "")
    assert order_submit._zip_netlist(z) == "b.ipc"
    assert order_submit._zip_netlist(tmp_path / "none.zip") is None


# ------------------------------------------------------------ live (slow)

def _netconn_pad_components(bg, net) -> int:
    import netconn
    g = netconn.build(bg, net, include_zones=True)
    seen, n = set(), 0
    for p in (nd for nd in g.adj if nd[0] == "pad"):
        if p in seen:
            continue
        n += 1
        stack_, seen_ = [p], seen
        seen_.add(p)
        while stack_:
            for v, _w, _e in g.adj[stack_.pop()]:
                if v not in seen_:
                    seen_.add(v)
                    stack_.append(v)
    return n


@pytest.mark.parametrize("board", ["lite-main", "core"])
def test_pinned_board_ships_a_netlist_its_gerbers_match(board, tmp_path):
    """Known answer: the pinned boards are DRC-clean (0 unconnected), so the
    shipped copper must give 0 shorts / 0 opens against the shipped .ipc.
    netconn cross-check: it joins on true contacts only (no pad-to-fill,
    no T into a track body), so it may split a net the copper joins but
    never join what the copper splits: gerber fragments <= netconn's."""
    import fab_export
    # on a copy: kicad-cli leaves a .kicad_prl beside the board, and any
    # file in pinned/ moves bin/score.py's digest
    shutil.copytree(REPO / "pinned" / board / "kicad", tmp_path / "kicad")
    pcb = tmp_path / "kicad" / f"{board}.kicad_pcb"
    man = fab_export.run(pcb, tmp_path / "fab")
    ipc = Path(man["ipc_netlist"])
    assert ipc.is_file() and ipc.parent == Path(man["gerber_dir"])
    with zipfile.ZipFile(man["gerber_zip"]) as zf:
        assert ipc.name in zf.namelist()

    bg = geom.load_board(pcb)
    d = ipcd356.parse(ipc.read_text())
    assert ipcd356.resolve(d, bg) == []
    res = ipcd356.compare(gerblib.open_fab(man["gerber_dir"]), d,
                          ipcd356.net_tie_classes(pcb.read_text(), bg))
    assert res["shorts"] == [] and res["opens"] == []
    assert res["tie_opens"] == []
    for net, frags in res["fragments_per_net"].items():
        assert frags <= _netconn_pad_components(bg, net), net

    rep = dfm_check.run(pcb, fab_dir=tmp_path / "fab", polarity=False,
                        skip=("zone_fill", "placement"))
    assert rep["netlist"]["source"] == "package"
    assert not [v for v in rep["violations"] if v["kind"] in (
        "dfm_net_short", "dfm_net_open", "dfm_net_tie_open")]


def test_layers_named_by_the_board_are_found_by_protel_extension(tmp_path):
    """An Altium import keeps its layer names, so kicad-cli writes
    'b-Top Solder.gts', 'b-Bottom Overlay.gbo': mask/silk/paste by extension."""
    for n in ("b-Top Layer.gtl", "b-Bottom Layer.gbl", "b-Top Solder.gts",
              "b-Bottom Solder.gbs", "b-Top Overlay.gto", "b-Bottom Overlay.gbo",
              "b-Top Paste.gtp", "b-Edge_Cuts.gm1"):
        (tmp_path / n).write_text("G04 empty*\nM02*\n")
    fab = gerblib.FabStack(tmp_path)
    assert set(fab.copper_files) == {"F.Cu", "B.Cu"}
    assert set(fab.mask_files) == {"F", "B"} and set(fab.silk_files) == {"F", "B"}
    assert set(fab.paste_files) == {"F"} and fab.edge_file is not None


def test_ipc_netlist_of_a_board_with_an_aux_origin_is_absolute(tmp_path):
    """kicad-cli writes IPC-D-356 from the aux origin; the gerbers are
    absolute. fab_export's netlist must land where the gerbers' copper is."""
    import fab_export
    import kc
    try:
        cli = kc.resolve_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("no kicad-cli")
    moved = tmp_path / "pd-trigger.kicad_pcb"
    text = PD_PCB.read_text(encoding="utf-8")
    text = re.sub(r"\(aux_axis_origin [-\d.]+ [-\d.]+\)\s*", "", text)
    moved.write_text(text.replace("(setup\n", "(setup\n\t\t(aux_axis_origin 50 80)\n", 1),
                     encoding="utf-8")
    assert fab_export.aux_origin(moved) == (50.0, 80.0)
    plain = tmp_path / "plain.ipc"
    kc.export_ipcd356(cli, PD_PCB, plain)
    fixed = tmp_path / "fixed.ipc"
    assert fab_export.export_ipc_absolute(cli, moved, fixed)["status"] == "pass"

    def coords(p):
        return sorted(ln[: 80] for ln in p.read_text().splitlines() if ln[:3] in ("317", "327"))
    assert coords(fixed) == coords(plain)
    shifted = tmp_path / "shifted.ipc"
    kc.export_ipcd356(cli, moved, shifted)
    assert coords(shifted) != coords(plain)  # what kicad-cli alone writes


def test_roundrect_flash_is_one_pad_of_its_full_size(tmp_path):
    """KiCad's RoundRect macro = body polygon + 4 corner circles + 4 edge
    lines. Read as one pad, a 1.524 x 1.778 mm pad is that size (the body
    alone was 1.143 x 1.397: every ring check came up short by the radius)."""
    g = ("%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\n%AMRoundRect*\n"
         "4,1,4,$2,$3,$4,$5,$6,$7,$8,$9,$2,$3,0*\n"
         "1,1,$1+$1,$2,$3*\n1,1,$1+$1,$4,$5*\n1,1,$1+$1,$6,$7*\n1,1,$1+$1,$8,$9*\n"
         "20,1,$1+$1,$2,$3,$4,$5,0*\n20,1,$1+$1,$4,$5,$6,$7,0*\n"
         "20,1,$1+$1,$6,$7,$8,$9,0*\n20,1,$1+$1,$8,$9,$2,$3,0*%\n"
         "%ADD18RoundRect,0.190500X0.571500X0.698500X-0.571500X0.698500X"
         "-0.571500X-0.698500X0.571500X-0.698500X0*%\n"
         "D18*\nX10000000Y-20000000D03*\nM02*\n")
    (tmp_path / "b-F_Cu.gtl").write_text(g)
    lg = gerblib.FabStack(tmp_path).copper("F.Cu")
    assert len(lg.pads) == 1
    x0, y0, x1, y1 = lg.pads[0].bounds
    assert x1 - x0 == pytest.approx(1.524, abs=0.01) and y1 - y0 == pytest.approx(1.778, abs=0.01)
