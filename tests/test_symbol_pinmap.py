"""netlist_audit's symbol pin-map rules (miss class 5, #1086, #1114):
symbol_value_mismatch, symbol_pinmap, bus_order_flipped and mcu_i2c_role,
with scripts/lib/pinmaplib.py.

Each rule plants a fault (the errata board's rev A, or a regulator drawn
with another package's pin map) beside a quiet twin (the fix)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import netlist_audit  # noqa: E402
import pinmaplib  # noqa: E402

KINDS = ("symbol_value_mismatch", "symbol_pinmap", "bus_order_flipped",
         "mcu_i2c_role")


def _m(ref, pin, func="", ptype="passive"):
    return {"ref": ref, "pin": pin, "pintype": ptype, "pinfunction": func}


def _audit(comps, nets, stock="", libpart_pins=None, kinds=KINDS,
           part_pinouts=()):
    parsed = {"components": {r: (v if isinstance(v, dict) else
                                 {"value": v, "libsource": ("", "")})
                             for r, v in comps.items()},
              "nets": nets, "libpart_pins": libpart_pins or {}}
    viol, facts = netlist_audit.audit(parsed, {}, None, pin_requirements=[],
                                      pin_states=[], stock_symbols=stock,
                                      part_pinouts=list(part_pinouts))
    return [x for x in viol if x["kind"] in kinds], facts


# ------------------------------------------------ symbol_value_mismatch

def _digit(value, symbol="7Seg 0.2in", fp="Luis:7Seg SMD 0.8in Single",
           ref="U2"):
    """netscore-big's score digit: a 10-pin display on `symbol`."""
    comps = {ref: {"value": value, "footprint": fp,
                   "libsource": ("Luis", symbol)}}
    nets = {f"/SEG{i}": [_m(ref, str(i), f"SEG{i}")] for i in range(1, 11)}
    return comps, nets


def test_netscore_big_08in_digit_on_the_02in_symbol_is_the_planted_fault():
    hits, facts = _audit(*_digit("SMD 0.8in"))
    assert [(h["kind"], h["refs"], h["severity"]) for h in hits] == [
        ("symbol_value_mismatch", ["U2"], "warning")]
    assert "0.2in" in hits[0]["msg"] and "0.8in" in hits[0]["msg"]
    assert facts["symbol_sizes_checked"] == 1


def test_the_fix_a_symbol_of_the_placed_size_is_quiet():
    hits, facts = _audit(*_digit("SMD 0.8in", symbol="7Seg 0.8in"))
    assert hits == [] and facts["symbol_sizes_checked"] == 1


def test_a_value_with_no_size_falls_back_to_the_footprint():
    assert _audit(*_digit("GS-2011AB"))[0] != []
    assert _audit(*_digit("GS-2011AB", fp="Luis:7Seg 0.2in"))[0] == []


def test_a_connector_is_not_judged():
    hits, facts = _audit(*_digit("0.8in", symbol="Conn 0.2in", ref="J1"))
    assert hits == [] and facts["symbol_sizes_checked"] == 0


# ------------------------------------------------ symbol_pinmap

_SOT223 = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
_TO252 = "Package_TO_SOT_SMD:TO-252-2"


def _sym(name, fp, pins, extends=None):
    body = f'(extends "{extends}")' if extends else "".join(
        f'(pin power_in line (at 0 0 0) (length 2.54) (name "{n}") '
        f'(number "{num}"))' for num, n in pins.items())
    return (f'(kicad_symbol_lib (version 20251024) (symbol "{name}" '
            f'(property "Reference" "U") (property "Footprint" "{fp}") '
            f'(symbol "{name}_0_1" {body})))\n'
            if not extends else
            f'(kicad_symbol_lib (version 20251024) (symbol "{name}" {body} '
            f'(property "Footprint" "{fp}")))\n')


@pytest.fixture()
def stock(tmp_path):
    """A stock dir with an LDO whose SOT-223 symbol has GND 1, VO 2, VI 3,
    a derived symbol for the 5 V part, and a Device lib (generic)."""
    lib = tmp_path / "Regulator_Linear.kicad_symdir"
    lib.mkdir()
    (lib / "XYZ1117-33.kicad_sym").write_text(_sym(
        "XYZ1117-33", _SOT223, {"1": "GND", "2": "VO", "3": "VI"}))
    (lib / "XYZ1117-50.kicad_sym").write_text(_sym(
        "XYZ1117-50", _SOT223, {}, extends="XYZ1117-33"))
    dev = tmp_path / "Device.kicad_symdir"
    dev.mkdir()
    (dev / "R.kicad_sym").write_text(_sym("R", "", {"1": "~", "2": "~"}))
    pinmaplib.stock_index.cache_clear()
    return str(tmp_path)


def _ldo(pins, value="XYZ1117-33", fp=_SOT223, mpn=None):
    """U1, drawn with the board's own symbol MyLib:LDO whose pins are
    `pins` {number: name}."""
    comp = {"value": value, "footprint": fp, "libsource": ("MyLib", "LDO")}
    if mpn:
        comp["mpn"] = mpn
    net_of = {"GND": "GND", "VI": "+5V", "VO": "+3V3", "IN": "+5V",
              "OUT": "+3V3"}
    nets: dict = {}
    for num, name in pins.items():
        nets.setdefault(net_of.get(name, f"/N{num}"), []).append(
            _m("U1", num, name, "power_in"))
    return {"U1": comp}, nets, {("MyLib", "LDO"): dict(pins)}


def test_ldo_drawn_with_another_packages_pin_map_is_the_planted_fault(stock):
    # VI and VO swapped, as a TO-252 variant's symbol (IN 2, OUT 3) has
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"})
    hits, facts = _audit(comps, nets, stock, lp)
    assert [(h["kind"], h["refs"]) for h in hits] == [
        ("symbol_pinmap", ["U1"])]
    assert hits[0]["stock"] == "Regulator_Linear:XYZ1117-33"
    assert {p["name"]: (p["placed"], p["stock"]) for p in hits[0]["pins"]} \
        == {"IN": (["2"], ["3"]), "OUT": (["3"], ["2"])}
    assert facts["symbol_pinmaps_checked"] == 1


def test_ldo_with_the_stock_pin_map_is_quiet(stock):
    comps, nets, lp = _ldo({"1": "GND", "2": "VO", "3": "VI", "4": "VO"})
    hits, facts = _audit(comps, nets, stock, lp)
    assert hits == [] and facts["symbol_pinmaps_checked"] == 1


def test_the_other_package_variant_is_not_judged(stock):
    # the same IN 2 / OUT 3 map on a TO-252: right for that variant, and
    # the stock symbol is SOT-223 only, so nothing to compare it with
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"}, fp=_TO252)
    hits, facts = _audit(comps, nets, stock, lp)
    assert hits == [] and facts["symbol_pinmaps_checked"] == 0


def test_a_derived_stock_symbol_takes_its_parents_pins(stock):
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"},
                           value="XYZ1117-50")
    assert [h["stock"] for h in _audit(comps, nets, stock, lp)[0]] == [
        "Regulator_Linear:XYZ1117-50"]


def test_the_mpn_names_the_part_when_the_value_does_not(stock):
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"},
                           value="3V3 LDO", mpn="XYZ1117-33")
    assert len(_audit(comps, nets, stock, lp)[0]) == 1


def test_a_value_naming_no_stock_part_or_no_stock_dir_is_not_judged(stock):
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"},
                           value="MYREG")
    assert _audit(comps, nets, stock, lp)[1]["symbol_pinmaps_checked"] == 0
    comps, nets, lp = _ldo({"1": "GND", "2": "VI", "3": "VO"})
    assert _audit(comps, nets, "", lp)[1]["symbol_pinmaps_checked"] == 0


def test_package_family_reads_the_package_token():
    assert pinmaplib.package_family(_SOT223) == "SOT2233"
    assert pinmaplib.package_family(_TO252) == "TO2522"
    assert pinmaplib.same_package("SOT-223", _SOT223)
    assert not pinmaplib.same_package(_TO252, _SOT223)
    assert pinmaplib.same_package("MyLib:Custom", _SOT223)



# ------------------------------------------------ symbol_pinmap: package
# numbering (a board's PLCC-44 symbol against a stock symbol of the same
# name numbered for the DIP-40: every pin "swapped" by the shift)

_DIP40 = "Package_DIP:DIP-40_W15.24mm"
_PLCC44 = "Package_LCC:PLCC-44_SMD-Socket"
_CPU_DIP = {"30": "A0", "31": "A1", "32": "A2", "33": "A3", "14": "D0",
            "21": "~{RD}", "22": "~{WR}", "29": "GND"}
# the same die in the PLCC-44: each pin a few pads on from the DIP's
_CPU_PLCC = {"34": "A0", "35": "A1", "36": "A2", "37": "A3", "16": "D0",
             "24": "~{RD}", "25": "~{WR}", "33": "GND"}


def _sym_f(name, fp, pins, filters):
    body = "".join(
        f'(pin bidirectional line (at 0 0 0) (length 2.54) (name "{n}") '
        f'(number "{num}"))' for num, n in pins.items())
    return (f'(kicad_symbol_lib (version 20251024) (symbol "{name}" '
            f'(property "Reference" "U") (property "Footprint" "{fp}") '
            f'(property "ki_fp_filters" "{filters}") '
            f'(symbol "{name}_0_1" {body})))\n')


@pytest.fixture()
def cpus(tmp_path):
    """A CPU lib: XZ80P, DIP-40 by default; XZ80A, no default footprint
    and filters for the DIP-40 and an SSOP; XZ80D, no default footprint
    and only the DIP's filter. All three numbered for the DIP."""
    lib = tmp_path / "CPU.kicad_symdir"
    lib.mkdir()
    (lib / "XZ80P.kicad_sym").write_text(_sym_f(
        "XZ80P", _DIP40, _CPU_DIP, "DIP*W15.24*"))
    (lib / "XZ80A.kicad_sym").write_text(_sym_f(
        "XZ80A", "", _CPU_DIP, "DIP*W15.24* SSOP*8.8x17.5mm?P0.8mm*"))
    (lib / "XZ80D.kicad_sym").write_text(_sym_f(
        "XZ80D", "", _CPU_DIP, "DIP*W15.24*"))
    pinmaplib.stock_index.cache_clear()
    pinmaplib.stock_symbol.cache_clear()
    return str(tmp_path)


def _cpu(pins, value, fp):
    comp = {"value": value, "footprint": fp, "libsource": ("MyLib", "CPU")}
    nets: dict = {}
    for num, name in pins.items():
        nets.setdefault(f"/{name}", []).append(_m("U1", num, name, "bidi"))
    return {"U1": comp}, nets, {("MyLib", "CPU"): dict(pins)}


@pytest.mark.parametrize("value", ["XZ80P", "XZ80A", "XZ80D"])
def test_a_plcc_part_against_a_dip_numbered_stock_symbol_is_quiet(cpus,
                                                                  value):
    # the stock symbol is the DIP's (by its footprint, else its filters):
    # the PLCC's own numbering is not compared with it
    comps, nets, lp = _cpu(_CPU_PLCC, value, _PLCC44)
    hits, facts = _audit(comps, nets, cpus, lp)
    assert hits == [] and facts["symbol_pinmaps_checked"] == 0


def test_a_custom_footprint_naming_the_plcc_is_quiet(cpus):
    comps, nets, lp = _cpu(_CPU_PLCC, "XZ80A", "MyLib:Z80_PLCC_44")
    assert _audit(comps, nets, cpus, lp)[0] == []


def test_a_footprint_naming_no_package_among_several_is_not_judged(cpus):
    # XZ80A fits the DIP-40 and an SSOP: which numbering holds for a
    # footprint called 'Z80' can't be told
    comps, nets, lp = _cpu(_CPU_PLCC, "XZ80A", "MyLib:Z80")
    hits, facts = _audit(comps, nets, cpus, lp)
    assert hits == [] and facts["symbol_pinmaps_checked"] == 0


@pytest.mark.parametrize("value", ["XZ80P", "XZ80A", "XZ80D"])
def test_a_real_swap_within_the_dip_is_the_planted_fault(cpus, value):
    # ~RD and ~WR swapped on the DIP-40 itself
    pins = dict(_CPU_DIP)
    pins["21"], pins["22"] = pins["22"], pins["21"]
    comps, nets, lp = _cpu(pins, value, _DIP40)
    hits, facts = _audit(comps, nets, cpus, lp)
    assert [(h["kind"], h["stock"]) for h in hits] == [
        ("symbol_pinmap", f"CPU:{value}")]
    assert facts["symbol_pinmaps_checked"] == 1


def test_the_dip_drawn_as_its_stock_symbol_is_quiet(cpus):
    comps, nets, lp = _cpu(_CPU_DIP, "XZ80A", _DIP40)
    hits, facts = _audit(comps, nets, cpus, lp)
    assert hits == [] and facts["symbol_pinmaps_checked"] == 1


def test_stock_package_match_reads_footprint_then_filters():
    dip = {"footprint": _DIP40, "filters": ["DIP*W15.24*"]}
    two = {"footprint": "", "filters": ["DIP*W15.24*", "SSOP*8.8x17.5mm*"]}
    assert pinmaplib.stock_package_match(_DIP40, dip) is True
    assert pinmaplib.stock_package_match(_PLCC44, dip) is False
    assert pinmaplib.stock_package_match(_PLCC44, two) is False
    assert pinmaplib.stock_package_match(_DIP40, two) is True
    assert pinmaplib.stock_package_match("MyLib:Z80", two) is None
    assert pinmaplib.stock_package_match(
        "MyLib:Z80", {"footprint": "", "filters": ["DIP*"]}) is True
    assert pinmaplib.stock_package_match(_PLCC44, {"footprint": ""}) is True
    assert pinmaplib.package_family("MyLib:Z80_PLCC_44") == "PLCC44"
    assert pinmaplib.package_family("MyLib:Custom") is None

# ------------------------------------------------ symbol_pinmap: pin tables
# (#1135) a part with no stock symbol in its package is judged against a
# reference/part_pinouts.yaml row read from its datasheet's pin table

_DRV = {"1": "INLA", "2": "INLB", "3": "INLC", "4": "GVDD", "5": "MODE",
        "6": "GND", "9": "GLC", "10": "GLB", "11": "GLA", "21": "DT",
        "22": "INHA", "23": "INHB", "24": "INHC"}
_DRV_FP = "Package_DFN_QFN:VQFN-24-1EP_4x4mm_P0.5mm_EP2.45x2.45mm"


def _drv_row(**kw):
    row = {"part": "DRV8300 (24-pin VQFN, RGE)", "match": "DRV8300",
           "footprint": "QFN-24|RGE", "pins": dict(_DRV), "verified": True,
           "source": "TI DRV8300 datasheet, Table 6-1"}
    row.update(kw)
    return row


def _drv(pins, value="DRV8300DRGER", fp=_DRV_FP):
    """U3, a DRV8300 on the board's own symbol MyLib:DRV whose pins are
    `pins` {number: name}."""
    comp = {"value": value, "footprint": fp, "libsource": ("MyLib", "DRV")}
    nets = {f"/N{num}": [_m("U3", num, name, "input")]
            for num, name in pins.items()}
    return {"U3": comp}, nets, {("MyLib", "DRV"): dict(pins)}


def _swapped(pins, a, b):
    out = dict(pins)
    na = next(k for k, v in pins.items() if v == a)
    nb = next(k for k, v in pins.items() if v == b)
    out[na], out[nb] = b, a
    return out


def test_driver_drawn_with_inha_and_inla_swapped_is_the_planted_fault(stock):
    comps, nets, lp = _drv(_swapped(_DRV, "INHA", "INLA"))
    hits, facts = _audit(comps, nets, stock, lp, part_pinouts=[_drv_row()])
    assert [(h["kind"], h["refs"]) for h in hits] == [
        ("symbol_pinmap", ["U3"])]
    assert hits[0]["pinout"] == "DRV8300 (24-pin VQFN, RGE)"
    assert "the datasheet" in hits[0]["msg"] and "Table 6-1" in hits[0]["msg"]
    assert {p["name"]: (p["placed"], p["stock"]) for p in hits[0]["pins"]} \
        == {"INHA": (["1"], ["22"]), "INLA": (["22"], ["1"])}
    assert facts["symbol_pinmaps_checked"] == 1
    assert facts["symbol_pinmaps_by_table"] == 1


def test_driver_drawn_as_its_pin_table_is_quiet(stock):
    # the placed symbol also numbers the NC pins and the pad: only names
    # both maps carry are compared
    comps, nets, lp = _drv({**_DRV, "7": "NC", "8": "NC", "25": "PAD"})
    hits, facts = _audit(comps, nets, stock, lp, part_pinouts=[_drv_row()])
    assert hits == [] and facts["symbol_pinmaps_by_table"] == 1


def test_a_disagreeing_unverified_row_is_not_judged(stock):
    comps, nets, lp = _drv(_swapped(_DRV, "INHA", "INLA"))
    hits, facts = _audit(comps, nets, stock, lp,
                         part_pinouts=[_drv_row(verified=False)])
    assert hits == [] and facts["unverified_findings"] == 1
    assert facts["not_judged"][0].startswith("symbol_pinmap (warning) on U3")


def test_another_package_or_no_footprint_gets_no_row(stock):
    bad = _swapped(_DRV, "INHA", "INLA")
    for fp in ("Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm", ""):
        comps, nets, lp = _drv(bad, fp=fp)
        hits, facts = _audit(comps, nets, stock, lp,
                             part_pinouts=[_drv_row()])
        assert hits == [] and facts["symbol_pinmaps_checked"] == 0


def test_a_value_the_row_does_not_match_is_not_judged(stock):
    comps, nets, lp = _drv(_swapped(_DRV, "INHA", "INLA"), value="DRV8301")
    hits, facts = _audit(comps, nets, stock, lp,
                         part_pinouts=[_drv_row(match="DRV8300$")])
    assert hits == [] and facts["symbol_pinmaps_checked"] == 0


def test_fewer_than_three_shared_names_is_not_judged(stock):
    # a symbol named its own way (IN_HI_A ...) shares only GND and DT
    pins = {"6": "GND", "21": "DT", "1": "IN_LO_A", "22": "IN_HI_A",
            "4": "VDRV"}
    comps, nets, lp = _drv(pins)
    hits, facts = _audit(comps, nets, stock, lp, part_pinouts=[_drv_row()])
    assert hits == [] and facts["symbol_pinmaps_checked"] == 0


def test_a_row_naming_a_stock_symbol_takes_its_pins(stock):
    # a module with a stock symbol under another name (the Pico row)
    lib = Path(stock) / "MCU_Module.kicad_symdir"
    lib.mkdir()
    (lib / "OtherModule.kicad_sym").write_text(_sym(
        "OtherModule", "Module:OtherModule",
        {"1": "GP0", "2": "GP1", "3": "GND", "4": "GP2", "5": "GP3"}))
    pinmaplib.stock_index.cache_clear()
    row = {"part": "Test module", "match": "TESTMOD", "footprint": "TestMod",
           "stock": "MCU_Module:OtherModule", "verified": True}
    good = {"1": "GP0", "2": "GP1", "3": "GND", "4": "GP2", "5": "GP3"}
    comp = {"U9": {"value": "TESTMOD", "footprint": "MyLib:TestMod",
                   "libsource": ("MyLib", "TM")}}
    for pins, n in ((good, 0), (_swapped(good, "GP0", "GP3"), 1)):
        hits, facts = _audit(comp, {}, stock, {("MyLib", "TM"): pins},
                             part_pinouts=[row])
        assert len(hits) == n and facts["symbol_pinmaps_by_table"] == 1


def test_a_part_with_a_same_package_stock_symbol_ignores_the_table(stock):
    # the stock symbol agrees: a disagreeing row is never read
    comps, nets, lp = _ldo({"1": "GND", "2": "VO", "3": "VI"})
    row = {"part": "wrong", "match": "XYZ1117", "footprint": "SOT-223",
           "pins": {"1": "VI", "2": "VO", "3": "GND"}, "verified": True}
    hits, facts = _audit(comps, nets, stock, lp, part_pinouts=[row])
    assert hits == [] and facts["symbol_pinmaps_by_table"] == 0


def test_pin_names_fold_unicode_minus_and_dashes():
    assert pinmaplib.pin_names("\u2212IN") == pinmaplib.pin_names("-IN")
    assert pinmaplib.pin_names("V\u2013") == pinmaplib.pin_names("V-")


def test_pinout_row_matches_value_and_footprint():
    rows = [_drv_row(match="DRV8300D?$", footprint="TSSOP"), _drv_row()]
    assert pinmaplib.pinout_row(rows, ["DRV8300DRGER"], _DRV_FP) is rows[1]
    assert pinmaplib.pinout_row(rows, ["DRV8300"], "X:TSSOP-20") is rows[0]
    assert pinmaplib.pinout_row(rows, ["DRV8300"], None) is None
    assert pinmaplib.pinout_row([{"match": "(", "footprint": "QFN"}],
                                ["DRV8300"], _DRV_FP) is None


def test_every_repo_pinout_row_loads_and_is_well_formed():
    rows = netlist_audit.load_part_pinouts()
    assert len(rows) >= 10
    for r in rows:
        assert r["part"] and r["match"] and r["footprint"], r
        assert bool(r.get("pins")) != bool(r.get("stock")), r["part"]
        assert r.get("pins") is None or len(r["pins"]) >= 3
        assert r.get("verified") is True, r["part"]
        assert r.get("stock") or r.get("cite"), r["part"]


# ------------------------------------------------ bus_order_flipped

def _counter(unit1_divs):
    """gcm's SN74LV393A U26: 1QA..1QD on CLK_50d<unit1_divs>MHZ, 2QA..2QD
    on d32..d256."""
    comps = {"U26": {"value": "SN74LV393A", "footprint": "SOIC-14",
                     "libsource": ("GCM", "SN74LV393A")}}
    nets = {}
    for (pin, q), d in zip([("3", "1QA"), ("4", "1QB"), ("5", "1QC"),
                            ("6", "1QD")], unit1_divs):
        nets[f"/ClockGenerator/CLK_50d{d}MHZ"] = [_m("U26", pin, q, "output")]
    for (pin, q), d in zip([("11", "2QA"), ("10", "2QB"), ("9", "2QC"),
                            ("8", "2QD")], (32, 64, 128, 256)):
        nets[f"/ClockGenerator/CLK_50d{d}MHZ"] = [_m("U26", pin, q, "output")]
    return comps, nets


def test_gcm_v50_one_counter_unit_wired_in_reverse_is_the_planted_fault():
    hits, facts = _audit(*_counter((16, 8, 4, 2)))
    assert [(h["kind"], h["refs"]) for h in hits] == [
        ("bus_order_flipped", ["U26"])]
    assert "(down)" in hits[0]["msg"] and "(up)" in hits[0]["msg"]
    assert facts["channel_parts_checked"] == 1


def test_gcm_v51_both_units_counting_up_is_quiet():
    hits, facts = _audit(*_counter((2, 4, 8, 16)))
    assert hits == [] and facts["channel_parts_checked"] == 1


def test_channel_nets_with_no_common_count_are_not_judged():
    comps, nets = _counter((2, 4, 8, 16))
    nets = {k.replace("CLK_50d", "TAP_").replace("16MHZ", "X")
            .replace("8MHZ", "Y"): v for k, v in nets.items()}
    assert _audit(comps, nets)[0] == []


# ------------------------------------------------ mcu_i2c_role

def _pico(sda_gpio, scl_gpio, names=("GPIO{}",)):
    """pico-if1's U301 (a Pico on the board's own symbol) with the OLED
    module's SDA on GPIO `sda_gpio` and SCK on GPIO `scl_gpio`."""
    comps = {"U301": {"value": "Pico", "footprint": "MCU_RaspberryPi:Pico",
                      "libsource": ("pico", "Pico")},
             "J5": {"value": "OLED", "footprint": "PinHeader_1x04",
                    "libsource": ("Connector", "Conn_01x04")}}
    pin = {4: "6", 5: "7", 6: "9", 7: "10"}
    nets = {"OLED_SDA": [_m("U301", pin[sda_gpio],
                            names[0].format(sda_gpio), "bidirectional"),
                         _m("J5", "4")],
            "OLED_SCK": [_m("U301", pin[scl_gpio],
                            names[0].format(scl_gpio), "bidirectional"),
                         _m("J5", "3")],
            "GND": [_m("U301", "3", "GND", "power_in"), _m("J5", "1")]}
    return comps, nets


def test_pico_if1_v13_oled_sda_and_sck_crossed_is_the_planted_fault():
    # GPIO4 is I2C0 SDA, GPIO5 I2C0 SCL: v1.3 has SCK on 4, SDA on 5
    hits, facts = _audit(*_pico(sda_gpio=5, scl_gpio=4))
    assert [(h["kind"], h["refs"], h["block"]) for h in hits] == [
        ("mcu_i2c_role", ["U301"], "I2C0")]
    assert "pico-sdk" in hits[0]["msg"]
    assert facts["mcu_i2c_parts_checked"] == 1


def test_pico_if1_v14_oled_on_the_right_pins_is_quiet():
    hits, facts = _audit(*_pico(sda_gpio=4, scl_gpio=5))
    assert hits == [] and facts["mcu_i2c_parts_checked"] == 1


def test_the_pico_symbols_short_gp_names_are_read():
    assert len(_audit(*_pico(5, 4, names=("GP{}",)))[0]) == 1


def test_two_blocks_lines_on_one_bus_are_not_a_crossing():
    # SDA on GPIO6 (I2C1 SDA), SCK on GPIO5 (I2C0 SCL): no one block is
    # crossed (a bit-banged bus, not this rule's)
    assert _audit(*_pico(sda_gpio=6, scl_gpio=5))[0] == []


def test_mcu_i2c_function_maps_pin_names():
    t = {"GPIO4": "I2C0_SDA", "GPIO26": "I2C1_SDA"}
    assert pinmaplib.mcu_i2c_function("GP4", t) == "I2C0_SDA"
    assert pinmaplib.mcu_i2c_function("GPIO26_ADC0", t) == "I2C1_SDA"
    assert pinmaplib.mcu_i2c_function("GPIO40", t) is None


# ------------------------------------------------ #1114: SCL_24 names

def _expanders(scl, sda):
    """Two PCA6416As both strapped low on one bus, their bus pins named
    `scl` and `sda` (SCL_22-style, as some libraries number them)."""
    comps = {"U24": "PCA6416APW", "U25": "PCA6416APW"}
    nets = {"/SCL": [_m(u, "22", scl, "input") for u in ("U24", "U25")],
            "/SDA": [_m(u, "23", sda, "bidirectional")
                     for u in ("U24", "U25")],
            "+3V3": [_m(u, "2", "VDD_I2C", "power_in")
                     for u in ("U24", "U25")],
            "GND": [_m(u, "21", "ADDR", "input") for u in ("U24", "U25")]}
    return comps, nets


@pytest.mark.parametrize("scl,sda", [("SCL", "SDA"), ("SCL_22", "SDA_23")])
def test_numbered_bus_pin_names_still_collide(scl, sda):
    hits, _ = _audit(*_expanders(scl, sda), kinds=("i2c_address_collision",))
    assert [(h["refs"], h["address"]) for h in hits] == [
        (["U24", "U25"], "0x20")]


def test_strip_bus_number_leaves_other_names():
    s = netlist_audit._strip_bus_number
    assert s("SCL_24") == "SCL" and s("SDA_23") == "SDA"
    assert s("GPIO_24") == "GPIO_24" and s("A_1") == "A_1"
