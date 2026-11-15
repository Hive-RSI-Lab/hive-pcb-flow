"""check_straps on modules (taxonomy class 13): ESP8266 / ESP-WROOM-02 and
the ESP-01/07/12 modules, the ESP32-S2, the Raspberry Pi Pico and the
nRF52840 (MDBT50Q), each with a planted fault and its quiet twin. Rows:
reference/reset_states.yaml, each cited to the maker's datasheet."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))
import check_straps  # noqa: E402


def _board(tmp_path, comps: dict, nets: dict) -> dict:
    """comps {ref: value}; nets {name: [(ref, pin, pinfunction)]}: the
    check's JSON for that netlist on an empty board."""
    c = " ".join(f'(comp (ref "{r}") (value "{v}"))' for r, v in comps.items())
    n = []
    for i, (name, nodes) in enumerate(nets.items()):
        nd = " ".join(f'(node (ref "{r}") (pin "{p}") (pinfunction "{f}") '
                      f'(pintype "passive"))' for r, p, f in nodes)
        n.append(f'(net (code "{i + 1}") (name "{name}") {nd})')
    (tmp_path / "s.net").write_text(
        f'(export (version "E") (components {c}) (nets {" ".join(n)}))',
        encoding="utf-8")
    pcb = tmp_path / "s.kicad_pcb"
    pcb.write_text("""(kicad_pcb (version 20260206) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup)
  (gr_rect (start 0 0) (end 60 40) (stroke (width 0.1)) (fill no)
    (layer "Edge.Cuts")))
""", encoding="utf-8")
    payload, _ = check_straps.run(["--pcb", str(pcb), "--netlist",
                                   str(tmp_path / "s.net")])
    return payload


def _kinds(payload) -> dict:
    out = {}
    for v in payload["violations"]:
        out.setdefault(v["kind"], []).append(v)
    return out


def _esp8266(tmp_path, value="ESP-WROOM-02", io15_to="GND", en_to="+3V3",
             io0_to=None, names=("GPIO15", "GPIO0", "GPIO2", "EN")):
    """An ESP8266 part: IO15 through R1 10k to io15_to, EN through R2 10k
    to en_to, IO0 through R3 10k to io0_to, IO2 bare (None = floating)."""
    io15, io0, io2, en = names
    comps = {"U1": value, "R1": "10k", "R2": "10k", "R3": "10k"}
    nets = {"+3V3": [("U1", "1", "3V3")], "GND": [("U1", "9", "GND")],
            "/IO15": [("U1", "15", io15), ("R1", "1", "")],
            "/EN": [("U1", "3", en), ("R2", "1", "")],
            "/IO0": [("U1", "18", io0), ("R3", "1", "")],
            "/IO2": [("U1", "17", io2)]}
    for ref, rail, net in (("R1", io15_to, "/IO15"), ("R2", en_to, "/EN"),
                           ("R3", io0_to, "/IO0")):
        if rail:
            nets[rail].append((ref, "2", ""))
        else:   # no resistor fitted
            del comps[ref]
            nets[net].remove((ref, "1", ""))
    return _board(tmp_path, comps, nets)


def test_wroom02_strapped_for_flash_boot_passes(tmp_path):
    p = _esp8266(tmp_path)
    assert p["violations"] == [], p["violations"]
    seen = p["checked"][0]
    assert seen["mcus"] == ["U1"] and seen["mcus_unknown"] == []
    straps = {s["strap"]: s for s in seen["straps"]}
    assert set(straps) == {"IO15", "IO0", "IO2", "EN"}
    assert "low" in straps["IO15"]["por"] and "high" in straps["IO2"]["por"]


def test_planted_wroom02_gpio15_pulled_up_fails(tmp_path):
    """MTDO high at power-on boots the SDIO mode, not the flash."""
    (v,) = _kinds(_esp8266(tmp_path, io15_to="+3V3"))["strap_level"]
    assert v["strap"] == "IO15" and v["severity"] == "error"
    assert "SDIO" in v["msg"]


def test_planted_wroom02_gpio0_pulled_down_fails(tmp_path):
    (v,) = _kinds(_esp8266(tmp_path, io0_to="GND"))["strap_level"]
    assert v["strap"] == "IO0"


def test_bare_esp8266ex_floating_gpio15_and_en_fail(tmp_path):
    """The bare chip and the WROOM-02 have no strap resistors of their
    own: MTDO and CHIP_EN left floating are faults, named as on the chip."""
    p = _esp8266(tmp_path, value="ESP8266EX", io15_to=None, en_to=None,
                 names=("MTDO", "GPIO0", "GPIO2", "CHIP_EN"))
    assert {v["strap"] for v in _kinds(p)["strap_level"]} == {"IO15", "EN"}


def test_esp12f_floating_straps_are_not_judged(tmp_path):
    """An ESP-12F may pull GPIO15 and EN on the module; no datasheet here
    says, so a strap the board leaves floating is not judged."""
    p = _esp8266(tmp_path, value="ESP-12F", io15_to=None, en_to=None)
    assert p["violations"] == [], p["violations"]
    seen = p["checked"][0]
    assert seen["mcus"] == ["U1"] and seen["mcus_unknown"] == []
    left = p["not_judged"]
    assert len(left) == 2 and any("IO15" in x for x in left)
    assert any(x.startswith("U1 EN") for x in left)


def test_planted_esp12f_gpio15_pulled_up_still_fails(tmp_path):
    """A wrong level the board itself sets is judged on any module."""
    (v,) = _kinds(_esp8266(tmp_path, value="ESP-12F",
                           io15_to="+3V3"))["strap_level"]
    assert v["strap"] == "IO15"


def test_esp_psram_is_no_mcu(tmp_path):
    p = _board(tmp_path, {"U2": "ESP-PSRAM64H"},
               {"/CS": [("U2", "1", "CE#")]})
    assert p["violations"] == [] and p["checked"][0]["mcus"] == []


def _s2(tmp_path, io45_to):
    comps = {"U1": "ESP32-S2-WROOM", "R1": "10k", "R2": "10k"}
    nets = {"+3V3": [("U1", "2", "3V3"), ("R2", "2", "")],
            "GND": [("U1", "1", "GND")],
            "/EN": [("U1", "45", "EN"), ("R2", "1", "")],
            "/IO45": [("U1", "38", "IO45"), ("R1", "1", "")]}
    nets[io45_to].append(("R1", "2", ""))
    return _board(tmp_path, comps, nets)


def test_s2_gpio45_pulled_down_passes(tmp_path):
    p = _s2(tmp_path, "GND")
    assert p["violations"] == [], p["violations"]
    assert p["checked"][0]["mcus_unknown"] == []


def test_planted_s2_gpio45_pulled_up_fails(tmp_path):
    """GPIO45 = 1 sets VDD_SPI to 1.8 V under the module's 3.3 V flash."""
    (v,) = _kinds(_s2(tmp_path, "+3V3"))["strap_level"]
    assert v["strap"] == "IO45" and "1.8 V" in v["msg"]


def _pico(tmp_path, value="RaspberryPi_Pico", gate_rail="GND"):
    """A Pico with GPIO26_ADC0 driving Q1's gate (10k to gate_rail)."""
    comps = {"U1": value, "Q1": "2N7002", "R1": "10k"}
    nets = {"GND": [("U1", "3", "GND")], "+3V3": [("U1", "36", "3V3")],
            "/RUN": [("U1", "30", "RUN")],
            "/GATE": [("U1", "31", "GPIO26_ADC0"), ("Q1", "1", "G"),
                      ("R1", "1", "")],
            "/LOAD": [("Q1", "3", "D")]}
    nets[gate_rail].append(("R1", "2", ""))
    nets["GND"].append(("Q1", "2", "S"))
    return _board(tmp_path, comps, nets)


def test_pico_header_names_map_to_the_rp2040(tmp_path):
    board = type("B", (), {})()
    board.pins = {"U1": {"31": {"name": "GPIO26_ADC0"},
                         "1": {"name": "GP0"}, "30": {"name": "RUN"}}}
    kb = check_straps.load_kb()
    row = next(r for r in kb["mcus"] if r["family"] == "Raspberry Pi Pico")
    pins = check_straps.mcu_pins(board, {"ref": "U1", "row": row})
    assert pins["IO26"] == "31" and pins["IO0"] == "1"
    # the Pico takes the RP2040's pulls by `like`
    assert row["reset_pulls"]["IO26"]["pull"] == "down"


def test_pico_is_judged_not_unknown(tmp_path):
    p = _pico(tmp_path)
    seen = p["checked"][0]
    assert seen["mcus"] == ["U1"] and seen["mcus_unknown"] == []
    assert "mcu_unknown" not in _kinds(p)


def test_pico_2_is_not_taken_for_an_rp2040(tmp_path):
    kb = check_straps.load_kb()
    for val in ("Pico 2", "Pico2", "Pico_2W", "RaspberryPi_Pico2"):
        assert not any(r["_match"].search(val) for r in kb["mcus"]), val
    for val in ("Pico", "Pico W", "Pico_W", "RaspberryPi_Pico_W",
                "Pico-1 ROM"):
        assert any(r["family"] == "Raspberry Pi Pico"
                   and r["_match"].search(val) for r in kb["mcus"]), val


def test_mdbt50q_is_judged_with_no_straps(tmp_path):
    p = _board(tmp_path, {"U1": "MDBT50Q-1MV2"},
               {"/RST": [("U1", "40", "~{RESET}/P0.18")]})
    assert p["violations"] == [], p["violations"]
    seen = p["checked"][0]
    assert seen["mcus"] == ["U1"] and seen["mcus_unknown"] == []
