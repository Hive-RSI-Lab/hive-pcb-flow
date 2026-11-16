"""check_return_path judges the impedance of a line over a hatched plane (#1010).

Since #870 a hatch reads as the copper it is, so its cells are no longer
voids. But a hatch has less copper than a plane and the line over it runs
above the Z0 a solid-plane solve gives. The planted fault is a flex line
sized for its target over solid copper and run over a sparse hatch; its
quiet twins are the same line over solid copper and over a dense hatch.
A hatch too coarse for the corridor, or too coarse for the edge rate, is
not_judged with its reason, and so is a hatched net with no target.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_return_path  # noqa: E402
import check_si  # noqa: E402
import geom  # noqa: E402
import impedance  # noqa: E402

pytestmark = pytest.mark.skipif(not impedance.SOLVER_OK,
                                reason="field solver needs numpy + scipy")

W, H = 30.0, 10.0
STACK = ('(setup (stackup\n'
         '  (layer "F.Cu" (type "copper") (thickness 0.018))\n'
         '  (layer "dielectric 1" (type "core") (thickness 0.1)'
         ' (material "Polyimide") (epsilon_r 3.2))\n'
         '  (layer "B.Cu" (type "copper") (thickness 0.018))))\n')


def _fp(x0, y0, x1, y1) -> str:
    return (f'    (filled_polygon (layer "B.Cu")\n'
            f'      (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1})'
            f' (xy {x0} {y1})))\n')


def _zone(body: str, W=W, H=H) -> str:
    return ('  (zone (net "GND") (layer "B.Cu") (name "GND_PLANE")\n'
            f'    (polygon (pts (xy 0 0) (xy {W} 0) (xy {W} {H}) (xy 0 {H})))\n'
            + body + "  )\n")


def solid(W=W, H=H) -> str:
    return _zone(_fp(0, 0, W, H), W, H)


def hatched(thick: float, gap: float, W=W, H=H) -> str:
    """Bars `thick` wide every thick + gap both ways, plus a border: geom
    unions them into one polygon with a hole per cell."""
    pitch = thick + gap
    bars, x = [], 0.0
    while x < W:
        bars.append((x, 0, min(x + thick, W), H))
        x += pitch
    y = 0.0
    while y < H:
        bars.append((0, y, W, min(y + thick, H)))
        y += pitch
    bars += [(0, 0, W, thick), (0, H - thick, W, H),
             (0, 0, thick, H), (W - thick, 0, W, H)]
    return _zone(f"    (fill yes (mode hatch) (hatch_thickness {thick})"
                 f" (hatch_gap {gap}))\n"
                 + "".join(_fp(*[round(v, 4) for v in b]) for b in bars), W, H)


def _board(tmp_path, plane: str, width=0.2) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 0.136))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  {STACK}
  (gr_rect (start 0 0) (end {W} {H}) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
  (segment (start 2 5) (end 28 5) (width {width}) (layer "F.Cu") (net "SIG"))
{plane})
""", encoding="utf-8")
    return p


def _check(tmp_path, plane, width=0.2, **entry):
    bg = geom.load_board(_board(tmp_path, plane, width))
    return check_return_path.check_net(bg, {"net": "SIG", "reference": "GND",
                                            **entry}, 3.0)


def _target(tmp_path, width=0.2) -> float:
    """The line's Z0 over solid copper: what it was sized for."""
    bg = geom.load_board(_board(tmp_path, solid(), width))
    z, _, model = check_return_path.solid_plane_z0(bg, "F.Cu", width)
    assert model == "coated microstrip"
    return round(z)


def _kinds(vs):
    return [v["kind"] for v in vs]


# ------------------------------------------------------ planted fault

def test_sparse_hatch_misses_its_target(tmp_path):
    z = _target(tmp_path)
    # 0.1 mm bars every 0.6 mm: 31 % copper, cells 0.5 mm (2.5x the
    # 0.1 mm dielectric, under the 1.2 mm corridor)
    vs, facts = _check(tmp_path, hatched(0.1, 0.5), z0_ohm=z)
    hz = [v for v in vs if v["kind"] == "hatch_impedance"]
    assert len(hz) == 1, vs
    v = hz[0]
    assert v["severity"] == "warning"          # an estimate, never an error
    assert v["layer"] == "B.Cu" and v["signal_layer"] == "F.Cu"
    assert v["z0_estimate_ohm"] > 1.5 * z
    assert v["hatch_fill"] == pytest.approx(0.31, abs=0.03)
    # the hatch still reads as copper: no corridor voids at its cells
    assert "corridor_void" not in _kinds(vs)
    rec = facts["reference_resolution"][0]
    assert rec["hatched"] is True
    assert rec["hatch"]["opening_mm"] == pytest.approx(0.5, abs=0.01)
    assert "hatch_at" not in rec
    assert facts["hatch_impedance"][0]["z0_estimate_ohm"] == v["z0_estimate_ohm"]
    assert "not_judged" not in facts


def test_impedance_ohm_is_the_target_when_z0_ohm_is_absent(tmp_path):
    z = _target(tmp_path)
    vs, facts = _check(tmp_path, hatched(0.1, 0.5), impedance_ohm=z)
    assert _kinds(vs) == ["hatch_impedance"]
    assert facts["hatch_impedance"][0]["target_ohm"] == z


# ------------------------------------------------------ quiet twins

def test_same_line_over_solid_copper_is_quiet(tmp_path):
    z = _target(tmp_path)
    vs, facts = _check(tmp_path, solid(), z0_ohm=z)
    assert vs == []
    assert "hatch_impedance" not in facts and "not_judged" not in facts


def test_dense_hatch_meets_its_target(tmp_path):
    z = _target(tmp_path)
    # 0.3 mm bars every 0.4 mm: 94 % copper, cells 0.1 mm
    vs, facts = _check(tmp_path, hatched(0.3, 0.1), z0_ohm=z)
    assert vs == []
    est = facts["hatch_impedance"][0]
    assert z < est["z0_estimate_ohm"] < 1.1 * z
    assert est["target_ohm"] == z
    assert "not_judged" not in facts


# ------------------------------------------------------ not judged

def test_cell_wider_than_corridor_is_not_judged(tmp_path):
    # 0.1 mm trace: a 0.6 mm corridor, under a 1.0 mm cell
    vs, facts = _check(tmp_path, hatched(0.2, 1.0), width=0.1, z0_ohm=50)
    assert "hatch_impedance" not in _kinds(vs)
    assert len(facts["not_judged"]) == 1
    assert "too coarse to model" in facts["not_judged"][0]
    assert "z0_estimate_ohm" not in facts["hatch_impedance"][0]


def test_pitch_against_a_fast_edge_is_not_judged(tmp_path):
    z = _target(tmp_path)
    # 20 ps edge: 25 GHz knee, wavelength ~7 mm, so a 0.6 mm pitch is
    # over a twentieth of it
    vs, facts = _check(tmp_path, hatched(0.1, 0.5), z0_ohm=z, t_rise_ns=0.02)
    assert "hatch_impedance" not in _kinds(vs)
    assert "periodic line" in facts["not_judged"][0]
    # the same hatch at a 1 ns edge is judged (and misses)
    vs, _ = _check(tmp_path, hatched(0.1, 0.5), z0_ohm=z, t_rise_ns=1.0)
    assert "hatch_impedance" in _kinds(vs)


def test_hatch_without_a_target_is_not_judged(tmp_path):
    vs, facts = _check(tmp_path, hatched(0.1, 0.5))
    assert vs == []
    assert "no z0_ohm or impedance_ohm target" in facts["not_judged"][0]
    assert facts["hatch_impedance"][0]["z0_estimate_ohm"] > 0


def test_cli_reports_not_judged_at_the_top(tmp_path):
    pcb = _board(tmp_path, hatched(0.1, 0.5))
    cons = tmp_path / "constraints.json"
    cons.write_text(json.dumps({"high_speed": [{"net": "SIG"}]}),
                    encoding="utf-8")
    payload, _ = check_return_path.run(["--pcb", str(pcb),
                                        "--constraints", str(cons)])
    assert payload["status"] == "pass"
    assert len(payload["not_judged"]) == 1
    assert "not_judged" not in payload["checked"][0]


# ------------------------------------------------------ through check_si

def _si_board(tmp_path, plane: str, z0) -> dict:
    """A 110 mm SPI clock, U1 to U2, on the flex line: check_si infers it a
    transmission line and hands it to check_return_path with its target."""
    def fp(ref, x):
        return (f'  (footprint "t:{ref}" (at {x} 3) (layer "F.Cu")\n'
                f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
                f'    (property "Value" "{"MCU" if ref == "U1" else "FLASH"}"'
                ' (at 0 0 0) (layer "F.Fab"))\n'
                '    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu")'
                ' (net "/SPI_CLK"))\n  )\n')
    p = tmp_path / "si.kicad_pcb"
    p.write_text(f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 0.136))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  {STACK}
  (gr_rect (start 0 0) (end 120 6) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{fp("U1", 5)}{fp("U2", 115)}  (segment (start 5 3) (end 115 3) (width 0.2) (layer "F.Cu") (net "/SPI_CLK"))
{plane})
""", encoding="utf-8")
    c = tmp_path / "constraints.json"
    c.write_text(json.dumps({"si": {"nets": {"/SPI_CLK": {
        "class": "spi", "z0_ohm": z0}}}}), encoding="utf-8")
    payload, _ = check_si.run(["--pcb", str(p), "--constraints", str(c)])
    return payload


def test_check_si_judges_its_target_over_a_hatch(tmp_path):
    z = _target(tmp_path)
    got = _si_board(tmp_path, hatched(0.1, 0.5, W=120, H=6), z)
    hz = [v for v in got["violations"] if v["kind"] == "hatch_impedance"]
    assert len(hz) == 1 and hz[0]["net"] == "/SPI_CLK"
    assert hz[0]["source"] == "check_si"
    # its solid-plane line model sees the line on target: no other miss
    assert "si_impedance_target" not in _kinds(got["violations"])
    quiet = _si_board(tmp_path, solid(W=120, H=6), z)
    assert "hatch_impedance" not in _kinds(quiet["violations"])
