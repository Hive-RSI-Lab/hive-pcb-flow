"""Mated connector pins (check_mate_pins.py, reference/connector_pinmap.yaml):
MECH-05 declared mated pairs and MECH-06 identical unkeyed connectors.

Each synthetic case builds its own boards and asserts both the pin or
connector that is flagged and the one that is not. The planted faults
(mech-05-pinout-swap, mech-06-unkeyed-rails) are read from their manifest.d
fragment; the real-board cases (PCB-0011-A's twin 5-pin headers, the lumina
carrier and par stack) skip when the boards repo is absent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GOLDEN = REPO / "tests" / "golden"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
sys.path.insert(0, str(GOLDEN / "mutations"))
import check_mate_pins  # noqa: E402
import checklib  # noqa: E402
import mech_05_pi_header_mirrored  # noqa: E402
import mech_05_pinout_swap  # noqa: E402

from _boards import real_board  # noqa: E402

HDR4 = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
SKT4 = "Connector_PinSocket_2.54mm:PinSocket_1x04_P2.54mm_Vertical"
SKT3 = "Connector_PinSocket_2.54mm:PinSocket_1x03_P2.54mm_Vertical"
HDR2 = "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
JST2 = "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical"


def _fp(ref, fpid, x, y, nets, layer="F.Cu", pitch=2.54):
    """A 1xN row along +x from (x, y); nets[i] is pin i+1's net."""
    pads = "".join(
        f'    (pad "{i + 1}" thru_hole circle (at {pitch * i} 0) (size 1.7 1.7)'
        f' (drill 1) (layers "*.Cu" "*.Mask")'
        + (f' (net "{n}")' if n else "") + ")\n"
        for i, n in enumerate(nets))
    crt = "F.CrtYd" if layer == "F.Cu" else "B.CrtYd"
    return (f'  (footprint "{fpid}" (layer "{layer}") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 -2 0))\n'
            f'    (attr through_hole)\n'
            f'    (fp_rect (start -1.5 -1.5) (end {pitch * len(nets) + 1} 1.5)'
            f' (stroke (width 0.05)) (fill no) (layer "{crt}"))\n'
            f'{pads}  )\n')


def _board(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""(kicad_pcb
  (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 40 30) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
""", encoding="utf-8")
    return path


def _run(pcb: Path, pairs=None, tmp=None) -> list[dict]:
    argv = ["--pcb", str(pcb)]
    if pairs is not None:
        cons = (tmp or pcb.parent) / "constraints.json"
        cons.write_text(json.dumps({"mating_pairs": pairs}), encoding="utf-8")
        argv += ["--constraints", str(cons)]
    payload, _ = check_mate_pins.run(argv)
    return payload["violations"]


def _pair(tmp_path, main_nets, mate_nets, mate_fp=SKT4, mate_layer="B.Cu",
          mount="stack", **extra):
    main = _board(tmp_path / "main.kicad_pcb",
                  _fp("J2", HDR4, 10, 10, main_nets))
    _board(tmp_path / "mate" / "d.kicad_pcb",
           _fp("J1", mate_fp, 10, 10, mate_nets, layer=mate_layer))
    return _run(main, [{"ref": "J2", "mate_ref": "J1",
                        "mate_pcb": "mate/d.kicad_pcb", "mount": mount,
                        **extra}])


NETS = ["+3V3", "/SWDIO", "/SWCLK", "GND"]


# ------------------------------------------------------------- MECH-05 pairs

def test_stacked_pair_wired_straight_passes(tmp_path):
    assert _pair(tmp_path, NETS, ["+3V3", "SWDIO", "SWCLK", "GND"]) == []


def test_stacked_pair_with_rails_swapped_is_a_supply_short(tmp_path):
    vs = _pair(tmp_path, NETS, ["GND", "/SWDIO", "/SWCLK", "+3V3"])
    assert {v["pin"] for v in vs} == {"1", "4"}       # 2 and 3 still meet
    assert all(v["kind"] == "mate_pin_mismatch" and v["severity"] == "error"
               and v["supply_short"] for v in vs)
    by_pin = {v["pin"]: v for v in vs}
    assert by_pin["1"]["pos"] == [10.0, 10.0]          # J2 pin 1
    assert by_pin["1"]["mate_net"] == "GND"


def test_signal_swap_is_a_mismatch_but_not_a_short(tmp_path):
    vs = _pair(tmp_path, NETS, ["+3V3", "/SWCLK", "/SWDIO", "GND"])
    assert {v["pin"] for v in vs} == {"2", "3"}
    assert not any(v["supply_short"] for v in vs)


def test_flip_mount_mirrors_the_mate(tmp_path):
    """The same straight socket mated face to face (board turned over) puts
    pin 1 on pin 4: every pin meets the wrong net."""
    assert _pair(tmp_path, NETS, NETS, mount="flip") != []
    # wired mirror-image for a flip, it passes
    assert _pair(tmp_path, NETS, list(reversed(NETS)), mount="flip") == []


def test_net_map_names_the_mate_side(tmp_path):
    mate = ["VDD", "/SWDIO", "/SWCLK", "GND"]
    assert {v["pin"] for v in _pair(tmp_path, NETS, mate)} == {"1"}
    assert _pair(tmp_path, NETS, mate, net_map={"+3V3": "VDD"}) == []


def test_unconnected_pin_is_not_compared(tmp_path):
    vs = _pair(tmp_path, NETS, ["+3V3", None, "unconnected-(J1-Pad3)", "/X"])
    assert [v["pin"] for v in vs] == ["4"]


def test_cable_pair_on_one_board_meets_pin_n_to_pin_n(tmp_path):
    pcb = _board(tmp_path / "b.kicad_pcb",
                 _fp("J1", HDR4, 5, 5, NETS)
                 + _fp("J5", HDR4, 5, 20, ["+3V3", "/SWCLK", "/SWDIO", "GND"]))
    vs = _run(pcb, [{"ref": "J1", "mate_ref": "J5", "mount": "cable"}])
    assert {v["pin"] for v in vs} == {"2", "3"}
    assert all(v["kind"] == "mate_pin_mismatch" for v in vs)


def test_patterns_that_do_not_line_up(tmp_path):
    vs = _pair(tmp_path, NETS, ["+3V3", "/SWDIO", "/SWCLK"], mate_fp=SKT3)
    assert [v["kind"] for v in vs] == ["mate_family_mismatch"]
    assert "do not line up" in vs[0]["msg"]


def test_known_families_that_do_not_mate(tmp_path):
    vs = _pair(tmp_path, NETS, NETS, mate_fp=JST2.replace("1x02", "1x04"))
    assert [v["kind"] for v in vs] == ["mate_family_mismatch"]
    assert "does not plug into" in vs[0]["msg"]


def test_unresolved_pair(tmp_path):
    main = _board(tmp_path / "main.kicad_pcb",
                  _fp("J2", HDR4, 10, 10, NETS))
    _board(tmp_path / "d.kicad_pcb", _fp("J1", SKT4, 10, 10, NETS, "B.Cu"))
    ok = {"ref": "J2", "mate_ref": "J1", "mate_pcb": "d.kicad_pcb"}
    assert _run(main, [ok]) == []
    for bad, why in (({"mate_ref": "J9"}, "J9 is not on"),
                     ({"mate_pcb": "gone.kicad_pcb"}, "not found"),
                     ({"ref": "J7"}, "J7 is not on this board")):
        vs = _run(main, [{**ok, **bad}])
        assert [v["kind"] for v in vs] == ["mate_pair_unresolved"], bad
        assert why in vs[0]["msg"]


def test_bad_pair_declarations_stop_the_check(tmp_path):
    main = _board(tmp_path / "main.kicad_pcb", _fp("J2", HDR4, 10, 10, NETS))
    with pytest.raises(checklib.CheckError, match="mount"):
        _run(main, [{"ref": "J2", "mate_ref": "J2", "mount": "sideways"}])
    with pytest.raises(checklib.CheckError, match="mate_ref"):
        _run(main, [{"ref": "J2"}])


# ------------------------------------------------- MECH-06 unkeyed twins

def test_identical_unkeyed_headers_with_different_rails(tmp_path):
    pcb = _board(tmp_path / "b.kicad_pcb",
                 _fp("J1", HDR2, 5, 5, ["+5V", "GND"])
                 + _fp("J3", HDR2, 5, 20, ["+3V3", "GND"]))
    vs = _run(pcb)
    assert [(v["kind"], v["pin"], v["refs"]) for v in vs] == [
        ("mate_unkeyed_rail_swap", "1", ["J1", "J3"])]      # pin 2 is GND both
    assert vs[0]["pos"] == [5.0, 5.0]


def test_twins_that_do_not_trip_the_rule(tmp_path):
    cases = {
        "same rails": (HDR2, ["+5V", "GND"], HDR2, ["+5V", "GND"]),
        "signals only": (HDR2, ["/TX", "GND"], HDR2, ["/RX", "GND"]),
        "keyed family": (JST2, ["+5V", "GND"], JST2, ["+3V3", "GND"]),
        "different footprints": (HDR2, ["+5V", "GND"],
                                 HDR4, ["+3V3", "GND", "/A", "/B"]),
    }
    for name, (fa, na, fb, nb) in cases.items():
        pcb = _board(tmp_path / name / "b.kicad_pcb",
                     _fp("J1", fa, 5, 5, na) + _fp("J3", fb, 5, 20, nb))
        assert _run(pcb) == [], name


def test_rail_and_ground_names():
    assert all(map(check_mate_pins.is_rail,
                   ["+3V3", "+5V", "VBUS", "/VCC", "VDD_IO", "+48V_SW", "V5"]))
    assert all(map(check_mate_pins.is_gnd, ["GND", "AGND", "PGND", "VSS"]))
    assert not any(map(check_mate_pins.is_rail,
                       ["GND", "/SWDIO", "/VSENSE_ADC", "/IO3", None]))
    assert check_mate_pins.canon("/control/ID_ADC") == "ID_ADC"


# ------------------------------------------------- golden corpus

@pytest.mark.parametrize("board", ["blinky2", "usbbuck4", "rf4"])
def test_golden_boards_are_clean(board):
    d = GOLDEN / board
    payload, _ = check_mate_pins.run(
        ["--pcb", str(d / f"{board}.kicad_pcb"),
         "--constraints", str(d / "constraints.json")])
    assert payload["violations"] == []


FRAGMENT = yaml.safe_load(
    (GOLDEN / "manifest.d" / "check_mate_pins.yaml").read_text(
        encoding="utf-8"))["mutants"]


@pytest.mark.parametrize("name", sorted(FRAGMENT))
def test_planted_fault_is_caught_where_it_is(name):
    m = FRAGMENT[name]
    d = GOLDEN / "mutants" / name
    argv = ["--pcb", str(d / f"{m['board']}.kicad_pcb")]
    cons = d / "constraints.json"
    argv += ["--constraints", str(cons if cons.exists()
                                  else GOLDEN / m["board"] / "constraints.json")]
    payload, _ = check_mate_pins.run(argv)
    exp = m["expect"]
    hits = [v for v in payload["violations"] if v["kind"] == exp["kind"]
            and exp["ref"] in v["refs"] and v["severity"] == "error"
            and v["pos"] == exp["pos"]]
    assert len(hits) == 1, payload["violations"]
    assert {v["kind"] for v in payload["violations"]} == {exp["kind"]}


def test_mech05_daughter_wired_straight_is_clean(tmp_path):
    """The planted fault's own fixture with the swap left out passes, so the
    catch is the swap and not the fixture."""
    (tmp_path / "mate").mkdir()
    (tmp_path / mech_05_pinout_swap.MATE).write_text(
        mech_05_pinout_swap.daughter(swap=False), encoding="utf-8")
    pair = {"ref": "J2", "mate_ref": "J1", "mate_pcb": mech_05_pinout_swap.MATE,
            "mount": "stack"}
    pcb = GOLDEN / "usbbuck4" / "usbbuck4.kicad_pcb"
    assert _run(pcb, [pair], tmp=tmp_path) == []
    (tmp_path / mech_05_pinout_swap.MATE).write_text(
        mech_05_pinout_swap.daughter(swap=True), encoding="utf-8")
    assert {v["pin"] for v in _run(pcb, [pair], tmp=tmp_path)} == {"1", "4"}


# ------------------------------------------- MECH-05 standard headers

def _pi_board(tmp_path, mirrored, at=(20, 10, -90), nets=None, layer="B.Cu"):
    fp = mech_05_pi_header_mirrored.header(
        mirrored=mirrored, ref="J1", at=at,
        nets=nets or mech_05_pi_header_mirrored.NETS)
    if layer == "F.Cu":
        fp = fp.replace('(layer "B.Cu")', '(layer "F.Cu")')
    return _board(tmp_path / "pi.kicad_pcb", fp)


@pytest.mark.parametrize("angle", [0, 90, 180, -90])
def test_back_side_pi_header_mirrored_in_any_rotation(tmp_path, angle):
    """A mirror can't be turned away: digits v1 J1 was rotated 180 deg."""
    vs = _run(_pi_board(tmp_path, True, at=(20, 10, angle)))
    assert [(v["kind"], v["refs"]) for v in vs] == [
        ("mate_mirrored_pinout", ["J1"])]
    assert "pin 2 (+5V) meets the mate's pin 1 (3V3)" in vs[0]["msg"]
    assert vs[0]["supply_pins"] == {"1": "3V3", "2": "5V", "6": "GND"}


@pytest.mark.parametrize("angle", [0, 90, 180, -90])
def test_back_side_pi_header_numbered_for_the_back_passes(tmp_path, angle):
    assert _run(_pi_board(tmp_path, False, at=(20, 10, angle))) == []


def test_front_side_pi_header_is_not_judged(tmp_path):
    """On F.Cu both patterns are right somewhere (a HAT's socket soldered
    from below; a host face down above, digits v2)."""
    assert _run(_pi_board(tmp_path, True, layer="F.Cu")) == []
    assert _run(_pi_board(tmp_path, False, layer="F.Cu")) == []


def test_a_40_pin_header_that_is_not_a_pi_is_not_judged(tmp_path):
    # GND alone is not enough, and a supply on a pin the Pi gives another
    # (+5V on pin 1) says it is some other 2x20
    assert _run(_pi_board(tmp_path, True, nets={"6": "GND", "9": "GND"})) == []
    assert _run(_pi_board(tmp_path, True, nets={
        "1": "+5V", "2": "+5V", "6": "GND"})) == []


def test_declared_pair_is_left_to_the_pair_rules(tmp_path):
    pcb = _pi_board(tmp_path, True)
    vs = _run(pcb, [{"ref": "J1", "mate_ref": "J1"}])
    assert "mate_mirrored_pinout" not in {v["kind"] for v in vs}


def test_handedness_reads_the_pattern():
    hdr = [("1", None, 0, 0), ("2", None, 2.54, 0), ("3", None, 0, 2.54)]
    mir = [("1", None, 0, 0), ("2", None, 2.54, 0), ("3", None, 0, -2.54)]
    one_row = [("1", None, 0, 0), ("2", None, 2.54, 0), ("3", None, 5.08, 0)]
    assert check_mate_pins.handedness(hdr, 2.54, 0.2) == 1
    assert check_mate_pins.handedness(mir, 2.54, 0.2) == -1
    assert check_mate_pins.handedness(one_row, 2.54, 0.2) is None


# ------------------------------------------------- real boards

def _pcb(ws: Path) -> Path:
    return next(iter(sorted((ws / "kicad").glob("*.kicad_pcb"))))


def test_bb_mcu_twin_headers_are_flagged():
    """PCB-0011-A, the MECH-06 hit: J2 (SWD) and J3 (IO) are the same 5-pin
    header and J2's pin 3 is +3V3 where J3's is /IO3."""
    vs = _run(_pcb(real_board("bb-mcu")))
    assert [(v["kind"], v["pin"], v["refs"]) for v in vs] == [
        ("mate_unkeyed_rail_swap", "3", ["J2", "J3"])]


def test_lumina_carrier_and_par_stack(tmp_path):
    """PCB-0004-A carries PCB-0005-A on two headers. Stacked the same way up,
    the power header J3 meets net for net; J4's three expansion lines carry
    different names on each board, which net_map says are the same. Turned
    over (flip), +48V_SW meets GND."""
    carrier = _pcb(real_board("lumina-carrier"))
    par = _pcb(real_board("lumina-par"))
    names = {"ADC0_CONN": "ADC0", "ADC1_CONN": "ADC1",
             "ID_ADC_CONN": "ID_ADC"}
    pairs = [{"ref": r, "mate_ref": r, "mate_pcb": str(par),
              "net_map": names} for r in ("J3", "J4")]
    assert _run(carrier, pairs, tmp=tmp_path) == []
    bare = _run(carrier, [{**p, "net_map": {}} for p in pairs], tmp=tmp_path)
    assert {(v["connector"], v["pin"]) for v in bare} == {
        ("J4", "20"), ("J4", "21"), ("J4", "22")}
    flip = _run(carrier, [{**pairs[0], "mount": "flip"}], tmp=tmp_path)
    assert any(v["supply_short"] and v["net"] == "+48V_SW" for v in flip)


# ------------------------------------- bottom-side connector mirrored (#474)

HDR10 = "Connector_PinHeader_2.54mm:PinHeader_2x05_P2.54mm_Vertical"
SKT10 = "Connector_PinSocket_2.54mm:PinSocket_2x05_P2.54mm_Vertical"
NETS10 = ["+5V", "GND", "/SDA", "/SCL", "/TX", "/RX", "/INT", "/RST",
          "+3V3", "GND"]


def _fp2x5(ref, fpid, nets, layer="F.Cu"):
    """A 2x5 odd/even header at (10, 10) as KiCad stores it: a footprint
    flipped to B.Cu has its pad x mirrored (pcbnew bakes the flip in)."""
    sx = -1 if layer == "B.Cu" else 1
    pads = "".join(
        f'    (pad "{2 * i + r + 1}" thru_hole circle (at {sx * 2.54 * r} '
        f'{2.54 * i}) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") '
        f'(net "{nets[2 * i + r]}"))\n' for i in range(5) for r in range(2))
    crt = "F.CrtYd" if layer == "F.Cu" else "B.CrtYd"
    return (f'  (footprint "{fpid}" (layer "{layer}") (at 10 10)\n'
            f'    (property "Reference" "{ref}" (at 0 -2 0))\n'
            f'    (attr through_hole)\n'
            f'    (fp_rect (start -4 -1.5) (end 4 12) (stroke (width 0.05))'
            f' (fill no) (layer "{crt}"))\n{pads}  )\n')


def _pair10(tmp_path, mate_layer, mount="stack"):
    main = _board(tmp_path / "main.kicad_pcb", _fp2x5("J2", HDR10, NETS10))
    _board(tmp_path / "mate" / "d.kicad_pcb",
           _fp2x5("J1", SKT10, NETS10, layer=mate_layer))
    return _run(main, [{"ref": "J2", "mate_ref": "J1",
                        "mate_pcb": "mate/d.kicad_pcb", "mount": mount}])


def test_bottom_side_socket_mirrored_against_its_mate(tmp_path):
    # planted fault: the mate's socket, numbered pin n = net n like the
    # header, flipped to B.Cu: its rows swap, every pin meets the other row
    vs = _pair10(tmp_path, "B.Cu")
    mir = [v for v in vs if v["kind"] == "mate_mirrored"]
    assert len(mir) == 1 and mir[0]["severity"] == "error"
    assert mir[0]["bottom"] == ["J1"] and mir[0]["mismatched"] == 10
    assert "pin 1 meets pin 2" in mir[0]["msg"]
    assert {v["kind"] for v in vs} == {"mate_mirrored", "mate_pin_mismatch"}


def test_same_socket_on_the_top_mates(tmp_path):
    assert _pair10(tmp_path, "F.Cu") == []


def test_front_side_pair_with_wrong_nets_is_not_called_a_mirror(tmp_path):
    main = _board(tmp_path / "main.kicad_pcb", _fp2x5("J2", HDR10, NETS10))
    swapped = NETS10[:2] + [NETS10[3], NETS10[2]] + NETS10[4:]
    _board(tmp_path / "mate" / "d.kicad_pcb", _fp2x5("J1", SKT10, swapped))
    vs = _run(main, [{"ref": "J2", "mate_ref": "J1",
                      "mate_pcb": "mate/d.kicad_pcb"}])
    assert {v["kind"] for v in vs} == {"mate_pin_mismatch"}


# ------------------------------------- MECH-05 against a stated mate (#1124)

def _fp2row(ref, fpid, n, row_pitch, pitch=2.54):
    """2 rows of n pins along +y, row_pitch apart in x, at (10, 5)."""
    pads = "".join(
        f'    (pad "{2 * i + r + 1}" thru_hole circle (at {row_pitch * r} '
        f'{pitch * i}) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask"))\n'
        for i in range(n) for r in range(2))
    return (f'  (footprint "{fpid}" (layer "F.Cu") (at 10 5)\n'
            f'    (property "Reference" "{ref}" (at 0 -2 0))\n'
            f'    (attr through_hole)\n'
            f'    (fp_rect (start -1.5 -1.5) (end {row_pitch + 1.5} '
            f'{pitch * n}) (stroke (width 0.05)) (fill no) (layer "F.CrtYd"))'
            f'\n{pads}  )\n')


def test_stated_mate_of_the_same_gender(tmp_path):
    pcb = _board(tmp_path / "b.kicad_pcb", _fp("J1", SKT4, 10, 10, NETS))
    vs = _run(pcb, [{"ref": "J1", "mate_standard": "pin-socket-2.54"}])
    assert [v["kind"] for v in vs] == ["mate_gender_mismatch"]
    assert "female" in vs[0]["msg"]
    assert _run(pcb, [{"ref": "J1", "mate_standard": "pin-header-2.54"}]) \
        == []


def test_stated_mate_row_spacing(tmp_path):
    # a DIP-40 socket (rows 15.24 apart) where a Pico (17.78) goes
    dip = _board(tmp_path / "dip.kicad_pcb",
                 _fp2row("U1", "Package_DIP:DIP-40_W15.24mm_Socket", 20,
                         15.24))
    vs = _run(dip, [{"ref": "U1", "mate_standard": "raspberry-pi-pico"}])
    assert [v["kind"] for v in vs] == ["mate_row_spacing"]
    assert vs[0]["row_pitch_mm"] == 15.24 and "17.78" in vs[0]["msg"]
    ok = _board(tmp_path / "ok.kicad_pcb",
                _fp2row("U1", "x:Pico_Socket", 20, 17.78))
    assert _run(ok, [{"ref": "U1", "mate_standard": "raspberry-pi-pico"}]) \
        == []


def test_stated_edge_mate_takes_either_row_spacing(tmp_path):
    for rp, want in ((2.54, ["mate_row_spacing"]), (3.56, []), (5.08, [])):
        pcb = _board(tmp_path / f"e{rp}.kicad_pcb",
                     _fp2row("Z1", "zxbus:ZX_Bus_Pin_Header", 28, rp))
        vs = _run(pcb, [{"ref": "Z1", "mate_standard": "zx-spectrum-edge"}])
        assert [v["kind"] for v in vs] == want, rp


def test_stated_mate_pitch_and_unknowns(tmp_path):
    pcb = _board(tmp_path / "b.kicad_pcb",
                 _fp2row("U1", "x:Thing", 20, 17.78, pitch=2.0))
    vs = _run(pcb, [{"ref": "U1", "mate_standard": "raspberry-pi-pico"}])
    assert [v["kind"] for v in vs] == ["mate_family_mismatch"]
    with pytest.raises(checklib.CheckError, match="mate_standard"):
        _run(pcb, [{"ref": "U1", "mate_standard": "no-such-thing"}])
    with pytest.raises(checklib.CheckError, match="mate_ref"):
        _run(pcb, [{"ref": "U1", "mate_ref": "J1",
                    "mate_standard": "raspberry-pi-pico"}])


def test_declared_pair_with_other_row_spacing_is_named(tmp_path):
    main = _board(tmp_path / "main.kicad_pcb",
                  _fp2row("J2", "x:Hdr", 5, 2.54))
    _board(tmp_path / "mate" / "d.kicad_pcb",
           _fp2row("J1", "x:Skt", 5, 5.08))
    vs = _run(main, [{"ref": "J2", "mate_ref": "J1",
                      "mate_pcb": "mate/d.kicad_pcb", "mount": "stack"}])
    assert [v["kind"] for v in vs] == ["mate_row_spacing"]


def test_row_geometry():
    class P:
        def __init__(s, x, y):
            s.local, s.number = (x, y), "1"

    class F:
        pads = [P(0, 2.54 * i) for i in range(3)] + \
            [P(5.08, 2.54 * i) for i in range(3)]
    assert check_mate_pins.row_geometry(F) == \
        {"rows": 2, "row_pitch": 5.08, "pitch": 2.54}
    F.pads = [P(0, 0), P(1, 1), P(2, 3)]
    assert check_mate_pins.row_geometry(F) is None
