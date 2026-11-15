"""check_antenna and check_straps (issue #130): the two checks the first-spin
review of hifi-receiver-2's core did by hand (review-2 DIGITAL-19 and
DIGITAL-1/2), on synthetic boards and netlists."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))
import check_antenna  # noqa: E402
import check_reset_drive  # noqa: E402
import check_straps  # noqa: E402
import verify_all  # noqa: E402


# ------------------------------------------------------------- the antenna

def _module(at: str, value: str = "ESP32-WROVER-E") -> str:
    """An 18 x 26 mm module: pads down both long sides from local y -6 to
    +12, so the pad-free end is local -y: 13 - 6.45 = 6.55 mm, less the 1 mm
    PAD_MARGIN_MM by the end pads, 5.55 mm. A pad's
    (at) angle carries the footprint's, as KiCad writes it."""
    rot = at.split()[2] if len(at.split()) > 2 else "0"
    pads = []
    for i, y in enumerate(range(-6, 13, 2)):
        pads.append(f'(pad "{i + 1}" smd rect (at -8.5 {y} {rot}) (size 1 0.9) '
                    f'(layers "F.Cu") (net "GND"))')
        pads.append(f'(pad "{i + 21}" smd rect (at 8.5 {y} {rot}) (size 1 0.9) '
                    f'(layers "F.Cu") (net "GND"))')
    return f"""  (footprint "t:ESP32-WROOM" (at {at}) (layer "F.Cu")
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "{value}" (at 0 0 0) (layer "F.Fab"))
    (fp_rect (start -9 -13) (end 9 13) (stroke (width 0.05)) (fill no)
      (layer "F.CrtYd"))
    {' '.join(pads)})
"""


def _plane(layer: str) -> str:
    pts = "(pts (xy 0 0) (xy 60 0) (xy 60 40) (xy 0 40))"
    return (f'  (zone (net "GND") (layer "{layer}") (polygon {pts})\n'
            f'    (filled_polygon (layer "{layer}") {pts}))\n')


def _pcb(tmp_path, body: str) -> Path:
    p = tmp_path / "ant.kicad_pcb"
    p.write_text(f"""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 60 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
{body})
""", encoding="utf-8")
    return p


def _antenna(pcb, constraints=None):
    payload, _ = check_antenna.run(
        ["--pcb", str(pcb)] + (["--constraints", str(constraints)]
                               if constraints else []))
    return payload, {v["kind"] for v in payload["violations"]}


def test_antenna_over_the_edge_and_clear_passes(tmp_path):
    payload, kinds = _antenna(_pcb(tmp_path, _module("30 10")))
    assert payload["status"] == "pass", json.dumps(payload["violations"])
    mod = payload["checked"][0]["modules"][0]
    assert mod["antenna"] == "pcb" and abs(mod["depth_mm"] - 5.55) < 0.01
    # local y -13..-7.45 at (30, 10): board y -3..2.55, 3 mm over the edge
    assert mod["antenna_bbox"] == [21.0, -3.0, 39.0, 2.55]


def test_planes_under_the_antenna_fail_on_every_layer(tmp_path):
    """core's DIGITAL-19: GND fill under the antenna on each layer."""
    pcb = _pcb(tmp_path, _module("30 10") + _plane("F.Cu") + _plane("B.Cu"))
    payload, kinds = _antenna(pcb)
    errs = [v for v in payload["violations"] if v["kind"] == "antenna_copper"]
    assert sorted(v["layer"] for v in errs) == ["B.Cu", "F.Cu"]
    assert all(v["severity"] == "error" and v["refs"] == ["U1"] for v in errs)
    # 18 x 2.55 mm of the antenna is over the board
    assert abs(errs[0]["area_mm2"] - 18 * 2.55) < 0.5
    assert "antenna_side_clearance" in kinds


def test_a_track_under_the_antenna_fails(tmp_path):
    body = _module("30 10") + (
        '  (segment (start 25 1) (end 25 30) (width 0.2) (layer "B.Cu") '
        '(net "/I2S_DIN"))\n')
    payload, _ = _antenna(_pcb(tmp_path, body))
    (v,) = [v for v in payload["violations"] if v["kind"] == "antenna_copper"]
    assert v["layer"] == "B.Cu" and v["net"] == "/I2S_DIN"


def test_antenna_mid_board_warns(tmp_path):
    payload, kinds = _antenna(_pcb(tmp_path, _module("30 20")))
    assert kinds == {"antenna_inboard"}
    (v,) = payload["violations"]
    assert v["severity"] == "warning" and abs(v["inset_mm"] - 7.0) < 0.01


def test_rotated_module_finds_its_antenna_end(tmp_path):
    """At 90 degrees the pad-free end turns with the part: the antenna is
    5.55 mm deep along x, 18 mm wide along y, and touches no pad."""
    payload, _ = _antenna(_pcb(tmp_path, _module("20 20 90")))
    x0, y0, x1, y1 = payload["checked"][0]["modules"][0]["antenna_bbox"]
    assert abs((x1 - x0) - 5.55) < 0.01 and abs((y1 - y0) - 18) < 0.01


def test_value_decides_over_the_footprint_name(tmp_path):
    """core's U7: a WROVER-E (PCB antenna) on a footprint named -IE (u.FL)."""
    mods = check_antenna.load_modules()
    assert check_antenna.antenna_kind(
        "ESP32-WROVER-E-N8R8", "aiee:WIFI-SMD_ESP32-WROVER-IE8MB",
        mods)[0] == "pcb"
    assert check_antenna.antenna_kind(
        "", "aiee:WIFI-SMD_ESP32-WROVER-IE8MB", mods)[0] == "external"
    for ext in ("ESP32-WROOM-32UE", "ESP32-S3-WROOM-1U-N8R8",
                "ESP32-C3-MINI-1U"):
        assert check_antenna.antenna_kind(ext, "", mods)[0] == "external"
    for pcb in ("ESP32-S3-WROOM-1-N16R8", "ESP32-C3-MINI-1", "ESP-12F"):
        assert check_antenna.antenna_kind(pcb, "", mods)[0] == "pcb"
    assert check_antenna.antenna_kind("RFM95W-868S2", "", mods)[0] is None


def test_external_antenna_and_constraints_override(tmp_path):
    pcb = _pcb(tmp_path, _module("30 20", "ESP32-WROOM-32UE")
               + _plane("F.Cu"))
    payload, _ = _antenna(pcb)
    assert payload["status"] == "pass"
    cons = tmp_path / "constraints.json"
    cons.write_text(json.dumps({"antennas": {"U1": "pcb"}}),
                    encoding="utf-8")
    _, kinds = _antenna(pcb, cons)
    assert "antenna_copper" in kinds



def _part(ref: str, at: str, layer: str = "B.Cu") -> str:
    """A 2 x 5 mm part whose one pad sits 3.5 mm from its courtyard's -y end,
    so the courtyard reaches under an antenna its pad doesn't."""
    side = layer[0]
    return f"""  (footprint "t:Tall" (at {at}) (layer "{layer}")
    (property "Reference" "{ref}" (at 0 0 0) (layer "{side}.SilkS"))
    (property "Value" "Cap" (at 0 0 0) (layer "{side}.Fab"))
    (fp_rect (start -1 -4) (end 1 1) (stroke (width 0.05)) (fill no)
      (layer "{side}.CrtYd"))
    (pad "1" smd rect (at 0 0.5) (size 0.5 0.5) (layers "{layer}")
      (net "GND")))
"""


def test_planted_part_under_the_antenna_on_the_back_fails(tmp_path):
    """#1188: a part's body under the antenna, its pad not: no copper in the
    area, but a body in the near field. The guessed antenna ends at board
    y 2.55; C9's courtyard (less 0.25 mm) starts at 2.25."""
    payload, kinds = _antenna(_pcb(tmp_path, _module("30 10")
                                   + _part("C9", "30 6.0")))
    assert kinds == {"antenna_part"}, json.dumps(payload["violations"])
    (v,) = payload["violations"]
    assert v["severity"] == "error" and v["refs"] == ["C9", "U1"]
    assert v["side"] == "back" and abs(v["area_mm2"] - 1.5 * 0.3) < 0.02


def test_part_moved_clear_of_the_antenna_passes(tmp_path):
    payload, _ = _antenna(_pcb(tmp_path, _module("30 10")
                               + _part("C9", "30 7.0")))
    assert payload["status"] == "pass", json.dumps(payload["violations"])


def test_an_outline_footprint_around_the_module_is_not_a_part_under_it(
        tmp_path):
    """#1188: mdbt-micro-50q's J1 is the board's own ProMicro pin-out as a
    footprint, its courtyard round the whole module: an outline, not a part
    under the antenna. Grown to just short of the module it is one."""
    def outline(y0):
        return f"""  (footprint "t:ProMicro" (at 30 10) (layer "B.Cu")
    (property "Reference" "J1" (at 0 0 0) (layer "B.SilkS"))
    (property "Value" "ProMicro" (at 0 0 0) (layer "B.Fab"))
    (fp_rect (start -10 {y0}) (end 10 14) (stroke (width 0.05)) (fill no)
      (layer "B.CrtYd"))
    (pad "1" smd rect (at 0 12) (size 0.5 0.5) (layers "B.Cu")
      (net "GND")))
"""
    payload, _ = _antenna(_pcb(tmp_path, _module("30 10") + outline(-14)))
    assert payload["status"] == "pass", json.dumps(payload["violations"])
    payload, kinds = _antenna(_pcb(tmp_path, _module("30 10") + outline(-10)))
    assert kinds == {"antenna_part"}, json.dumps(payload["violations"])


def test_an_outline_the_module_pokes_past_is_not_a_part_under_it(tmp_path):
    """#1188: on both mdbt-micro-50q revs J1's courtyard is on the module's
    side and holds its centre, but U2's courtyard pokes past J1's edge, so
    it does not hold the whole module. No real part sits round a module's
    centre on its own side; an outline does."""
    outline = """  (footprint "t:ProMicro" (at 30 10) (layer "F.Cu")
    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "ProMicro" (at 0 0 0) (layer "F.Fab"))
    (fp_rect (start -12 -15) (end 8 15) (stroke (width 0.05)) (fill no)
      (layer "F.CrtYd"))
    (pad "1" smd rect (at -11 14) (size 0.5 0.5) (layers "F.Cu")
      (net "GND")))
"""
    payload, _ = _antenna(_pcb(tmp_path, _module("30 10") + outline))
    assert payload["status"] == "pass", json.dumps(payload["violations"])


def test_datasheet_antenna_area_replaces_the_guess(tmp_path):
    """#1188: a WROOM-32E row carries its datasheet's 18 x 6.19 mm antenna
    area (§11.1 Fig. 13) on the 25.5 mm outline, so the 26 mm courtyard's
    0.25 mm margin is not antenna: local y -12.75..-6.56 at (30, 10)."""
    payload, _ = _antenna(_pcb(tmp_path, _module("30 10", "ESP32-WROOM-32E")))
    mod = payload["checked"][0]["modules"][0]
    assert mod["antenna_from"] == "datasheet" and mod["depth_mm"] == 6.19
    assert mod["antenna_bbox"] == [21.0, -2.75, 39.0, 3.44]
    assert "url" in check_antenna.antenna_kind(
        "ESP32-WROOM-32E-N4", "", check_antenna.load_modules())[1]["keepout"]


def test_a_wroom_1_value_on_a_1u_sized_footprint_keeps_the_guess(tmp_path):
    """#1188: netscore's U1 is a WROOM-1 value on a WROOM-1U footprint
    (courtyard 19.6 x 20.5). The datasheet's 25.5 mm outline doesn't fit a
    courtyard 22 mm long, so the area falls back to the pad-free end."""
    body = _module("30 10", "ESP32-S3-WROOM-1").replace(
        "(start -9 -13) (end 9 13)", "(start -9 -11) (end 9 11)")
    payload, _ = _antenna(_pcb(tmp_path, body))
    mod = payload["checked"][0]["modules"][0]
    assert mod["antenna_from"] == "footprint (outline mismatch)"
    assert mod["depth_mm"] == 3.55


def _rm2(at: str) -> str:
    """A 14.5 x 16.5 mm module, pins 1..10 down local x -6.5 from y -7.5,
    11..20 along the bottom, 21..30 up x +6.5: pin 1 and 21's end is -y."""
    pads = []
    for i in range(10):
        pads.append(f'(pad "{i + 1}" smd rect (at -6.5 {-7.5 + 1.5 * i}) '
                    '(size 1 0.8) (layers "F.Cu") (net "GND"))')
        pads.append(f'(pad "{i + 11}" smd rect (at {-5.4 + 1.2 * i} 7.5) '
                    '(size 0.8 1) (layers "F.Cu") (net "GND"))')
        pads.append(f'(pad "{30 - i}" smd rect (at 6.5 {-7.5 + 1.5 * i}) '
                    '(size 1 0.8) (layers "F.Cu") (net "GND"))')
    return f"""  (footprint "t:RM2" (at {at}) (layer "F.Cu")
    (property "Reference" "U2" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "RM2" (at 0 0 0) (layer "F.Fab"))
    (fp_rect (start -7.25 -8.25) (end 7.25 8.25) (stroke (width 0.05))
      (fill no) (layer "F.CrtYd"))
    {' '.join(pads)})
"""


def test_planted_track_in_the_rm2_keepout_past_the_module_fails(tmp_path):
    """#1188: RM2 datasheet §2.4 Fig. 6 keeps 13.5 mm beyond the pin-1 end
    and 10.5 mm each side free of metal. At (30, 30) that is board x
    12.25..47.75, y 8.25..26.75: a track at x 15 is outside the module but
    inside its keep-out."""
    trk = ('  (segment (start 15 10) (end 15 14) (width 0.2) (layer "F.Cu") '
           '(net "/SPI_CLK"))\n')
    payload, kinds = _antenna(_pcb(tmp_path, _rm2("30 30") + trk))
    assert kinds == {"antenna_copper"}, json.dumps(payload["violations"])
    mod = payload["checked"][0]["modules"][0]
    assert mod["keepout_bbox"] == [12.25, 8.25, 47.75, 26.75]


def test_track_outside_the_rm2_keepout_passes(tmp_path):
    """Its twin: the track at x 10, and the keep-out's own 13.5 mm run past
    the module, so no inboard warning (edge_mm: null)."""
    trk = ('  (segment (start 10 10) (end 10 14) (width 0.2) (layer "F.Cu") '
           '(net "/SPI_CLK"))\n')
    payload, _ = _antenna(_pcb(tmp_path, _rm2("30 30") + trk))
    assert payload["status"] == "pass", json.dumps(payload["violations"])


# --------------------------------------------------------------- the straps

def _netlist(tmp_path, comps: dict, nets: dict) -> Path:
    """comps {ref: value}; nets {name: [(ref, pin, pinfunction)]}."""
    c = " ".join(f'(comp (ref "{r}") (value "{v}"))' for r, v in comps.items())
    n = []
    for i, (name, nodes) in enumerate(nets.items()):
        nd = " ".join(f'(node (ref "{r}") (pin "{p}") (pinfunction "{f}") '
                      f'(pintype "passive"))' for r, p, f in nodes)
        n.append(f'(net (code "{i + 1}") (name "{name}") {nd})')
    p = tmp_path / "s.net"
    p.write_text(f'(export (version "E") (components {c}) '
                 f'(nets {" ".join(n)}))', encoding="utf-8")
    pcb = tmp_path / "s.kicad_pcb"
    pcb.write_text("""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 60 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts")))
""", encoding="utf-8")
    return pcb


def _core(tmp_path, expander="TCA9555", reset_pin=("3", "A2_3", "GND"),
          r45="100k", r_io12=None):
    """core's ESP32 / RMII oscillator / expander loop, cut down."""
    comps = {"U7": "ESP32-WROVER-E-N8R8", "U17": expander, "Y2": "50MHz",
             "R31": "10k", "R60": "33R", "R45": r45, "R43": "100k",
             "R6": "1k", "R7": "100k", "Q2": "2N7002", "C32": "1uF"}
    nets = {
        "+3V3": [("U7", "2", "3V3_2"), ("U17", "24", "VCC_24"),
                 ("Y2", "4", "VDD_4"), ("R31", "2", "")],
        "GND": [("U7", "1", "GND_1"), ("Y2", "2", "GND_2"),
                ("R45", "2", ""), ("R43", "2", ""), ("R7", "2", ""),
                ("C32", "2", "")],
        "/mcu/ESP_EN": [("U7", "3", "EN_3"), ("C32", "1", "")],
        "/ETH_CLK": [("U7", "25", "IO0_25"), ("R31", "1", ""),
                     ("R60", "2", "")],
        "/ethernet/ETH_OSC": [("Y2", "3", "OUTPUT_3"), ("R60", "1", "")],
        "/PHY_RST": [("Y2", "1", "Tri-state_1"), ("U17", "9", "P05_9"),
                     ("R45", "1", "")],
        "/PVDD_EN": [("U17", "7", "P03_7"), ("R43", "1", ""),
                     ("R6", "1", "")],
        "/PVDD_GATE": [("R6", "2", ""), ("R7", "1", ""), ("Q2", "1", "G")],
        "/I2S_DOUT_SUB": [("U7", "14", "IO12_14")],
    }
    ref, pin, net = reset_pin
    nets.setdefault(net, []).append(("U17", ref, pin))
    if r_io12:
        comps["R99"] = r_io12
        nets["/I2S_DOUT_SUB"].append(("R99", "1", ""))
        nets["+3V3"].append(("R99", "2", ""))
    pcb = _netlist(tmp_path, comps, nets)
    payload, _ = check_straps.run(["--pcb", str(pcb), "--netlist",
                                   str(tmp_path / "s.net")])
    return payload


def _by_kind(payload):
    out = {}
    for v in payload["violations"]:
        out.setdefault(v["kind"], []).append(v)
    return out


def test_core_gpio0_latches_the_oscillator(tmp_path):
    """DIGITAL-1/2 as built: P05's 100 k pull-up against R45 100 k is
    1.65 V on the oscillator's OE, and P05 holds through an ESP32 reset."""
    k = _by_kind(_core(tmp_path))
    (strap,) = k["strap_level"]
    assert strap["severity"] == "error" and strap["strap"] == "IO0"
    assert strap["events"] == ["mcu_reset", "por"]
    assert "maybe a clock" in strap["states"]["por"]
    assert "firmware" in strap["states"]["mcu_reset"]
    undef = {v["net"]: v["volts"] for v in k["io_reset_undefined"]}
    assert abs(undef["/PHY_RST"] - 1.65) < 0.01
    # 100 k up against 100 k || (1 k + 100 k) down: 1.10 V on PVDD_EN
    assert abs(undef["/PVDD_EN"] - 1.10) < 0.01
    assert [v["refs"] for v in k["io_reset_holds"]] == [["U17", "U7"]]
    # POWER-5: Q2's gate (through R6 1 k) at 1.10 V against 2N7002's 1.0 V
    (fet,) = k["fet_on_at_reset"]
    assert fet["refs"] == ["Q2", "U17"] and abs(fet["volts"] - 1.10) < 0.01


def test_core_fixed_with_tca9539_on_esp_en(tmp_path):
    """The review's fix: TCA9539 (no pull-ups) with RESET on ESP_EN, and
    R45 10 k. OE is low at power-on and after every ESP32 reset."""
    payload = _core(tmp_path, expander="TCA9539PWR",
                    reset_pin=("3", "~{RESET}_3", "/mcu/ESP_EN"), r45="10k")
    assert payload["status"] == "pass", json.dumps(payload["violations"])
    io0 = [s for s in payload["checked"][0]["straps"] if s["strap"] == "IO0"]
    assert io0[0]["por"].startswith("3.") and "high" in io0[0]["por"]


def test_mtdi_pulled_high_fails(tmp_path):
    k = _by_kind(_core(tmp_path, expander="TCA9539PWR",
                       reset_pin=("3", "~{RESET}_3", "/mcu/ESP_EN"),
                       r45="10k", r_io12="10k"))
    (v,) = k["strap_level"]
    assert v["strap"] == "IO12" and v["severity"] == "error"
    assert "1.8 V" in v["msg"]


def test_pin_names_normalise():
    assert check_straps.pin_name("IO0_25", "25") == "IO0"
    assert check_straps.pin_name("~{INT}_1", "1") == "INT"
    assert check_straps.pin_name("GPIO12", "14") == "IO12"


def test_a_known_mcu_without_a_netlist_says_so(tmp_path):
    """blinky2's STM32F103 has a reset_states row (#885): with no netlist
    beside the board the check says it couldn't judge, not pass."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text((REPO / "tests" / "golden" / "blinky2" /
                    "blinky2.kicad_pcb").read_text(encoding="utf-8"),
                   encoding="utf-8")
    payload, _ = check_straps.run(["--pcb", str(pcb)])
    assert [v["kind"] for v in payload["violations"]] == ["no_netlist"]


def test_both_checks_join_verify_all():
    names = {c["name"] for c in verify_all.CHECKS}
    assert {"check_antenna", "check_straps"} <= names


# ------------------------------------------- pins the chip pulls at reset

def _c3(tmp_path, gpio6: list, comps: dict | None = None, net="GPIO6"):
    """minuet v3 cut down: an ESP32-C3 whose GPIO6 (MTCK, pulled up by the
    chip from reset) reaches `gpio6`."""
    c = {"U4": "ESP32-C3-WROOM-02-N4", "R9": "10k", **(comps or {})}
    nets = {
        "+3.3V": [("U4", "1", "3V3_1"), ("R9", "2", "")],
        "GND": [("U4", "19", "GND_19")],
        "BOOT": [("U4", "8", "GPIO9_8"), ("R9", "1", "")],
        net: [("U4", "5", "GPIO6_5")] + gpio6,
    }
    pcb = _netlist(tmp_path, c, nets)
    payload, _ = check_straps.run(["--pcb", str(pcb), "--netlist",
                                   str(tmp_path / "s.net")])
    return _by_kind(payload), payload


def test_c3_gpio6_bare_on_a_connector_warns(tmp_path):
    """minuet-4: GPIO6 to the EXPANSION header with nothing but the
    chip's own pull-up; the accessory's LED strip comes on at reset."""
    k, _ = _c3(tmp_path, [("J4", "5", "Pin_5_5")], {"J4": "Conn_02x08"})
    (v,) = k["reset_pull_offboard"]
    assert v["net"] == "GPIO6" and v["pin"] == "IO6"
    assert "J4.5" in v["msg"] and "high" in v["msg"]
    assert "strap_level" not in k


def test_c3_gpio5_bare_on_a_connector_is_quiet(tmp_path):
    """MTDI (GPIO5) and MTMS (GPIO4) are "1" in the C3 IO MUX reset column:
    input, no pull. Only MTCK (GPIO6, "1*") is pulled up, so GPIO5 on
    minuet's J4.6 is no reset_pull_offboard (it was, before #885)."""
    c = {"U4": "ESP32-C3-WROOM-02-N4", "R9": "10k", "J4": "Conn_02x08"}
    nets = {
        "+3.3V": [("U4", "1", "3V3_1"), ("R9", "2", "")],
        "GND": [("U4", "19", "GND_19")],
        "BOOT": [("U4", "8", "GPIO9_8"), ("R9", "1", "")],
        "GPIO5": [("U4", "4", "GPIO5_4"), ("J4", "6", "Pin_6_6")],
        "GPIO4": [("U4", "3", "GPIO4_3"), ("J4", "7", "Pin_7_7")],
    }
    pcb = _netlist(tmp_path, c, nets)
    payload, _ = check_straps.run(["--pcb", str(pcb), "--netlist",
                                   str(tmp_path / "s.net")])
    assert "reset_pull_offboard" not in _by_kind(payload)


def test_c3_gpio6_with_its_own_pull_is_quiet(tmp_path):
    """minuet v4's fix shape: a board pull on the line (here a pull-down
    that beats the chip's 45 k) says the idle level was chosen."""
    k, payload = _c3(tmp_path, [("J4", "5", "Pin_5_5"), ("R13", "1", "")],
                     {"J4": "Conn_02x08", "R13": "10k"})
    assert "reset_pull_offboard" not in k
    pulls = payload["checked"][0]["reset_pulls"]
    assert [p for p in pulls if p["name"] == "IO6"]


def test_c3_gpio6_on_a_jtag_header_is_quiet(tmp_path):
    k, _ = _c3(tmp_path, [("J9", "3", "TCK_3")], {"J9": "JTAG"},
               net="/MTCK")
    assert "reset_pull_offboard" not in k


def test_c3_gpio6_drives_a_fet_gate_on(tmp_path):
    """The on-board shape: GPIO6 through 1 k to a 2N7002's gate, no
    pull-down, so the chip's pull-up turns the strip's low side on."""
    c = {"R1": "1k", "Q1": "2N7002"}
    pcb_nodes = [("R1", "1", "")]
    k, _ = _c3(tmp_path, pcb_nodes, c)
    assert not k  # the gate isn't wired yet: R1's far side is open
    tmp2 = tmp_path / "b"
    tmp2.mkdir()
    comps = {"U4": "ESP32-C3-WROOM-02-N4", "R1": "1k", "Q1": "2N7002"}
    nets = {"+3.3V": [("U4", "1", "3V3_1")], "GND": [("Q1", "2", "S")],
            "GPIO6": [("U4", "5", "GPIO6_5"), ("R1", "1", "")],
            "/LED_GATE": [("R1", "2", ""), ("Q1", "1", "G")]}
    pcb = _netlist(tmp2, comps, nets)
    payload, _ = check_straps.run(["--pcb", str(pcb), "--netlist",
                                   str(tmp2 / "s.net")])
    (v,) = _by_kind(payload)["load_on_at_reset"]
    assert v["refs"] == ["Q1", "U4"] and v["volts"] > 3.0
    # a 10 k pull-down on the gate holds it at 0.6 V, under VGS(th)
    tmp3 = tmp_path / "c"
    tmp3.mkdir()
    comps["R2"] = "10k"
    nets["/LED_GATE"].append(("R2", "1", ""))
    nets["GND"].append(("R2", "2", ""))
    pcb = _netlist(tmp3, comps, nets)
    payload, _ = check_straps.run(["--pcb", str(pcb), "--netlist",
                                   str(tmp3 / "s.net")])
    assert "load_on_at_reset" not in _by_kind(payload)


def test_c3_gpio6_on_a_load_switch_enable(tmp_path):
    k, _ = _c3(tmp_path, [("U8", "3", "EN_3")], {"U8": "TPS22918"})
    (v,) = k["load_on_at_reset"]
    assert v["part_pin"] == "EN"
    # an active-low enable pulled high leaves its part off
    tmp2 = tmp_path / "b"
    tmp2.mkdir()
    k, _ = _c3(tmp2, [("U8", "3", "~{EN}_3")], {"U8": "AP2553"})
    assert "load_on_at_reset" not in k


def test_part_on_polarity():
    assert check_straps.part_on("EN", False, "high") is True
    assert check_straps.part_on("EN", True, "high") is False
    assert check_straps.part_on("NEN", False, "low") is True
    assert check_straps.part_on("SHDN", False, "high") is True
    assert check_straps.part_on("SHDN", False, "low") is False
    assert check_straps.part_on("DRVOFF", False, "low") is True
    assert check_straps.part_on("SDA", False, "high") is None
    assert check_straps.part_on("ONE", False, "high") is None


# ------------------------------------------- codec input rows (#831)

def _codec_on_strap(tmp_path, value, pin, fn, strap=("14", "IO12_14")):
    comps = {"U7": "ESP32-WROVER-E-N8R8", "U5": value}
    nets = {"+3V3": [("U7", "2", "3V3_2")], "GND": [("U7", "1", "GND_1")],
            "/mcu/ESP_EN": [("U7", "3", "EN_3")],
            "/SIG": [("U7", strap[0], strap[1]), ("U5", pin, fn)]}
    pcb = _netlist(tmp_path, comps, nets)
    payload, _ = check_straps.run(["--pcb", str(pcb), "--netlist",
                                   str(tmp_path / "s.net")])
    return _by_kind(payload)


def test_codec_input_pins_dont_drive_a_strap(tmp_path):
    """ES9018K2M / ES7243E / ES8388 clock and data pins are inputs at
    reset (datasheet-cited rows), so the strap isn't strap_unknown."""
    for value, pin, fn in (("ES9018K2M", "5", "DATA1_5"),
                           ("ES7243E", "7", "MCLK_7"),
                           ("ES8388", "2", "MCLK_2")):
        k = _codec_on_strap(tmp_path, value, pin, fn)
        assert "strap_unknown" not in k, (value, k)


def test_codec_master_clock_pin_still_flags_a_strap(tmp_path):
    """ES8388 SCLK defaults to master (an output at reset): that pin has no
    input row, so a strap on it stays flagged."""
    k = _codec_on_strap(tmp_path, "ES8388", "1", "SCLK_1")
    assert "strap_unknown" in k


# ------------------------------------- switches named by their job (#885)

def _c3_boot(tmp_path, ref, value):
    comps = {"U1": "ESP32-C3-WROOM-02-N4", ref: value}
    nets = {"+3V3": [("U1", "1", "3V3_1")], "GND": [("U1", "19", "GND_19"),
                                                     (ref, "2", "2")],
            "/EN": [("U1", "2", "EN_2")],
            "Net-(U1-IO9)": [("U1", "8", "IO9_8"), (ref, "1", "1")]}
    pcb = _netlist(tmp_path, comps, nets)
    payload, _ = check_straps.run(["--pcb", str(pcb), "--netlist",
                                   str(tmp_path / "s.net")])
    return _by_kind(payload)


def test_a_boot_button_named_boot1_is_a_switch(tmp_path):
    """oasis v0.2's BOOT button is ref boot1, value SW_Push: open at reset,
    not a part of unknown drive on GPIO9."""
    assert "strap_unknown" not in _c3_boot(tmp_path, "boot1", "SW_Push")


def test_planted_unknown_part_on_io9_still_flags(tmp_path):
    """Planted fault: the same net with a part of unknown reset state on it
    is still strap_unknown; only switch values are let through."""
    assert "strap_unknown" in _c3_boot(tmp_path, "boot1", "74LVC1G125")


# ------------------------------------------- STM32F1 (blinky2, #885)

def _f103(tmp_path, boot0_to):
    """blinky2's STM32F103: BOOT0 through R1 10k to boot0_to, SWDIO and
    SWCLK out to the debug header J1."""
    comps = {"U1": "STM32F103C8T6", "R1": "10k", "J1": "Conn_01x04"}
    nets = {"+3V3": [("U1", "24", "VDD_24")] + (
                [("R1", "2", "")] if boot0_to == "+3V3" else []),
            "GND": [("U1", "23", "VSS_23")] + (
                [("R1", "2", "")] if boot0_to == "GND" else []),
            "/NRST": [("U1", "7", "NRST_7")],
            "/BOOT0": [("U1", "44", "BOOT0_44"), ("R1", "1", "")],
            "/SWDIO": [("U1", "34", "PA13_34"), ("J1", "2", "Pin_2")],
            "/SWCLK": [("U1", "37", "PA14_37"), ("J1", "3", "Pin_3")]}
    pcb = _netlist(tmp_path, comps, nets)
    payload, _ = check_straps.run(["--pcb", str(pcb), "--netlist",
                                   str(tmp_path / "s.net")])
    return payload


def test_f103_boot0_pulled_down_with_swd_header_passes(tmp_path):
    p = _f103(tmp_path, "GND")
    assert p["violations"] == [], p["violations"]
    assert p["checked"][0]["mcus_unknown"] == []


def test_planted_f103_boot0_pulled_up_fails(tmp_path):
    """Planted fault: BOOT0 to 3V3 boots the ROM loader, not the flash."""
    (v,) = _by_kind(_f103(tmp_path, "+3V3"))["strap_level"]
    assert v["strap"] == "BOOT0" and v["severity"] == "error"


# ------------------------------- AM62x (a SoC whose straps have no pull)

AM62_UP = {0, 3, 5, 8, 11}      # the BOOTMODE bits pulled up; the rest down


def _am62(tmp_path, bank="+1V8", up_rail=None, floating=(),
          value="AM6254ATCGHAALW", ball=lambda n: f"GPMC0_AD{n}",
          tie=False):
    """An AM62x's BOOTMODE00..15 balls, each through its own 10k
    (R<n + 1>) up to `up_rail` (default: the bank) or down to GND, with
    VDDSHV3, the balls' IO bank, on `bank`. floating: bits with no pull.
    tie: bit 5's resistor goes to /BOOT5_SEL, which only JP1 (a 3-pad
    solder jumper, a net tie: pad 1 the bank, 2 /BOOT5_SEL, 3 GND) sets."""
    up_rail = up_rail or bank
    comps = {"U1": value}
    nets = {bank: [("U1", "W1", "VDDSHV3")], "GND": [("U1", "W2", "VSS")]}
    nets.setdefault(up_rail, [])
    for n in range(16):
        net = f"/BOOTMODE{n:02d}"
        nets[net] = [("U1", f"B{n + 1}", ball(n))]
        if n in floating:
            continue
        comps[f"R{n + 1}"] = "10k"
        nets[net].append((f"R{n + 1}", "1", ""))
        to = "/BOOT5_SEL" if tie and n == 5 else (
            up_rail if n in AM62_UP else "GND")
        nets.setdefault(to, []).append((f"R{n + 1}", "2", ""))
    if tie:
        comps["JP1"] = "SolderJumper_3_Bridged12"
        nets[bank].append(("JP1", "1", "A"))
        nets["/BOOT5_SEL"].append(("JP1", "2", "C"))
        nets["GND"].append(("JP1", "3", "B"))
    return _netlist(tmp_path, comps, nets)


def _jumper(pcb, bridge_layer, bank="+1V8"):
    """Rewrite `pcb` with JP1 on it: pads 1 to 3 at x -1.3, 0, 1.3 and
    its 1-2 bridge drawn on `bridge_layer`."""
    pads = (("1", -1.3, bank), ("2", 0, "/BOOT5_SEL"), ("3", 1.3, "GND"))
    pad_text = "".join(
        f'    (pad "{n}" smd rect (at {x} 0) (size 1 1.5) '
        f'(layers "F.Cu" "F.Mask") (net "{net}"))\n' for n, x, net in pads)
    pcb.write_text(f"""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (1 "F.Mask" user)
    (5 "F.SilkS" user) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 60 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts"))
  (footprint "Jumper:SolderJumper-3_Bridged12" (layer "F.Cu") (at 30 20)
    (property "Reference" "JP1" (at 0 -2 0) (layer "F.SilkS"))
    (net_tie_pad_groups "1, 2")
    (fp_poly (pts (xy -0.9 -0.3) (xy -0.4 -0.3) (xy -0.4 0.3) (xy -0.9 0.3))
      (stroke (width 0) (type solid)) (fill yes) (layer "{bridge_layer}"))
{pad_text}  ))
""", encoding="utf-8")
    return pcb


def _run(mod, pcb):
    return mod.run(["--pcb", str(pcb),
                    "--netlist", str(pcb.with_name("s.net"))])[0]


def _straps_by_name(payload):
    return {f["strap"]: f for f in payload["checked"][0]["straps"]}


def test_am62_every_bootmode_pulled_passes(tmp_path):
    p = _run(check_straps, _am62(tmp_path))
    assert p["status"] == "pass", p["violations"]
    seen = p["checked"][0]
    assert seen["mcus"] == ["U1"] and seen["mcus_unknown"] == []
    straps = _straps_by_name(p)
    assert sorted(straps) == [f"BOOTMODE{n:02d}" for n in range(16)]
    assert {f["want"] for f in straps.values()} == {"defined"}
    assert {f["vdd"] for f in straps.values()} == {1.8}
    assert "high" in straps["BOOTMODE05"]["por"]
    assert "low" in straps["BOOTMODE09"]["por"]


def test_planted_am62_floating_bootmode_fails(tmp_path):
    """Planted fault: the ball has no pull during reset, so one with no
    resistor latches whatever it drifts to."""
    (v,) = _by_kind(_run(check_straps, _am62(tmp_path, floating={9})))[
        "strap_level"]
    assert v["strap"] == "BOOTMODE09" and v["severity"] == "error"
    assert "must read a defined level (high or low)" in v["msg"]
    assert v["events"] == ["mcu_reset", "por"]
    assert "floating" in v["states"]["por"]


def test_am62_thresholds_follow_the_bank_voltage(tmp_path):
    """A pull-up to 1.8 V is high on a 1.8 V bank (VIH 0.65 x 1.8 = 1.17 V)
    and undefined on a 3.3 V one (VIH 2.0 V)."""
    p = _run(check_straps, _am62(tmp_path, bank="+1V8"))
    assert p["status"] == "pass", p["violations"]
    bad = _by_kind(_run(check_straps, _am62(tmp_path, bank="+3V3",
                                            up_rail="+1V8")))["strap_level"]
    assert sorted(v["strap"] for v in bad) == [
        f"BOOTMODE{n:02d}" for n in sorted(AM62_UP)]
    assert all("undefined" in v["states"]["por"] for v in bad)
    assert all("VIH 2.00 V" in v["states"]["por"] for v in bad)


def test_am62_bank_with_no_voltage_in_its_name_takes_the_row_vdd(tmp_path):
    p = _run(check_straps, _am62(tmp_path, bank="/VIO"))
    assert {f["vdd"] for f in _straps_by_name(p).values()} == {3.3}


def test_am62_sip_and_bootmode_ball_names(tmp_path):
    """Octavo's OSD62x SiPs carry the AM62x, and a symbol may name the
    balls BOOTMODE0..15 rather than GPMC0_AD0..15."""
    p = _run(check_straps, _am62(tmp_path, value="OSD6254-1G-IPM",
                                 ball=lambda n: f"BOOTMODE{n}"))
    assert p["status"] == "pass", p["violations"]
    assert len(_straps_by_name(p)) == 16


def test_am62_strap_set_by_a_net_tie_bridged_on_copper(tmp_path):
    """The netlist keeps each of a net tie's pads on its own net; the
    jumper's copper bridge joins /BOOT5_SEL to the bank, so BOOTMODE05
    reads high and the tie is no unknown part."""
    pcb = _jumper(_am62(tmp_path, tie=True), "F.Cu")
    p = _run(check_straps, pcb)
    assert p["status"] == "pass", p["violations"]
    assert "high" in _straps_by_name(p)["BOOTMODE05"]["por"]
    (tie,) = p["checked"][0]["net_ties"]
    assert tie["ref"] == "JP1" and tie["joins"] == [["+1V8", "/BOOT5_SEL"]]


def test_planted_am62_net_tie_bridge_on_silk_floats(tmp_path):
    """Planted fault: the bridge drawn on silk joins nothing, so the
    strap behind it floats."""
    pcb = _jumper(_am62(tmp_path, tie=True), "F.SilkS")
    (v,) = _by_kind(_run(check_straps, pcb))["strap_level"]
    assert v["strap"] == "BOOTMODE05" and v["severity"] == "error"
    assert "floating" in v["states"]["por"]
    assert _run(check_straps, pcb)["checked"][0]["net_ties"][0]["joins"] == []


def test_reset_drive_says_it_read_no_am62_pin(tmp_path):
    """check_reset_drive reads pins named IOn: an AM62x's balls aren't,
    so it says so rather than passing as if it had read them."""
    p = _run(check_reset_drive, _am62(tmp_path))
    assert p["status"] == "pass" and "nothing_to_judge" not in p
    (line,) = p["not_judged"]
    assert line.startswith("U1 (AM6254ATCGHAALW): no pin named IOn")
