"""check_esd (reference/ingest/emc-thermal.md ESD-1..5): an ESD clamp on
every external connector line, first on the line, short to ground, on
synthetic boards."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_esd  # noqa: E402


def _fp(ref: str, value: str, at: str, pads: list[tuple]) -> str:
    """pads: (number, x, y, net) in footprint-local mm, 1 x 1 mm SMD."""
    body = " ".join(
        f'(pad "{n}" smd rect (at {x} {y}) (size 1 1) (layers "F.Cu") '
        f'(net "{net}"))' for n, x, y, net in pads)
    return (f'  (footprint "t:{ref}" (at {at}) (layer "F.Cu")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'    (property "Value" "{value}" (at 0 0 0) (layer "F.Fab"))\n'
            f'    {body})\n')


def _seg(x0, y0, x1, y1, net="SIG", layer="F.Cu") -> str:
    return (f'  (segment (start {x0} {y0}) (end {x1} {y1}) (width 0.2) '
            f'(layer "{layer}") (net "{net}"))\n')


def _via(x, y, net="GND") -> str:
    return (f'  (via (at {x} {y}) (size 0.6) (drill 0.3) '
            f'(layers "F.Cu" "B.Cu") (net "{net}"))\n')


def _pcb(tmp_path, body: str) -> Path:
    p = tmp_path / "esd.kicad_pcb"
    p.write_text(f"""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 60 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
""", encoding="utf-8")
    return p


def _run(pcb, constraints: dict | None = None, tmp_path=None):
    argv = ["--pcb", str(pcb)]
    if constraints is not None:
        c = pcb.parent / "constraints.json"
        c.write_text(json.dumps(constraints), encoding="utf-8")
        argv += ["--constraints", str(c)]
    payload, _ = check_esd.run(argv)
    return payload, {v["kind"] for v in payload["violations"]}


# J1 at (5, 10): pin 1 SIG at (5, 10), pin 2 GND at (5, 12)
J1 = _fp("J1", "USB-C", "5 10", [("1", 0, 0, "SIG"), ("2", 0, 2, "GND")])
U1 = _fp("U1", "MCU", "30 10", [("1", 0, 0, "SIG")])


def _clamp(x: float, value="PESD5V0S1BL") -> str:
    """D1 with its SIG pin at (x, 10) and its GND pin at (x, 12)."""
    return _fp("D1", value, f"{x} 10", [("1", 0, 0, "SIG"), ("2", 0, 2, "GND")])


def test_clamp_at_the_connector_passes(tmp_path):
    pcb = _pcb(tmp_path, J1 + U1 + _clamp(8) + _seg(5, 10, 8, 10)
               + _seg(8, 10, 30, 10) + _via(8, 13))
    payload, kinds = _run(pcb)
    assert payload["status"] == "pass", json.dumps(payload["violations"])
    line = payload["checked"][0]["lines"][0]
    assert line["clamp"] == "D1" and line["routed"]
    assert line["mm"] == pytest.approx(3.0) and line["vias"] == 0
    assert line["stub_mm"] == pytest.approx(0.0, abs=1e-6)


def test_no_clamp_is_an_error(tmp_path):
    pcb = _pcb(tmp_path, J1 + U1 + _seg(5, 10, 30, 10))
    payload, kinds = _run(pcb)
    assert kinds == {"esd_unclamped"}
    assert payload["violations"][0]["severity"] == "error"


def test_power_line_unclamped_is_a_warning(tmp_path):
    j = _fp("J1", "DC jack", "5 10", [("1", 0, 0, "+5V"), ("2", 0, 2, "GND")])
    u = _fp("U1", "LDO", "30 10", [("1", 0, 0, "+5V")])
    payload, kinds = _run(_pcb(tmp_path, j + u))
    assert kinds == {"esd_unclamped"}
    assert payload["violations"][0]["severity"] == "warning"


def test_ic_reached_before_the_clamp(tmp_path):
    """SLVA680 case 2: the line splits at a tee, one branch to the IC,
    the other on to the clamp."""
    u = _fp("U1", "MCU", "10 5", [("1", 0, 0, "SIG")])
    pcb = _pcb(tmp_path, J1 + u + _clamp(20) + _seg(5, 10, 10, 10)
               + _seg(10, 10, 10, 5) + _seg(10, 10, 20, 10) + _via(20, 13))
    payload, kinds = _run(pcb)
    assert "esd_branch_first" in kinds, kinds
    line = payload["checked"][0]["lines"][0]
    assert line["stub_mm"] == pytest.approx(10.0)
    # 10 mm of stub + 0.2 mm to the ground via at ~1 nH/mm, 3.75e10 A/s
    assert line["est_extra_nh"] == pytest.approx(10.2)
    assert line["est_extra_v"] == pytest.approx(382.5)


def test_via_before_the_clamp(tmp_path):
    pcb = _pcb(tmp_path, J1 + U1 + _clamp(8) + _seg(5, 10, 6, 10)
               + _via(6, 10, "SIG") + _seg(6, 10, 7, 10, layer="B.Cu")
               + _via(7, 10, "SIG") + _seg(7, 10, 8, 10)
               + _seg(8, 10, 30, 10) + _via(8, 13))
    payload, kinds = _run(pcb)
    assert kinds == {"esd_via_first"}
    assert payload["checked"][0]["lines"][0]["vias"] == 2


def test_far_clamp(tmp_path):
    pcb = _pcb(tmp_path, J1 + U1 + _clamp(25) + _seg(5, 10, 25, 10)
               + _seg(25, 10, 30, 10) + _via(25, 13))
    payload, kinds = _run(pcb)
    assert kinds == {"esd_clamp_far"}
    assert payload["violations"][0]["mm"] == pytest.approx(20.0)


def test_clamp_ground_far_from_a_via(tmp_path):
    pcb = _pcb(tmp_path, J1 + U1 + _clamp(8) + _seg(5, 10, 8, 10)
               + _seg(8, 10, 30, 10) + _via(8, 18))
    payload, kinds = _run(pcb)
    assert kinds == {"esd_ground_far"}
    v = payload["violations"][0]
    # pad edge (y 12.5) to via edge (y 17.7)
    assert v["ground_via_mm"] == pytest.approx(5.2, abs=0.05)


def test_clamp_behind_a_series_resistor(tmp_path):
    r = _fp("R1", "33", "10 10", [("1", 0, 0, "SIG"), ("2", 1, 0, "SIG2")])
    d = _fp("D1", "ESD9B5.0ST5G", "14 10",
            [("1", 0, 0, "SIG2"), ("2", 0, 2, "GND")])
    u = _fp("U1", "MCU", "30 10", [("1", 0, 0, "SIG2")])
    payload, kinds = _run(_pcb(tmp_path, J1 + r + d + u + _via(14, 13)))
    assert kinds == {"esd_behind_series"}


def test_internal_connectors_are_skipped(tmp_path):
    ffc = _fp("J1", "FFC 20P", "5 10", [("1", 0, 0, "SIG"), ("2", 0, 2, "GND")])
    payload, kinds = _run(_pcb(tmp_path, ffc + U1))
    assert payload["status"] == "pass"
    assert payload["checked"][0]["internal_by_value"] == ["J1"]
    payload, kinds = _run(_pcb(tmp_path, J1 + U1), {"esd": {"internal": ["J1"]}})
    assert payload["status"] == "pass"
    payload, kinds = _run(_pcb(tmp_path, J1 + U1),
                          {"esd": {"exempt_nets": ["SIG"]}})
    assert payload["status"] == "pass"


def test_explicit_clamp_and_level(tmp_path):
    d = _clamp(8, value="XYZ123")
    body = J1 + U1 + d + _seg(5, 10, 8, 10) + _seg(8, 10, 30, 10) + _via(8, 13)
    payload, kinds = _run(_pcb(tmp_path, body))
    assert kinds == {"esd_unclamped"}
    payload, kinds = _run(_pcb(tmp_path, body),
                          {"esd": {"clamps": ["D1"], "level_kv": 4}})
    assert payload["status"] == "pass"
    assert payload["checked"][0]["di_dt_a_per_s"] == pytest.approx(1.875e10)


def test_iec_level4_di_dt():
    # IEC 61000-4-2:2008 Table 3: 30 A first peak at 8 kV, 0.8 ns rise
    assert check_esd.dI_dt(8) == pytest.approx(3.75e10)


# ------------------------------------------------- the corridor (ESD-9, #494)

import emsolve  # noqa: E402


def test_filament_mutual_is_grover():
    """Equal spans: Grover's mu0 l/2pi (asinh(l/d) - sqrt(1 + d^2/l^2) + d/l);
    swapping the filaments or sliding both changes nothing."""
    l, d = 10.0, 0.4
    want = 2e-7 * l * 1e-3 * (math.asinh(l / d) - math.sqrt(1 + (d / l) ** 2)
                              + d / l)
    assert emsolve.filament_mutual(0, l, 0, l, d) == pytest.approx(want, rel=1e-12)
    a = emsolve.filament_mutual(0, 10, 3, 7, 0.5)
    assert emsolve.filament_mutual(3, 7, 0, 10, 0.5) == pytest.approx(a)
    assert emsolve.filament_mutual(5, 15, 8, 12, 0.5) == pytest.approx(a)
    # two halves of a filament add up to the whole
    whole = emsolve.filament_mutual(0, 10, 0, 10, 0.5)
    halves = (emsolve.filament_mutual(0, 10, 0, 5, 0.5)
              + emsolve.filament_mutual(0, 10, 5, 10, 0.5))
    assert halves == pytest.approx(whole)


def test_bar_pair_mutual_known_answer():
    """Issue #494 known answer: two parallel 10 x 0.2 mm traces 0.2 mm apart.
    FastHenry (sim_solve validate's fasthenry_pair, nwinc 5) reads M = 5.937
    nH; Grover at the GMD must sit within 5 % (it is within 0.2 %)."""
    m = emsolve.bar_pair_mutual(10, 0.2, 0.035, 0.2)
    assert m == pytest.approx(5.937e-9, rel=0.05)
    assert m == pytest.approx(5.937e-9, rel=0.005)


def test_read_zc_matrix():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "Zc.mat"
        p.write_text("Row 1:  n1  to  n2\nImpedance matrix for frequency = "
                     "1000 2 x 2\n     0.0246305  +6.21501e-05j  -2.00528e-11"
                     "  +3.73053e-05j \n -1.97591e-11  +3.73192e-05j     "
                     "0.0246305  +6.21251e-05j\n")
        f, z = emsolve.read_zc_matrix(p)
    assert f == 1000 and len(z) == 2
    assert z[0][1] == complex(-2.00528e-11, 3.73053e-05)


def _gnd_plane(layer="B.Cu") -> str:
    return (f'  (zone (net "GND") (layer "{layer}") (hatch edge 0.5)\n'
            '    (polygon (pts (xy 0 0) (xy 60 0) (xy 60 40) (xy 0 40)))\n'
            f'    (filled_polygon (layer "{layer}") (pts (xy 0 0) (xy 60 0) '
            '(xy 60 40) (xy 0 40))))\n')


def _usb_stretch(gpio_y: float | None) -> str:
    """J1 pin 1 (USB_DP) runs 23 mm on F.Cu to the clamp D1 at x = 28,
    then on to the MCU; GPIO1 (when given) runs beside it at y = gpio_y."""
    j = _fp("J1", "USB-C", "5 10", [("1", 0, 0, "USB_DP"), ("2", 0, 2, "GND")])
    d = _fp("D1", "USBLC6-2SC6", "28 10", [("1", 0, 0, "USB_DP"),
                                            ("2", 0, 2, "GND")])
    u = _fp("U1", "MCU", "40 10", [("1", 0, 0, "USB_DP"),
                                    ("2", 0, 20, "GPIO1")])
    body = j + d + u + _gnd_plane() + _via(28, 12.8)
    body += _seg(5, 10, 28, 10, net="USB_DP") + _seg(28, 10, 40, 10, net="USB_DP")
    if gpio_y is not None:
        body += (_seg(8, gpio_y, 26, gpio_y, net="GPIO1")
                 + _seg(26, gpio_y, 26, 30, net="GPIO1")
                 + _seg(26, 30, 40, 30, net="GPIO1"))
    return body


def test_gpio_beside_the_usb_stretch_is_flagged(tmp_path):
    payload, kinds = _run(_pcb(tmp_path, _usb_stretch(10.4)))
    assert "esd_corridor_coupled" in kinds, kinds
    v = next(v for v in payload["violations"]
             if v["kind"] == "esd_corridor_coupled")
    assert v["victim"] == "GPIO1" and v["severity"] == "warning"
    assert v["parallel_mm"] == pytest.approx(18.0, abs=0.5)
    assert v["gap_mm"] == pytest.approx(0.2, abs=1e-3)
    # loop mutual over the B.Cu plane (h 1.6 mm assumed): Grover at d = GMD
    # less the image at sqrt(d^2 + (2h)^2), times 3.75e10 A/s
    line = payload["checked"][0]["lines"][0]
    cf = line["corridor"]
    assert cf["model"] == "loop_over_plane" and cf["plane"] == "B.Cu"
    h = cf["plane_h_mm"]
    gmd = emsolve.rect_gmd(0.2, 0.035, 0.4, n=4)
    m = (emsolve.filament_mutual(0, 23, 3, 21, gmd)
         - emsolve.filament_mutual(0, 23, 3, 21, math.hypot(0.4, 2 * h)))
    assert v["m_nh"] == pytest.approx(m * 1e9, rel=0.01)
    assert v["coupled_v"] == pytest.approx(m * 3.75e10, rel=0.01)
    assert v["coupled_v"] > 100


def test_gpio_moved_out_of_the_corridor_is_clean(tmp_path):
    payload, kinds = _run(_pcb(tmp_path, _usb_stretch(22.0)))
    assert "esd_corridor_coupled" not in kinds, payload["violations"]
    cf = payload["checked"][0]["lines"][0]["corridor"]
    assert cf["corridor_mm"] < 12.0
    assert not [n for n in cf["neighbours"] if n["net"] == "GPIO1"]


def test_corridor_width_from_the_coupled_volts(tmp_path):
    """The default corridor is where a full-length neighbour gets
    CORRIDOR_V: a thinner dielectric to the plane narrows it."""
    w16 = check_esd.corridor_width(23, 1.6, 3.75e10, 3.6)
    w02 = check_esd.corridor_width(23, 0.2, 3.75e10, 3.6)
    assert w02 < w16
    m = (emsolve.filament_mutual(0, 23, 0, 23, w02)
         - emsolve.filament_mutual(0, 23, 0, 23, math.hypot(w02, 0.4)))
    assert m * 3.75e10 == pytest.approx(3.6, rel=1e-3)
    assert check_esd.corridor_width(23, None, 3.75e10, 3.6) == \
        check_esd.CORRIDOR_MAX_MM


def test_corridor_victim_limits_and_partners(tmp_path):
    """A higher abs max from the constraints clears it; a clamped partner
    line and a power rail are listed, not judged."""
    payload, kinds = _run(_pcb(tmp_path, _usb_stretch(10.4)),
                          {"esd": {"victim_abs_max_v": {"GPIO*": 1000}}})
    assert "esd_corridor_coupled" not in kinds
    body = _usb_stretch(None).replace('"GPIO1"', '"+3V3"')
    body += _seg(8, 10.4, 26, 10.4, net="+3V3")
    payload, kinds = _run(_pcb(tmp_path, body))
    assert "esd_corridor_coupled" not in kinds
    nb = payload["checked"][0]["lines"][0]["corridor"]["neighbours"]
    assert nb and nb[0]["judged"] is False and "power" in nb[0]["why_not"]


CORE = REPO / "pinned" / "core" / "kicad" / "core.kicad_pcb"


def _plant_on_core(tmp_path, dx: float, side: float = 0.4) -> Path:
    """pinned core with a planted net /PLANT_GPIO following J1's USB_DP
    stretch `side` mm off it (shifted dx mm in x), on F.Cu."""
    from shapely.geometry import LineString
    bg = check_esd.geom.load_board(CORE)
    net = next(n for n in bg.nets if n.endswith("USB_DP"))
    g = check_esd.NetGraph(bg, net)
    j = next(p for p in bg.pads_of(net) if p.ref == "J1")
    u = next(p for p in bg.pads_of(net) if p.ref == "U2")
    best = g.paths_from(check_esd._pad_node(j))
    path = check_esd.NetGraph.walk(best, check_esd._pad_node(u))
    tracks = check_esd.stretch_tracks(g, path)
    longest = max(tracks, key=lambda t: t.length)
    assert longest.layer == "F.Cu"
    # the stretch's longest straight runs, each offset to one side
    segs = []
    for t in tracks:
        if t.length < 2.0:
            continue
        o = LineString(t.shape.coords).offset_curve(side)
        c = list(o.coords)
        segs.append(f'  (segment (start {c[0][0] + dx} {c[0][1]}) '
                    f'(end {c[-1][0] + dx} {c[-1][1]}) (width 0.2) '
                    '(layer "F.Cu") (net "/PLANT_GPIO"))\n')
    assert len(segs) >= 4
    text = CORE.read_text(encoding="utf-8").rstrip()
    assert text.endswith(")")
    out = tmp_path / "core.kicad_pcb"
    out.write_text(text[:-1] + "".join(segs) + ")\n", encoding="utf-8")
    return out


def test_planted_gpio_on_pinned_core(tmp_path):
    """#494 planted fault: a GPIO beside USB D+'s connector-to-clamp stretch
    on a pinned board gets a finding with its volts; moved out, none."""
    payload, _ = check_esd.run(["--pcb", str(_plant_on_core(tmp_path, 0.0))])
    hit = [v for v in payload["violations"]
           if v["kind"] == "esd_corridor_coupled"
           and v["victim"] == "/PLANT_GPIO"]
    assert hit, [v["kind"] for v in payload["violations"]]
    assert hit[0]["coupled_v"] > 3.6
    assert hit[0]["parallel_mm"] > 8
    assert hit[0]["corridor_mm"] == pytest.approx(2.2, abs=0.1)
    payload, _ = check_esd.run(["--pcb", str(_plant_on_core(tmp_path, 40.0))])
    assert not [v for v in payload["violations"]
                if v.get("victim") == "/PLANT_GPIO"]


def test_victim_limit_order():
    """Constraints beat the pin ratings, which beat the default."""
    rated = {"N": (5.5, "U1 pin 2: x")}
    assert check_esd.victim_limit({}, "N", rated) == rated["N"]
    assert check_esd.victim_limit({}, "M", rated)[0] == check_esd.VICTIM_ABS_MAX_V
    assert check_esd.victim_limit({"victim_abs_max_v": 12}, "N", rated)[0] == 12
    assert check_esd.victim_limit({"victim_abs_max_v": {"N*": 9}}, "N", rated)[0] == 9


def test_victim_abs_max_read_from_pin_ratings(tmp_path, monkeypatch):
    """A rated IC pin on the victim sets its limit, and names the source."""
    import re as _re
    import sim_board
    monkeypatch.setattr(sim_board, "load_pin_ratings", lambda *a: [{
        "match": "^AMP1", "pins": "^IN[+-]$", "abs_max_v": 26.0, "source": "ds",
        "_m": _re.compile("^AMP1", _re.I), "_p": _re.compile("^IN[+-]$")}])
    pad = ('(pad "2" smd rect (at 0 0) (size 1 1) (layers "F.Cu") '
           '(net "VICT") (pinfunction "IN+"))')
    fp = ('  (footprint "t:U3" (at 40 30) (layer "F.Cu")\n'
          '    (property "Reference" "U3" (at 0 0 0) (layer "F.SilkS"))\n'
          '    (property "Value" "AMP1" (at 0 0 0) (layer "F.Fab"))\n'
          f'    {pad})\n')
    bg = check_esd.geom.load_board(_pcb(tmp_path, fp))
    got = check_esd.rated_pins(bg, None, {"VICT"})
    assert got["VICT"][0] == 26.0 and "pin_ratings" in got["VICT"][1]


def test_loop_mutual_over_a_plane_closed_form():
    """Long lines over a plane: M(d) - M(image) tends to the textbook
    microstrip mutual mu0/4pi ln(1 + (2h/d)^2) per length."""
    l, d, h = 1000.0, 0.4, 0.2
    m = (emsolve.filament_mutual(0, l, 0, l, d)
         - emsolve.filament_mutual(0, l, 0, l, math.hypot(d, 2 * h)))
    want = 1e-7 * math.log(1 + (2 * h / d) ** 2) * l * 1e-3
    assert m == pytest.approx(want, rel=0.002)


# ---- clamp ratings (#310): reference/esd_clamps.yaml against the line

def _clamp_p(ref: str, net: str, y: float, props: dict) -> str:
    """A clamp at x = 8 with its line pad at (8, y), GND pad at (8, y + 2)
    and the given datasheet properties (MPN, LCSC)."""
    fp = _fp(ref, props.pop("Value", "ESD"), f"8 {y}",
             [("1", 0, 0, net), ("2", 0, 2, "GND")])
    extra = "".join(f'    (property "{k}" "{v}" (at 0 0 0) (layer "F.Fab"))\n'
                    for k, v in props.items())
    head, tail = fp.split("    (pad", 1)
    return head + extra + "    (pad" + tail


def _usb_pair(tmp_path, dp: dict, dn: dict) -> Path:
    """J1 D+/D- at (5, 10) / (5, 14), clamps D1 / D2 straight on them,
    on to U1; a ground via by each clamp."""
    j = _fp("J1", "USB-C", "5 10", [("1", 0, 0, "/USB_D+"),
                                    ("2", 0, 4, "/USB_D-"), ("3", 0, 8, "GND")])
    u = _fp("U1", "MCU", "30 10", [("1", 0, 0, "/USB_D+"), ("2", 0, 4, "/USB_D-")])
    body = (j + u + _clamp_p("D1", "/USB_D+", 10, dict(dp))
            + _clamp_p("D2", "/USB_D-", 14, dict(dn))
            + _seg(5, 10, 8, 10, "/USB_D+") + _seg(8, 10, 30, 10, "/USB_D+")
            + _seg(5, 14, 8, 14, "/USB_D-") + _seg(8, 14, 30, 14, "/USB_D-")
            + _via(8, 13) + _via(8, 17))
    return _pcb(tmp_path, body)


def _ratings(payload) -> dict:
    return {ln["net"]: ln.get("rating") for c in payload["checked"]
            for ln in c["lines"] if ln.get("clamp")}


ESD321 = {"MPN": "ESD321DPYR"}


def test_rated_clamp_on_usb_fs_passes(tmp_path):
    payload, kinds = _run(_usb_pair(tmp_path, ESD321, ESD321))
    assert not kinds & {"esd_clamp_cap", "esd_clamp_vrwm", "esd_pair_mismatch"}
    r = _ratings(payload)["/USB_D+"]
    assert r["interface"] == "usb_fs" and r["cap_pf"] == 1.1 and r["cap_kind"] == "max"
    assert r["cap"] == "pass" and r["budget_pf"] == 50.0
    assert r["hs_cap"] == "pass"    # 1.1 pF is under the HS budget too: a fact
    assert r["vrwm"] == 3.6 and r["working_v"] == 3.6 and r["vrwm_check"] == "pass"
    assert r["pair"] == "pass"


def test_clamp_over_the_hs_budget_is_an_error(tmp_path):
    srv = {"Value": "SRV05-4", "LCSC": "C85364"}
    pcb = _usb_pair(tmp_path, srv, srv)
    payload, kinds = _run(pcb, {"esd": {"interfaces": {"/USB_D*": "usb_hs"}}})
    caps = [v for v in payload["violations"] if v["kind"] == "esd_clamp_cap"]
    assert len(caps) == 2 and all(v["severity"] == "error" for v in caps)
    assert caps[0]["budget_pf"] == 4.0 and caps[0]["cap_pf"] == 5.0
    # at full speed (the default) the same part passes, the HS figure a fact
    payload, kinds = _run(pcb)
    assert "esd_clamp_cap" not in kinds
    r = _ratings(payload)["/USB_D-"]
    assert r["cap"] == "pass" and r["hs_cap"] == "fail"


def test_typical_only_capacitance_over_budget_is_a_warning(tmp_path):
    z = {"MPN": "ESD5Z5.0T1G"}
    payload, _ = _run(_usb_pair(tmp_path, z, z))
    caps = [v for v in payload["violations"] if v["kind"] == "esd_clamp_cap"]
    assert caps and all(v["severity"] == "warning" and v["cap_kind"] == "typ"
                        for v in caps)


def test_vrwm_below_the_line_voltage_is_an_error(tmp_path):
    payload, kinds = _run(_usb_pair(tmp_path, ESD321, ESD321),
                          {"esd": {"line_v": {"/USB_D*": 5}}})
    v = [x for x in payload["violations"] if x["kind"] == "esd_clamp_vrwm"]
    assert len(v) == 2 and v[0]["severity"] == "error"
    assert v[0]["vrwm"] == 3.6 and v[0]["working_v"] == 5


def test_mismatched_pair_is_a_warning(tmp_path):
    payload, kinds = _run(_usb_pair(tmp_path, ESD321, {"MPN": "TPD1E04U04DPLR"}))
    assert "esd_pair_mismatch" in kinds
    assert _ratings(payload)["/USB_D+"]["pair"] == "fail"


def test_clamp_with_no_row_is_not_judged(tmp_path):
    payload, kinds = _run(_usb_pair(tmp_path, {"MPN": "XYZ123"}, {"MPN": "XYZ123"}))
    assert not kinds & {"esd_clamp_cap", "esd_clamp_vrwm", "esd_pair_mismatch"}
    r = _ratings(payload)["/USB_D+"]
    assert r["row"] is None and r["cap"] == "not_judged"
    assert r["vrwm_check"] == "not_judged"
    assert any("no row" in w for w in r["why_not"])


def test_second_source_without_its_own_row_is_not_judged(tmp_path):
    # MCC's TPD1E04U04DPLR-TP has its own sheet: TI's row does not hold
    mcc = {"MPN": "TPD1E04U04DPLR-TP", "LCSC": "C41408060"}
    payload, _ = _run(_usb_pair(tmp_path, mcc, mcc))
    assert _ratings(payload)["/USB_D+"]["row"] is None
    payload, _ = _run(_usb_pair(tmp_path, {"MPN": "TPD1E04U04DPLR"},
                                {"MPN": "TPD1E04U04DPLR"}))
    assert "TPD1E04U04" in _ratings(payload)["/USB_D+"]["row"]
    # USBLC6-2 is rated only for the LCSC code whose sheet was read
    usb = {"Value": "USBLC6-2SC6"}
    payload, _ = _run(_usb_pair(tmp_path, usb, usb))
    assert _ratings(payload)["/USB_D+"]["row"] is None
    usb = {"Value": "USBLC6-2SC6", "LCSC": "C2827654"}
    payload, _ = _run(_usb_pair(tmp_path, usb, usb))
    assert _ratings(payload)["/USB_D+"]["cap_pf"] == 1.0


def test_unknown_interface_kind_is_not_judged(tmp_path):
    payload, kinds = _run(_usb_pair(tmp_path, ESD321, ESD321),
                          {"esd": {"interfaces": {"/USB_D*": "hdmi"}}})
    r = _ratings(payload)["/USB_D+"]
    assert r["interface"] is None and r["cap"] == "not_judged"
    assert "not one of" in r["interface_from"]


def test_usb_names_pair_up():
    for p, n in (("/USB_D+", "/USB_D-"), ("D_USB_P", "D_USB_N"),
                 ("/USB/DP", "/USB/DM")):
        a, b = check_esd.usb_data_pol(p), check_esd.usb_data_pol(n)
        assert a and b and a[0] == b[0] and {a[1], b[1]} == {"P", "N"}, (p, n)
    assert check_esd.usb_data_pol("/FPGA/TDI") is None


# ---- bipolar lines: the clamp's polarity against a line that swings both
# sides of ground (esd.bipolar_v)

def _two_lines(tmp_path, part: dict, gnd_b: str = "GND", net_a="/E1_RX",
               net_b="/LINE_B") -> Path:
    """J1 pins 1 / 2 on net_a / net_b, the same clamp part D1 / D2 straight
    on each, on to U1; D2's ground pin on gnd_b."""
    j = _fp("J1", "RJ45", "5 10", [("1", 0, 0, net_a), ("2", 0, 4, net_b),
                                   ("3", 0, 8, "GND")])
    u = _fp("U1", "LIU", "30 10", [("1", 0, 0, net_a), ("2", 0, 4, net_b)])
    d2 = _fp("D2", part.get("Value", "ESD"), "8 14",
             [("1", 0, 0, net_b), ("2", 0, 2, gnd_b)])
    extra = "".join(f'    (property "{k}" "{v}" (at 0 0 0) (layer "F.Fab"))\n'
                    for k, v in part.items() if k != "Value")
    head, tail = d2.split("    (pad", 1)
    body = (j + u + _clamp_p("D1", net_a, 10, dict(part))
            + head + extra + "    (pad" + tail
            + _seg(5, 10, 8, 10, net_a) + _seg(8, 10, 30, 10, net_a)
            + _seg(5, 14, 8, 14, net_b) + _seg(8, 14, 30, 14, net_b)
            + _via(8, 13) + _via(8, 17))
    return _pcb(tmp_path, body)


IP4220 = {"Value": "IP4220CZ6"}


def test_one_way_clamp_to_ground_on_a_bipolar_line_is_an_error(tmp_path):
    """The planted fault: a steering array (IP4220CZ6, one way, VF 0.7 V)
    with its ground pin on GND on an E1 line swinging +/-3.3 V; its twin on
    a line not marked bipolar is quiet."""
    payload, kinds = _run(_two_lines(tmp_path, IP4220),
                          {"esd": {"bipolar_v": {"/E1_*": 3.3}}})
    bad = [v for v in payload["violations"] if v["kind"] == "esd_clamp_bipolar"]
    assert len(bad) == 1, json.dumps(bad)
    v = bad[0]
    assert v["severity"] == "error" and v["net"] == "/E1_RX"
    assert v["vf"] == 0.7 and v["bipolar_v"] == 3.3 and v["polarity"] == "uni"
    assert v["ground_pins_on"] == ["GND"] and "ground-side diode" in v["msg"]
    r = _ratings(payload)
    assert r["/E1_RX"]["bipolar_check"] == "fail"
    assert r["/LINE_B"]["bipolar_check"] is None    # the quiet twin
    assert r["/LINE_B"]["bipolar_v"] is None


def test_peak_under_the_forward_drop_passes(tmp_path):
    payload, kinds = _run(_two_lines(tmp_path, IP4220),
                          {"esd": {"bipolar_v": {"/E1_*": 0.5}}})
    assert "esd_clamp_bipolar" not in kinds
    assert _ratings(payload)["/E1_RX"]["bipolar_check"] == "pass"


def test_one_way_clamp_with_no_ground_pin_is_not_judged(tmp_path):
    """A transformer's line side: the array's GND pin left floating, so
    it clamps only line to line (as ice1usb's U7 / U12)."""
    payload, kinds = _run(_two_lines(tmp_path, IP4220, gnd_b="unconnected-(D2-Pad2)",
                                     net_a="/LINE_A", net_b="/E1_TIP"),
                          {"esd": {"bipolar_v": {"/E1_*": 3.3}}})
    assert "esd_clamp_bipolar" not in kinds
    r = _ratings(payload)["/E1_TIP"]
    assert r["bipolar_check"] == "not_judged" and r["ground_pins_on"] == []
    assert any("line to line" in w for w in r["why_not"])


def test_two_way_clamp_is_judged_on_its_stand_off(tmp_path):
    pesd = {"Value": "PESD0402-140"}
    pcb = _two_lines(tmp_path, pesd)
    payload, kinds = _run(pcb, {"esd": {"bipolar_v": {"/E1_*": 12}}})
    assert "esd_clamp_bipolar" not in kinds
    assert _ratings(payload)["/E1_RX"]["bipolar_check"] == "pass"
    payload, kinds = _run(pcb, {"esd": {"bipolar_v": {"/E1_*": 15}}})
    v = [x for x in payload["violations"] if x["kind"] == "esd_clamp_bipolar"]
    assert len(v) == 1 and v[0]["polarity"] == "bi" and v[0]["vrwm"] == 14


def test_bipolar_line_without_polarity_or_vf_is_not_judged(tmp_path):
    # ESD441: one way, but its sheet gives no forward drop
    payload, kinds = _run(_two_lines(tmp_path, {"MPN": "ESD441DPLR"}),
                          {"esd": {"bipolar_v": 3.3}})
    assert "esd_clamp_bipolar" not in kinds
    r = _ratings(payload)["/E1_RX"]
    assert r["bipolar_check"] == "not_judged"
    assert any("forward drop" in w for w in r["why_not"])
    # USBLC6-2 (Msksemi): the row carries no polarity
    usb = {"Value": "USBLC6-2SC6", "LCSC": "C2827654"}
    payload, kinds = _run(_two_lines(tmp_path, usb), {"esd": {"bipolar_v": 3.3}})
    r = _ratings(payload)["/LINE_B"]
    assert r["bipolar_check"] == "not_judged"
    assert any("no polarity" in w for w in r["why_not"])


# #312: the goldens' esd blocks exempt two true findings so their verify runs
# stay clean. Drop the exemptions and the check must still find them.
@pytest.mark.parametrize("board,nets", [
    ("rf4", {"/RF_FEED"}),
    ("usbbuck4", {"/USB_DP", "/USB_DM"}),
])
def test_golden_exempt_nets_are_true_findings(tmp_path, board, nets):
    src = REPO / "tests" / "golden" / board
    cons = json.loads((src / "constraints.json").read_text(encoding="utf-8"))
    assert set(cons["esd"]["exempt_nets"]) == nets
    del cons["esd"]["exempt_nets"]
    c = tmp_path / "constraints.json"
    c.write_text(json.dumps(cons), encoding="utf-8")
    payload, _ = check_esd.run(["--pcb", str(src / f"{board}.kicad_pcb"),
                                "--constraints", str(c),
                                "--out", str(tmp_path / "esd.json")])
    hit = {v["net"] for v in payload["violations"]
           if v["kind"] == "esd_unclamped" and v["severity"] == "error"}
    assert hit == nets


# #1496: the five false groups the pinned boards' triage found (83 of 114
# hits false). Each group has a planted line it clears and a twin with the
# protection taken away that must fire again.
def _fp_lib(ref: str, value: str, at: str, pads: list[tuple], lib: str,
            props: dict | None = None) -> str:
    """_fp with the footprint's library name and extra properties."""
    body = " ".join(
        f'(pad "{n}" smd rect (at {x} {y}) (size 1 1) (layers "F.Cu") '
        f'(net "{net}"))' for n, x, y, net in pads)
    extra = "".join(f'    (property "{k}" "{v}" (at 0 0 0) (layer "F.Fab"))\n'
                    for k, v in (props or {}).items())
    return (f'  (footprint "{lib}" (at {at}) (layer "F.Cu")\n'
            f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'    (property "Value" "{value}" (at 0 0 0) (layer "F.Fab"))\n'
            f'{extra}    {body})\n')


J_PADS = [("1", 0, 0, "SIG"), ("2", 0, 2, "GND")]


@pytest.mark.parametrize("lib", [
    "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    "Connector_PinSocket_2.54mm:PinSocket_1x02_P2.54mm_Vertical",
    "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical",
    "Connector_JST:JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal",
    "Connector_JST:JST_ZH_B2B-ZR_1x02_P1.50mm_Vertical",
    "aiee:HDR-TH_2P-P2.54-V-M",
])
def test_header_footprint_is_internal_and_external_brings_it_back(tmp_path, lib):
    j = _fp_lib("J1", "Conn_01x02", "5 10", J_PADS, lib)
    payload, kinds = _run(_pcb(tmp_path, j + U1 + _seg(5, 10, 30, 10)))
    assert payload["status"] == "pass", kinds
    assert payload["checked"][0]["internal_by_footprint"] == ["J1"]
    payload, kinds = _run(_pcb(tmp_path, j + U1 + _seg(5, 10, 30, 10)),
                          {"esd": {"external": ["J1"]}})
    assert kinds == {"esd_unclamped"}


@pytest.mark.parametrize("lib", [
    "Connector_JST:JST_VH_B2P-VH_1x02_P3.96mm_Vertical",
    "Connector_JST:JST_XH_B2B-XH-A_1x02_P2.50mm_Vertical",
    "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm",
    "Connector_USB:USB_C_Receptacle_GCT_USB4105",
])
def test_field_wiring_footprints_stay_judged(tmp_path, lib):
    j = _fp_lib("J1", "Conn_01x02", "5 10", J_PADS, lib)
    payload, kinds = _run(_pcb(tmp_path, j + U1 + _seg(5, 10, 30, 10)))
    assert kinds == {"esd_unclamped"}


# k2293 (G6): connectors that mate with another board or a part inside the
# same product, by footprint or Value. esd.external keeps a ref judged.
DF12 = "Connector_Hirose:Hirose_DF12_DF12C3.0-60DS-0.5V_2x30_P0.50mm_Vertical"


@pytest.mark.parametrize("lib,value", [
    (DF12, "Conn_02x30"),
    ("Connector_Hirose:Hirose_DF40C-30DS-0.4V_2x15_P0.40mm_Vertical",
     "Conn_02x15"),
    ("Connector_Hirose:Hirose_DF14A-4P-1.25H_1x04_P1.25mm_Horizontal",
     "Conn_01x04"),
    ("Connector_FFC-FPC:Hirose_FH12-24S-0.5SH_1x24-1MP_P0.50mm_Horizontal",
     "Conn_01x24"),
    ("Connector_FFC-FPC:Hirose_FH34SRJ-8S-0.5SH_1x08-1MP_P0.50mm_Horizontal",
     "FH34SRJ-8S"),
    ("Connector_DIN:DIN41612_C_3x32_Male_Horizontal_THT", "Backplane"),
    ("gcm:EUROCARD_96", "Conn_03x32"),
    ("gcm:MODULE_RECEPTACLE", "5530843-4"),
    ("cards:FPC-20P_0.5mm", "Conn_01x20"),
])
def test_board_to_board_family_is_internal(tmp_path, lib, value):
    j = _fp_lib("J1", value, "5 10", J_PADS, lib)
    payload, kinds = _run(_pcb(tmp_path, j + U1 + _seg(5, 10, 30, 10)))
    assert payload["status"] == "pass", kinds
    assert payload["checked"][0]["internal_board_to_board"] == ["J1"]


def test_df12_named_external_stays_judged(tmp_path):
    """The planted fault: a DF12 the designer lists in esd.external is a
    cable to the outside after all, and its unclamped line fires."""
    j = _fp_lib("J1", "Conn_02x30", "5 10", J_PADS, DF12)
    payload, kinds = _run(_pcb(tmp_path, j + U1 + _seg(5, 10, 30, 10)),
                          {"esd": {"external": ["J1"]}})
    assert kinds == {"esd_unclamped"}
    assert payload["checked"][0]["internal_board_to_board"] == []


@pytest.mark.parametrize("lib,value", [
    ("Connector_IDC:IDC-Header_2x05_P2.54mm_Vertical", "EXTERNAL I/O"),
    ("Connector_PCBEdge:BUS_PCIexpress_x1", "Card edge"),
    ("Connector_Hirose:Hirose_DF13-02P-1.25DSA_1x02_P1.25mm_Vertical",
     "Conn_01x02"),
    ("Connector_Generic:XFFCX_1x02", "Conn_01x02"),
])
def test_idc_card_edge_and_lookalikes_stay_judged(tmp_path, lib, value):
    j = _fp_lib("J1", value, "5 10", J_PADS, lib)
    payload, kinds = _run(_pcb(tmp_path, j + U1 + _seg(5, 10, 30, 10)))
    assert kinds == {"esd_unclamped"}


# k2293 follow-up: a line that reaches no part it could damage (only other
# connectors' pads, series passives and LEDs) is not judged as unclamped.
def _no_ic(tail: str) -> str:
    j2 = _fp("J2", "Conn_02x04", "40 10", [("1", 0, 0, "SIG2")])
    r = _fp("R1", "330", "20 10", [("1", 0, 0, "SIG"), ("2", 1, 0, "SIG2")])
    return J1 + r + j2 + tail + _seg(5, 10, 20, 10)


def test_line_to_another_connector_only_has_no_victim(tmp_path):
    led = _fp_lib("D1", "LED", "30 12",
                  [("1", 0, 0, "SIG2"), ("2", 0, 1, "GND")],
                  "LED_SMD:LED_0603_1608Metric")
    payload, kinds = _run(_pcb(tmp_path, _no_ic(led)))
    assert payload["status"] == "pass", kinds
    line = payload["checked"][0]["lines"][0]
    assert line["no_victim"] and line["clamp"] is None


def test_pull_down_to_ground_alone_stays_judged(tmp_path):
    """A CC-style line: a resistor to ground and nothing onward (no other
    connector, no LED) is not a no-victim line; #1496 called these real."""
    r = _fp("R1", "5.1k", "20 10", [("1", 0, 0, "SIG"), ("2", 1, 0, "GND")])
    payload, kinds = _run(_pcb(tmp_path, J1 + r + _seg(5, 10, 20, 10)))
    assert kinds == {"esd_unclamped"}


@pytest.mark.parametrize("ref,value,pads", [
    # the quiet twin: an IC pin behind the resistor
    ("U2", "MCU", [("1", 0, 0, "SIG2")]),
    # a capacitor or a resistor network is a part the strike can hurt
    ("C2", "100nF", [("1", 0, 0, "SIG2"), ("2", 0, 2, "GND")]),
    ("RN1", "4x10k", [("1", 0, 0, "SIG2"), ("8", 0, 2, "X")]),
    # a diode that is no LED is a part too
    ("D1", "1N4148", [("1", 0, 0, "SIG2"), ("2", 0, 2, "GND")]),
])
def test_line_that_reaches_a_part_is_still_unclamped(tmp_path, ref, value,
                                                     pads):
    payload, kinds = _run(_pcb(tmp_path, _no_ic(_fp(ref, value, "45 10",
                                                    pads))))
    assert kinds == {"esd_unclamped"}
    assert "no_victim" not in payload["checked"][0]["lines"][0]


def _shunt(value: str, x: float = 8) -> str:
    return _fp("C1", value, f"{x} 10", [("1", 0, 0, "SIG"), ("2", 0, 2, "GND")])


def test_shunt_cap_at_the_pin_soaks_the_strike(tmp_path):
    pcb = _pcb(tmp_path, J1 + U1 + _shunt("2.2uF") + _seg(5, 10, 8, 10)
               + _seg(8, 10, 30, 10))
    payload, kinds = _run(pcb)
    assert payload["status"] == "pass", kinds
    sc = payload["checked"][0]["lines"][0]["shunt_c"]
    assert sc["ref"] == "C1" and sc["routed"] and sc["mm"] == pytest.approx(3.0)
    # 150 pF at 8 kV is 1.2 uC: 0.545 V on 2.2 uF
    assert sc["est_v"] == pytest.approx(150e-12 * 8e3 / 2.2e-6, abs=1e-3)


@pytest.mark.parametrize("value,x", [("100nF", 8), ("2.2uF", 40)])
def test_small_or_late_shunt_cap_is_no_clamp(tmp_path, value, x):
    """A 100 nF cap is no charge sink; a 2.2 uF one the strike meets only
    after the IC pin does not protect the pin."""
    pcb = _pcb(tmp_path, J1 + U1 + _shunt(value, x) + _seg(5, 10, 40, 10))
    payload, kinds = _run(pcb)
    assert kinds == {"esd_unclamped"}


def _magjack(value: str, props: dict | None = None) -> str:
    return _fp_lib("J1", value, "5 10", J_PADS,
                   "Connector_RJ:RJ45_Hanrun_HR911105A_Horizontal"
                   if "HR911105" in value else "Connector_RJ:RJ45_Amphenol",
                   props)


def test_magjack_lines_sit_behind_the_transformer(tmp_path):
    payload, kinds = _run(_pcb(tmp_path, _magjack("HR911105A") + U1))
    assert payload["status"] == "pass", kinds
    c = payload["checked"][0]
    assert c["barriers"] == ["J1"] and c["lines"][0]["barrier"] == "J1"
    # a plain RJ45 has no transformer in it: back to unclamped
    payload, kinds = _run(_pcb(tmp_path, _magjack("RJ45") + U1))
    assert kinds == {"esd_unclamped"}
    # named in the constraints instead
    payload, kinds = _run(_pcb(tmp_path, _magjack("RJ45") + U1),
                          {"esd": {"barriers": ["J1"]}})
    assert payload["status"] == "pass"


def _rail_behind(series: str, net: str = "+12V") -> Path:
    j = _fp("J1", "DC jack", "5 10", [("1", 0, 0, net), ("2", 0, 2, "GND")])
    s = _fp(series, "PPTC", "10 10", [("1", 0, 0, net), ("2", 1, 0, net + "_F")])
    d = _fp("D1", "SMBJ15A", "14 10", [("1", 0, 0, net + "_F"), ("2", 0, 2, "GND")])
    u = _fp("U1", "BUCK", "30 10", [("1", 0, 0, net + "_F")])
    return j + s + d + u + _via(14, 13)


def test_rail_tvs_behind_its_input_fuse_is_the_usual_order(tmp_path):
    payload, kinds = _run(_pcb(tmp_path, _rail_behind("F1")))
    assert payload["status"] == "pass", kinds
    line = payload["checked"][0]["lines"][0]
    assert line["series"] == "F1" and line["series_fuse"]
    # a sheet's /12V_IN is a rail by its leaf name
    payload, kinds = _run(_pcb(tmp_path, _rail_behind("F1", "/12V_IN")))
    assert payload["status"] == "pass", kinds
    # a resistor or ferrite ahead of the TVS still crosses the strike first
    payload, kinds = _run(_pcb(tmp_path, _rail_behind("R1")))
    assert kinds == {"esd_behind_series"}
    # and a signal line behind a fuse still warns
    payload, kinds = _run(_pcb(tmp_path, _rail_behind("F1", "SIG")))
    assert kinds == {"esd_behind_series"}


def test_abs_max_from_the_library_extraction_counts_part_wide_rows(tmp_path):
    """INA186A1 (C2058311): IN+/IN- are rated -0.3..42 V part-wide in hwde's
    library extraction; a board whose parts/ has no copy reads it there, and
    without the library it is unrated."""
    import sim_inrush
    fp = _fp_lib("U3", "INA186A1", "40 30",
                 [("4", 0, 0, "VICT"), ("3", 1, 0, "+3V3")], "t:U3")
    bg = check_esd.geom.load_board(_pcb(tmp_path, fp))
    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / "parts.json").write_text(json.dumps(
        {"parts": [{"refdes": ["U3"], "lcsc": "C2058311", "mpn": "INA186A1"}]}))
    got = check_esd.rated_pins(bg, parts, {"VICT"})
    assert got["VICT"][0] == pytest.approx(42.0), got
    comps = {"U3": {"ref": "U3", "kind": "U", "value": "INA186A1",
                    "pins": {"4": {"net": "VICT", "fn": "4"}}}}
    raw = {"U3": {"refdes": ["U3"], "lcsc": "C2058311"}}
    rated, unrated = sim_inrush.pin_abs_max({"comps": comps}, raw, parts,
                                            {"VICT"})
    assert not any(r["net"] == "VICT" and r["max_v"] == 42.0 for r in rated)


PINNED_ESD = {
    "core": "pinned/core/kicad/core.kicad_pcb",
    "digits-v1": "pinned/digits-v1/kicad/digits-pcb.kicad_pcb",
    "lite-main": "pinned/lite-main/kicad/lite-main.kicad_pcb",
    "minuet-v3": "pinned/minuet-v3/kicad/minuet.kicad_pcb",
    "oasis-v0-2": "pinned/oasis-v0-2/kicad/pcb.kicad_pcb",
    "hwde-led-test": "boards/hwde-led-test/kicad/hwde-led-test.kicad_pcb",
}


def test_pinned_boards_keep_every_real_esd_hit():
    """#1496 bar: all 31 hits triaged real stay found, and what is left
    beyond them is the known false ones with a stated reason."""
    real, false, got = set(), set(), set()
    for board, rel in PINNED_ESD.items():
        pcb = REPO / rel
        known = json.loads((REPO / "pinned" / "known" / f"{board}.json")
                           .read_text(encoding="utf-8"))["known"]
        for k in known:
            if k.get("check") == "check_esd":
                (real if k["verdict"] == "real" else false).add(
                    (board, k["kind"], k["net"], tuple(k["refs"])))
        cdir = pcb.parent.parent
        cons = cdir / "constraints.json"
        viols, _ = check_esd.check(
            pcb, json.loads(cons.read_text()) if cons.is_file() else None,
            cdir / "parts" if (cdir / "parts").is_dir() else None)
        got |= {(board, v["kind"], v["net"], tuple(v["refs"])) for v in viols}
    assert len(real) == 31
    assert real <= got, sorted(real - got)
    assert got - real <= false, sorted(got - real - false)
