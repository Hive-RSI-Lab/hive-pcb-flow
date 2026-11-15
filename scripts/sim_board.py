#!/usr/bin/env python
"""sim_board.py - ngspice on a board's own netlist, plus the hand calcs.

sim_run.py gates testbenches an agent wrote; this script needs none. It
reads the board's exported netlist, builds the whole board's passive and
discrete network in SPICE by itself, runs it in the KiCad-bundled ngspice
under a few drive scenarios, and turns the numbers into findings a
reviewer would otherwise work out by hand:

  scenarios (DC operating points, rails as ideal sources, ICs open):
    default   every IC pin floats: only the passives and rails set nodes
    io_low    every IC I/O net (a GPIO-like or status pin name, or an
              SoC pad named by its function: ENET2_TX_EN, UART4_TX_DATA)
              driven to 0 V through 25 ohm; BJTs and MOSFETs with generic
              models follow on their own. Not driven: a net named as
              analog (ADC_x, x_SNS, x_DIV), and pins constraints
              `pin_modes` / --pin-mode mark analog, input or nc
    io_high   every IC I/O net driven to its IC's I/O supply: the
              highest rail on its power pins, or the pin
              reference/io_supply.yaml, constraints `io_supply` or
              --io-supply names (a USB-PD sink fed from VBUS whose GPIOs
              run from its own 3.3 V VDDD)
    io_run    io_high, but status lines (PG, FAULT, INT...) asserted low:
              the powered-up state load switches and enables see
  plus one transient: rails ramp 0 -> V over --ramp-ms with every R, C, L
  and diode in place, for the enable/reset RC nets.

Findings (violation kind; severity):
  resistor_overpower      R dissipation above its rating (error in the
                          default scenario, warning in io_* ones); above
                          half its rating is a derating warning. The
                          rating is P70 derated linearly to 0 at 155 C
                          when constraints give an ambient_c above 70 C
  resistor_overvoltage    the volts across an R above its limiting element
                          voltage (parts.json Voltage-Supply(Max), else the
                          chip size's D/CRCW e3 Umax): an 0805 2 MOhm on a
                          400 V bus dissipates 0.08 W but is over 150 V
  cap_overvoltage         DC across a cap above its rated voltage (error);
  cap_dc_bias             class-II ceramic losing its capacitance to DC
                          bias: warning when its rail's stated floor
                          (constraints power[].min_cout or a regulator's
                          cited effective pin_requirements row, as
                          mlcc_dcbias) is missed on the low curve and this
                          part keeps less than the share it needs; info
                          above half its rating or under half its nominal
                          otherwise (an uncited screen), with a rail that
                          states no floor listed as not judged
  fb_divider_mismatch     an adjustable regulator's FB divider sets a Vout
                          more than 3 % (warning) / 5 % (error) off the
                          output rail's declared voltage; a constraints
                          voltage is the rail's worst case, so one from
                          the divider's lowest output to 10 % over its
                          nominal fits (check_ratings.worst_case_fits)
  led_overcurrent         LED current above 20 mA, or the part's own
                          figure, in some scenario (warning)
  led_dark                an LED that never reaches 0.5 mA (warning)
  gpio_overcurrent        an IC I/O net sourcing/sinking > 20 mA when driven
  input_above_supply      an IC pin's net above that IC's supply + 0.3 V,
                          or for a pin rated above it (reference/
                          pin_ratings.yaml, a parts.json common-mode
                          figure) above that rating; a switch-node or
                          bootstrap pin (HB, HS, BST, SW) is listed
  enable_never_high       an EN/RESET RC net, set by passives from a rail,
                          that never reaches 0.7 VDD (error);
                          enable_before_supply: crosses before its supply
                          reaches 90 % (warning). One an IC pin drives is
                          listed, not judged.
  i2c_pullup_*            missing (warning), too strong for 3 mA sink
                          (error), too weak for the bus speed (warning).
                          A line is a net named SDA/SCL or on an SDA/SCL
                          pin, except a pin pin_modes marks input or nc
                          (a strap sharing the name)
  mosfet_vgs              |Vgs| over the part's rating (error), or over
                          12 V with no rating known (warning)
  mosfet_underdrive       the FET's on-state |Vgs| (the scenario that
                          drives it hardest, at nominal rails and again at
                          each rail's declared minimum) below its Vgs(th)
                          max (error), or below the lowest Vgs its RDS(on)
                          is specified at (warning): a switch that never
                          turns on, or runs hot, at the low end of a rail

Facts carry every number: node voltages per scenario, rail currents, LED
currents, FB-set voltages with their tolerance spread, RC corners, EN
delays, crystal loads. `known_unknowns` lists what the tool could not
decide (parts it did not model, a regulator Vref it does not know or has
not verified, a MOSFET with no Vgs rating, assumed I2C bus capacitance) -
the review takes those to a datasheet. An engine failure (ngspice errors,
times out, or leaves no vectors) is no crash: the passes that need no
operating point still run, and `not_judged` says what was left out; a
board with nothing to simulate says so in `nothing_to_judge`. A crystal
with a 0 F load cap gets no load figure and a `not_judged` line. This is a
screen with ideal ICs, not a sign-off: digital pin drive, regulator
dynamics and switching are not in it (docs/playbook.md "Sim legs").

Rail ranges: a constraints.json `voltages` entry may carry `min` (volts),
the lowest the rail runs at - a USB-PD VBUS at its lowest allowed
contract, a battery at cut-off. `--rail-min NET=V` adds one. Each such
rail's minimum gets the four DC scenarios again ("<scenario>@min", every
other rail nominal); mosfet_underdrive judges those too.

Inputs: --net kicad/<board>.net, or --workspace <ws> (finds kicad/*.net,
kicad/constraints.json, architecture/power_tree.md, parts/parts.json).
Rail voltages: constraints.json `voltages`, the power tree's rail table, a
fixed-output regulator's OUT pin (LP5907-3.3), then the net name - the same
ladder check_ratings uses, except that a sheet-local net only passives
touch (/charger/CHG_VBUS_RC, a snubber's mid node) is no rail by its name.
`--rail NET=V` adds or overrides one.

CLI:
  sim_board.py --workspace boards/<b> [--out reports/sim_board.json]
  sim_board.py --net x.net [--constraints c.json] [--parts parts/]
               [--rail VBUS=20] [--rail-min VBUS=9] [--vref U2=0.596]
               [--pin-mode U1.PB11=analog] [--io-supply U3=VDDD]
               [--i2c-khz 400]
               [--i2c-cb-pf 50] [--ramp-ms 1] [--timeout 120]
Exit 0 pass / 1 violations / 2 error.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

import yaml  # noqa: E402

import check_ratings  # noqa: E402
import checklib  # noqa: E402
import engcalc  # noqa: E402
import mlcc  # noqa: E402
import simlib  # noqa: E402
import tolerance  # noqa: E402
from lib import env  # noqa: E402

SCRIPT = "sim_board"
SOURCE = "sim_board"
VREF_YAML = SCRIPTS.parent / "reference" / "regulator_vref.yaml"
IO_SUPPLY_YAML = SCRIPTS.parent / "reference" / "io_supply.yaml"
GPIO_DRIVE_YAML = SCRIPTS.parent / "reference" / "gpio_drive.yaml"

GPIO_FN = re.compile(
    r"^(GPIO\d*|IO\d+|P[A-K]?\d+|PIO\d+|D\d+|A\d+|RXD?\d?|TXD?\d?|U\d?[RT]XD?"
    r"|SDA|SCL|SCK|SCLK|MOSI|MISO|CS|SS|INT\w*|IRQ"
    r"|PG|PGOOD|FAULT\w*|DTR|RTS|CTS|DSR"
    r"|CDATA|CCLK|SENSOR_V[PN]|ADC\d*)$")
# SoC pads named by their default function (i.MX ENET2_TX_EN, UART4_TX_DATA,
# GPIO1_IO03; Rockchip/Allwinner the same way): muxable, so I/O. DDR, USB,
# clock, crystal and boot/reset pads are not.
SOC_PAD_FN = re.compile(
    r"^(GPIO\d*_IO\d+|GPIO\d+_[A-D]\d+(_\w+)?"
    r"|(ENET|RGMII|RMII|UART|USART|LPUART|LCD|CSI|SD|USDHC|NAND|ECSPI|SPI"
    r"|QSPI|I2C|SAI|I2S|PWM|KPP|JTAG|GPMI|EIM|SNVS_TAMPER|SPDIF|MQS|ESAI)"
    r"\d*_[A-Z0-9_]+)$")
SOC_NOT_IO = re.compile(r"^(DRAM|DDR|USB|CCM|XTAL|RTC_X|POR|ONOFF|BOOT_MODE"
                        r"|TEST_MODE|JTAG_MOD|PMIC|SNVS_PMIC|VDD|NVCC|GND"
                        r"|ENET\d?_REF_CLK|\w*_(CLK[PN]|[DP][PN]|REXT|VBUS))")
# A net named as an analog signal (ADC_1V35, VOUT_SNS, ISENSE_A) on a
# GPIO-named pin: the pin is an ADC input, not driven.
ANALOG_NET = {"ADC", "AIN", "SNS", "SENSE", "VSENSE", "ISENSE", "MON", "MEAS",
              "DIV", "VMON", "IMON"}
# constraints `pin_modes` / --pin-mode values
PIN_MODES = {"io": "io", "gpio": "io", "output": "io", "out": "io",
             "bidir": "io", "od": "od", "open_drain": "od", "opendrain": "od",
             "analog": "off", "adc": "off", "input": "off", "in": "off",
             "nc": "off", "unused": "off", "off": "off"}
POWER_FN = re.compile(
    r"^(V(DD|CC|IN|BAT|BUS|HV|S)\w*|[ADP]V(DD|CC)\w*|AVCC|PVCC|HPVDD|3V3|5V"
    r"|V3|IN|VDDIO\w*)$")
ENABLE_FN = re.compile(r"^N?(EN|CHIP_PU|CHIP_EN|RST|RESET|PWRKEY|XRES|MCLR"
                       r"|SHDN|SDZ)$")
FB_FN = re.compile(r"^(V?FB\d?|ADJ|VSENSE|VOS)$")
I2C_RE = re.compile(r"(^|[^A-Z])(SDA|SCL)([^A-Z]|$)")
GPIO_MAX_A = 0.020
LED_MAX_A = 0.020
LED_DARK_A = 0.5e-3
VGS_UNKNOWN_V = 12.0
# below this on-state |Vgs| a FET is taken as never driven on in the sim
# (its gate left to an IC pin the sim holds open), not as under-driven
VGS_ON_FLOOR_V = 0.5
# a gate driven at the very Vgs RDS(on) is specified at (a 2.5 V rail on a
# part rated @2.5V) is at spec: the sim's few-mV drops don't make it under
VGS_SPEC_TOL_V = 0.05


# ------------------------------------------------------------------ board

def norm_fn(fn: str, pin: str) -> list[str]:
    """EasyEDA-style 'EN_3' on pin 3 -> ['EN']; '~{RESET}' -> ['RESET'];
    'CFG2/SCL_2' -> ['CFG2', 'SCL']. Upper-case alternatives."""
    s = (fn or "").strip()
    if s.endswith(f"_{pin}"):
        s = s[: -len(pin) - 1]
    s = s.replace("~{", "").replace("}", "")
    return [a.strip().upper() for a in s.split("/") if a.strip()]


def load_board(net_path: Path) -> dict:
    parsed = simlib.parse_netlist(net_path, fields=True)
    comps = {r: {"ref": r, "value": c.get("value") or "",
                 "footprint": c.get("footprint") or "", "pins": {},
                 "mpn": next((c["fields"][k] for k in check_ratings.MPN_PROPS
                              if (c.get("fields") or {}).get(k)), "")}
             for r, c in parsed["components"].items()}
    for net, nodes in parsed["nets"].items():
        if net.startswith("unconnected-"):
            continue
        for nd in nodes:
            c = comps.get(nd["ref"])
            if c is None:
                continue
            c["pins"][nd["pin"]] = {"net": net,
                                    "fn": norm_fn(nd.get("pinfunction", ""),
                                                  nd["pin"])}
    for c in comps.values():
        m = re.match(r"[A-Za-z]+", c["ref"])
        c["kind"] = m.group(0).upper() if m else ""
    return {"comps": comps, "nets": sorted({p["net"] for c in comps.values()
                                            for p in c["pins"].values()})}


def _mpn_key(mpn: str) -> str:
    return re.sub(r"[\s\-_/.]", "", mpn or "").upper()


def load_parts(parts_dir: Path | None, board: dict | None = None,
               unmatched: list | None = None) -> dict[str, dict]:
    """parts.json rows by refdes, attributes flattened to {name: value}.
    A row with no `refdes` falls back to its `mpn`: given the board, every
    comp whose netlist MPN field is the same part (case, spaces, dashes,
    slashes and dots aside) gets the row, unless a refdes row already names that comp
    (#1260). Such a row that matches no comp goes on `unmatched` as
    {mpn, why}."""
    out: dict[str, dict] = {}
    pj = parts_dir / "parts.json" if parts_dir else None
    if not pj or not pj.is_file():
        return out
    by_mpn: list[tuple[str, dict]] = []
    for p in checklib.load_json(pj, "parts.json").get("parts") or []:
        attrs = {a.get("name", ""): str(a.get("value", ""))
                 for a in p.get("attributes") or []}
        row = {"mpn": p.get("mpn") or "", "description":
               p.get("description") or "", "attrs": attrs}
        for r in p.get("refdes") or []:
            out[r] = row
        if not p.get("refdes") and _mpn_key(row["mpn"]):
            by_mpn.append((_mpn_key(row["mpn"]), row))
    if board is None:
        return out
    comps: dict[str, list[str]] = {}
    for ref, c in sorted(board["comps"].items()):
        if c.get("mpn"):
            comps.setdefault(_mpn_key(c["mpn"]), []).append(ref)
    for key, row in by_mpn:
        refs = [r for r in comps.get(key, []) if r not in out]
        for r in refs:
            out[r] = {**row, "matched_by": "mpn"}
        if not comps.get(key) and unmatched is not None:
            unmatched.append({"mpn": row["mpn"], "why": "parts.json row with "
                              "no refdes and no netlist comp with that MPN"})
    return out


def attr(part: dict | None, *patterns: str) -> str | None:
    """The first attribute whose name matches one of the regexes."""
    for pat in patterns:
        for k, v in ((part or {}).get("attrs") or {}).items():
            if re.search(pat, k, re.I):
                return v
    return None


def resolve_rails(board: dict, parts: dict, constraints: dict | None,
                  power_tree: Path | None, overrides: dict[str, float]
                  ) -> dict[str, tuple[float, str]]:
    """{net: (volts, source)} - the check_ratings ladder plus --rail."""
    nets = set(board["nets"])
    out: dict[str, tuple[float, str]] = {}

    def setv(net, v, src):
        if net in nets and net not in out and v is not None:
            out[net] = (float(v), src)

    for n, v in overrides.items():
        if n not in nets:
            raise checklib.CheckError(f"--rail {n}: no such net")
        setv(n, v, "--rail")
    for e in (constraints or {}).get("voltages") or []:
        setv(e.get("net"), e.get("voltage"), "constraints")
    for net, v in check_ratings.power_tree_volts(power_tree, nets).items():
        setv(net, v, "power_tree")
    for c in board["comps"].values():
        mpn = (parts.get(c["ref"]) or {}).get("mpn") or c["value"]
        v = check_ratings.reg_out_volts(mpn) if c["kind"] == "U" else None
        if v is None:
            continue
        for p in c["pins"].values():
            if set(p["fn"]) & {"OUT", "VOUT", "VO"}:
                setv(p["net"], v, f"regulator {c['ref']}")
    passive_only = _passive_only_nets(board)
    for net in nets:
        v = check_ratings.volts_from_name(net)
        if v and net in passive_only and net not in out:
            # a sheet-local net only passives touch (/charger/CHG_VBUS_RC,
            # an RC snubber's mid node) is named after a rail, not one
            continue
        setv(net, v, "net name")
    return out


PASSIVE_KINDS = {"R", "C", "L", "FB", "TP"}
# reference prefixes that take a net off the board (an I2C bus's peer)
OFFBOARD_KINDS = {"J", "P", "CN", "CON", "TP"}


def _passive_only_nets(board: dict) -> set[str]:
    """Sheet-local nets ('/sheet/NAME') whose every pin is on a resistor,
    capacitor, inductor, bead or test point."""
    kinds: dict[str, set[str]] = {}
    for c in board["comps"].values():
        for p in c["pins"].values():
            kinds.setdefault(p["net"], set()).add(c["kind"])
    return {n for n, ks in kinds.items()
            if n.startswith("/") and n.count("/") >= 2
            and ks and ks <= PASSIVE_KINDS}


def rail_mins(rails: dict, constraints: dict | None,
              overrides: dict[str, float]) -> dict[str, float]:
    """{rail net: its declared minimum} - constraints `voltages[].min`,
    then --rail-min. Only those below the rail's nominal volts."""
    out: dict[str, float] = {}
    for e in (constraints or {}).get("voltages") or []:
        if e.get("net") in rails and e.get("min") is not None:
            out[e["net"]] = float(e["min"])
    for n, v in overrides.items():
        if n not in rails:
            raise checklib.CheckError(f"--rail-min {n}: not a rail (give "
                                      "its voltage with --rail first)")
        out[n] = v
    return {n: v for n, v in out.items() if v < rails[n][0]}


# ----------------------------------------------------------------- models

ZENER_RE = re.compile(r"BZX|BZT|MMSZ|ZMM|1N47|1N52|ZENER|\bZD", re.I)
TVS_RE = re.compile(r"SMBJ|SMAJ|SMCJ|SMF\d|P6KE|P4SMA|PESD|ESD|TVS|SD05|"
                    r"SRV05|USBLC", re.I)
SCHOTTKY_RE = re.compile(r"SS1\d|SS2\d|SS3\d|SS5\d|B5819|B530|B540|BAT\d|"
                         r"1N58|SK\d|MBR|SCHOTTKY|SB\d{3}|RB\d{3}|PMEG", re.I)
LED_RE = re.compile(r"LED", re.I)
# Small discretes by MPN prefix when parts.json says nothing.
NPN_RE = re.compile(r"^(MMBT3904|2N3904|BC8[45]7|BC547|S8050|SS8050|MMBT2222|"
                    r"2N2222|PMBT3904|MMBT4401|BC817|MMDT3904|MBT3904|"
                    r"MMDT2222|MBT2222|MMDT4401)", re.I)
PNP_RE = re.compile(r"^(MMBT3906|2N3906|BC8[56]7|BC557|S8550|SS8550|MMBT2907|"
                    r"2N2907|PMBT3906|MMBT4403|BC807|MMDT3906|MBT3906|"
                    r"MMDT2907|MBT2907|MMDT4403)", re.I)
# One NPN and one PNP (or N and P FET) in a package: one polarity read off
# the MPN would be wrong for a unit, so these stay open.
COMPLEMENTARY_RE = re.compile(r"BC84[67]BPN|MMDT3946|MBT3946|NPN\s*[+/&]\s*PNP|"
                              r"PNP\s*[+/&]\s*NPN|N\s*[+/&]\s*P-?Channel|"
                              r"Complementary", re.I)
# Dual-transistor pinouts by pin number when the pin functions don't say
# which unit a pin is in: {unit: {role: pin}}. SOT-363 duals (MMDT3904,
# BC847BS, MBT3904DW; 2N7002DW, BSS138DW) are E1 B1 C2 E2 B2 C1 / S1 G1
# D2 S2 G2 D1; SOIC-8 dual FETs are S1 G1 S2 G2 D2 D2 D1 D1.
DUAL_PINOUTS = (
    {"A": {"E": ("1",), "B": ("2",), "C": ("6",)},
     "B": {"E": ("4",), "B": ("5",), "C": ("3",)}},
    {"A": {"S": ("1",), "G": ("2",), "D": ("6",)},
     "B": {"S": ("4",), "G": ("5",), "D": ("3",)}},
    {"A": {"S": ("1",), "G": ("2",), "D": ("7", "8")},
     "B": {"S": ("3",), "G": ("4",), "D": ("5", "6")}},
)
DUAL_RE = re.compile(r"^(MMDT\d|MBT\d+DW|BC8[45]7BS|BC8[56]7BS|BC846BS|"
                     r"2N7002DW|BSS138DW|BSS84DW|DMN\d+DW|DMG\d+DW|"
                     r"AO48\d\d|SI49\d\d)", re.I)
NMOS_RE = re.compile(r"^(AO340[02]|AO3416|2N7002|BSS138|SI230[24]|IRLML2502|"
                     r"IRLML6344|DMN|AO4406|AO4410|NCE\d+N)", re.I)
PMOS_RE = re.compile(r"^(AO3401|AO3407|AO3415|AO4485|AO440[79]|SI230[15]|"
                     r"IRLML6402|BSS84|DMG2305|DMP|NCE\d+P|FDN340P)", re.I)


def _role_net(c: dict, letters: tuple[str, ...]) -> dict[str, str] | None:
    """{letter: net} for a 2/3-terminal part from its pin functions; None
    when a role is missing or spans two nets."""
    out: dict[str, str] = {}
    for p in c["pins"].values():
        for f in p["fn"]:
            if f in letters:
                if out.get(f, p["net"]) != p["net"]:
                    return None
                out[f] = p["net"]
    return out if set(out) == set(letters) else None


def transistor_units(c: dict, letters: tuple[str, ...],
                     mpn: str = "") -> list[tuple[str, dict[str, str]]]:
    """[(unit, {letter: net})] for a transistor: [("", roles)] for a single
    part; [("A", ...), ("B", ...)] for a dual, read from numbered pin
    functions (E1/B1/C1) or, where the functions repeat a role across two
    nets or are blank on a known dual, from DUAL_PINOUTS. [] when the pins
    can't be read."""
    one = _role_net(c, letters)
    if one:
        return [("", one)]
    num: dict[str, dict[str, set]] = {}
    for p in c["pins"].values():
        for f in p["fn"]:
            m = re.fullmatch(r"([A-Z])(\d)", f)
            if m and m.group(1) in letters:
                num.setdefault(m.group(2), {}).setdefault(
                    m.group(1), set()).add(p["net"])
    if len(num) == 2 and all(set(r) == set(letters) and
                             all(len(n) == 1 for n in r.values())
                             for r in num.values()):
        return [(chr(ord("A") + i), {k: next(iter(n)) for k, n in r.items()})
                for i, (_, r) in enumerate(sorted(num.items()))]
    pins = c["pins"]
    labelled = any(f in letters for p in pins.values() for f in p["fn"])
    if not labelled and not DUAL_RE.match(mpn or ""):
        return []
    for pinout in DUAL_PINOUTS:
        if set(pinout["A"]) != set(letters):
            continue
        want = {pin: role for u in pinout.values()
                for role, ps in u.items() for pin in ps}
        if set(pins) != set(want):
            continue
        if labelled and any(set(pins[pin]["fn"]) & set(letters)
                            and role not in pins[pin]["fn"]
                            for pin, role in want.items()):
            continue
        units = []
        for u, roles in pinout.items():
            r = {}
            for role, ps in roles.items():
                nets = {pins[x]["net"] for x in ps}
                if len(nets) != 1:
                    break
                r[role] = nets.pop()
            else:
                units.append((u, r))
        if len(units) == 2:
            return units
    return []


def classify_diode(c: dict, part: dict | None) -> dict:
    text = " ".join([c["value"], c["footprint"], (part or {}).get("mpn", ""),
                     (part or {}).get("description", "")])
    color = engcalc.led_color(c["value"], attr(part, "Color"),
                              (part or {}).get("description"))
    if c["ref"].upper().startswith("LED") or LED_RE.search(c["footprint"]) \
            or (color and engcalc.parse_value(c["value"]) is None
                and not ZENER_RE.search(text)):
        vf_txt = attr(part, r"Forward\(Vf\)|Forward Voltage|^Vf")
        vfs = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", vf_txt or "")]
        it = engcalc.parse_value((attr(part, r"Test Current") or "")
                                 .replace("A", "")) if part else None
        vf = sum(vfs[:2]) / len(vfs[:2]) if vfs else engcalc.led_vf(color)
        i0 = it or 5e-3
        n = 2.0
        is_ = i0 / math.exp(vf / (n * 0.025852))
        return {"type": "led", "vf": vf, "at_a": i0, "color": color,
                "vf_source": "parts.json" if vfs else
                ("colour table" if color else "assumed 2.0 V"),
                "card": f"D(IS={is_:.3e} N={n} RS=2)"}
    if TVS_RE.search(text):
        bv = engcalc.parse_volts(attr(part, r"Breakdown"))
        if bv is None:
            m = re.search(r"(?:SMBJ|SMAJ|SMCJ|P6KE|P4SMA)(\d+(?:\.\d+)?)",
                          text, re.I)
            bv = float(m.group(1)) * 1.11 if m else None
        return {"type": "tvs", "bv": bv,
                "card": f"D(IS=1e-12 N=1 BV={bv:g} IBV=1m)" if bv
                else "D(IS=1e-12 N=1)"}
    if ZENER_RE.search(text):
        m = re.search(r"(\d+)V(\d+)", text) or re.search(
            r"(\d+(?:\.\d+)?)\s*V\b", text)
        bv = (float(f"{m.group(1)}.{m.group(2)}") if m and m.lastindex == 2
              else float(m.group(1)) if m else None)
        return {"type": "zener", "bv": bv,
                "card": f"D(IS=1e-14 N=1 BV={bv:g} IBV=5m)" if bv
                else "D(IS=1e-14 N=1)"}
    if SCHOTTKY_RE.search(text):
        return {"type": "schottky", "card": "D(IS=1e-6 N=1.05 RS=0.05)"}
    return {"type": "diode", "card": "D(IS=1e-14 N=1 RS=0.05)"}


def classify_transistor(c: dict, part: dict | None) -> list[dict]:
    """One element description per transistor unit (two for a dual), [] when
    its type or pins can't be read."""
    text = " ".join([c["value"], (part or {}).get("mpn", ""),
                     (part or {}).get("description", ""),
                     attr(part, r"^type$|^Number$|Transistor Type") or ""])
    mpn = (part or {}).get("mpn") or c["value"]
    bjt = transistor_units(c, ("B", "C", "E"), mpn)
    fet = transistor_units(c, ("G", "D", "S"), mpn)
    if len(bjt) + len(fet) > 1 and COMPLEMENTARY_RE.search(text):
        return []
    if bjt:
        pol = ("pnp" if re.search(r"\bPNP\b", text, re.I) or PNP_RE.match(mpn)
               else "npn" if re.search(r"\bNPN\b", text, re.I)
               or NPN_RE.match(mpn) else None)
        if pol:
            return [{"type": pol, "unit": u, "roles": r,
                     "card": f"{pol.upper()}(BF=100 IS=1e-14 VAF=100)"}
                    for u, r in bjt]
    if fet:
        pol = ("pmos" if re.search(r"P-?Channel|PMOS", text, re.I)
               or PMOS_RE.match(mpn) else
               "nmos" if re.search(r"N-?Channel|NMOS", text, re.I)
               or NMOS_RE.match(mpn) else None)
        if pol:
            vth = engcalc.parse_volts(attr(part, r"Threshold")) or 1.5
            vto = -vth if pol == "pmos" else vth
            return [{"type": pol, "unit": u, "roles": r, "vth": vth,
                     "card": f"{pol.upper()}(LEVEL=1 VTO={vto:g} KP=5)"}
                    for u, r in fet]
    return []


# ---------------------------------------------------------------- network

def build_model(board: dict, parts: dict, rails: dict) -> dict:
    """The board as SPICE elements. Every net gets a node (grounds are 0);
    R, C, L, diodes and recognised BJTs/MOSFETs become elements; ICs,
    connectors, switches and anything unreadable stay open and are listed
    in `unmodelled`."""
    grounds = {n for n in board["nets"] if check_ratings.is_ground(n)}
    names = simlib.rename_map([n for n in board["nets"] if n not in grounds])
    node = {n: ("0" if n in grounds else names[n]) for n in board["nets"]}
    elems: list[dict] = []
    unmodelled: list[dict] = []
    for ref in sorted(board["comps"], key=_natural):
        c = board["comps"][ref]
        part = parts.get(ref)
        nets = sorted({p["net"] for p in c["pins"].values()})
        k = c["kind"]
        if k in ("R", "C", "L", "FB") and len(c["pins"]) == 2 and \
                len(nets) == 2:
            v = engcalc.parse_value(c["value"])
            if v is None:
                v = engcalc.parse_value(attr(part, r"^Resistance|^Capacit|"
                                             r"^Inductance") or "")
            if v is None:
                unmodelled.append({"ref": ref, "value": c["value"],
                                   "why": "value does not read as a number"})
                continue
            a, b = (node[n] for n in nets)
            e = {"ref": ref, "kind": "C" if k == "C" else
                 "L" if k in ("L", "FB") else "R", "nets": nets,
                 "value": v, "footprint": c["footprint"]}
            if e["kind"] == "R":
                rv = max(v, 1e-3)
                e["dc"] = e["tran"] = f"R{ref} {a} {b} {rv:.6g}"
            elif e["kind"] == "L":
                e["dc"] = f"R{ref} {a} {b} 1e-3"
                e["tran"] = f"L{ref} {a} {b} {v:.6g}"
            else:
                e["dc"] = None
                e["tran"] = f"C{ref} {a} {b} {v:.6g}"
            elems.append(e)
        elif k in ("D", "LED") and len(c["pins"]) >= 2:
            roles = _role_net(c, ("A", "K"))
            if roles is None and set(c["pins"]) == {"1", "2"}:
                roles = {"K": c["pins"]["1"]["net"], "A": c["pins"]["2"]["net"]}
            if roles is None or roles["A"] == roles["K"]:
                unmodelled.append({"ref": ref, "value": c["value"],
                                   "why": "anode/cathode not identified"})
                continue
            d = classify_diode(c, part)
            model = f"D_{ref}".upper()
            line = f"D{ref} {node[roles['A']]} {node[roles['K']]} {model}"
            elems.append({"ref": ref, "kind": "D", "nets": nets, **d,
                          "a": roles["A"], "k": roles["K"], "dc": line,
                          "tran": line,
                          "model": f".model {model} {d['card']}"})
        elif k == "Q":
            ts = classify_transistor(c, part)
            if not ts:
                unmodelled.append({"ref": ref, "value": c["value"],
                                   "why": "transistor type or pins unknown"})
                continue
            for t in ts:
                # a dual's units are elements Q1A and Q1B of part Q1
                name = ref + t.pop("unit")
                model = f"Q_{name}".upper()
                n = t["roles"]
                if t["type"] in ("npn", "pnp"):
                    line = (f"Q{name} {node[n['C']]} {node[n['B']]} "
                            f"{node[n['E']]} {model}")
                else:
                    line = (f"M{name} {node[n['D']]} {node[n['G']]} "
                            f"{node[n['S']]} {node[n['S']]} {model}")
                elems.append({"ref": name, "comp": ref, "kind": "Q",
                              "nets": sorted(set(n.values())) if len(ts) > 1
                              else nets, **t, "dc": line, "tran": line,
                              "model": f".model {model} {t['card']}"})
        else:
            unmodelled.append({"ref": ref, "value": c["value"],
                               "why": "IC/connector/switch: left open"})
    return {"node": node, "grounds": grounds, "elems": elems,
            "unmodelled": unmodelled}


def _natural(ref: str):
    m = re.match(r"([A-Za-z]+)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0), ref) if m else (ref, 0, ref)


def ic_supply(c: dict, rails: dict) -> float | None:
    """An IC's supply: the highest rail on a power-named pin, else on any."""
    pw = [rails[p["net"]][0] for p in c["pins"].values()
          if p["net"] in rails and any(POWER_FN.match(f) for f in p["fn"])]
    anyr = [rails[p["net"]][0] for p in c["pins"].values() if p["net"] in rails]
    v = max(pw) if pw else max(anyr) if anyr else None
    return v if v and v > 0 else None


def load_io_supply(path: Path = IO_SUPPLY_YAML) -> list[dict]:
    if not path.is_file():
        return []
    rows = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) \
        .get("parts") or []
    return [{**r, "_m": re.compile(r["match"], re.I),
             "_p": re.compile(r["supply_pins"])} for r in rows]


def io_supply_pinned(constraints: dict | None,
                     overrides: list[str] | None = None
                     ) -> dict[str, tuple[str, str]]:
    """{ref: (pin number or function, source)} from constraints `io_supply`
    ({"U3": "VDDD"}) and --io-supply REF=PIN (which wins)."""
    raw = (constraints or {}).get("io_supply") or {}
    if not isinstance(raw, dict) or not all(
            isinstance(v, str) and v.strip() for v in raw.values()):
        raise checklib.CheckError("constraints io_supply: want {ref: pin}")
    out = {str(r).strip(): (v.strip(), "constraints io_supply")
           for r, v in raw.items()}
    for it in overrides or []:
        r, sep, k = it.partition("=")
        if not sep or not r.strip() or not k.strip():
            raise checklib.CheckError(f"--io-supply {it!r}: want REF=PIN")
        out[r.strip()] = (k.strip(), "--io-supply")
    return out


def io_supply_specs(board: dict, parts: dict, rails: dict, pinned: dict,
                    table: list[dict], unknowns: list[dict]) -> dict:
    """{ref: {pins, volts, source}}: the pins whose rail an IC's I/O swings
    to, where that is not its highest supply. `pinned` (io_supply_pinned)
    first: its pin must exist and be on a rail. Then the first
    reference/io_supply.yaml row that matches the part's MPN or value; a
    row whose pins the IC lacks, or that are off every rail with no volts
    in the row, goes to `unknowns` and leaves the IC on its highest
    supply."""
    out: dict[str, dict] = {}
    for ref, (key, src) in sorted(pinned.items()):
        c = board["comps"].get(ref)
        if c is None or c["kind"] not in ("U", "IC"):
            raise checklib.CheckError(f"{src} {ref}={key}: no IC {ref} on "
                                      "the board")
        k = key.upper()
        pins = sorted((pin for pin, p in c["pins"].items()
                       if pin.upper() == k or k in p["fn"]), key=_natural)
        if not pins:
            raise checklib.CheckError(f"{src} {ref}={key}: {ref} has no pin "
                                      f"{key}")
        if not any(c["pins"][pin]["net"] in rails for pin in pins):
            raise checklib.CheckError(
                f"{src} {ref}={key}: {ref} pin {key} is on "
                f"{c['pins'][pins[0]]['net']}, not a rail: give its voltage "
                "in constraints voltages or --rail")
        out[ref] = {"pins": pins, "volts": None, "source": src}
    for c in sorted(board["comps"].values(), key=lambda x: _natural(x["ref"])):
        if c["kind"] not in ("U", "IC") or c["ref"] in out:
            continue
        names = [x for x in ((parts.get(c["ref"]) or {}).get("mpn"),
                             c["value"]) if x]
        row = next((r for r in table
                    if any(r["_m"].search(x) for x in names)), None)
        if row is None:
            continue
        pins = sorted((pin for pin, p in c["pins"].items()
                       if any(row["_p"].match(f) for f in p["fn"])),
                      key=_natural)
        volts = row.get("volts")
        src = ("reference/io_supply.yaml: "
               + " ".join(str(row.get("source", "")).split())
               + ("" if row.get("verified") else ", not re-read"))
        if pins and (volts or any(c["pins"][p]["net"] in rails
                                  for p in pins)):
            out[c["ref"]] = {"pins": pins,
                             "volts": float(volts) if volts else None,
                             "source": src}
            continue
        unknowns.append({
            "ref": c["ref"], "what": f"{c['value']} I/O supply",
            "why": ("reference/io_supply.yaml gives its I/O supply as the "
                    f"pin {row['supply_pins']}, but "
                    + ("no pin matches" if not pins else
                       "that pin is on no rail and the row gives no volts")
                    + ": its I/O is driven at its highest supply (name the "
                      "pin in constraints io_supply)")})
    return out


def ic_io_supply(c: dict, rails: dict, spec: dict | None) -> float | None:
    """The level an IC's I/O pins swing to: the highest rail on its
    io_supply pins, else the row's volts, else its supply (ic_supply)."""
    if spec:
        on = [rails[c["pins"][p]["net"]][0] for p in spec["pins"]
              if c["pins"][p]["net"] in rails]
        v = max(on) if on else spec.get("volts")
        if v and v > 0:
            return v
    return ic_supply(c, rails)


OPEN_DRAIN_FN = re.compile(r"^N?(PG|PGOOD|POK|FAULT\w*|INT\w*|IRQ|ALERT"
                           r"|CHRG|STAT\w*)$")


def pin_modes(constraints: dict | None, overrides: list[str] | None = None
              ) -> dict[str, dict[str, str]]:
    """{ref: {pin number or function: 'io' | 'od' | 'off'}} from
    constraints `pin_modes` ({"U1": {"PB11": "analog", "B15": "output"}})
    and --pin-mode REF.PIN=MODE (which wins)."""
    out: dict[str, dict[str, str]] = {}
    raw = (constraints or {}).get("pin_modes") or {}
    if not isinstance(raw, dict):
        raise checklib.CheckError("constraints pin_modes: want "
                                  "{ref: {pin: mode}}")
    items = [(r, k, v) for r, m in raw.items() for k, v in (m or {}).items()]
    for it in overrides or []:
        key, sep, v = it.partition("=")
        r, dot, k = key.partition(".")
        if not sep or not dot or not r or not k:
            raise checklib.CheckError(f"--pin-mode {it!r}: want REF.PIN=MODE")
        items.append((r, k, v))
    for r, k, v in items:
        mode = PIN_MODES.get(str(v).strip().lower())
        if mode is None:
            raise checklib.CheckError(
                f"pin mode {r}.{k}={v!r}: want one of "
                f"{', '.join(sorted(PIN_MODES))}")
        out.setdefault(r.strip(), {})[str(k).strip().upper()] = mode
    return out


def analog_net(net: str) -> bool:
    return bool(ANALOG_NET & set(re.split(r"[^A-Z0-9]+",
                                          check_ratings.base_net(net).upper())))


def pin_role(c: dict, pin: str, p: dict, modes: dict | None
             ) -> tuple[str | None, str]:
    """(mode, why) for an IC pin: 'io' driven, 'od' driven low as an
    open-drain status line, 'off' or None not driven."""
    m = (modes or {}).get(c["ref"]) or {}
    for k in [pin.upper(), *p["fn"]]:
        if k in m:
            return m[k], "pin_modes"
    fns = p["fn"]
    if any(OPEN_DRAIN_FN.match(f) for f in fns):
        return "od", "status name"
    gp = any(GPIO_FN.match(f) for f in fns)
    soc = not gp and any(SOC_PAD_FN.match(f) and not SOC_NOT_IO.match(f)
                         for f in fns)
    if not (gp or soc):
        return None, ""
    if analog_net(p["net"]):
        return "off", "analog net name"
    return "io", "GPIO name" if gp else "SoC pad name"


def io_nets(board: dict, rails: dict, model: dict,
            modes: dict | None = None, skipped: list | None = None,
            io_supply: dict | None = None) -> tuple[dict, set]:
    """({net: drive volts}, open-drain nets) for IC I/O nets: a net on a
    GPIO-named IC pin or an SoC pad named by its function (ENET2_TX_EN),
    not a rail, not an analog-named net (ADC_x, x_SNS), with `modes`
    (pin_modes) overriding per pin. Nets joined by a 0-ohm link count as one
    and get one drive (on the first of them), at the lowest supply of the
    ICs on any of them: a driven pin counts its IC's I/O supply
    (`io_supply`, io_supply_specs), any other pin its highest supply.
    Open-drain nets are those with a status-named pin (PG, FAULT, INT...).
    `skipped` collects the pins left undriven by pin_modes or an analog net
    name."""
    grp = _groups_by_zero_ohm(model)
    supply: dict[str, float] = {}
    io: set[str] = set()
    od: set[str] = set()
    off: set[str] = set()
    for c in board["comps"].values():
        if c["kind"] not in ("U", "IC"):
            continue
        vs = ic_supply(c, rails)
        vio = ic_io_supply(c, rails, (io_supply or {}).get(c["ref"]))
        for pin, p in c["pins"].items():
            n = p["net"]
            if n in rails or n in model["grounds"] or vs is None:
                continue
            g = grp[n]
            mode, why = pin_role(c, pin, p, modes)
            v = vio if mode in ("io", "od") else vs
            supply[g] = min(supply.get(g, v), v)
            if mode == "off":
                if why == "pin_modes":
                    off.add(g)
                if skipped is not None:
                    skipped.append({"ref": c["ref"], "pin": pin, "net": n,
                                    "why": why})
            elif mode in ("io", "od"):
                io.add(g)
                if mode == "od":
                    od.add(g)
    first = {}
    for n in sorted(grp):
        first.setdefault(grp[n], n)
    # a net a pin_modes 'off' names stays undriven even when another IC's
    # pin on it reads as I/O by name
    io -= off
    return ({first[g]: supply[g] for g in sorted(io)},
            {first[g] for g in od if g in io})


def io_drivers(board: dict, rails: dict, model: dict,
               modes: dict | None = None) -> dict[str, list[tuple]]:
    """{io net (as io_nets names it): [(comp, pin, pin dict)]}: the IC pins
    io_nets reads as driven (io or od) on each net or its 0-ohm group."""
    grp = _groups_by_zero_ohm(model)
    first: dict[str, str] = {}
    for n in sorted(grp):
        first.setdefault(grp[n], n)
    out: dict[str, list[tuple]] = {}
    for c in board["comps"].values():
        if c["kind"] not in ("U", "IC") or ic_supply(c, rails) is None:
            continue
        for pin, p in c["pins"].items():
            n = p["net"]
            if n in rails or n in model["grounds"] or n not in grp:
                continue
            if pin_role(c, pin, p, modes)[0] in ("io", "od"):
                out.setdefault(first[grp[n]], []).append((c, pin, p))
    return out


def load_gpio_drive(path: Path = GPIO_DRIVE_YAML) -> list[dict]:
    if not path.is_file():
        return []
    rows = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) \
        .get("parts") or []
    return [{**r, "_m": re.compile(r["match"], re.I),
             "_p": re.compile(r["pins"]) if r.get("pins") else None}
            for r in rows]


def gpio_limit(c: dict, p: dict, part: dict | None, table: list[dict],
               sinks: bool) -> tuple[float, str | None]:
    """(amps, source) a driven IC pin may sink (or source): its
    gpio_drive.yaml row's iol_a (ioh_a), else GPIO_MAX_A with source None."""
    names = [x for x in ((part or {}).get("mpn"), c["value"]) if x]
    fns = _pin_fns(p)
    for r in table:
        if any(r["_m"].search(x) for x in names) and \
                (r["_p"] is None or any(r["_p"].match(f) for f in fns)):
            v = r.get("iol_a" if sinks else "ioh_a")
            if v is not None:
                return float(v), ("reference/gpio_drive.yaml: "
                                  + " ".join(str(r.get("source", "")).split())
                                  + ("" if r.get("verified") else
                                     ", not re-read"))
    return GPIO_MAX_A, None


RAIL_RANK = {"--rail": 0, "constraints": 1, "power_tree": 2}


def rail_sources(rails: dict, model: dict) -> dict[str, str]:
    """{rail net: the rail net whose source drives it}. Rails joined by a
    0-ohm link, inductor or ferrite (+3V3 -> FB2 -> +3V3E) are one node at
    DC: two ideal sources there would fight through the link, so only the
    best-declared one (--rail, constraints, power tree, regulator, then
    name; then the higher voltage) gets a source."""
    grp = _groups_by_zero_ohm(model)
    best: dict[str, str] = {}
    for net, (v, src) in rails.items():
        g = grp.get(net, net)
        key = (RAIL_RANK.get(src, 3 if src.startswith("regulator") else 4),
               -v, net)
        cur = best.get(g)
        if cur is None or key < (RAIL_RANK.get(rails[cur][1], 3 if
                                 rails[cur][1].startswith("regulator") else 4),
                                 -rails[cur][0], cur):
            best[g] = net
    return {net: best[grp.get(net, net)] for net in rails}


def circuit(model: dict, rails: dict, analysis: str,
            drive: dict[str, float] | None = None, ramp_s: float = 1e-3,
            tstop: float = 0.0) -> str:
    """One SPICE deck: rails as sources (PWL ramps for a transient), IO
    drives through 25 ohm, the elements, their models, the analysis."""
    node = model["node"]
    lines = [f"* sim_board {analysis}",
             ".options rshunt=1e9 savecurrents"]
    used = {node[n] for e in model["elems"] for n in e["nets"]}
    srcs = rail_sources(rails, model)
    for net, (v, _src) in sorted(rails.items()):
        nd = node.get(net)
        if srcs[net] != net or nd in (None, "0") or nd not in used or v == 0:
            continue
        if analysis == "tran":
            lines.append(f"VR_{nd} {nd} 0 PWL(0 0 {ramp_s:.6g} {v:.6g})")
        else:
            lines.append(f"VR_{nd} {nd} 0 DC {v:.6g}")
    for net, v in sorted((drive or {}).items()):
        nd = node[net]
        if nd not in used:
            continue
        lines.append(f"VIO_{nd} IO_{nd} 0 DC {v:.6g}")
        lines.append(f"RIO_{nd} IO_{nd} {nd} 25")
    key = "tran" if analysis == "tran" else "dc"
    for e in model["elems"]:
        if e.get(key):
            lines.append(e[key])
    for e in model["elems"]:
        if e.get("model"):
            lines.append(e["model"])
    if analysis == "tran":
        lines.append(f".tran {tstop / 2000:.6g} {tstop:.6g} uic")
    else:
        lines.append(".op")
    lines.append(".end")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- engine

WORKER_GRACE_S = 15


def run_engine(decks: list[str], dll: Path, timeout: float) -> list[dict]:
    """Every deck through simlib.run_vectors in one killable worker."""
    with tempfile.TemporaryDirectory(prefix="sim_board-") as td:
        f = Path(td) / "decks.json"
        f.write_text(json.dumps(decks), encoding="utf-8")
        cmd = [sys.executable, str(Path(__file__).resolve()),
               "--exec-decks", str(f), "--dll", str(dll)]
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=timeout + WORKER_GRACE_S)
        except subprocess.TimeoutExpired:
            raise checklib.CheckError(f"engine timeout after {timeout:g} s")
    try:
        res = json.loads(cp.stdout)
    except json.JSONDecodeError:
        tail = (cp.stderr or cp.stdout or "").strip()[-400:]
        raise checklib.CheckError(f"engine worker died (exit "
                                  f"{cp.returncode}): {tail or 'no output'}")
    if res.get("status") != "ok":
        raise checklib.CheckError(f"engine: {res.get('error')}")
    return res["plots"], res.get("ngspice_version")


def exec_decks(path: Path, dll: Path) -> dict:
    decks = json.loads(path.read_text(encoding="utf-8"))
    simlib.setup_engine_env(dll)
    return simlib.run_vectors(decks)


def op_view(plot: dict, model: dict) -> dict:
    """{'v': {net: volts}, 'i': {ref: amps}, 'src': {name: amps}} from an op
    plot. A net no element touches has no vector and no voltage."""
    v = {}
    for net, nd in model["node"].items():
        if nd == "0":
            v[net] = 0.0
        elif nd.lower() in plot:
            v[net] = plot[nd.lower()][0]
    cur = {}
    for e in model["elems"]:
        pfx = {"R": "r", "L": "r", "D": "d", "C": "c"}.get(e["kind"])
        if pfx is None:
            continue
        key = f"@{pfx}{e['ref'].lower()}[{'id' if pfx == 'd' else 'i'}]"
        if key in plot:
            cur[e["ref"]] = plot[key][0]
    src = {k[:-len("#branch")]: vals[0] for k, vals in plot.items()
           if k.endswith("#branch")}
    return {"v": v, "i": cur, "src": src}


def driven_nets(model: dict, rails: dict) -> set[str]:
    """Nets with a DC path (R, L, diode, transistor) to a rail or ground -
    the ones whose op voltage means something; the rest float with the
    open ICs (rshunt holds them near 0 V)."""
    parent: dict[str, str] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in model["elems"]:
        if e["kind"] == "C":
            continue
        ns = e["nets"]
        for n in ns[1:]:
            parent[find(n)] = find(ns[0])
    roots = {find(n) for n in list(rails) + list(model["grounds"])}
    return {n for n in model["node"] if find(n) in roots}


# --------------------------------------------------------------- findings

def _vio(kind, sev, refs, net, msg, **extra):
    return checklib.violation(SCRIPT, sev, None, None, net, refs, msg,
                              SOURCE, kind=kind, **extra)


def _f(x: float, unit: str = "") -> str:
    if x == 0:
        return f"0 {unit}".strip()
    for s, m in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1), ("m", 1e-3),
                 ("u", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(x) >= m:
            return f"{x / m:.3g} {s}{unit}".strip()
    return f"{x:.3g} {unit}".strip()


def load_vrefs(path: Path = VREF_YAML) -> list[dict]:
    if not path.is_file():
        return []
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
        "regulators") or []


def find_vref(c: dict, part: dict | None, pinned: dict, table: list[dict]):
    """(vref, tol, source, verified) for an adjustable regulator, or None."""
    if c["ref"] in pinned:
        return pinned[c["ref"]], 0.02, "--vref", True
    for txt in (c["value"], (part or {}).get("mpn") or ""):
        for row in table:
            if txt and re.search(row["match"], txt, re.I):
                return (float(row["vref"]), float(row.get("tol", 0.02)),
                        row.get("source", "regulator_vref.yaml"),
                        bool(row.get("verified")))
    return None


def resistor_rating(e: dict, part: dict | None) -> tuple[float | None, str]:
    w = engcalc.parse_watts(attr(part, r"^Power"))
    if w:
        return w, "parts.json"
    w = engcalc.resistor_rating_w(e["footprint"])
    return (w, f"{engcalc.package_code(e['footprint'])} package default") \
        if w else (None, "unknown")


def resistor_umax(e: dict, part: dict | None) -> tuple[float | None, str]:
    v = engcalc.parse_volts(attr(part, r"^Voltage-Supply\(Max\)",
                                 r"^Working Voltage"))
    if v:
        return v, "parts.json"
    v = engcalc.resistor_umax_v(e["footprint"])
    return (v, f"{engcalc.package_code(e['footprint'])} package default, "
               "D/CRCW e3") if v else (None, "unknown")


def cap_rating(c: dict, part: dict | None) -> tuple[float | None, str]:
    v = engcalc.parse_volts(attr(part, r"^Voltage Rat"))
    if v:
        return v, "parts.json"
    v = engcalc.parse_volts(c["value"])
    return (v, "value field") if v else (None, "unknown")


def _groups_by_zero_ohm(model: dict) -> dict[str, str]:
    """net -> representative; nets joined by an inductor/ferrite (a DC
    short) or a resistor of 1 ohm or less are one."""
    parent: dict[str, str] = {n: n for n in model["node"]}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in model["elems"]:
        if e["kind"] == "L" or (e["kind"] == "R" and e["value"] <= 1.0):
            a, b = e["nets"]
            parent[find(a)] = find(b)
    return {n: find(n) for n in parent}


def enable_nets(board: dict, model: dict, rails: dict) -> list[dict]:
    """IC enable/reset pins on a net with a capacitor to ground."""
    caps = {}
    for e in model["elems"]:
        if e["kind"] == "C":
            a, b = e["nets"]
            for x, y in ((a, b), (b, a)):
                if y in model["grounds"]:
                    caps.setdefault(x, []).append(e)
    out, seen = [], set()
    for c in board["comps"].values():
        if c["kind"] not in ("U", "IC"):
            continue
        for p in c["pins"].values():
            n = p["net"]
            if n in rails or n not in caps or (c["ref"], n) in seen:
                continue
            if any(ENABLE_FN.match(f) for f in p["fn"]):
                vs = ic_supply(c, rails)
                if vs:
                    seen.add((c["ref"], n))
                    out.append({"ref": c["ref"], "net": n, "vdd": vs,
                                "c": sum(e["value"] for e in caps[n])})
    return out


def supply_net(c: dict, rails: dict) -> str | None:
    best = None
    for p in c["pins"].values():
        if p["net"] in rails and any(POWER_FN.match(f) for f in p["fn"]):
            if best is None or rails[p["net"]][0] > rails[best][0]:
                best = p["net"]
    return best


def analyze(board: dict, parts: dict, rails: dict, opts: dict) -> tuple:
    model = build_model(board, parts, rails)
    grounds = model["grounds"]
    skipped: list[dict] = []
    sup_unknowns: list[dict] = []
    io_sup = io_supply_specs(board, parts, rails, opts.get("io_supply") or {},
                             load_io_supply(), sup_unknowns)
    io, od = io_nets(board, rails, model, opts.get("pin_modes"), skipped,
                     io_sup)
    # io_run: outputs high and status lines (PG, FAULT...) asserted low -
    # the powered-up, running state most enables and load switches see.
    scen = {"default": {}, "io_low": {n: 0.0 for n in io}, "io_high": io,
            "io_run": {n: (0.0 if n in od else v) for n, v in io.items()}}
    ens = enable_nets(board, model, rails)
    ramp = opts["ramp_ms"] * 1e-3
    taus = []
    for en in ens:
        rs = [e["value"] for e in model["elems"] if e["kind"] == "R"
              and en["net"] in e["nets"] and e["value"] > 1]
        if rs:
            taus.append(min(rs) * en["c"])
    tstop = min(2.0, max(10e-3, 5 * ramp, 6 * max(taus or [0])))
    # every rail with a declared minimum at that minimum, the IO drives
    # re-derived from the lowered supplies: the "@min" scenarios
    mins = opts.get("rail_min") or {}
    runs = [(name, rails, d) for name, d in scen.items()]
    if mins:
        lo = {n: ((mins[n], src) if n in mins else (v, src))
              for n, (v, src) in rails.items()}
        io_lo, od_lo = io_nets(board, lo, model, opts.get("pin_modes"),
                               io_supply=io_sup)
        runs += [(f"{name}@min", lo, d) for name, d in
                 (("default", {}), ("io_low", {n: 0.0 for n in io_lo}),
                  ("io_high", io_lo),
                  ("io_run", {n: (0.0 if n in od_lo else v)
                              for n, v in io_lo.items()}))]
    decks = [circuit(model, r, "op", d) for _, r, d in runs]
    if ens:
        decks.append(circuit(model, rails, "tran", ramp_s=ramp, tstop=tstop))
    if not model["elems"]:
        # nothing to put in a deck: ngspice answers an empty .op with "no
        # vectors" (#1261)
        return (model, None, None, scen, ens, [], [], {}, None,
                "no passive or modelled device to simulate")
    try:
        plots, version = run_engine(decks, opts["dll"], opts["timeout"])
    except checklib.CheckError as exc:
        # an engine failure loses the operating point, not the board: the
        # passes that need no simulation still run (#1261)
        return model, None, None, scen, ens, [], [], {}, None, str(exc)
    ops = {}
    for (name, r, drive), plot in zip(runs, plots):
        view = op_view(plot, model)
        view["driven"] = driven_nets(model, {**r, **drive})
        ops[name] = view
    tran = plots[len(runs)] if ens else None

    vios: list[dict] = []
    unknowns: list[dict] = [
        {"ref": u["ref"], "what": f"{u['value']} not simulated",
         "why": u["why"]} for u in model["unmodelled"]
        if not u["why"].startswith("IC/")] + sup_unknowns
    facts: dict = {"scenarios": {k: {"io_nets_driven": len(d)}
                                 for k, _, d in runs}}
    if io_sup:
        facts["io_supply"] = [
            {"ref": r, "pins": s["pins"],
             "volts": ic_io_supply(board["comps"][r], rails, s),
             "source": s["source"]} for r, s in sorted(io_sup.items())]
    if skipped:
        facts["io_pins_not_driven"] = skipped[:50]
    if mins:
        facts["rail_min_v"] = dict(sorted(mins.items()))

    def volts(sc, net):
        o = ops[sc]
        return o["v"].get(net) if net in o["driven"] else None

    # resistors: dissipation against rating derated to the ambient, and
    # the volts across against the limiting element voltage, every scenario
    t_amb = opts.get("ambient_c")
    derate = engcalc.resistor_derate(t_amb)
    rfacts = []
    for e in (x for x in model["elems"] if x["kind"] == "R" and x["value"] > 0):
        worst = vworst = None
        for sc in ops:
            va, vb = (volts(sc, n) for n in e["nets"])
            if va is None or vb is None:
                continue
            p = (va - vb) ** 2 / e["value"]
            if worst is None or p > worst[0]:
                worst = (p, sc)
            if vworst is None or abs(va - vb) > vworst[0]:
                vworst = (abs(va - vb), sc)
        if worst is None:
            continue
        umax, usrc = resistor_umax(e, parts.get(e["ref"]))
        if umax is not None and vworst[0] > umax:
            sev = "error" if vworst[1] == "default" else "warning"
            vios.append(_vio("resistor_overvoltage", sev, [e["ref"]],
                             e["nets"][0],
                             f"{e['ref']} has {_f(vworst[0], 'V')} across it "
                             f"({vworst[1]}), over its {_f(umax, 'V')} "
                             f"limiting element voltage ({usrc}): it arcs "
                             "or drifts before its power rating matters",
                             v_across=vworst[0], umax_v=umax,
                             scenario=vworst[1]))
        rating, rsrc = resistor_rating(e, parts.get(e["ref"]))
        if rating is not None and derate < 1.0:
            rating, rsrc = rating * derate, (
                f"{rsrc}, derated x{derate:.2f} at {_f(t_amb)} C ambient")
        rfacts.append({"ref": e["ref"], "p_w": checklib.rnd(worst[0], 6),
                       "scenario": worst[1], "rating_w": rating,
                       "rating_source": rsrc,
                       "v_across": checklib.rnd(vworst[0], 4),
                       "umax_v": umax})
        if rating is None:
            if worst[0] > 0.05:
                unknowns.append({"ref": e["ref"], "what": "power rating",
                                 "why": f"dissipates {_f(worst[0], 'W')}, "
                                        "no package or parts.json rating"})
            continue
        if worst[0] > rating:
            sev = "error" if worst[1] == "default" else "warning"
            vios.append(_vio("resistor_overpower", sev, [e["ref"]],
                             e["nets"][0],
                             f"{e['ref']} dissipates {_f(worst[0], 'W')} "
                             f"({worst[1]}) over its {_f(rating, 'W')} "
                             f"rating ({rsrc})", power_w=worst[0],
                             rating_w=rating, scenario=worst[1]))
        elif worst[0] > 0.5 * rating:
            vios.append(_vio("resistor_overpower", "warning", [e["ref"]],
                             e["nets"][0],
                             f"{e['ref']} dissipates {_f(worst[0], 'W')} "
                             f"({worst[1]}), over half its "
                             f"{_f(rating, 'W')} rating ({rsrc})",
                             power_w=worst[0], rating_w=rating,
                             scenario=worst[1]))
    facts["resistors"] = sorted(rfacts, key=lambda r: -r["p_w"])[:25]

    # capacitors: DC across against rating and DC-bias derating
    cfacts = []
    crows: list[dict] = []
    for e in (x for x in model["elems"] if x["kind"] == "C"):
        vs = [abs(volts(sc, e["nets"][0]) - volts(sc, e["nets"][1]))
              for sc in ops if volts(sc, e["nets"][0]) is not None
              and volts(sc, e["nets"][1]) is not None]
        if not vs:
            continue
        vdc = max(vs)
        part = parts.get(e["ref"])
        rating, rsrc = cap_rating(board["comps"][e["ref"]], part)
        diel = attr(part, r"Temperature Coefficient|Dielectric") or ""
        comp = board["comps"][e["ref"]]
        eff = mlcc.c_eff(e["value"], vdc, footprint=comp["footprint"],
                         value=comp["value"], text=diel,
                         mpn=(part or {}).get("mpn", ""), rated_v=rating)
        cfacts.append({"ref": e["ref"], "vdc": checklib.rnd(vdc, 3),
                       "rating_v": rating, "dielectric": diel or None,
                       "c_nom": e["value"],
                       "c_eff": checklib.rnd(eff["c_eff"], 12),
                       "c_eff_low": checklib.rnd(eff["c_eff_low"], 12),
                       "c_eff_curve": eff["curve"]})
        ends = [n for n in e["nets"] if n not in model["grounds"]]
        crows.append({"ref": e["ref"], "net": e["nets"][0], "vdc": vdc,
                      "rail": ends[0] if len(ends) == 1 else None,
                      "rating_v": rating, "dielectric": diel,
                      "class2": bool(re.match(r"[XYZ]\d[A-Z]", diel.upper())),
                      "c_nom": e["value"], "c_eff": eff["c_eff"],
                      "c_eff_low": eff["c_eff_low"],
                      "fraction": eff["fraction"], "curve": eff["curve"]})
        if rating is None:
            if vdc >= 6.0:
                unknowns.append({"ref": e["ref"], "what": "voltage rating",
                                 "why": f"sits at {vdc:.3g} V DC, rating "
                                        "not in parts.json or value"})
            continue
        if vdc > rating:
            vios.append(_vio("cap_overvoltage", "error", [e["ref"]],
                             e["nets"][0], f"{e['ref']} sees {vdc:.3g} V DC "
                             f"over its {rating:g} V rating ({rsrc})",
                             vdc=vdc, rating_v=rating))
    cap_dc_bias_findings(crows, opts.get("cap_floors") or {}, vios, unknowns)
    facts["capacitors_dc"] = sorted(cfacts, key=lambda r: -r["vdc"])[:25]
    facts["rail_currents_a"] = {
        net: checklib.rnd(-ops["default"]["src"].get(
            f"vr_{model['node'][net].lower()}", 0.0), 6)
        for net in sorted(rails) if model["node"][net] != "0"
        and f"vr_{model['node'][net].lower()}" in ops["default"]["src"]}
    facts["node_volts"] = {
        sc: {n: checklib.rnd(v, 4) for n, v in sorted(o["v"].items())
             if n in o["driven"] and n not in rails and n not in grounds}
        for sc, o in ops.items()}
    return (model, ops, tran, scen, ens, vios, unknowns, facts, version,
            None)


# The generic screen below is lib/mlcc.py's +-25 % class-II curve and a
# half-rating line no datasheet sets, so on its own it is info (#1232).
# What warns is a rail's stated floor - constraints power[].min_cout or a
# regulator's cited `effective: true` pin_requirements row, the lookup
# mlcc_dcbias uses (#1067) - missed on the low curve.
DC_BIAS_SCREEN = 0.5


def cap_dc_bias_findings(rows: list[dict], floors: dict[str, dict],
                         vios: list, unknowns: list) -> None:
    """cap_dc_bias per class-II part. rows: every capacitor with a known DC
    ({ref, net, rail, vdc, rating_v, dielectric, class2,
    c_nom, c_eff, c_eff_low, fraction, curve}); floors: {net: {min_c,
    source}}. Warning only for a part keeping (low curve) less than the
    share of the rail's nominal its floor needs, on a rail whose low-curve
    total misses that floor; a screened part anywhere else is info, and its
    rail, with no floor stated, goes to known_unknowns as not judged."""
    totals: dict[str, dict] = {}
    for r in rows:
        if r["rail"]:
            t = totals.setdefault(r["rail"], {"c_nom": 0.0, "c_eff_low": 0.0})
            t["c_nom"] += r["c_nom"]
            t["c_eff_low"] += r["c_eff_low"]
    unjudged: set[str] = set()
    for r in rows:
        if not r["class2"] or r["rating_v"] is None or \
                r["vdc"] > r["rating_v"]:
            continue
        fl = floors.get(r["rail"]) if r["rail"] else None
        t = totals.get(r["rail"]) if r["rail"] else None
        need = fl["min_c"] / t["c_nom"] if fl and t and t["c_nom"] else None
        keeps_low = r["c_eff_low"] / r["c_nom"] if r["c_nom"] else 1.0
        part = (f"{r['ref']} ({_f(r['c_nom'], 'F')} {r['dielectric']}) at "
                f"{r['vdc']:.3g} V DC is {r['vdc'] / r['rating_v']:.0%} of "
                f"its {r['rating_v']:g} V rating: about "
                f"{_f(r['c_eff'], 'F')} effective "
                f"({_f(r['c_eff_low'], 'F')} low, {r['curve']} curve, "
                "mlcc_dcbias.py)")
        common = dict(vdc=r["vdc"], rating_v=r["rating_v"], c_eff=r["c_eff"],
                      c_eff_low=r["c_eff_low"])
        if need is not None and keeps_low < need and \
                t["c_eff_low"] < fl["min_c"]:
            vios.append(_vio(
                "cap_dc_bias", "warning", [r["ref"]], r["net"],
                part + f"; {r['rail']} holds its {_f(fl['min_c'], 'F')} "
                f"effective floor ({fl['source']}) only if every part keeps "
                f"{need:.0%} of its {_f(t['c_nom'], 'F')} nominal",
                need_fraction=checklib.rnd(need, 4),
                floor_source=fl["source"], **common))
        elif r["vdc"] > DC_BIAS_SCREEN * r["rating_v"] or \
                r["fraction"] < DC_BIAS_SCREEN:
            why = (f"{r['rail']} holds its {_f(fl['min_c'], 'F')} floor "
                   f"({fl['source']})" if fl else
                   "no effective-capacitance floor stated for "
                   f"{r['rail'] or r['net']}: not judged")
            vios.append(_vio(
                "cap_dc_bias", "info", [r["ref"]], r["net"],
                part + f" (generic 50 % screen; {why})", **common))
            if not fl and (r["rail"] or r["net"]) not in unjudged:
                unjudged.add(r["rail"] or r["net"])
                unknowns.append({
                    "ref": r["ref"], "what": "effective-capacitance floor",
                    "why": f"{r['rail'] or r['net']}: no constraints "
                           "power[].min_cout or regulator pin_requirements "
                           "row marked effective, so its DC-bias loss is "
                           "not judged"})


def regulator_findings(board, parts, rails, model, opts, vios, unknowns,
                       facts):
    table = load_vrefs()
    out = []
    pins: dict[str, int] = {}
    for c in board["comps"].values():
        for p in c["pins"].values():
            pins[p["net"]] = pins.get(p["net"], 0) + 1
    for c in board["comps"].values():
        if c["kind"] not in ("U", "IC"):
            continue
        fbs = {p["net"] for p in c["pins"].values()
               if any(FB_FN.match(f) for f in p["fn"])}
        for fb in sorted(fbs):
            if fb in rails:
                continue   # FB tied straight to the output: fixed ratio 1
            legs = [_r_chain(e, fb, model, pins, rails)
                    for e in model["elems"]
                    if e["kind"] == "R" and fb in e["nets"]]
            tops = [g for g in legs if g["end"] not in model["grounds"]]
            bots = [g for g in legs if g["end"] in model["grounds"]]
            if len(tops) != 1 or len(bots) != 1:
                unknowns.append({"ref": c["ref"], "what": f"FB net {fb}",
                                 "why": f"{len(tops)} top / {len(bots)} "
                                        "bottom resistors: not a plain "
                                        "divider, set the Vout by hand"})
                continue
            top, bot = tops[0], bots[0]
            out_net = top["end"]
            vr = find_vref(c, parts.get(c["ref"]), opts["vref"], table)
            row = {"ref": c["ref"], "fb_net": fb, "out_net": out_net,
                   "r_top": top["ref"], "r_bot": bot["ref"],
                   "ratio": checklib.rnd(1 + top["value"] / bot["value"], 5)}
            if vr is None:
                unknowns.append({"ref": c["ref"], "what": "FB reference",
                                 "why": f"{c['value']}: Vref not in "
                                        "regulator_vref.yaml; pass --vref "
                                        f"{c['ref']}=<V> from its datasheet"})
                out.append(row)
                continue
            vref, vtol, vsrc, verified = vr
            rtol = max(r_tolerance(board, parts, r)
                       for r in top["refs"] + bot["refs"])
            fbv = engcalc.fb_vout(vref, top["value"], bot["value"], rtol, vtol)
            wc = fb_wca(vref, vtol, top["value"], bot["value"], rtol)
            row.update({"vref": vref, "vref_source": vsrc,
                        "vref_verified": verified, "r_tol": rtol,
                        "vref_tol": vtol,
                        "vout": {k: checklib.rnd(v, 4) for k, v in fbv.items()},
                        "vout_rss": wc["rss"], "vout_top3": wc["top3"]})
            if not verified:
                unknowns.append({"ref": c["ref"], "what": f"Vref {vref:g} V",
                                 "why": f"{vsrc} - not re-read from the "
                                        "datasheet (regulator_vref.yaml "
                                        "verified: false)"})
            declared = rails.get(out_net)
            if declared and declared[0] > 0:
                d = fbv["nom"] / declared[0] - 1
                row.update({"declared_v": declared[0],
                            "declared_source": declared[1],
                            "delta": checklib.rnd(d, 4)})
                # a constraints voltage is the rail's worst case, not its
                # set point: 5.25 V over a 4.99 V divider is that rail
                worst_ok = declared[1] == "constraints" and \
                    check_ratings.worst_case_fits(fbv, declared[0])
                if abs(d) > 0.03 and not worst_ok:
                    vios.append(_vio(
                        "fb_divider_mismatch",
                        "error" if abs(d) > 0.05 else "warning",
                        [c["ref"], *top["refs"], *bot["refs"]], out_net,
                        f"{c['ref']} FB divider {top['ref']}/{bot['ref']} "
                        f"({_f(top['value'], 'ohm')}/"
                        f"{_f(bot['value'], 'ohm')}, Vref {vref:g} V) sets "
                        f"{fbv['nom']:.3f} V [{fbv['min']:.3f}, "
                        f"{fbv['max']:.3f}], {d:+.1%} off {out_net}'s "
                        f"declared {declared[0]:g} V ({declared[1]})",
                        vout=fbv, declared_v=declared[0]))
            out.append(row)
    facts["regulators_fb"] = out


def _r_chain(e: dict, fb: str, model: dict, pins: dict[str, int],
             rails: dict) -> dict:
    """The resistor leg from FB through `e`: resistors in series are walked
    through any net only two resistor pins touch (no rail, no ground), so
    R_a + R_b to ground reads as one bottom of R_a + R_b."""
    rs = [x for x in model["elems"] if x["kind"] == "R"]
    chain, net, cur = [e], fb, e
    while True:
        net = _other(cur, net)
        if net in model["grounds"] or net in rails or pins.get(net) != 2:
            break
        nxt = [x for x in rs if net in x["nets"] and x is not cur]
        if len(nxt) != 1 or nxt[0] in chain or fb in nxt[0]["nets"]:
            break
        cur = nxt[0]
        chain.append(cur)
    refs = [x["ref"] for x in chain]
    return {"ref": "+".join(refs), "refs": refs, "end": net,
            "value": sum(x["value"] for x in chain)}


def _other(e: dict, net: str) -> str:
    a, b = e["nets"]
    return b if a == net else a


def device_findings(board, parts, rails, model, ops, scen, vios, unknowns,
                    facts, modes=None):
    # LEDs
    leds = []
    for e in (x for x in model["elems"] if x["kind"] == "D"
              and x["type"] == "led"):
        cur = {sc: ops[sc]["i"].get(e["ref"], 0.0) for sc in ops}
        sc_max = max(cur, key=lambda s: cur[s])
        imax = cur[sc_max]
        lim = engcalc.parse_value((attr(parts.get(e["ref"]),
                                        r"Forward Current|Current - Forward|"
                                        r"^If\b") or "").replace("A", ""))
        lim = lim or LED_MAX_A
        leds.append({"ref": e["ref"], "color": e.get("color"),
                     "vf_model": e["vf"], "vf_source": e["vf_source"],
                     "current_a": {k: checklib.rnd(v, 6)
                                   for k, v in cur.items()}})
        if imax > lim:
            vios.append(_vio("led_overcurrent", "warning", [e["ref"]], e["a"],
                             f"{e['ref']} carries {_f(imax, 'A')} "
                             f"({sc_max}), over {_f(lim, 'A')}",
                             current_a=imax, scenario=sc_max))
        elif imax < LED_DARK_A:
            vios.append(_vio("led_dark", "warning", [e["ref"]], e["a"],
                             f"{e['ref']} never passes {_f(imax, 'A')} in any "
                             "scenario (default, every IO low, every IO "
                             "high): dark, or driven by something the sim "
                             "left open", current_a=imax))
        if e["vf_source"] != "parts.json":
            unknowns.append({"ref": e["ref"], "what": "LED Vf",
                             "why": f"{e['vf']:g} V from the "
                                    f"{e['vf_source']}"})
    facts["leds"] = leds

    # GPIO drive currents in the io scenarios, each judged against the
    # driving pin's own IOH (sourcing) or IOL (sinking) where
    # reference/gpio_drive.yaml has it, else a flat GPIO_MAX_A
    rev = {nd: n for n, nd in model["node"].items()}
    drivers = io_drivers(board, rails, model, modes)
    drive_tab = load_gpio_drive()
    assumed: dict[str, set] = {}
    gp = []
    for sc in ("io_low", "io_high", "io_run"):
        for name, i in ops[sc]["src"].items():
            if not name.startswith("vio_"):
                continue
            net = rev.get(name[4:].upper()) or rev.get(name[4:])
            if net is None or abs(i) < 1e-4:
                continue
            gp.append({"net": net, "scenario": sc,
                       "current_a": checklib.rnd(-i, 6)})
            sinks = i > 0
            # the weakest of the pins the sim drives the net from
            lim, src, ref = GPIO_MAX_A, None, None
            for c, _pin, p in drivers.get(net, []):
                lv, ls = gpio_limit(c, p, parts.get(c["ref"]), drive_tab,
                                    sinks)
                if ls is None:
                    assumed.setdefault(c["ref"], set()).add(net)
                if ref is None or lv < lim:
                    lim, src, ref = lv, ls, c["ref"]
            if abs(i) <= lim:
                continue
            io_dir = "IOL" if sinks else "IOH"
            why = (f"past the {_f(lim, 'A')} {io_dir} {ref}'s pin is "
                   f"specified at ({src})" if src else
                   "past the ~20 mA most MCU pins allow - check the pin's "
                   "datasheet limit")
            vios.append(_vio("gpio_overcurrent", "warning",
                             [ref] if ref else [], net,
                             f"{net} {'sinks' if sinks else 'sources'} "
                             f"{_f(abs(i), 'A')} when driven ({sc}): {why}",
                             current_a=i, scenario=sc, limit_a=lim,
                             limit_source=src or "assumed"))
    for ref in sorted(assumed):
        unknowns.append({"ref": ref, "what": "GPIO drive limit",
                         "why": f"assumed {_f(GPIO_MAX_A, 'A')}: no "
                                "reference/gpio_drive.yaml row for its "
                                f"driven pins ({', '.join(sorted(assumed[ref]))})"})
    facts["io_currents"] = sorted(gp, key=lambda r: -abs(r["current_a"]))[:25]

    # IC pins above their own supply (default scenario), or above their
    # own rating where reference/pin_ratings.yaml or parts.json has one
    table = load_pin_ratings()
    for c in board["comps"].values():
        if c["kind"] not in ("U", "IC"):
            continue
        vs = ic_supply(c, rails)
        if not vs:
            continue
        part = parts.get(c["ref"])
        for pin, p in sorted(c["pins"].items()):
            n = p["net"]
            if n in rails or any(POWER_FN.match(f) for f in p["fn"]):
                continue
            v = ops["default"]["v"].get(n) \
                if n in ops["default"]["driven"] else None
            if v is None or v <= vs + 0.3:
                continue
            fns = '/'.join(p['fn']) or '?'
            lim, src = pin_rating(c, p, part, table)
            if lim == "switch_node":
                unknowns.append({"ref": c["ref"], "what": f"pin {pin} "
                                 f"({fns}) at {v:.3g} V",
                                 "why": "a switch-node/bootstrap pin above "
                                        f"the {vs:g} V supply by design: "
                                        "its rating is the datasheet's"})
            elif lim is not None:
                if v > lim:
                    vios.append(_vio("input_above_supply", "warning",
                                     [c["ref"]], n,
                                     f"{c['ref']} pin {pin} ({fns}) sits at "
                                     f"{v:.3g} V on {n}, above its {lim:g} V "
                                     f"rating ({src})", volts=v, supply=vs,
                                     rating=lim))
            else:
                vios.append(_vio("input_above_supply", "warning", [c["ref"]],
                                 n, f"{c['ref']} pin {pin} ({fns}) sits at "
                                 f"{v:.3g} V on {n}, above the part's "
                                 f"{vs:g} V supply + 0.3 V - unless the pin "
                                 "is rated for it (reference/"
                                 "pin_ratings.yaml)", volts=v, supply=vs))

    # MOSFET gate-source stress
    fets = []
    for e in (x for x in model["elems"] if x["kind"] == "Q"
              and x["type"] in ("nmos", "pmos")):
        g, s = e["roles"]["G"], e["roles"]["S"]
        vgs = {sc: ops[sc]["v"][g] - ops[sc]["v"][s] for sc in ops
               if g in ops[sc]["driven"] and s in ops[sc]["driven"]
               and g in ops[sc]["v"] and s in ops[sc]["v"]}
        if not vgs:
            unknowns.append({"ref": e["ref"], "what": "gate drive",
                             "why": "gate has no DC path in any scenario"})
            continue
        sc = max(vgs, key=lambda k: abs(vgs[k]))
        part = parts.get(e.get("comp", e["ref"]))
        rating = engcalc.parse_volts(
            attr(part, r"Gate[- ]?Source Voltage|^V\s*GS\b|Vgs\s*\(max"))
        fets.append({"ref": e["ref"], "type": e["type"],
                     "vgs": {k: checklib.rnd(v, 3) for k, v in vgs.items()},
                     "vgs_rating": rating})
        worst = abs(vgs[sc])
        if rating and worst > rating:
            vios.append(_vio("mosfet_vgs", "error", [e["ref"]], g,
                             f"{e['ref']} |Vgs| reaches {worst:.3g} V "
                             f"({sc}) over its {rating:g} V rating",
                             vgs=vgs[sc], scenario=sc))
        elif not rating:
            unknowns.append({"ref": e["ref"], "what": "Vgs rating",
                             "why": f"|Vgs| reaches {worst:.3g} V ({sc}); "
                                    "no rating in parts.json"})
            if worst > VGS_UNKNOWN_V:
                vios.append(_vio("mosfet_vgs", "warning", [e["ref"]], g,
                                 f"{e['ref']} |Vgs| reaches {worst:.3g} V "
                                 f"({sc}) and its rating is unknown: many "
                                 "FETs stop at 12 V or 20 V",
                                 vgs=vgs[sc], scenario=sc))
        underdrive(e, part, vgs, fets[-1], vios, unknowns)
    facts["mosfets"] = fets


PIN_RATINGS_YAML = SCRIPTS.parent / "reference" / "pin_ratings.yaml"
SWITCH_NODE_FN = re.compile(r"^(HB\d?|HS\d?|HO\d?|BST\d?|BOOT\d?|CBOOT|SW\d?"
                            r"|LX\d?|PH\d?|VSW|SW_\w+)$")
SENSE_IN_FN = re.compile(r"^(IN[+-]|IN[PN]|[+-]IN|VIN[+-]|SENSE[-+PN]|RS[+-]"
                         r"|CS[+-])$")


def load_pin_ratings(path: Path = PIN_RATINGS_YAML) -> list[dict]:
    if not path.is_file():
        return []
    rows = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) \
        .get("parts") or []
    return [{**r, "_m": re.compile(r["match"], re.I),
             "_p": re.compile(r["pins"])} for r in rows]


def _pin_fns(p: dict) -> list[str]:
    return [f.replace("\u2013", "-").replace("\u2212", "-") for f in p["fn"]]


def pin_rating(c: dict, p: dict, part: dict | None, table: list[dict]
               ) -> tuple[float | str | None, str]:
    """(max volts, source) for an IC pin rated above its supply:
    pin_ratings.yaml by MPN and pin name, then a parts.json common-mode
    attribute on a sense input; ('switch_node', ...) for a bootstrap/switch
    pin; (None, '') when nothing says."""
    fns = _pin_fns(p)
    names = [x for x in ((part or {}).get("mpn"), c["value"]) if x]
    for r in table:
        if any(r["_m"].search(x) for x in names) and \
                any(r["_p"].match(f) for f in fns):
            return float(r["max_v"]), ("reference/pin_ratings.yaml: "
                                       f"{r.get('source', '')}"
                                       + ("" if r.get("verified") else
                                          ", not re-read"))
    if any(SENSE_IN_FN.match(f) for f in fns):
        txt = attr(part, r"Common[- ]?Mode") or ""
        vs = [float(x) for x in
              re.findall(r"(-?\d+(?:\.\d+)?)\s*V(?![a-zA-Z])", txt)]
        if vs:
            return max(vs), f"parts.json common-mode {txt}"
    if any(SWITCH_NODE_FN.match(f) for f in fns):
        return "switch_node", ""
    return None, ""


def fet_vth_max(part: dict | None) -> tuple[float | None, str]:
    """(|Vgs(th)| max, source): a max-labelled threshold attribute, else
    the plain Threshold figure (LCSC seldom says min, typ or max), the
    largest magnitude in a range ("1~2.5V")."""
    for pat, src in ((r"(Threshold|Vgs\s*\(th\)).*max", "parts.json max"),
                     (r"Threshold|Vgs\s*\(th\)", "parts.json Threshold "
                      "figure (min/typ/max not stated)")):
        txt = attr(part, pat)
        vs = [abs(float(x)) for x in
              re.findall(r"(-?\d+(?:\.\d+)?)\s*V(?![a-zA-Z])", txt or "")]
        if vs:
            return max(vs), src
    return None, ""


def fet_rdson_vgs(part: dict | None) -> float | None:
    """The lowest |Vgs| the part's RDS(on) is specified at:
    '12.5mΩ@10V、16mΩ@4.5V' -> 4.5."""
    txt = attr(part, r"RDS\s*\(?on|On[- ]?Resistance")
    vs = [abs(float(x)) for x in
          re.findall(r"@\s*(-?\d+(?:\.\d+)?)\s*V(?![a-zA-Z])", txt or "")]
    return min(vs) if vs else None


def underdrive(e, part, vgs, row, vios, unknowns):
    """mosfet_underdrive: in each rail set (nominal, and "@min" when rails
    have a declared minimum) the scenario driving the FET hardest is its
    on state; that |Vgs| against Vgs(th) max (error) and the RDS(on) Vgs
    (warning). The worst rail set is the finding."""
    sign = 1.0 if e["type"] == "nmos" else -1.0
    on: dict[str, tuple[str, float]] = {}
    for sc, v in vgs.items():
        rset = sc.partition("@")[2] or "nominal"
        if rset not in on or sign * v > on[rset][1]:
            on[rset] = (sc, sign * v)
    vth, vth_src = fet_vth_max(part)
    rds_vgs = fet_rdson_vgs(part)
    row.update({"on_vgs": {sc: checklib.rnd(v, 3) for sc, v in on.values()},
                "vth_max": vth, "vth_source": vth_src or None,
                "rdson_vgs": rds_vgs})
    if max(v for _, v in on.values()) < VGS_ON_FLOOR_V:
        unknowns.append({"ref": e["ref"], "what": "on-state drive",
                         "why": "no scenario drives the gate past "
                                f"{VGS_ON_FLOOR_V:g} V: its driver is "
                                "something the sim leaves open"})
        return
    if vth is None:
        unknowns.append({"ref": e["ref"], "what": "Vgs(th)",
                         "why": "no threshold in parts.json: on-state drive "
                                "not judged against it"})
    if rds_vgs is None:
        unknowns.append({"ref": e["ref"], "what": "RDS(on) test Vgs",
                         "why": "no RDS(on)@Vgs in parts.json: on-state "
                                "drive not judged against it"})
    found = None
    for sc, v in sorted(on.values(), key=lambda t: t[1]):
        if v < VGS_ON_FLOOR_V:
            continue    # off in this rail set: the nominal one judges it
        if vth is not None and v < vth:
            found = ("error", sc, v, f"under its {vth:g} V Vgs(th) "
                     f"({vth_src}): it may never turn on")
        elif rds_vgs is not None and v < rds_vgs - VGS_SPEC_TOL_V:
            found = ("warning", sc, v, f"under the {rds_vgs:g} V its "
                     "RDS(on) is specified at: on-resistance and heat are "
                     "past the datasheet's figures")
        if found:
            break
    if found:
        sev, sc, v, why = found
        vios.append(_vio("mosfet_underdrive", sev, [e["ref"]],
                         e["roles"]["G"],
                         f"{e['ref']} is driven on with only |Vgs| "
                         f"{v:.3g} V ({sc}), {why}", vgs=sign * v,
                         scenario=sc, vth_max=vth, rdson_vgs=rds_vgs))


def timing_findings(board, rails, model, ops, tran, ens, opts, vios, facts):
    out = []
    if not tran:
        facts["enables"] = out
        return
    t = tran.get("time") or []
    ramp = opts["ramp_ms"] * 1e-3
    rail_driven = driven_nets(model, rails)
    for en in ens:
        nd = model["node"][en["net"]].lower()
        ys = tran.get(nd)
        if not ys:
            continue
        moves = [abs(ops[sc]["v"].get(en["net"], 0.0)
                     - ops["default"]["v"].get(en["net"], 0.0)) for sc in ops
                 if "@" not in sc]
        if en["net"] not in rail_driven or max(moves) > 0.1:
            out.append({"ref": en["ref"], "net": en["net"], "vdd": en["vdd"],
                        "c_f": en["c"], "judged": False,
                        "final_v_until_driven": checklib.rnd(ys[-1], 4),
                        "why": "an IC pin drives it (it moves when the I/O "
                               "nets are driven); the sim leaves IC pins "
                               "open, so its timing is firmware's"})
            continue
        thr = 0.7 * en["vdd"]
        t_x = next((ti for ti, y in zip(t, ys) if y >= thr), None)
        t_sup = 0.9 * ramp
        row = {"ref": en["ref"], "net": en["net"], "vdd": en["vdd"],
               "c_f": en["c"], "threshold_v": checklib.rnd(thr, 3),
               "final_v": checklib.rnd(ys[-1], 4),
               "t_cross_s": checklib.rnd(t_x, 6) if t_x is not None else None,
               "t_supply90_s": checklib.rnd(t_sup, 6), "ramp_s": ramp}
        out.append(row)
        if t_x is None:
            vios.append(_vio("enable_never_high", "error", [en["ref"]],
                             en["net"], f"{en['ref']} {en['net']} settles at "
                             f"{ys[-1]:.3g} V, never reaching 0.7 x "
                             f"{en['vdd']:g} V = {thr:.3g} V in "
                             f"{t[-1] * 1e3:.3g} ms: the part may never "
                             "come out of enable/reset", final_v=ys[-1]))
        elif t_x < t_sup:
            vios.append(_vio("enable_before_supply", "warning", [en["ref"]],
                             en["net"], f"{en['ref']} {en['net']} crosses "
                             f"{thr:.3g} V at {t_x * 1e3:.3g} ms, before its "
                             f"supply reaches 90 % ({t_sup * 1e3:.3g} ms on a "
                             f"{ramp * 1e3:g} ms ramp): no enable delay",
                             t_cross_s=t_x))
    facts["enables"] = out


def i2c_findings(board, rails, model, opts, vios, unknowns, facts):
    grp = _groups_by_zero_ohm(model)
    members: dict[str, set] = {}
    for n, r in grp.items():
        members.setdefault(r, set()).add(n)
    buses = {}
    modes = opts.get("pin_modes")
    for c in board["comps"].values():
        for pin, p in c["pins"].items():
            r = grp.get(p["net"])
            if r is None or p["net"] in rails:
                continue
            name = check_ratings.base_net(p["net"])
            # an SDA/SCL pin pin_modes marks input/nc is a strap sharing the
            # name (a hub's CFG_SEL0/SCL), not a bus by itself
            by_fn = bool({"SDA", "SCL"} & set(p["fn"])) and \
                pin_role(c, pin, p, modes) != ("off", "pin_modes")
            if I2C_RE.search(name) or by_fn:
                buses.setdefault(r, {"ics": set(), "off": set()})
    # a second pass, so an IC met before the pin that names the bus counts
    for c in board["comps"].values():
        for p in c["pins"].values():
            b = buses.get(grp.get(p["net"]))
            if b is None or p["net"] in rails:
                continue
            if c["kind"] in ("U", "IC"):
                b["ics"].add(c["ref"])
            elif c["kind"] in OFFBOARD_KINDS:
                b["off"].add(c["ref"])
    out, lone = [], []
    for r, b in sorted(buses.items()):
        nets = members[r]
        pulls = [(e, _other(e, n)) for e in model["elems"] if e["kind"] == "R"
                 and e["value"] > 1 for n in nets if n in e["nets"]
                 and _other(e, n) in rails and rails[_other(e, n)][0] > 0]
        label = "/".join(sorted(nets))
        if len(b["ics"]) < 1:
            continue
        # a bus needs a peer: a second IC, or a connector or test point
        # taking it off the board; one IC alone is a port left unused (or
        # strapped) and is not judged
        if len(b["ics"]) < 2 and not b["off"]:
            (ic,) = b["ics"]
            lone.append({"nets": sorted(nets), "ic": ic,
                         "pullups": [e["ref"] for e, _ in pulls]})
            continue
        if not pulls:
            vios.append(_vio("i2c_pullup_missing", "warning", sorted(b["ics"]),
                             label, f"I2C line {label} has no pull-up "
                             "resistor to a rail: internal pull-ups "
                             "(~30-50 k) are too weak for 100 kHz past a few "
                             "pF"))
            continue
        rp = 1 / sum(1 / e["value"] for e, _ in pulls)
        vdd = max(rails[n][0] for _, n in pulls)
        cb = opts["i2c_cb_pf"] or (len(b["ics"]) * engcalc.I2C_PIN_PF
                                   + engcalc.I2C_TRACE_PF)
        calc = engcalc.i2c_pullup(vdd, rp, cb, opts["i2c_khz"])
        row = {"nets": sorted(nets), "pullups": [e["ref"] for e, _ in pulls],
               "rp": checklib.rnd(rp, 1), "vdd": vdd, "cb_pf": cb,
               "cb_assumed": not opts["i2c_cb_pf"], "khz": opts["i2c_khz"],
               **{k: checklib.rnd(v, 2) if isinstance(v, float) else v
                  for k, v in calc.items()}}
        if b["off"]:
            row["off_board"] = sorted(b["off"])
            unknowns.append({"ref": ",".join(sorted(b["off"])),
                             "what": "I2C bus capacitance off the board",
                             "why": f"{label} leaves the board through "
                                    f"{', '.join(sorted(b['off']))}: the "
                                    "cable and far-end devices are not in "
                                    f"the {cb:g} pF counted"})
        out.append(row)
        refs = [e["ref"] for e, _ in pulls]
        if rp < calc["rmin"]:
            vios.append(_vio("i2c_pullup_strong", "error", refs, label,
                             f"{label} pull-up {_f(rp, 'ohm')} is below "
                             f"(VDD-0.4)/3 mA = {_f(calc['rmin'], 'ohm')}: a "
                             "standard device cannot pull it to VOL",
                             rp=rp))
        elif rp > calc["rmax"]:
            vios.append(_vio("i2c_pullup_weak", "warning", refs, label,
                             f"{label} pull-up {_f(rp, 'ohm')} with ~{cb:g} "
                             f"pF rises in {calc['tr_ns']:.0f} ns, over "
                             f"{calc['tr_max_ns']:.0f} ns for "
                             f"{opts['i2c_khz']} kHz", rp=rp))
    if out and not opts["i2c_cb_pf"]:
        unknowns.append({"ref": None, "what": "I2C bus capacitance",
                         "why": f"assumed {engcalc.I2C_PIN_PF:g} pF per IC "
                                f"pin + {engcalc.I2C_TRACE_PF:g} pF trace; "
                                "pass --i2c-cb-pf once the layout is known"})
    facts["i2c"] = out
    facts["i2c_no_peer"] = lone
    for ic in sorted({x["ic"] for x in lone}):
        nets = [n for x in lone if x["ic"] == ic for n in x["nets"]]
        unknowns.append({"ref": ic, "what": "I2C port with no peer",
                         "why": "no I2C peer on the board (no second IC, "
                                "connector or test point): unused or "
                                f"off-board, not judged ({', '.join(nets)})"})


def r_tolerance(board, parts, ref) -> float:
    """A resistor's tolerance from its BOM fields (lib/tolerance.py: the
    parts.json Tolerance attribute, else the value field, else 1%)."""
    c = board["comps"].get(ref) or {}
    p = tolerance.part_tolerance("R", c.get("value", ""),
                                 attr(parts.get(ref), r"^Tolerance"),
                                 nom=1.0)
    return max(1 - p.lo, p.hi - 1)


def fb_wca(vref, vtol, r_top, r_bot, rtol) -> dict:
    """RSS band (tolerance = 3 sigma) and top-3 sensitivities of
    Vout = Vref (1 + Rtop/Rbot); the EVA band is engcalc.fb_vout's closed
    form."""
    g = "gauss"  # WCCA RSS: the tolerance is 3 sigma
    params = {"vref": tolerance.make_param("vref", vref, vtol, dist=g),
              "r_top": tolerance.make_param("r_top", r_top, rtol, dist=g),
              "r_bot": tolerance.make_param("r_bot", r_bot, rtol, dist=g)}
    res = tolerance.analyse(
        params, lambda p: {"vout": p["vref"] * (1 + p["r_top"] / p["r_bot"])},
        analysis="wca")
    m = res["measures"]["vout"]
    return {"rss": {"sigma": checklib.rnd(m["rss_sigma"], 5),
                    "min": checklib.rnd(m["rss_min"], 4),
                    "max": checklib.rnd(m["rss_max"], 4)},
            "top3": [t["param"] for t in res["top3"]["vout"]]}


def element_tol(board, parts, e) -> tolerance.Param | None:
    c = board["comps"].get(e["ref"]) or {}
    return tolerance.part_tolerance(
        e["kind"], c.get("value", ""), attr(parts.get(e["ref"]),
                                            r"^Tolerance"),
        footprint=e.get("footprint") or "", nom=e["value"], name=e["ref"])


def passive_facts(board, rails, model, facts, parts=None,
                  not_judged=None):
    caps_to_gnd: dict[str, list] = {}
    for e in model["elems"]:
        if e["kind"] == "C":
            a, b = e["nets"]
            for x, y in ((a, b), (b, a)):
                if y in model["grounds"]:
                    caps_to_gnd.setdefault(x, []).append(e)
    filters = []
    for e in model["elems"]:
        if e["kind"] != "R" or e["value"] <= 1:
            continue
        for n in e["nets"]:
            src = _other(e, n)
            if n in rails or n in model["grounds"] or n not in caps_to_gnd \
                    or src in model["grounds"]:
                continue
            c = sum(x["value"] for x in caps_to_gnd[n])
            if not c > 0:
                # a 0 F (or unparsed) cap has no corner: not an RC network
                # (#1261)
                continue
            fc = engcalc.rc_corner_hz(e["value"], c)
            row = {"type": "low-pass", "r": e["ref"],
                   "c": [x["ref"] for x in caps_to_gnd[n]],
                   "from": src, "node": n,
                   "tau_s": checklib.rnd(e["value"] * c, 9),
                   "fc_hz": checklib.rnd(fc, 3),
                   "audio_band": 20 <= fc <= 20000}
            rp = element_tol(board, parts or {}, e)
            cps = [element_tol(board, parts or {}, x)
                   for x in caps_to_gnd[n]]
            if rp is not None and all(cps) \
                    and rp.lo > 0 and sum(p.lo for p in cps) > 0:
                # fc falls with R and every C: the corners are R and C all
                # high (min) and all low (max)
                c_lo = sum(p.lo for p in cps)
                c_hi = sum(p.hi for p in cps)
                row["fc_corners_hz"] = [
                    checklib.rnd(engcalc.rc_corner_hz(rp.hi, c_hi), 3),
                    checklib.rnd(engcalc.rc_corner_hz(rp.lo, c_lo), 3)]
                row["tol"] = {"r": rp.source,
                              "c": sorted({p.source for p in cps})}
            filters.append(row)
    facts["rc_networks"] = filters[:60]
    xtals = []
    for c in board["comps"].values():
        if c["kind"] not in ("Y", "X", "XTAL"):
            continue
        sides = [n for n in sorted({p["net"] for p in c["pins"].values()})
                 if n not in model["grounds"] and n in caps_to_gnd]
        if len(sides) == 2:
            c1, c2 = (sum(x["value"] for x in caps_to_gnd[n]) * 1e12
                      for n in sides)
            if not (c1 > 0 and c2 > 0):
                # a 0 F load cap (unfitted or a placeholder value) leaves no
                # load to work out (#1261)
                (not_judged if not_judged is not None else []).append(
                    f"{c['ref']}: crystal load not worked out, a load cap "
                    f"is 0 F")
                continue
            xtals.append({"ref": c["ref"], "value": c["value"],
                          "c1_pf": c1, "c2_pf": c2, "stray_pf_assumed":
                          engcalc.CRYSTAL_STRAY_PF,
                          "cl_pf": checklib.rnd(
                              engcalc.crystal_load_pf(c1, c2), 2)})
    facts["crystals"] = xtals


# ------------------------------------------------------------------- run

def _pairs(items: list[str], what: str) -> dict[str, float]:
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        try:
            out[k.strip()] = float(v)
        except ValueError:
            raise checklib.CheckError(f"{what} {it!r}: want NAME=volts")
        if not sep or not k.strip():
            raise checklib.CheckError(f"{what} {it!r}: want NAME=volts")
    return out


def resolve_inputs(a) -> dict:
    ws = Path(a.workspace) if a.workspace else None
    net = Path(a.net) if a.net else None
    if net is None and ws:
        nets = sorted((ws / "kicad").glob("*.net"))
        if not nets:
            raise checklib.CheckError(f"no kicad/*.net in {ws}: export the "
                                      "netlist (kicad-cli sch export netlist)")
        net = nets[0]
    if net is None or not net.is_file():
        raise checklib.CheckError("give --net <board.net> or --workspace")
    kdir = net.parent
    ws = ws or kdir.parent
    cons = Path(a.constraints) if a.constraints else kdir / "constraints.json"
    parts = Path(a.parts) if a.parts else ws / "parts"
    tree = Path(a.power_tree) if a.power_tree else \
        ws / "architecture" / "power_tree.md"
    return {"net": net, "constraints": cons if cons.is_file() else None,
            "parts": parts if parts.is_dir() else None,
            "power_tree": tree if tree.is_file() else None}


def run(a) -> dict:
    inp = resolve_inputs(a)
    dll = env.find_ngspice_dll()
    if dll is None:
        raise checklib.CheckError(
            "no ngspice shared library found - install KiCad 10 with its "
            "simulator or set HWDE_NGSPICE_DLL")
    board = load_board(inp["net"])
    unmatched: list[dict] = []
    parts = load_parts(inp["parts"], board, unmatched)
    cons = checklib.load_json(inp["constraints"], "constraints") \
        if inp["constraints"] else None
    rails = resolve_rails(board, parts, cons, inp["power_tree"],
                          _pairs(a.rail, "--rail"))
    opts = {"dll": dll, "timeout": a.timeout, "ramp_ms": a.ramp_ms,
            "rail_min": rail_mins(rails, cons,
                                  _pairs(a.rail_min, "--rail-min")),
            "pin_modes": pin_modes(cons, a.pin_mode),
            "io_supply": io_supply_pinned(cons, a.io_supply),
            "vref": _pairs(a.vref, "--vref"), "i2c_khz": a.i2c_khz,
            "i2c_cb_pf": a.i2c_cb_pf,
            "ambient_c": (cons or {}).get("ambient_c")
            if isinstance((cons or {}).get("ambient_c"), (int, float))
            else None}
    import mlcc_dcbias  # noqa: PLC0415 - it imports this module
    opts["cap_floors"] = mlcc_dcbias.floors_for(
        board, parts, cons, {}, mlcc_dcbias.load_pin_requirements())
    (model, ops, tran, scen, ens, vios, unknowns, facts,
     version, sim_error) = analyze(board, parts, rails, opts)
    not_judged = []
    nothing = sim_error if not model["elems"] else None
    if sim_error and not nothing:
        not_judged.append(
            f"operating point and enable transient not simulated "
            f"({sim_error}), so resistor, capacitor, device and timing "
            f"findings are not judged")
    regulator_findings(board, parts, rails, model, opts, vios, unknowns, facts)
    if ops is not None:
        device_findings(board, parts, rails, model, ops, scen, vios, unknowns,
                        facts, opts.get("pin_modes"))
        timing_findings(board, rails, model, ops, tran, ens, opts, vios,
                        facts)
    i2c_findings(board, rails, model, opts, vios, unknowns, facts)
    passive_facts(board, rails, model, facts, parts, not_judged)
    unknowns += [{"ref": "", "what": f"{n} not a rail",
                  "why": "named like a rail but only passives on this "
                         "sheet-local net: left to the network (give "
                         "--rail if it is one)"}
                 for n in sorted(_passive_only_nets(board))
                 if n not in rails and check_ratings.volts_from_name(n)]
    unknowns += [{"ref": "", "what": f"parts.json row {u['mpn']}",
                  "why": u["why"]} for u in unmatched]
    ics = sorted(u["ref"] for u in model["unmodelled"]
                 if u["why"].startswith("IC/"))
    return checklib.report(
        SCRIPT, inp["net"], vios, input={k: str(v) if v else None
                                         for k, v in inp.items()},
        engine={"dll": str(dll), "ngspice_version": version},
        rails={n: {"volts": v, "source": s} for n, (v, s) in
               sorted(rails.items())},
        modelled=len(model["elems"]), left_open=ics,
        known_unknowns=unknowns, not_judged=not_judged,
        nothing_to_judge=nothing, **facts)


def main(argv: list[str] | None = None) -> int:
    checklib.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace")
    ap.add_argument("--net")
    ap.add_argument("--constraints")
    ap.add_argument("--parts", help="parts dir holding parts.json")
    ap.add_argument("--power-tree")
    ap.add_argument("--rail", action="append", default=[],
                    help="NET=volts, adds or overrides a rail")
    ap.add_argument("--rail-min", action="append", default=[],
                    help="NET=volts, the rail's lowest operating voltage")
    ap.add_argument("--pin-mode", action="append", default=[],
                    help="REF.PIN=MODE (io/output, od, analog/input/nc): "
                         "how the io scenarios treat an IC pin")
    ap.add_argument("--io-supply", action="append", default=[],
                    help="REF=PIN (number or function): the supply pin an "
                         "IC's I/O swings to, over reference/io_supply.yaml")
    ap.add_argument("--vref", action="append", default=[],
                    help="REF=volts, a regulator's FB reference")
    ap.add_argument("--i2c-khz", type=int, default=400,
                    choices=sorted(engcalc.I2C_MODES))
    ap.add_argument("--i2c-cb-pf", type=float, default=None)
    ap.add_argument("--ramp-ms", type=float, default=1.0,
                    help="rail ramp time for the enable transient")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--out")
    ap.add_argument("--exec-decks", help=argparse.SUPPRESS)
    ap.add_argument("--dll", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.exec_decks:
        try:
            res = exec_decks(Path(a.exec_decks), Path(a.dll))
        except Exception as exc:  # noqa: BLE001
            res = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(res))
        return 0
    return checklib.cli_wrap(SCRIPT, lambda: (run(a), a.out))


if __name__ == "__main__":
    sys.exit(main())
