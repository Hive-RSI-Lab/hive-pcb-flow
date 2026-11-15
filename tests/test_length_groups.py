"""check_length_groups (hive-hwde #324): routed length per net (track plus via
barrel from the stackup, pad to pad) against its group's reference."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_length_groups as clg  # noqa: E402
import checklib  # noqa: E402
import geom  # noqa: E402

STACKUP4 = """  (setup (stackup
    (layer "F.Cu" (type "copper") (thickness 0.035))
    (layer "dielectric 1" (type "prepreg") (thickness 0.1) (epsilon_r 4.1))
    (layer "In1.Cu" (type "copper") (thickness 0.0152))
    (layer "dielectric 2" (type "core") (thickness 1.2) (epsilon_r 4.6))
    (layer "In2.Cu" (type "copper") (thickness 0.0152))
    (layer "dielectric 3" (type "prepreg") (thickness 0.1) (epsilon_r 4.1))
    (layer "B.Cu" (type "copper") (thickness 0.035))))
"""


def _pcb(tmp_path, body: str, name="b", four=False) -> Path:
    layers = ('(0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) '
              '(2 "B.Cu" signal)' if four else '(0 "F.Cu" signal) (2 "B.Cu" signal)')
    text = f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers {layers} (25 "Edge.Cuts" user))
{STACKUP4 if four else "  (setup)"}
  (gr_rect (start 0 0) (end 60 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
"""
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(text, encoding="utf-8")
    return p


def fp(ref, x, y, pads) -> str:
    """A footprint at (x, y) with SMD pads [(number, dx, dy, net)] on F.Cu."""
    out = [f'  (footprint "t:{ref}" (at {x} {y}) (layer "F.Cu")\n'
           f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n']
    for num, dx, dy, net in pads:
        out.append(f'    (pad "{num}" smd rect (at {dx} {dy}) (size 0.4 0.4) '
                   f'(layers "F.Cu") (net "{net}"))\n')
    return "".join(out) + "  )\n"


def seg(x1, y1, x2, y2, net, layer="F.Cu", w=0.15) -> str:
    return (f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) '
            f'(layer "{layer}") (net "{net}"))\n')


def via(x, y, net, layers=("F.Cu", "B.Cu")) -> str:
    ls = " ".join(f'"{l}"' for l in layers)
    return (f'  (via (at {x} {y}) (size 0.45) (drill 0.2) (layers {ls}) '
            f'(net "{net}"))\n')


def straight(net, y, x0=5, x1=45, extra=0.0) -> str:
    """U1 pad at x0 to U2 pad at x1 on row y, with an optional detour that
    adds `extra` mm (a rectangular bump of height extra/2)."""
    if not extra:
        return seg(x0, y, x1, y, net)
    h = extra / 2
    m = (x0 + x1) / 2
    return (seg(x0, y, m, y, net) + seg(m, y, m, y + h, net)
            + seg(m, y + h, m + 1, y + h, net) + seg(m + 1, y + h, m + 1, y, net)
            + seg(m + 1, y, x1, y, net))


def board_bus(tmp_path, extras: dict, name="bus"):
    """Nets /D0../D3 and /DQS from U1 (x=5) to U2 (x=45), row y = 5 + 3 i."""
    nets = ["/D0", "/D1", "/D2", "/D3", "/DQS"]
    pads1 = [(str(i + 1), 0, 3 * i, n) for i, n in enumerate(nets)]
    pads2 = [(str(i + 1), 0, 3 * i, n) for i, n in enumerate(nets)]
    body = fp("U1", 5, 5, pads1) + fp("U2", 45, 5, pads2)
    for i, n in enumerate(nets):
        body += straight(n, 5 + 3 * i, extra=extras.get(n, 0.0))
    return _pcb(tmp_path, body, name)


def run(pcb, groups=None, tmp_path=None):
    argv = ["--pcb", str(pcb)]
    if groups is not None:
        c = Path(pcb).with_suffix(".json")
        c.write_text(json.dumps({"length_groups": groups}), encoding="utf-8")
        argv += ["--constraints", str(c)]
    payload, _ = clg.run(argv)
    return payload


def kinds(p):
    return sorted(v["kind"] for v in p["violations"])


# ------------------------------------------------------------ measurement

def test_net_length_pad_to_pad_ignores_stub(tmp_path):
    body = (fp("U1", 5, 5, [("1", 0, 0, "/A")]) + fp("U2", 45, 5, [("1", 0, 0, "/A")])
            + seg(5, 5, 45, 5, "/A") + seg(20, 5, 20, 15, "/A"))   # 10 mm stub
    bg = geom.load_board(_pcb(tmp_path, body))
    m = clg.net_length(bg, "/A")
    assert m["connected"] and m["length_mm"] == pytest.approx(40.0)
    assert m["total_track_mm"] == pytest.approx(50.0)
    assert {m["from"], m["to"]} == {"U1.1", "U2.1"}


def test_via_barrel_from_stackup(tmp_path):
    # F.Cu 5..25, via, B.Cu 25..45: barrel = 0.1 + 1.2 + 0.1 dielectric plus
    # the two inner coppers 2 * 0.0152
    body = (fp("U1", 5, 5, [("1", 0, 0, "/A")])
            + '  (footprint "t:U2" (at 45 5) (layer "B.Cu")\n'
              '    (property "Reference" "U2" (at 0 0 0) (layer "B.SilkS"))\n'
              '    (pad "1" smd rect (at 0 0) (size 0.4 0.4) (layers "B.Cu") (net "/A")))\n'
            + seg(5, 5, 25, 5, "/A") + via(25, 5, "/A")
            + seg(25, 5, 45, 5, "/A", layer="B.Cu"))
    bg = geom.load_board(_pcb(tmp_path, body, four=True))
    m = clg.net_length(bg, "/A")
    barrel = 0.1 + 1.2 + 0.1 + 2 * 0.0152
    assert m["vias"] == 1
    assert m["via_mm"] == pytest.approx(barrel, abs=1e-4)
    assert m["length_mm"] == pytest.approx(40 + barrel, abs=1e-4)
    assert m["layers"] == {"B.Cu": 20.0, "F.Cu": 20.0}
    assert clg.barrel_mm(bg, "F.Cu", "In1.Cu") == pytest.approx(0.1)
    assert clg.barrel_mm(bg, "F.Cu", "F.Cu") == 0.0


def test_inner_layer_is_slower(tmp_path):
    bg = geom.load_board(_pcb(tmp_path, "", four=True))
    assert clg.layer_eps_eff(bg, "In1.Cu") > clg.layer_eps_eff(bg, "F.Cu")


def test_terminals_limit_the_path(tmp_path):
    # /A runs U1 -> R1 -> U2; with terminals [U1, R1] only that leg counts
    body = (fp("U1", 5, 5, [("1", 0, 0, "/A")]) + fp("R1", 25, 5, [("1", 0, 0, "/A")])
            + fp("U2", 45, 5, [("1", 0, 0, "/A")]) + seg(5, 5, 25, 5, "/A")
            + seg(25, 5, 45, 5, "/A"))
    bg = geom.load_board(_pcb(tmp_path, body))
    assert clg.net_length(bg, "/A")["length_mm"] == pytest.approx(40.0)
    assert clg.net_length(bg, "/A", ["U1", "R1"])["length_mm"] == pytest.approx(20.0)


# ------------------------------------------------------------ groups

def test_matched_group_passes(tmp_path):
    p = run(board_bus(tmp_path, {"/D2": 0.4}),
            [{"name": "BYTE0", "nets": ["D[0-3]"], "ref_nets": ["DQS"],
              "tol_mm": 0.635}])
    assert p["status"] == "pass", p["violations"]
    g = p["groups"][0]
    assert g["reference_mm"] == pytest.approx(40.0)
    assert len(g["nets"]) == 4 and g["spread_mm"] == pytest.approx(0.4)


def test_long_member_fails_with_adjust(tmp_path):
    p = run(board_bus(tmp_path, {"/D1": 2.0}),
            [{"name": "BYTE0", "nets": ["/D*"], "ref_net": "/DQS", "tol_mm": 0.635}])
    assert kinds(p) == ["length_group_mismatch"]
    v = p["violations"][0]
    assert v["net"] == "/D1" and v["severity"] == "error"
    assert v["delta_mm"] == pytest.approx(2.0)
    assert v["adjust_mm"] == pytest.approx(0.635 - 2.0)


def test_no_reference_matches_to_longest(tmp_path):
    # D3 is 3 mm longer than the rest: the three short ones are flagged
    p = run(board_bus(tmp_path, {"/D3": 3.0}),
            [{"name": "B", "nets": ["D0", "D1", "D2", "D3"], "ref_nets": [],
              "tol_mm": 1.0}])
    assert sorted(v["net"] for v in p["violations"]) == ["/D0", "/D1", "/D2"]
    assert all(v["adjust_mm"] == pytest.approx(2.0) for v in p["violations"])


def _series_clock(tmp_path, route_clk=True):
    """/D0 runs U1 -> U2 (40 mm); the clock runs U1 -> R1 as /CLK_R (20 mm)
    and R1 -> U2 as /CLK (20 mm): one 40 mm clock split by a series resistor."""
    body = (fp("U1", 5, 5, [("1", 0, 0, "/D0"), ("2", 0, 3, "/CLK_R")])
            + fp("R1", 25, 8, [("1", -0.5, 0, "/CLK_R"), ("2", 0.5, 0, "/CLK")])
            + fp("U2", 45, 5, [("1", 0, 0, "/D0"), ("2", 0, 3, "/CLK")])
            + seg(5, 5, 45, 5, "/D0") + seg(5, 8, 24.5, 8, "/CLK_R"))
    if route_clk:
        body += seg(25.5, 8, 45, 8, "/CLK")
    return _pcb(tmp_path, body, "series")


def test_sum_reference_spans_a_series_resistor(tmp_path):
    g = {"name": "MMC", "nets": ["/D0"], "ref_nets": ["/CLK_R", "/CLK"],
         "tol_mm": 2.0}
    # min of the two halves is ~20 mm: the 40 mm data net looks 20 mm long
    assert "length_group_mismatch" in kinds(run(_series_clock(tmp_path), [g]))
    p = run(_series_clock(tmp_path), [{**g, "ref_stat": "sum"}])
    assert p["status"] == "pass", p["violations"]
    assert p["groups"][0]["reference_mm"] == pytest.approx(40.0, abs=1.1)


def test_sum_reference_needs_every_part(tmp_path):
    g = {"name": "MMC", "nets": ["/D0"], "ref_nets": ["/CLK_R", "/CLK"],
         "ref_stat": "sum", "tol_mm": 2.0}
    p = run(_series_clock(tmp_path, route_clk=False), [g])
    assert "length_group_mismatch" not in kinds(p)
    assert "length_group_unrouted" in kinds(p)
    assert p["groups"][0]["reference_mm"] is None


def test_window_and_bounds(tmp_path):
    pcb = board_bus(tmp_path, {"/D0": 1.0, "/D1": 6.0, "/DQS": 2.0})
    # window [-5.08, 0] below DQS (42 mm): D1 (46) is over, D2 (40) inside
    p = run(pcb, [{"name": "AC", "nets": ["D0", "D1", "D2"], "ref_nets": ["DQS"],
                   "window_mm": [-5.08, 0]}])
    assert [v["net"] for v in p["violations"]] == ["/D1"]
    # max_mm and not_longer_than
    p = run(pcb, [{"name": "X", "nets": ["D0", "D1", "D2"], "max_mm": 45},
                  {"name": "Y", "nets": ["D0", "D2"], "not_longer_than": ["DQS"]}])
    bound = [(v["group"], v["net"], v["bound"]) for v in p["violations"]]
    assert bound == [("X", "/D1", "max_mm")]
    p = run(pcb, [{"name": "Y", "nets": ["D1"], "not_longer_than": ["DQS"]}])
    assert kinds(p) == ["length_group_bound"]


def test_unrouted_open_and_stale(tmp_path):
    body = (fp("U1", 5, 5, [("1", 0, 0, "/A"), ("2", 0, 3, "/B"), ("3", 0, 6, "/C")])
            + fp("U2", 45, 5, [("1", 0, 0, "/A"), ("2", 0, 3, "/B"), ("3", 0, 6, "/C")])
            + seg(5, 5, 45, 5, "/A") + seg(5, 8, 20, 8, "/B"))   # B stops short
    p = run(_pcb(tmp_path, body),
            [{"name": "G", "nets": ["A", "B", "C", "ZZ*"], "tol_mm": 0.5}])
    assert kinds(p) == ["length_group_no_nets", "length_group_open",
                        "length_group_unrouted"]
    assert all(v["severity"] == "warning" for v in p["violations"])


def test_comment_keys_pass_and_typos_still_stop(tmp_path):
    """#1097: a `_why` on a group passes, as constraints_lint passes it;
    a misspelled key still stops the check."""
    pcb = board_bus(tmp_path, {"/D2": 0.4})
    g = {"name": "BYTE0", "nets": ["D[0-3]"], "ref_nets": ["DQS"],
         "tol_mm": 0.635, "_why": "DDR byte lane"}
    assert run(pcb, [g])["status"] == "pass"
    with pytest.raises(checklib.CheckError, match="unknown key"):
        run(pcb, [dict(g, tol_mn=1)])


def test_bad_group_is_an_error(tmp_path):
    pcb = board_bus(tmp_path, {})
    for bad in ({"name": "x"}, {"nets": ["D0"], "window_mm": [1, -1]},
                {"nets": ["D0"]}, {"nets": ["D0"], "tol_mm": 1, "ref_stat": "median"},
                {"nets": ["D0"], "tol_mm": 1, "typo": 1}):
        with pytest.raises(checklib.CheckError):
            run(pcb, [bad])


def test_ddr_nets_without_groups_warn(tmp_path):
    nets = [f"/DRAM_D{i}" for i in range(8)]
    pads = [(str(i + 1), 0, i, n) for i, n in enumerate(nets)]
    p = run(_pcb(tmp_path, fp("U1", 5, 5, pads)))
    assert kinds(p) == ["length_group_undeclared"]
    p = run(board_bus(tmp_path, {}))
    assert p["status"] == "pass" and p["groups"] == []


def test_linux_computer_schema_loads(tmp_path):
    """hw-lab linux-computer's constraints use {name, nets, ref_nets, tol_mm}
    with exact names and ref_nets [] - the schema must take it as written."""
    p = run(board_bus(tmp_path, {}),
            [{"name": "CK", "nets": ["/D0", "/D1"], "ref_nets": [], "tol_mm": 0.127},
             {"name": "BYTE0", "nets": ["/D2", "/D3"], "ref_nets": ["/DQS"],
              "tol_mm": 0.635}])
    assert p["status"] == "pass"
