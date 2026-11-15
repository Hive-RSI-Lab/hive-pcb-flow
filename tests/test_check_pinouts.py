"""check_pinouts (#1027): off-board connectors against their standard pinout,
on netlists cut to the four real errata it was seeded from."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import check_pinouts as cp  # noqa: E402
import errata_corpus as ec  # noqa: E402


def board(nets, comps=()):
    """nets: {name: [(ref, pin, pinfunction)]} -> parse_netlist's shape;
    comps: refs, or {ref: (value, footprint)}."""
    refs = {r for ms in nets.values() for r, _, _ in ms} | set(comps)
    vf = comps if isinstance(comps, dict) else {}
    return {"components": {r: {"value": vf.get(r, ("", ""))[0],
                               "footprint": vf.get(r, ("", ""))[1],
                               "libsource": ""}
                           for r in refs},
            "nets": {n: [{"ref": r, "pin": p, "pinfunction": f, "pintype": ""}
                         for r, p, f in ms] for n, ms in nets.items()}}


def kinds(viols):
    return sorted((v["kind"], tuple(v["refs"])) for v in viols)


def jtag(tck="TCK", tdo="TDO", tdi="TDI", gnd10="GND", series=False):
    """sizif's J11 with the FPGA functions each header net reaches."""
    tck_net = [("J11", "1", ""), ("U6", "36", tck)]
    nets = {"/TCK": tck_net,
            "GND": [("J11", "2", ""), ("U6", "1", "GND")],
            "/TDO": [("J11", "3", ""), ("U6", "34", tdo)],
            "+3V3": [("J11", "4", ""), ("U6", "2", "VCCIO")],
            "/TMS": [("J11", "5", ""), ("U6", "33", "TMS")],
            "/TDI": [("J11", "9", ""), ("U6", "35", tdi)]}
    nets.setdefault(gnd10, []).append(("J11", "10", ""))
    if series:
        nets["/TCK"] = [("J11", "1", ""), ("R5", "1", "")]
        nets["/TCK_FPGA"] = [("R5", "2", ""), ("U6", "36", tck)]
    return board(nets)


def test_table_rows_cite_a_source():
    t = cp.load_table()
    assert {"altera-jtag-10", "audio-trs", "audio-trrs-ctia",
            "bedlift-lift-actuator"} <= set(t)
    for name, row in t.items():
        assert row["kind"] in ("jtag", "audio_jack", "stated", "header"), name
        assert row["source"] and row["url"].startswith("https://"), name


def test_jtag_crossed_at_the_ic_fires_though_net_names_agree():
    v, judged = cp.check(jtag(tck="TDO", tdo="TDI", tdi="TCK"), {})
    assert judged == [{"ref": "J11", "standard": "altera-jtag-10"}]
    assert [x["pin"] for x in v] == ["1", "3", "9"]
    assert {x["kind"] for x in v} == {"pinout_mismatch"}


def test_jtag_right_is_clean_and_through_a_series_resistor():
    assert cp.check(jtag(), {})[0] == []
    assert cp.check(jtag(series=True), {})[0] == []
    v = cp.check(jtag(tck="TDO", tdo="TCK", series=True), {})[0]
    assert {x["pin"] for x in v} == {"1", "3"}


def test_jtag_header_off_the_altera_layout_is_not_judged():
    """Pin 10 off ground: some other 10-pin JTAG layout, not this row's."""
    v, judged = cp.check(jtag(tck="TDO", tdo="TCK", gnd10="/SPARE"), {})
    assert v == [] and judged == []


def jack(**pins):
    return board({net: [("J4", p, "")] for p, net in pins.items()})


def test_jack_common_on_the_tip_fires():
    """echoplayer r1-rev1's J4."""
    v, _ = cp.check(jack(T="/audio/hp_com", R1="/audio/hp_left",
                         R2="/audio/hp_right", S="/audio/hp_mic"), {})
    assert kinds(v) == [("pinout_mismatch", ("J4",))]
    assert "tip T" in v[0]["msg"]


def test_jack_ctia_omtp_and_trs_are_clean():
    for pins, fit in [
            (dict(T="/hp_left", R1="/hp_right", R2="GND", S="/mic"),
             "audio-trrs-ctia"),
            (dict(T="/hp_left", R1="/hp_right", R2="/mic", S="GND"),
             "audio-trrs-omtp"),
            (dict(T="/HP_L", R="/HP_R", S="GND"), "audio-trs")]:
        v, judged = cp.check(jack(**pins), {})
        assert v == [], pins
        assert fit in judged[0]["fits"]


def test_jack_with_unnamed_nets_is_not_judged():
    v, _ = cp.check(jack(T="Net-(J4-T)", R="Net-(J4-R)", S="GND"), {})
    assert v == []


def actuator(p2, p3, vhall_driver=True):
    """bed-lift's J8 (v0.2: p2=/VHALL, p3=GND; v1: p2=GND, p3=+5V)."""
    nets = {"/HALL2": [("J8", "1", "")], "/OUT2": [("J8", "4", "")],
            "/OUT1": [("J8", "5", "")], "/HALL1": [("J8", "6", "")],
            "+3.3V": [("U4", "5", "VCC")]}
    nets.setdefault(p2, []).append(("J8", "2", ""))
    nets.setdefault(p3, []).append(("J8", "3", ""))
    nets.setdefault("GND", []).append(("U4", "3", "GND"))
    if vhall_driver:
        nets.setdefault("/VHALL", []).append(("U4", "4", "Y"))
    return board(nets)


STATED = {"pinouts": [{"ref": "J8", "standard": "bedlift-lift-actuator"}]}


def test_stated_swap_and_low_supply_fire():
    v, judged = cp.check(actuator("/VHALL", "GND"), STATED)
    assert judged[0]["stated"]
    k = [(x["kind"], x.get("pin")) for x in v]
    assert ("pinout_mismatch", "2") in k and ("pinout_mismatch", "3") in k
    sv = [x for x in v if x["kind"] == "pinout_supply_volts"]
    assert sv and sv[0]["net"] == "/VHALL" and sv[0]["volts"] == 3.3
    assert "U4" in sv[0]["msg"]


def test_stated_right_wiring_is_clean():
    assert cp.check(actuator("GND", "+5V", vhall_driver=False), STATED)[0] == []


def test_stated_needs_the_constraint_and_a_known_ref():
    assert cp.check(actuator("/VHALL", "GND"), {}) == ([], [])
    v, _ = cp.check(actuator("GND", "+5V"),
                    {"pinouts": [{"ref": "J99", "standard": "bedlift-lift-actuator"},
                                 {"ref": "J8", "standard": "no-such"}]})
    assert [(x["kind"], x["severity"]) for x in v] == [
        ("pinout_unresolved", "warning")] * 2


def test_cli_reads_a_netlist(tmp_path):
    net = ROOT / "tests" / "golden" / "mutants" / "pinout-jtag-swap" / "usbbuck4.net"
    if not net.is_file():
        pytest.skip("mutant not generated")
    out = tmp_path / "r.json"
    rc = cp.main(["--pcb", str(net.with_suffix(".kicad_pcb")),
                  "--netlist", str(net), "--out", str(out)])
    d = json.loads(out.read_text())
    assert rc != 0 and {x["kind"] for x in d["violations"]} == {"pinout_mismatch"}


def test_corpus_writes_a_catchs_constraints_per_revision(tmp_path, monkeypatch):
    seen = []

    def fake(cache, b, rev, script, constraints=None):
        seen.append((rev, constraints))
        return ([{"kind": "pinout_mismatch", "refs": ["J8"]}]
                if rev == "rev_a" else [])
    monkeypatch.setattr(ec, "audit", fake)
    c = {"script": "check_pinouts", "kind": "pinout_mismatch", "refs": ["J8"],
         "constraints": {"rev_a": {"pinouts": [{"ref": "J8"}]},
                         "rev_b": {"pinouts": [{"ref": "J10"}]}}}
    res = ec.run(tmp_path, {"id": "x", "defects": [{"id": "x-1", "catch": c}]})
    assert res["x-1"] == {"ran": True, "rev_a": True, "rev_b": False}
    assert seen == [("rev_a", {"pinouts": [{"ref": "J8"}]}),
                    ("rev_b", {"pinouts": [{"ref": "J10"}]})]


def test_constraints_lint_knows_pinouts(tmp_path):
    import constraints_lint as cl
    ok, bad = tmp_path / "ok.json", tmp_path / "bad.json"
    ok.write_text(json.dumps(STATED))
    bad.write_text(json.dumps({"pinouts": [{"ref": 3}]}))
    assert cl.lint_file(ok)[0] == []
    assert [v["kind"] for v in cl.lint_file(bad)[0]] == ["bad_type"]


# ---- standard headers (#1070): each row from its published standard ----

QWIIC_FP = "Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal"


def header(ref, value, fp, pins, ic=None):
    """pins: {pin: net}; ic: {net: (ic_pin, pinfunction)} on U1."""
    nets = {}
    for p, net in pins.items():
        nets.setdefault(net, []).append((ref, p, ""))
    for net, (p, f) in (ic or {}).items():
        nets.setdefault(net, []).append(("U1", p, f))
    return board(nets, {ref: (value, fp)})


def qwiic(value="Qwiic", sda="/SDA", scl="/SCL", vcc="+3V3", mp="GND"):
    return header("J2", value, QWIIC_FP,
                  {"1": "GND", "2": vcc, "3": sda, "4": scl, "MP": mp},
                  ic={"/SDA": ("10", "I2C1_SDA"), "/SCL": ("11", "I2C1_SCL")})


def test_qwiic_right_is_clean_and_found_by_name_or_signals():
    v, judged = cp.check(qwiic(), {})
    assert v == [] and judged == [{"ref": "J2", "standard": "qwiic",
                                   "by": "name"}]
    v, judged = cp.check(qwiic(value="Conn_01x04"), {})
    assert v == [] and judged[0]["by"] == "signals"


def test_qwiic_sda_scl_crossed_fires():
    v, _ = cp.check(qwiic(sda="/SCL", scl="/SDA"), {})
    assert [(x["kind"], x["pin"]) for x in v] == [
        ("pinout_mismatch", "3"), ("pinout_mismatch", "4")]
    assert "SparkFun Qwiic" in v[0]["msg"] and "carries SCL" in v[0]["msg"]


def test_qwiic_at_5v_fires_but_stemma_qt_at_5v_is_clean():
    v, _ = cp.check(qwiic(vcc="+5V"), {})
    assert [(x["kind"], x["pin"]) for x in v] == [("pinout_supply_volts", "2")]
    v, judged = cp.check(qwiic(value="STEMMA QT", vcc="+5V"), {})
    assert v == [] and judged[0]["standard"] == "stemma-qt"


def test_four_pin_jst_without_i2c_is_not_judged():
    b = header("J5", "Conn_01x04", QWIIC_FP,
               {"1": "GND", "2": "+5V", "3": "/RX", "4": "/TX"})
    assert cp.check(b, {}) == ([], [])


def test_mounting_pad_on_a_signal_means_not_this_header():
    assert cp.check(qwiic(mp="/SDA"), {}) == ([], [])


SWD_FP = "Connector_PinHeader_1.27mm:PinHeader_2x05_P1.27mm_Vertical"


def swd(swdio="2", swclk="4", value="Conn_ARM_SWD"):
    pins = {"1": "+3V3", "3": "GND", "5": "GND", "9": "GND", "10": "/NRST",
            "6": "/SWO", "7": "unconnected-(J1-Pad7)",
            "8": "unconnected-(J1-Pad8)"}
    pins[swdio], pins[swclk] = "/SWDIO", "/SWCLK"
    return header("J1", value, SWD_FP, pins,
                  ic={"/SWDIO": ("34", "PA13/SWDIO"),
                      "/SWCLK": ("37", "PA14/SWCLK"),
                      "/NRST": ("7", "NRST"), "/SWO": ("55", "PB3/SWO")})


def test_cortex_swd_right_is_clean():
    v, judged = cp.check(swd(), {})
    assert v == [] and judged[0]["standard"] == "cortex-swd-10"


def test_cortex_swd_clock_and_data_swapped_fires():
    v, _ = cp.check(swd(swdio="4", swclk="2"), {})
    assert [x["pin"] for x in v] == ["2", "4"]
    assert "carries SWCLK" in v[0]["msg"] and "Arm Cortex" in v[0]["msg"]


def test_cortex_swd_found_by_signals_when_unnamed():
    v, judged = cp.check(swd(swdio="4", swclk="2", value="Conn_02x05"), {})
    assert judged[0]["by"] == "signals" and len(v) == 2


def test_inline_swd_header_is_not_judged():
    """A 1x04 2.54 mm SWD header has no standard pinout."""
    b = header("J3", "SWD", "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm",
               {"1": "+3V3", "2": "/SWDIO", "3": "/SWCLK", "4": "GND"},
               ic={"/SWDIO": ("34", "SWDIO"), "/SWCLK": ("37", "SWCLK")})
    assert cp.check(b, {}) == ([], [])


def rpi_debug(p1="/SWCLK", p3="/SWDIO"):
    return header("J2", "debug",
                  "Connector_JST:JST_SH_BM03B-SRSS-H_1x03-1MP_P1.00mm_Vertical",
                  {"1": p1, "2": "GND", "3": p3},
                  ic={"/SWCLK": ("24", "GPIO24"), "/SWDIO": ("25", "GPIO25")})


def test_rpi_debug_reversed_fires_right_is_clean():
    """digits-v1 J2: SWDIO on pin 1, SWCLK on pin 3."""
    assert cp.check(rpi_debug(), {})[0] == []
    v, _ = cp.check(rpi_debug("/SWDIO", "/SWCLK"), {})
    assert [x["pin"] for x in v] == ["1", "3"]


def test_pmod_ground_off_ground_fires():
    pins = {"1": "/P0", "2": "/P1", "3": "/P2", "4": "/P3", "5": "GND",
            "6": "+3V3"}
    b = header("J4", "Pmod", "Connector:Pmod_1x06", pins)
    v, judged = cp.check(b, {})
    assert v == [] and judged[0]["standard"] == "pmod-6"
    pins["5"], pins["6"] = "+3V3", "GND"
    v, _ = cp.check(header("J4", "Pmod", "Connector:Pmod_1x06", pins), {})
    assert {x["pin"] for x in v} == {"5", "6"}


UNO_FP = "Module:Arduino_UNO_R3"


def uno(v3="+3V3"):
    pins = {"2": "+5V", "3": "/RESET", "4": v3, "5": "+5V", "6": "GND",
            "7": "GND", "8": "/VIN", "13": "/SDA", "14": "/SCL",
            "29": "GND", "31": "/SDA", "32": "/SCL"}
    return header("A1", "Arduino_UNO_R3", UNO_FP, pins)


def test_arduino_5v_on_the_3v3_pin_fires():
    v, judged = cp.check(uno(), {})
    assert v == [] and judged[0]["standard"] == "arduino-uno-r3"
    v, _ = cp.check(uno(v3="+5V"), {})
    assert [(x["kind"], x["pin"]) for x in v] == [("pinout_supply_volts", "4")]


PI_FP = "Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical"


def pi40(p1="+3V3"):
    pins = {str(i): f"/GPIO{i}" for i in range(1, 41)}
    pins.update({"1": p1, "2": "+5V", "4": "+5V", "17": "+3V3",
                 "3": "/SDA", "5": "/SCL"})
    for g in ("6", "9", "14", "20", "25", "30", "34", "39"):
        pins[g] = "GND"
    return header("J1", "Conn_02x20", PI_FP, pins)


def test_pi_header_found_by_its_rails_and_5v_on_pin_1_fires():
    v, judged = cp.check(pi40(), {})
    assert v == [] and judged == [{"ref": "J1", "standard": "rpi-40",
                                   "by": "anchors"}]
    v, _ = cp.check(pi40(p1="+5V"), {})
    assert [(x["kind"], x["pin"]) for x in v] == [("pinout_supply_volts", "1")]


def test_2x20_without_pi_rails_is_not_judged():
    pins = {str(i): f"/D{i}" for i in range(1, 41)}
    pins["40"] = "GND"
    b = header("J1", "Conn_02x20", PI_FP, pins)
    assert cp.check(b, {}) == ([], [])


def test_2x20_with_a_few_rails_where_the_pi_has_them_is_not_judged():
    """byte-hamr rev B's J1: 12 V on pin 1 and ground on 6, 34 and 39 by
    chance, but ground on 2 and 4 and signals on five Pi ground pins."""
    pins = {str(i): f"/GPIO_{i}" for i in range(1, 41)}
    pins.update({"1": "/+12V_HDR", "2": "GND", "4": "GND", "6": "GND",
                 "9": "/+3V3_HDR", "34": "GND", "39": "GND"})
    assert cp.check(header("J1", "SBH11-PBPC-D20-ST-BK", PI_FP, pins),
                    {}) == ([], [])


# -- #1087 wired-OR push-pull, #1090 pulsed load without bulk ----------------

import mate_needs as mn  # noqa: E402


def test_mate_needs_rows_cite_a_source():
    t = mn.load_table()
    rows = [*t["wired_or"]["lines"].values(), *t["wired_or"]["drivers"].values(),
            *t["pulsed_loads"]["kinds"].values(),
            *t["bus_drivers"]["parts"].values(),
            *t["i2c_single_master"]["parts"].values()]
    for row in rows:
        assert row["source"]
        if row.get("cite"):
            assert row["url"].startswith("https://"), row["source"]
        for cite in row.get("cite") or ():
            assert len(cite["sha256"]) == 64 and cite["quote"], row["source"]


def shifter(value="SN74LVC8T245", dir_net="GND", oe_net="GND", oe_fn="~{OE}",
            a_net="/SDA", b_net="/MCU_D0", dir_fn="DIR"):
    nets = {"GND": [("U2", "11", "GND")], "+3V3": [("U1", "8", "VDD")],
            a_net: [("U1", "3", "SDA"), ("U2", "3", "A1")],
            b_net: [("U3", "10", "PB7"), ("U2", "21", "B1")]}
    for net, pin, fn in ((dir_net, "2", dir_fn), (oe_net, "22", oe_fn)):
        if fn:
            nets.setdefault(net, []).append(("U2", pin, fn))
    return board(nets, {"U1": ("BME280", ""), "U2": (value, ""),
                        "U3": ("STM32", "")})


def test_transceiver_driving_sda_fires():
    v, judged = cp.check(shifter(), {})
    assert kinds(v) == [("wired_or_push_pull", ("U2",))]
    assert v[0]["nets"] == ["/SDA"] and v[0]["driver"] == "sn74lvc8t245"


def test_transceiver_receiving_from_sda_is_clean():
    v, judged = cp.check(shifter(dir_net="+3V3"), {})
    assert v == [] and any(j.get("verdict") == "receives" for j in judged)


def test_transceiver_between_two_i2c_lines_fires_either_way():
    """Whichever way DIR points, one side drives an I2C line."""
    v, _ = cp.check(shifter(dir_net="+3V3", b_net="/MCU_SDA"), {})
    assert kinds(v) == [("wired_or_push_pull", ("U2",))]
    assert v[0]["nets"] == ["/MCU_SDA"]


def test_transceiver_with_oe_from_logic_is_listed_not_judged():
    v, judged = cp.check(shifter(oe_net="/BUS_EN"), {})
    assert v == [] and any(j.get("verdict") == "switched" for j in judged)


def test_transceiver_disabled_by_oe_high_is_clean():
    v, judged = cp.check(shifter(oe_net="+3V3"), {})
    assert v == [] and any(j.get("verdict") == "isolated" for j in judged)


def test_txb_with_oe_not_grounded_fires_and_grounded_is_clean():
    b = shifter(value="TXB0108", oe_net="+3V3", oe_fn="OE", dir_fn=None)
    assert kinds(cp.check(b, {})[0]) == [("wired_or_push_pull", ("U2",))]
    b = shifter(value="TXB0108", oe_net="GND", oe_fn="OE", dir_fn=None)
    assert cp.check(b, {})[0] == []


def test_open_drain_buffer_is_clean():
    b = board({"/SDA": [("U1", "3", "SDA"), ("U2", "4", "Y")],
               "/X": [("U2", "2", "A")]},
              {"U1": ("BME280", ""), "U2": ("SN74LVC1G07", "")})
    v, judged = cp.check(b, {})
    assert v == [] and any(j.get("verdict") == "open_drain" for j in judged)


def test_pull_up_resistor_is_not_a_hop_to_a_driver():
    """R1 pulls /SDA up to +3V3; U2 on the rail is no driver of the line."""
    b = board({"/SDA": [("U1", "3", "SDA"), ("R1", "1", "")],
               "+3V3": [("R1", "2", ""), ("U2", "1", "A1"),
                        ("U2", "2", "DIR"), ("U2", "22", "~{OE}")]},
              {"U1": ("BME280", ""), "R1": ("4k7", ""),
               "U2": ("SN74LVC8T245", "")})
    assert cp.check(b, {})[0] == []


def lidar(caps):
    nets = {"/5V_LIDAR": [("J7", "1", "")], "GND": [("J7", "6", "")],
            "/LIDAR_SDA": [("J7", "5", "")]}
    comps = {"J7": ("LIDAR-Lite v3", "")}
    for i, val in enumerate(caps, 1):
        nets["/5V_LIDAR"].append((f"C{i}", "1", ""))
        nets["GND"].append((f"C{i}", "2", ""))
        comps[f"C{i}"] = (val, "")
    return board(nets, comps)


def test_pulsed_load_with_no_bulk_fires():
    v, judged = cp.check(lidar(["100nF"]), {})
    assert kinds(v) == [("pulsed_load_no_bulk", ("J7",))]
    assert v[0]["load"] == "lidar_lite" and v[0]["vendor_uf"] == 680


def test_pulsed_load_with_bulk_is_clean():
    v, judged = cp.check(lidar(["100nF", "47uF"]), {})
    assert v == [] and any(j.get("verdict") == "ok" for j in judged)


def test_connector_not_a_pulsed_load_is_not_judged():
    b = board({"/5V": [("J7", "1", "")], "GND": [("J7", "2", "")]},
              {"J7": ("Conn_01x02", "")})
    assert cp.check(b, {}) == ([], [])


# -- class 9: shared-bus drivers --------------------------------------------

def strobe(second="74HC08", fn="1Y", oe_net="GND"):
    """A Z80's /WR with a second driver: a gate, or a 244 whose OE is on
    `oe_net`."""
    nets = {"/WR": [("U1", "22", "~{WR}"), ("U2", "3", fn),
                    ("U9", "27", "~{WE}")],
            "GND": [("U1", "29", "GND"), ("U2", "7", "GND")],
            "+5V": [("U1", "11", "VCC"), ("U2", "14", "VCC")],
            "/A": [("U2", "1", "1A")], "/B": [("U2", "2", "1B")]}
    if second.startswith("74HC244"):
        nets.setdefault(oe_net, []).append(("U2", "1", "~{1OE}"))
        nets["/A"] = [("U2", "2", "1A1")]
    return (board(nets, {"U1": ("Z84C0010", ""), "U2": (second, ""),
                                 "U9": ("AS6C1008", "")}))


def test_bus_strobe_with_a_push_pull_gate_fires():
    v = cp.check(strobe(), {})[0]
    assert kinds(v) == [("bus_driver_contention", ("U2",))]
    assert "the bus strobe /WR (WR)" in v[0]["msg"]
    assert "BUSACK" in v[0]["msg"]
    assert v[0]["drivers"] == ["U1", "U2"]


def test_bus_strobe_through_a_gated_buffer_is_quiet():
    b = strobe("74HC244", "1Y1", oe_net="/BUSAK")
    v, j = cp.check(b, {})
    assert v == []
    assert [x["verdict"] for x in j if x["standard"] == "bus_drivers"] == [
        "switched"]


def test_buffer_with_oe_grounded_on_a_strobe_fires():
    v = cp.check(strobe("74HC244", "1Y1", oe_net="GND"), {})[0]
    assert kinds(v) == [("bus_driver_contention", ("U2",))]
    assert "OE on GND" in v[0]["msg"]


def test_buffer_disabled_by_its_oe_rail_is_quiet():
    assert cp.check(strobe("74HC244", "1Y1", oe_net="+5V"), {})[0] == []


def test_open_drain_outputs_on_one_net_are_quiet():
    nets = {"/RST": [("U1", "2", "1Y"), ("U2", "2", "1Y"), ("R1", "1", "")],
            "+5V": [("R1", "2", "")]}
    b = (board(nets, {"U1": ("74AHC05", ""), "U2": ("74AHC05", "")}))
    v, j = cp.check(b, {})
    assert v == []
    assert [x["verdict"] for x in j if x["standard"] == "bus_drivers"] == [
        "wired_or"]


def test_one_parts_gates_tied_together_are_listed():
    nets = {"/X": [("U1", "3", "1Y"), ("U1", "6", "2Y")]}
    b = (board(nets, {"U1": ("74HC08", "")}))
    v, j = cp.check(b, {})
    assert v == []
    assert [x["verdict"] for x in j if x["standard"] == "bus_drivers"] == [
        "paralleled"]


def hub(smbusz=None, other="STM32F072CBT6", isolator=False):
    """A TUSB8041 on SDA/SCL with its EEPROM and one more IC."""
    nets = {"/SDA": [("U1", "38", "SDA/SMBDAT"), ("U2", "5", "SDA"),
                     ("R1", "1", "")],
            "/SCL": [("U1", "39", "SCL/SMBCLK"), ("U2", "6", "SCL"),
                     ("R2", "1", "")],
            "+3V3": [("R1", "2", ""), ("R2", "2", "")]}
    if smbusz:
        nets.setdefault(smbusz, []).append(("U1", "40", "SMBUSz"))
    comps = {"U1": ("TUSB8041RGC", ""), "U2": ("24LC64", "")}
    if other:
        nets["/SDA"].append(("U3", "43", "PB7/I2C1_SDA"))
        nets["/SCL"].append(("U3", "42", "PB6/I2C1_SCL"))
        comps["U3"] = (other, "")
    if isolator:
        nets["/SDA"].append(("U4", "4", "SDA"))
        nets["/SCL"].append(("U4", "5", "SCL"))
        comps["U4"] = ("PCA9541", "")
    return (board(nets, comps))


def test_single_master_hub_sharing_its_bus_fires():
    v = cp.check(hub(), {})[0]
    assert kinds(v) == [("i2c_single_master", ("U1",))] * 2
    assert "U3 (STM32F072CBT6)" in v[0]["msg"]
    assert "unconnected (internal pull-up)" in v[0]["msg"]


def test_single_master_hub_with_only_its_eeprom_is_quiet():
    v, j = cp.check(hub(other=None), {})
    assert v == []
    assert {x["verdict"] for x in j
            if x["standard"] == "i2c_single_master"} == {"own_bus"}


def test_hub_in_smbus_slave_mode_is_quiet():
    assert cp.check(hub(smbusz="GND"), {})[0] == []


def test_hub_mode_on_logic_is_listed():
    v, j = cp.check(hub(smbusz="/HUB_MODE"), {})
    assert v == []
    assert {x["verdict"] for x in j
            if x["standard"] == "i2c_single_master"} == {"switched"}


def test_hub_behind_an_arbiter_is_listed():
    v, j = cp.check(hub(isolator=True), {})
    assert v == []
    assert "isolated" in {x["verdict"] for x in j
                          if x["standard"] == "i2c_single_master"}
