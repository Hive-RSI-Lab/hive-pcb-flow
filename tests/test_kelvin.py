"""check_kelvin: what an amplifier reads on top of its shunt, from the copper.

Synthetic boards with hand-checkable copper: 1 oz is 0.49 mOhm/sq. The real-board
figures this was built against (hc-power, iso-io, against their cold reviews' own
solves) are in the PR, since those boards are not in this repo.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import check_kelvin  # noqa: E402
import geom  # noqa: E402

RS = 0.49  # mOhm/sq, 1 oz


def _fp(ref, val, x, y, *pads, size=2):
    rows = "".join(
        f'    (pad "{n}" smd rect (at {dx} 0) (size {size} {size}) (layers "F.Cu") '
        f'(net "{net}"){f" (pinfunction {chr(34)}{fn}{chr(34)})" if fn else ""})\n'
        for n, dx, net, fn in pads)
    return (f'  (footprint "t:X" (at {x} {y}) (layer "F.Cu")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0)) '
            f'(property "Value" "{val}" (at 0 0 0))\n{rows}  )\n')


def _seg(net, x0, y0, x1, y1, w):
    return (f'  (segment (start {x0} {y0}) (end {x1} {y1}) (width {w}) '
            f'(layer "F.Cu") (net "{net}"))\n')


def _board(tmp_path, body, name="k") -> Path:
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 60 30) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
""", encoding="utf-8")
    return p


def _circuit(tap_x, neck_w=0.5, shunt="10m"):
    """L1 -- neck on A -- R1 (shunt) -- 3 mm wide B to J1. U1 reads R1 through R2 (0R)
    on A, joined at x = tap_x (L1's pad at 30, R1's at 22), and directly on B."""
    return (_fp("R1", shunt, 20, 10, ("1", 2, "A", ""), ("2", -2, "B", ""))
            + _fp("L1", "4.7uH", 32, 10, ("1", -2, "A", ""), ("2", 2, "SW", ""))
            + _fp("J1", "CONN", 6, 10, ("1", 0, "B", ""))
            + _seg("A", 22, 10, 30, 10, neck_w)
            + _seg("B", 6, 10, 18, 10, 3)
            + _fp("R2", "0R", 24, 20, ("1", 0, "A", ""), ("2", 2, "K", ""), size=1)
            + _seg("A", 24, 20, tap_x, 20, 0.2) + _seg("A", tap_x, 20, tap_x, 10, 0.2)
            + _fp("U1", "INA240", 12, 20, ("1", 14, "K", "IN+"), ("2", 0, "B", "IN-"), size=1)
            + _seg("K", 26, 20, 26, 20.5, 0.2)
            + _seg("B", 12, 20, 18, 20, 0.2) + _seg("B", 18, 20, 18, 10, 0.2))


def _judge(pcb, paths=()):
    return check_kelvin.judge(geom.BoardGeom.from_file(pcb), list(paths))


def test_tap_past_a_neck_reads_the_neck(tmp_path):
    vios, facts, left = _judge(_board(tmp_path, _circuit(tap_x=30)))
    (f,) = facts
    assert (f["shunt"], f["ic"]) == ("R1", "U1") and not left
    # 6 mm between pad edges x 0.5 mm: 12 squares = 5.9 mOhm on a 10 mOhm shunt
    assert f["extra_mohm"] == pytest.approx(6 * RS / 0.5, rel=0.2)
    (v,) = vios
    assert v["kind"] == "kelvin_error" and v["severity"] == "error"
    assert set(v["refs"]) == {"R1", "U1"}


def test_tap_on_the_shunt_pad_is_clean(tmp_path):
    vios, facts, _ = _judge(_board(tmp_path, _circuit(tap_x=22)))
    assert not vios and facts[0]["extra_mohm"] < 0.1


def test_a_small_error_only_warns(tmp_path):
    # 3/16 of the way down the neck on a 100 mOhm shunt: 1.5 of 100 mOhm is under
    # the warning line; 3 mOhm on it warns
    vios, _, _ = _judge(_board(tmp_path, _circuit(tap_x=28, shunt="100m")))
    (v,) = vios
    assert v["severity"] == "warning" and 2 <= v["error_pct"] < 5


def test_declared_path_names_the_source(tmp_path):
    """A load FET on the net isn't what feeds the shunt: with no list its small
    current can't fail the tap (kindest source), and the list settles it."""
    body = _circuit(tap_x=30) + _fp("Q1", "AO3400", 28, 3, ("1", 2, "A", ""))
    bg = geom.BoardGeom.from_file(_board(tmp_path, body))
    src, shares = check_kelvin.entering(bg, "A", "R1", {"U1", "R2"}, [])
    assert src == ["L1", "Q1"] and shares is None
    src, shares = check_kelvin.entering(bg, "A", "R1", {"U1", "R2"}, [
        {"net": "A", "from": "L1", "to": "R1", "current_a": 5}])
    assert src == ["L1"] and shares == [1.0]


def test_four_terminal_shunt_and_no_reader(tmp_path):
    four = (_fp("R1", "10m", 20, 10, ("1", 2, "A", ""), ("2", -2, "B", ""),
                ("3", 4, "P", ""), ("4", -4, "N", ""))
            + _fp("U1", "INA240", 12, 20, ("1", 0, "P", ""), ("2", 4, "N", "")))
    vios, facts, left = _judge(_board(tmp_path, four))
    assert not vios and not facts and not left
    alone = _fp("R1", "10m", 20, 10, ("1", 2, "A", ""), ("2", -2, "B", ""))
    vios, facts, left = _judge(_board(tmp_path, alone, "b"))
    assert not vios and not facts and "no amplifier" in left[0]


def test_named_supply_pin_is_not_a_tap(tmp_path):
    """A hot-swap's VIN on one net and SENSE on the other has no pair."""
    body = (_fp("R1", "10m", 20, 10, ("1", 2, "A", ""), ("2", -2, "B", ""))
            + _fp("U1", "LM5069", 12, 20, ("1", 0, "A", "VIN"), ("2", 4, "B", "SENSE")))
    _, facts, left = _judge(_board(tmp_path, body))
    assert not facts and left


def test_channels_pair_by_number_and_by_net_name(tmp_path):
    # INA3221: IN+1/IN-1 on R1, IN+2 sharing A
    body = (_fp("R1", "100m", 20, 10, ("1", 2, "A", ""), ("2", -2, "B", ""))
            + _fp("U1", "INA3221", 12, 20, ("1", 0, "A", "IN+1"), ("2", 3, "B", "IN-1"),
                  ("3", 6, "A", "IN+2")))
    bg = geom.BoardGeom.from_file(_board(tmp_path, body))
    (r,) = check_kelvin.readers(bg, "R1", ["A", "B"])
    assert [t["pin"] for t in r["sides"]["A"]] == ["IN+1"]
    # DRV8353: three shunts' returns through 0Rs off GND; /SNA goes with /SPA
    assert check_kelvin._one_off("/SNA", "/SPA")
    assert not check_kelvin._one_off("/SNC", "/SPA")
    taps = [{"far": "/SNC"}, {"far": "/SNA"}]
    assert check_kelvin._by_name(taps, [], "/SPA") == [{"far": "/SNA"}]


def test_thin_track_on_a_coarse_grid_keeps_its_resistance(tmp_path):
    """A coarse grid burns a thin track in one cell wide at its own conductance."""
    body = (_fp("R1", "1m", 10, 10, ("1", 0, "A", ""), size=1)
            + _fp("L1", "1u", 30, 10, ("1", 0, "A", ""), size=1)
            + _seg("A", 10, 10, 30, 10, 0.2))
    bg = geom.BoardGeom.from_file(_board(tmp_path, body))
    g = check_kelvin.NetGrid(bg, "A", ["F.Cu"], 0.25)
    from scipy.sparse.linalg import spsolve
    lap = check_kelvin.laplacian(g)
    a = check_kelvin.pad_nodes(g, bg.pads_of(ref="R1")[0])
    b = check_kelvin.pad_nodes(g, bg.pads_of(ref="L1")[0])
    import numpy as np
    keep = np.setdiff1d(np.arange(g.n_nodes), a)
    rhs = np.zeros(g.n_nodes)
    rhs[b] = 1.0 / len(b)
    v = spsolve(lap[keep][:, keep].tocsc(), rhs[keep])
    full = np.zeros(g.n_nodes)
    full[keep] = v
    # 19 mm between pad edges at 0.2 mm wide
    assert full[b].mean() * 1e3 == pytest.approx(19 * RS / 0.2, rel=0.1)


def test_cli_and_registration(tmp_path):
    pcb = _board(tmp_path, _circuit(tap_x=30))
    cp = subprocess.run([sys.executable, str(SCRIPTS / "check_kelvin.py"), "--pcb", str(pcb)],
                        capture_output=True, text=True, timeout=300)
    assert cp.returncode == 1, cp.stderr
    rep = json.loads(cp.stdout)
    assert rep["violations"][0]["source"] == "check_kelvin"
    assert (REPO / "reference" / "remediations" / "kelvin_error.md").is_file()


# ---- declared sense taps (#936) ---------------------------------------------

def _zone(net, x0, y0, x1, y1, name="POUR"):
    pts = f"(xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})"
    return (f'  (zone (net "{net}") (name "{name}") (layer "F.Cu")\n'
            f'    (polygon (pts {pts}))\n'
            f'    (filled_polygon (layer "F.Cu") (pts {pts})))\n')


def _rail(sense=None):
    """L1 feeds C1 on P over a 1 mm track; R1.1 (a feedback divider's top) is
    to read P at C1.1. `sense` is the sense line's copper."""
    body = (_fp("L1", "4.7uH", 10, 10, ("1", 0, "P", ""))
            + _fp("C1", "22u", 30, 10, ("1", 0, "P", ""))
            + _seg("P", 10, 10, 30, 10, 1)
            + _fp("R1", "100k", 30, 20, ("1", 0, "P", ""), ("2", 2, "FB", ""), size=1))
    return body + (_seg("P", 30, 20, 30, 10, 0.2) if sense is None else sense)


SENSE = [{"net": "P", "sense_pads": {"R1.1": "C1.1"}}]


def _sense(pcb, power=SENSE):
    return check_kelvin.judge_sense(geom.BoardGeom.from_file(pcb), power)


def test_sense_line_on_its_own_track_to_the_pad_is_clean(tmp_path):
    """Both the power track and the sense track end on C1.1: that is the tap."""
    vios, (f,) = _sense(_board(tmp_path, _rail()))
    assert not vios and f["reached"] and not f["joins"]


def test_sense_line_teed_off_the_power_track_is_caught(tmp_path):
    t = _seg("P", 30, 20, 20, 20, 0.2) + _seg("P", 20, 20, 20, 10, 0.2)
    (v,), (f,) = _sense(_board(tmp_path, _rail(t)))
    assert v["kind"] == "sense_tap_off_pad" and v["severity"] == "error"
    assert "a power track" in v["msg"] and 8 < v["tap_mm"] < 10
    assert v["pos"][0] == pytest.approx(20, abs=0.2)


def test_sense_line_with_overlapping_jogs_is_clean(tmp_path):
    """#1043: a router's line runs short 45-degree jogs whose ends overlap
    in a cycle (each piece touches two others). It is still a dead end off
    C1.1, not power copper: pruning dead ends one by one called it rail."""
    t = (_seg("P", 30, 20, 30.35, 19.65, 0.2)
         + _seg("P", 30.35, 19.65, 30.9, 19.65, 0.2)
         + _seg("P", 30.9, 19.65, 30.95, 19.6, 0.2)
         + _seg("P", 30.95, 19.6, 30.95, 10, 0.2))
    vios, (f,) = _sense(_board(tmp_path, _rail(t)))
    assert not vios and f["reached"] and not f["joins"]


def test_sense_line_jogging_by_the_tap_still_reaches_it(tmp_path):
    """#1042: a pulled sense line bends within SENSE_SLACK_MM of C1.1 before
    it lands there. A contact by the tap is the tap only when it meets power
    copper; the line's own bend there leads on to the pad."""
    t = (_seg("P", 30, 20, 30, 11.3, 0.2)
         + _seg("P", 30, 11.3, 30.4, 11.3, 0.2)
         + _seg("P", 30.4, 11.3, 30.4, 10, 0.2))
    vios, (f,) = _sense(_board(tmp_path, _rail(t)))
    assert not vios and f["reached"] and not f["joins"]


def test_root_block_finds_pieces_on_a_cycle_through_the_root():
    r = ("root",)
    edges = [(r, "p1"), (r, "p2"), ("p1", "t"), ("t", "p2"),   # rail
             ("t", "s1"), ("s1", "s2"), ("s2", "s3"), ("s3", "s1"),  # spur
             (r, "p3")]                                         # lone pad
    neigh = {}
    for a, b in edges:
        neigh.setdefault(a, set()).add(b)
        neigh.setdefault(b, set()).add(a)
    assert check_kelvin.root_block(neigh, r) == {"p1", "p2", "t"}


def test_sense_pad_sitting_in_a_pour_is_caught(tmp_path):
    """The voice board's fault: the sense pad and its track sit in the rail's
    own pour, so it reads the pour under it, not C1.1."""
    pour = _zone("P", 26, 16, 34, 24)
    (v,), _ = _sense(_board(tmp_path, _rail() + pour))
    assert v["kind"] == "sense_tap_off_pad" and "P pour POUR" in v["msg"]
    assert v["tap_mm"] > 8 and v["refs"] == ["C1", "R1"]


def test_pour_that_only_meets_the_tap_pad_is_fine(tmp_path):
    """The pour feeds C1.1 from below; the sense line comes in from above and
    meets it only inside the pad."""
    vios, _ = _sense(_board(tmp_path, _rail() + _zone("P", 27, 5, 40, 10.5)))
    assert not vios


def test_sense_line_not_routed_is_missing(tmp_path):
    (v,), (f,) = _sense(_board(tmp_path, _rail("")))
    assert v["kind"] == "sense_tap_missing" and not f["reached"]


def test_bad_sense_pads_declaration(tmp_path):
    pcb = _board(tmp_path, _rail())
    for pads, why in [({"R9.1": "C1.1"}, "R9.1 is not a pad"),
                      ({"R1.1": "C1"}, "C1 is not a pad"),
                      ({"R1.1": "R1.2"}, "R1.2 is on FB, not P"),
                      ({"C1.1": "C1.1"}, "same pad")]:
        (v,), facts = _sense(pcb, [{"net": "P", "sense_pads": pads}])
        assert v["kind"] == "sense_tap_bad" and why in v["msg"] and not facts


def test_cli_reads_sense_pads(tmp_path):
    pcb = _board(tmp_path, _rail() + _zone("P", 26, 16, 34, 24))
    cons = tmp_path / "constraints.json"
    cons.write_text(json.dumps({"power": SENSE}))
    cp = subprocess.run([sys.executable, str(SCRIPTS / "check_kelvin.py"), "--pcb",
                         str(pcb), "--constraints", str(cons)],
                        capture_output=True, text=True, timeout=300)
    assert cp.returncode == 1, cp.stderr
    rep = json.loads(cp.stdout)
    assert [v["kind"] for v in rep["violations"]] == ["sense_tap_off_pad"]
    assert rep["sense_taps"][0]["tap"] == "C1.1"
    for kind in ("sense_tap_off_pad", "sense_tap_missing", "sense_tap_bad"):
        assert (REPO / "reference" / "remediations" / f"{kind}.md").is_file()
