"""#489: the routers draw rules_gen's per-layer impedance widths.

rules_gen writes layer-scoped stripline width/gap rules (#323); zrules reads
them back, route_critical hands them to KRT (lib/krt_launch.py) and
route_auto writes them into the DSN as class layer_rules for Freerouting, so
an inner run is born at the width DRC grades it against.

Known answers (JLC06161H-3313, pours on In1/In3/In4, In2 stripline): 100 ohm
pair on In2 = 0.0889 mm (the fab floor) with a 0.1681 mm gap, outer F.Cu
0.0948/0.13; 50 ohm single-ended on In2 = 0.1194 mm. The live tests build a
6-layer board whose F.Cu/B.Cu are kept out across the middle, so the pair
and the 50 ohm net must go L1 -> L3 (In2) and back.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
REFERENCE = REPO / "reference"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import board_init  # noqa: E402
import check_diffpair  # noqa: E402
import env  # noqa: E402
import geom  # noqa: E402
import route_critical as rc  # noqa: E402
import routelib  # noqa: E402
import rules_gen  # noqa: E402
import zrules  # noqa: E402

STACKUP = "JLC06161H-3313"
PLANES = [{"layer": "In1.Cu", "net": "GND"}, {"layer": "In3.Cu", "net": "GND"},
          {"layer": "In4.Cu", "net": "GND"}]
CONS = {"planes": PLANES,
        "diff_pairs": [{"p": "/CK_P", "n": "/CK_N", "base": "CK", "impedance_ohm": 100}],
        "high_speed": [{"net": "/D0", "reference": "GND", "impedance_ohm": 50}]}


def _stackup():
    return yaml.safe_load((REFERENCE / "stackups.yaml").read_text("utf-8"))["stackups"][STACKUP]


def _cap():
    return yaml.safe_load((REFERENCE / "jlc_capabilities.yaml").read_text("utf-8")
                          )["design_rules"]["6layer_1oz"]


def _dru(bga_refs=None) -> str:
    cap, stk = _cap(), _stackup()
    floor = float(cap["min_trace_width_mm"])
    pairs = rules_gen.detect_diff_pairs(CONS)
    dr, _ = rules_gen.diff_pair_rules(pairs, stk, CONS, floor, bga_refs)
    zr, _ = rules_gen.impedance_rules(CONS, stk, pairs, floor, bga_refs)
    return rules_gen.render_dru(dr + zr)


# ------------------------------------------------------------ zrules

def test_layer_geometry_reads_rules_gen_output():
    geo = zrules.layer_geometry(_dru())
    ck = geo["/CK_P"]["In2.Cu"]
    assert ck["width_mm"] == pytest.approx(0.0889)
    assert ck["width_min_mm"] == pytest.approx(0.0889)     # never under the floor
    assert ck["width_max_mm"] == pytest.approx(0.0978)
    assert ck["gap_mm"] == pytest.approx(0.1681)
    assert ck["gap_min_mm"] == pytest.approx(0.1513)
    assert geo["/CK_N"] == geo["/CK_P"]
    d0 = geo["/D0"]["In2.Cu"]
    assert d0["width_mm"] == pytest.approx(0.1194) and "gap_mm" not in d0
    # outer-only and guard rules carry no per-layer width
    assert set(geo) == {"/CK_P", "/CK_N", "/D0"}
    assert all(set(v) == {"In2.Cu"} for v in geo.values())
    assert zrules.layer_widths(geo, ["/CK_P", "/CK_N"]) == {"In2.Cu": 0.0889}


def test_layer_geometry_ignores_bga_courtyard_terms():
    geo = zrules.layer_geometry(_dru(["U100"]))
    assert set(geo) == {"/CK_P", "/CK_N", "/D0"}
    assert geo["/D0"]["In2.Cu"]["width_mm"] == pytest.approx(0.1194)


def test_layer_geometry_empty_and_missing(tmp_path):
    assert zrules.layer_geometry("") == {}
    assert zrules.board_layer_geometry(tmp_path / "x.kicad_pcb") == {}


# ------------------------------------------------------------ DSN layer rules

_DSN = '''(pcb x
  (resolution um 10)
  (structure
    (layer F.Cu (type signal) (property (index 0)))
    (layer In2.Cu (type signal) (property (index 2)))
    (layer B.Cu (type signal) (property (index 5)))
  )
  (network
    (class kicad_default /D0 GND "Net-(C1-Pad2)"
      (circuit
        (use_via "Via[0-5]_450:200_um")
      )
      (rule
        (width 200)
        (clearance 125)
      )
    )
    (class DP100 /CK_P
      (circuit
        (use_via "Via[0-5]_450:200_um")
      )
      (rule
        (width 94.8)
        (clearance 125)
      )
    )
  )
)
'''


def test_dsn_apply_layer_widths():
    text, done = routelib.dsn_apply_layer_widths(
        _DSN, {"/D0": {"In2.Cu": 0.1194, "In7.Cu": 0.1},
               "/CK_P": {"In2.Cu": 0.0889}})
    assert sorted(done) == ["/CK_P", "/D0"]
    # /D0 leaves the shared class for its own, keeping the class rules
    assert '(class kicad_default GND "Net-(C1-Pad2)"' in text
    d0 = text[text.index("(class aiee__D0 /D0"):]
    d0 = d0[:d0.index("(class DP100")]
    assert "(width 200)" in d0 and '(use_via "Via[0-5]_450:200_um")' in d0
    assert "(layer_rule In2.Cu\n        (rule (width 119.4))" in d0
    assert "In7.Cu" not in text                     # not a DSN layer: skipped
    # a net alone in its class keeps the class name
    ck = text[text.index("(class DP100 /CK_P"):]
    assert "(width 94.8)" in ck and "(rule (width 88.9))" in ck
    assert text.count("(class ") == 3
    # balanced s-expression
    assert text.count("(") == text.count(")")
    # nothing to do -> unchanged
    assert routelib.dsn_apply_layer_widths(_DSN, {}) == (_DSN, [])
    assert routelib.dsn_apply_layer_widths(_DSN, {"/X": {"In9.Cu": 0.1}}) == (_DSN, [])


_BAN_DRU = """(version 1)
(rule "aiee_diff_outer_only_CK_In4"
\t(layer "In4.Cu")
\t(constraint disallow track)
\t(condition "A.NetName == '/CK_P' || A.NetName == '/CK_N'")
)
(rule "aiee_diff_outer_only_X"
\t(layer inner)
\t(constraint disallow track)
\t(condition "A.NetName == '/X'")
)
(rule "not_a_net_list"
\t(layer "In2.Cu")
\t(constraint disallow track)
\t(condition "A.NetClass == 'HS'")
)
(rule "a_width"
\t(layer "In2.Cu")
\t(constraint track_width (min 0.1mm))
\t(condition "A.NetName == '/D0'")
)
"""
_SIX = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]


def test_dru_track_bans():
    """#1108: rules_gen's disallow-track layer rules, per net."""
    bans = routelib.dru_track_bans(_BAN_DRU, _SIX)
    assert bans == {"/CK_P": {"In4.Cu"}, "/CK_N": {"In4.Cu"},
                    "/X": {"In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu"}}
    assert routelib.dru_track_bans("", _SIX) == {}


def test_dsn_apply_track_bans():
    """#1108: a banned layer leaves the class's use_layer; a class whose
    nets are banned differently is split, rules and vias kept."""
    bans = {"/CK_P": {"In2.Cu"}, "/D0": {"In2.Cu", "B.Cu"}}
    text, done = routelib.dsn_apply_track_bans(_DSN, bans)
    assert sorted((d["net"], tuple(d["use_layer"])) for d in done) == [
        ("/CK_P", ("F.Cu", "B.Cu")), ("/D0", ("F.Cu",))]
    ck = text[text.index("(class DP100 /CK_P"):]
    assert "(use_layer F.Cu B.Cu)" in ck and "(width 94.8)" in ck
    assert '(class kicad_default GND "Net-(C1-Pad2)"' in text
    d0 = text[text.index("(class aiee_ban__D0 /D0"):]
    d0 = d0[:d0.index("(class DP100")]
    assert "(use_layer F.Cu)" in d0 and "(width 200)" in d0
    assert '(use_via "Via[0-5]_450:200_um")' in d0
    assert text.count("(use_layer") == 2 and text.count("(class ") == 3
    assert text.count("(") == text.count(")")
    # banned from every layer, or from a layer the DSN lacks: left alone
    every = {"/CK_P": {"F.Cu", "In2.Cu", "B.Cu"}, "/D0": {"In9.Cu"}}
    assert routelib.dsn_apply_track_bans(_DSN, every) == (_DSN, [])
    assert routelib.dsn_apply_track_bans(_DSN, {}) == (_DSN, [])


# ------------------------------------------------------------ route_critical

def _board(tmp_path, keepout=True, name="z6") -> Path:
    """JLC 6L board: U1 <-> J1 carrying /CK_P,/CK_N (0.5 mm pitch) and /D0,
    40 mm apart; F.Cu+B.Cu track keepout across x 25..35 (keepout=True)."""
    def pad(num, y, net):
        return (f'    (pad "{num}" smd rect (at 0 {y}) (size 0.6 0.3)\n'
                f'      (layers "F.Cu" "F.Mask") (net "{net}") (uuid "u-{num}-{net}"))\n')

    def fp(ref, x):
        pads = pad("1", 0.0, "/CK_P") + pad("2", 0.5, "/CK_N") + pad("3", 2.0, "/D0")
        return (f'  (footprint "t:{ref}" (layer "F.Cu") (at {x} 14)\n'
                f'    (property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS") '
                f'(uuid "r-{ref}"))\n{pads}  )\n')
    ko = '''  (zone (net 0) (net_name "") (layers "F.Cu" "B.Cu") (uuid "ko-1") (name "ko")
    (hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.25)
    (keepout (tracks not_allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed))
    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 25 0.5) (xy 35 0.5) (xy 35 29.5) (xy 25 29.5))))
''' if keepout else ""
    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text(f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal)
    (8 "In3.Cu" signal) (10 "In4.Cu" signal) (2 "B.Cu" signal)
    (13 "F.Paste" user) (1 "F.Mask" user) (3 "B.Mask" user)
    (5 "F.SilkS" user "F.Silkscreen") (31 "F.CrtYd" user "F.Courtyard")
    (25 "Edge.Cuts" user))
  (setup
  )
  (net 0 "") (net 1 "/CK_P") (net 2 "/CK_N") (net 3 "/D0")
  (gr_rect (start 0 0) (end 60 30) (stroke (width 0.1)) (fill no) (layer "Edge.Cuts"))
{fp("U1", 8)}{fp("J1", 52)}{ko})
""", encoding="utf-8")
    board_init.inject_stackup(pcb, _stackup())
    pcb.with_suffix(".kicad_pro").write_text(
        json.dumps(board_init.build_pro(name, _cap())), encoding="utf-8")
    cons = tmp_path / "constraints.json"
    cons.write_text(json.dumps(CONS), encoding="utf-8")
    assert rules_gen.main(["--constraints", str(cons), "--layers", "6",
                           "--stackup", STACKUP,
                           "--out-dru", str(pcb.with_suffix(".kicad_dru")),
                           "--pro", str(pcb.with_suffix(".kicad_pro")),
                           "--out", str(tmp_path / "rg.json")]) == 0
    return pcb


def test_pair_layer_plan_one_pitch_every_layer_on_target(tmp_path):
    pcb = _board(tmp_path)
    bg = geom.BoardGeom.from_file(pcb)
    plan = rc.pair_layer_plan(bg, 100, None, zrules.board_layer_geometry(pcb),
                              "/CK_P", "/CK_N")
    # In2's own pitch (0.0889 + 0.1681) is the widest; the outer pair is
    # re-solved to 100 ohm at that pitch
    assert plan["pitch_mm"] == pytest.approx(0.257, abs=1e-3)
    lw = plan["layer_widths"]
    assert lw["In2.Cu"] == pytest.approx(0.0889)
    assert lw["F.Cu"] == lw["B.Cu"] == plan["track_width"]
    assert plan["track_width"] + plan["gap"] == pytest.approx(plan["pitch_mm"], abs=1e-4)
    assert plan["layers"] == ["F.Cu", "In2.Cu", "B.Cu"]
    assert plan["costs"] == ["1", rc.STRIPLINE_COST, "3"]
    for lay, w in lw.items():
        z, _ = check_diffpair.layer_zdiff(bg, lay, w, plan["pitch_mm"] - w)
        assert z == pytest.approx(100, rel=0.02), lay
    # a pair with no per-layer rules routes as before (outer only)
    assert rc.pair_layer_plan(bg, 100, None, {}, "/CK_P", "/CK_N") is None


def test_pair_via_size_clears_hole_to_hole():
    jlc6 = {"via_diameter": 0.45, "via_drill": 0.2, "clearance": 0.125,
            "hole_to_hole": 0.5}
    # KRT spaces the P/N vias via + clearance apart: 0.575 + 0.125 = 0.7
    # = drill + hole_to_hole
    assert rc.pair_via_size(jlc6) == pytest.approx(0.575)
    assert rc.pair_via_size({**jlc6, "hole_to_hole": 0.25}) == pytest.approx(0.45)
    assert rc.pair_via_size(dict(rc.KICAD_DEFAULTS)) == rc.KICAD_DEFAULTS["via_diameter"]


def test_grading_floors_reads_hole_to_hole(tmp_path):
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(json.dumps(board_init.build_pro("b", _cap())), encoding="utf-8")
    assert rc.grading_floors(pro)["hole_to_hole"] == pytest.approx(
        float(_cap()["min_hole_to_hole_mm"]))


def test_run_krt_layer_widths_go_through_the_launcher(tmp_path, monkeypatch):
    seen = {}

    class CP:
        returncode = 0
        stdout = 'JSON_SUMMARY: {"successful": 1}\n'
        stderr = ""

    def fake_run(args, **kw):
        seen["args"] = args
        return CP()

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    out = tmp_path / "o.kicad_pcb"
    rc.run_krt(tmp_path, "route_diff.py", tmp_path / "b.kicad_pcb", out, [],
               rc.KICAD_DEFAULTS, tmp_path / "fab.txt", 0.05, 60,
               layer_widths={"F.Cu": 0.1, "In2.Cu": 0.0889})
    a = seen["args"]
    assert a[1].endswith("krt_launch.py") and a[2] == "--layer-widths"
    assert a[4:6] == ["--via-joint-ends", "route_diff.py"]
    assert json.loads(Path(a[3]).read_text()) == {"F.Cu": 0.1, "In2.Cu": 0.0889}
    rc.run_krt(tmp_path, "route_diff.py", tmp_path / "b.kicad_pcb", out, [],
               rc.KICAD_DEFAULTS, tmp_path / "fab.txt", 0.05, 60)
    assert seen["args"][1].endswith("krt_launch.py")
    assert seen["args"][2:4] == ["--via-joint-ends", "route_diff.py"]
    # no flag: still the launcher, which always keeps net-tie copper (#1008)
    rc.run_krt(tmp_path, "route.py", tmp_path / "b.kicad_pcb", out, [],
               rc.KICAD_DEFAULTS, tmp_path / "fab.txt", 0.05, 60)
    assert seen["args"][1].endswith("krt_launch.py")
    assert seen["args"][2] == "route.py"


def test_launcher_fills_layer_widths(tmp_path):
    """krt_launch.py --layer-widths against a stand-in KRT: the config the script
    builds carries the widths; one built with its own widths keeps them."""
    import subprocess
    (tmp_path / "routing_config.py").write_text(
        "from dataclasses import dataclass, field\n"
        "@dataclass\nclass GridRouteConfig:\n"
        "    track_width: float = 0.2\n"
        "    layer_widths: dict = field(default_factory=dict)\n", encoding="utf-8")
    (tmp_path / "route_x.py").write_text(
        "import sys, json\nfrom routing_config import GridRouteConfig\n"
        "a = GridRouteConfig(track_width=0.1)\n"
        "b = GridRouteConfig(layer_widths={'B.Cu': 0.3})\n"
        "print(json.dumps([a.layer_widths, b.layer_widths, sys.argv[1:]]))\n",
        encoding="utf-8")
    w = tmp_path / "w.json"
    w.write_text('{"In2.Cu": 0.0889}', encoding="utf-8")
    cp = subprocess.run([sys.executable, str(SCRIPTS / "lib" / "krt_launch.py"),
                         "--layer-widths", str(w), "route_x.py", "--nets", "/A"], cwd=tmp_path,
                        capture_output=True, text=True, check=True)
    assert json.loads(cp.stdout) == [{"In2.Cu": 0.0889}, {"B.Cu": 0.3}, ["--nets", "/A"]]


# ------------------------------------------------------------ live

def _drc(pcb: Path) -> dict:
    import kc
    return kc.run_drc(env.find_kicad_cli(), pcb, all_track_errors=True)


def _in2_widths(pcb: Path) -> dict:
    bg = geom.BoardGeom.from_file(pcb)
    return {net: sorted({round(t.width, 4) for t in bg.tracks_of(net)
                         if t.layer == "In2.Cu"})
            for net in ("/CK_P", "/CK_N", "/D0")}


def test_live_route_critical_pair_l1_l3_drc_clean(tmp_path):
    """KRT routes the 100 ohm pair and the 50 ohm net F.Cu -> In2 -> F.Cu at
    the DRU's per-layer widths: DRC 0 with no set_width fix, and Zdiff on
    each layer within 10 % of 100 ohm."""
    if env.find_kicad_cli() is None or env.find_krt() is None:
        pytest.skip("needs kicad-cli and KiCadRoutingTools")
    pcb = _board(tmp_path)
    out = tmp_path / "rc.json"
    code = rc.main(["--pcb", str(pcb), "--constraints", str(tmp_path / "constraints.json"),
                    "--out-report", str(out), "--timeout-s", "300"])
    rep = json.loads(out.read_text())
    assert code == 0, rep["violations"]
    routed = {tuple(r["nets"]): r for r in rep["facts"]["routed"]}
    pair = routed[("/CK_P", "/CK_N")]
    assert "In2.Cu" in pair["layers_used"]
    assert "In2.Cu" in routed[("/D0",)]["layers_used"]
    drc = _drc(pcb)
    assert drc["violations"] == [], drc["violations"]
    w = _in2_widths(pcb)
    assert w["/CK_P"] == w["/CK_N"] == [0.0889]
    assert w["/D0"] == [0.1194]
    bg = geom.BoardGeom.from_file(pcb)
    _, facts = check_diffpair.check_pair(bg, {"p": "/CK_P", "n": "/CK_N",
                                              "impedance_ohm": 100})
    layers = facts["impedance"]["layers"]
    assert {"F.Cu", "In2.Cu"} <= set(layers)
    for lay, row in layers.items():
        assert row["zdiff_ohm"] == pytest.approx(100, rel=0.10), (lay, row)


_SEEN: list[str] = []


def _no_display(capsys) -> bool:
    text = capsys.readouterr().out
    _SEEN.append(text)
    # route_swig's KiCad needs wx on an Xvfb; under xvfb-run -a with other
    # users' displays about one run in five fails to start it, either loud
    # (wxEntryStart) or as a swallowed import_ses failure
    return any(k in text for k in ("wxEntryStart", "X Display",
                                   "importable session"))


def test_live_freerouting_draws_dsn_layer_widths(tmp_path, capsys):
    """route_auto alone (Freerouting): the DSN's class layer_rules make the
    In2 runs come out at the DRU widths, DRC 0."""
    if env.find_kicad_cli() is None or env.find_freerouting_jar() is None \
            or env.find_java() is None:
        pytest.skip("needs kicad-cli, java and Freerouting")
    import route_auto
    pcb = _board(tmp_path)
    out = tmp_path / "ra.json"
    for attempt in (1, 2, 3):
        code = route_auto.main(["--pcb", str(pcb), "--out-report", str(out),
                                "--timeout-s", "300", "--no-krt-finish",
                                "--no-maze-finish"])
        rep = json.loads(out.read_text()) if out.is_file() else {}
        if code == 0 or not _no_display(capsys):
            break
    else:
        # KiCad's SWIG worker could not start wx on this machine's Xvfb
        # three times: an environment fault, not a routing result
        pytest.skip("route_swig: wxEntryStart failed (no usable X display)")
    assert code == 0, (rep, _SEEN, capsys.readouterr().out)
    assert sorted(rep["facts"]["dsn_layer_widths"]) == ["/CK_N", "/CK_P", "/D0"]
    # the DRU's unsized-inner guards reach the DSN: the pair and the 50 ohm
    # net may use only the outer layers and the In2 stripline
    bans = {b["net"]: b["use_layer"] for b in rep["facts"]["dsn_track_bans"]}
    assert sorted(bans) == ["/CK_N", "/CK_P", "/D0"]
    assert all(v == ["F.Cu", "In2.Cu", "B.Cu"] for v in bans.values()), bans
    drc = _drc(pcb)
    assert drc["violations"] == [], drc["violations"]
    w = _in2_widths(pcb)
    assert w["/D0"] == [0.1194]
    for net in ("/CK_P", "/CK_N"):        # DSN resolution is 0.1 um
        assert w[net] and all(0.0889 <= x <= 0.0978 for x in w[net]), w
