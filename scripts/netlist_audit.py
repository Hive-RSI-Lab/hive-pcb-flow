"""netlist_audit.py - netlist vs constraints.json cross-check (SPEC P4).

Audits an exported KiCad netlist (kicadsexpr format) against the design's
constraints.json (and optionally the S7-emitted decoupling.json), catching
schematic-level structural problems BEFORE board work starts:

 - missing_net (error): a net referenced anywhere in constraints.json
   (high_speed[].net + reference nets, power[].net, voltages[].net,
   diff_pairs[].p/.n, thermal[].net) does not exist in the netlist.
 - missing_ref (error): a refdes referenced by constraints.json
   (placement edges/groups/fixed/separation, thermal[].ref) that is not a
   netlist component - a P4 refdes rename/split would otherwise silently
   mute the constraint (place_anneal drops unknown refs; ladder row 86).
   Under --pcb a ref the board marks (attr board_only) - a mounting hole,
   fiducial or logo, never in a schematic - is not missing.
 - diffpair_naming (warning): an explicit diff_pairs entry whose p/n names
   do not follow a recognized differential suffix convention (_P/_N, DP/DM,
   D+/D-; check_diffpair.py's conservative families).
 - diffpair_unpaired (warning): a netlist net carrying a strong differential
   suffix whose partner net is absent. <STEM>_N with an active-low control
   stem (INT, RST, CS, EN, ...) is a single-ended signal, not half a pair.
 - power_no_consumers (warning): a declared power net with no power_in pin
   AND no series escape - a 2-pin member whose far pin leaves the rail to a
   non-ground net (passive loads - LEDs, droppers, fuses - never carry
   pintype power_in; such rails are recorded as passive_fed, not warned).
   A transistor on the rail with a driven control pin (a low-side switch)
   is an escape too, and an IC supply pin the symbol types passive
   (VDD/VCC/AVDD/VCCA/...) is a consumer (supply_pins_typed_passive).
 - pin_no_net (warning): a symbol pin (per the export's own libparts/units
   inventory) that appears in NO net, not even an unconnected-* singleton.
   Hierarchical exports DROP no-connect singleton nets, so these pads reach
   board_init with no net at all and nothing else reports them (row 68).
 - power_undeclared (warning): a net feeding power_in pins that is neither
   declared (power[] / voltages[]) nor ground-named (GND*/;*GND/VSS*/0V),
   nor a tap of a declared rail through a 2-pin R/L/bead (an RC sub-rail),
   nor a regulator's bootstrap net (a BOOT/BS pin and one 2-pin cap to the
   same part's switch pin: it rides on the switch node, no rail to declare).
 - dangling_net (warning): a single-pin net not named unconnected-* - the
   classic label-typo signature (label misspelled on one stub).
 - ground_island (warning): a ground-named net (not chassis/earth/shield)
   with no power pin on it and no R/L/bead/jumper/net tie to another
   ground net - a local label where the ground symbol belonged.
 - pin_net_swap (error): a net plainly named SDA/SCL, D+/D-, CANH/CANL on a
   pin whose name is its partner - the pair crossed at one part.
 - sot23_pinout (warning): a stock Package_TO_SOT_SMD SOT-23 transistor
   whose symbol pins 1-2-3 are not G-S-D (FET) or B-E-C (BJT), the order
   nearly every SOT-23 part has; the datasheet is the last word.
 - input_common_mode (warning): an op-amp or comparator in
   reference/opamp_cm.yaml with an input whose DC level - a rail, or
   solved from the resistors around it to rails of known volts (net name
   or constraints `voltages`; caps open, connectors unplugged, any driven
   pin on the way makes it unknown and skipped) - sits outside the part's
   input common-mode range on its own supply pins. The output is undefined
   there (errata corpus: minuet v3 LMV331, inputs at 3.1 V, range to 2.6 V).
 - opamp_input_no_dc_path (error): an op-amp, comparator or in-amp
   input (Amplifier_Operational/Instrumentation/Comparator symbols,
   opamp_cm.yaml, common op-amp part numbers) whose net reaches, through
   resistors, inductors, beads and jumpers, only capacitors, diodes, test
   points and other inputs: no path for its bias current, so it drifts to
   a rail (an AC-coupled input with no bias resistor).
   opamp_input_dc_via_connector_only (warning) when the only path is a
   connector pin: it floats with the cable out.
 - polarized_cap_reverse (warning): an electrolytic or tantalum (C_Polarized
   / CP symbol or footprint, + on pin 1 or the pin named +) whose + side
   rests more than 0.5 V below its - side, by the input_common_mode solve.
 - below_min_stable_gain (warning): a decompensated op-amp in
   reference/opamp_min_gain.yaml (OPA637, OPA228, an uncompensated NE5534)
   whose noise gain - 1/beta, the share of the output its resistors and
   capacitors feed back to the inverting input, read at GBW / min gain
   with every source zeroed - is under the part's minimum stable gain: a
   follower (1) or an inverting -1 stage (2) around a gain-5 part rings.
 - pin_part_missing: a part reference/pin_requirements.yaml says the IC's
   datasheet requires between two of its pins (CBK from FB_BK to GND_BK)
   that no part of that kind and value spans (error), or, in a
   hierarchical design, that only parts drawn on other sheets span - the
   rail's caps belong to other parts and none was drawn for this pin
   (warning: confirm one sits at the pin on the board).
 - pin_state: a pin reference/pin_states.yaml says the datasheet wants in
   a state its net isn't in (#1033): a reset input left floating, a chip
   select tied to ground instead of driven, a reference input on a net
   above its limit or on the part's own regulator output, a mode pin tied
   the wrong way. Each pin's net is read as open / ground / rail /
   own_supply / pulled_up / pulled_down / driven / external (0 ohm is a
   tie); the row's severity (error unless it says warning).
   A row's rail_pin makes `rail` mean the part's own rail pin's net: an
   expander's ADDR on 3.3 V while its VDD_P is 5 V reads low.
 - mcu_pwm_timer (warning): the PWM inputs of reference/mcu_pins.yaml
   loads (a DRV8874 in PWM mode) driven from one MCU whose pins can't all
   take distinct channels of a single timer (#1032, bedlift-5).
 - mcu_input_only_drive (warning): a chip-select input (~CS, nCS, CS#)
   joined, directly or over 0 ohm, to an MCU pin mcu_pins.yaml lists as
   input-only (#1032, glasgow-10: flash select on the FX2's RDY0).
 - i2c_address_collision (error): two devices on one SCL net whose
   address straps (reference/i2c_addresses.yaml) give the same 7-bit
   address (#1034, glasgow-2). A strap it can't read leaves that device out.
 - pull_vs_leakage: a signal net (0 ohm links followed) pulled to a rail
   of known volts by resistors too weak for the pins on it that
   reference/pin_leakage.yaml says sink or source current before they are
   set up (an unconfigured FPGA's weak pull-downs, input leakage) (#1026).
   The worst-case level is the pulls' Thevenin source less that current
   through their resistance; error when a listed receiver reads it as the
   wrong state, warning when it sits between VIL and VIH (receivers not
   listed: 0.3 / 0.7 of the rail). A net with a push-pull output is skipped.
 - driver_contention (warning): a reset input pin_leakage.yaml lists that
   shares its net with output, bidirectional or tri-state pins of two or
   more other parts (connectors and open-collector pins aside): they can
   fight on the line (#1026).
 - io_overvoltage (error): an output of a reference/io_levels.yaml driver
   whose guaranteed minimum high (its supply rail less voh_drop) reaches,
   directly or through series resistors (any divider to a known rail or
   ground solved), an I/O pin of a listed receiver above its vin_max: a 5 V
   part into a 3.3 V FPGA through dropping resistors only (#1029).
 - power_good_unused (warning): an IC's power-good output (PG, PGOOD,
   PWRGD, POK...) wired to its net but read by nothing there or one series
   resistor on: only resistors, capacitors, LEDs, test points and jumpers.
   Whatever the converter powers starts before its output is in regulation
   (#1030). A PG left unconnected is a choice and is not flagged.
 - metadata_mismatch (error): a decoupling association that contradicts the
   netlist (cap/ic missing, IC pin not on the stated rail, cap not spanning
   rail+gnd); value drift cap-vs-netlist is a warning.

Warnings alone do NOT fail the audit (fp_verify.py precedent): exit 1 only
on error-severity findings. Violations use the S2 normalized schema
(source "audit.netlist") so cluster_violations.py / gates merge them.

Compare mode (--compare OTHER.net): strict electrical-identity diff of two
netlists - same net names, same (ref, pin) memberships. Any difference
(missing/extra net, membership diff, rename) is an error violation
(kind netlist_diff). Used by the S7 acceptance (regenerated schematic must
be electrically identical to the golden) and by regen guards after
generator refactors.

CLI:
  netlist_audit.py --netlist X.net --constraints c.json
                   [--decoupling d.json] [--out report.json]
  netlist_audit.py --sch Y.kicad_sch --constraints c.json   (exports first)
  netlist_audit.py --netlist A.net --compare B.net [--out report.json]
Exit 0/1/2 per SPEC section 6.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

import sexpdata

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))

import checklib  # noqa: E402
import engcalc  # noqa: E402

SCRIPT = "netlist_audit"
SOURCE = "audit.netlist"
PIN_REQUIREMENTS = HERE.parent / "reference" / "pin_requirements.yaml"
PIN_STATES = HERE.parent / "reference" / "pin_states.yaml"
MCU_PINS = HERE.parent / "reference" / "mcu_pins.yaml"
I2C_ADDRESSES = HERE.parent / "reference" / "i2c_addresses.yaml"
PIN_LEAKAGE = HERE.parent / "reference" / "pin_leakage.yaml"
IO_LEVELS = HERE.parent / "reference" / "io_levels.yaml"

# Strong differential suffix families (subset of check_diffpair.SUFFIX_PAIRS:
# the bare +/- and P/N tokens are too ambiguous for unpaired-net warnings).
STRONG_SUFFIXES = [("_P", "_N"), ("DP", "DM"), ("D+", "D-")]

_GROUND_RE = re.compile(r"(^GND|GND$|^VSS|^0V$)")


# ------------------------------------------------------------ netlist parse

def _kids(node, key):
    return [x for x in node if isinstance(x, list) and x
            and str(x[0]) == key]


def _atom(node, key, default=None):
    for x in node:
        if isinstance(x, list) and x and str(x[0]) == key:
            return str(x[1]) if len(x) > 1 else default
    return default


def _pin_name(pin: str | None, func: str) -> str:
    """The pin's own name: kicad-cli writes a hidden or doubled name as
    '<name>_<number>' (S_3 for pin 3 named S)."""
    suffix = f"_{pin}"
    return func[:-len(suffix)] if pin and func.endswith(suffix) else func


def parse_netlist(path: Path | str) -> dict:
    """kicadsexpr netlist -> {"components": {ref: {value, footprint,
    libsource?, unit_pins?}}, "nets": {name: [{ref, pin, pintype}]},
    "libparts": {(lib, part): set(pin numbers)}}. Multiline or single-line
    formatting both parse (sexpdata, not regex - kicad-cli 10 pretty-prints
    netlists). unit_pins (kicad 10 per-comp units block) is the placed
    instance's own pin inventory; libparts is the shared per-symbol union
    (all units) used as the fallback."""
    p = Path(path)
    try:
        with open(p, encoding="utf-8") as fh:
            data = sexpdata.load(fh)
    except OSError as exc:
        raise checklib.CheckError(f"cannot read netlist {p}: {exc}") from exc
    except Exception as exc:
        raise checklib.CheckError(f"netlist {p} does not parse: {exc}") from exc
    if not isinstance(data, list) or not data or str(data[0]) != "export":
        raise checklib.CheckError(f"netlist {p}: not a kicadsexpr export")

    def _pin_nums(node) -> set[str]:
        out = set()
        for pblk in _kids(node, "pins"):
            for pin in _kids(pblk, "pin"):
                num = _atom(pin, "num")
                if num:
                    out.add(num)
        return out

    comps: dict[str, dict] = {}
    for blk in _kids(data, "components"):
        for c in _kids(blk, "comp"):
            ref = _atom(c, "ref")
            if not ref:
                continue
            comps[ref] = {"value": _atom(c, "value"),
                          "footprint": _atom(c, "footprint")}
            sp = _kids(c, "sheetpath")
            if sp and _atom(sp[0], "names"):
                comps[ref]["sheet"] = _atom(sp[0], "names")
            src = _kids(c, "libsource")
            if src:
                comps[ref]["libsource"] = (_atom(src[0], "lib") or "",
                                           _atom(src[0], "part") or "")
            unit_pins: set[str] = set()
            for ub in _kids(c, "units"):
                for u in _kids(ub, "unit"):
                    unit_pins |= _pin_nums(u)
            if unit_pins:
                comps[ref]["unit_pins"] = sorted(unit_pins)
    libparts: dict[tuple, set] = {}
    for blk in _kids(data, "libparts"):
        for lp in _kids(blk, "libpart"):
            key = (_atom(lp, "lib") or "", _atom(lp, "part") or "")
            libparts.setdefault(key, set()).update(_pin_nums(lp))
    nets: dict[str, list] = {}
    for blk in _kids(data, "nets"):
        for n in _kids(blk, "net"):
            name = _atom(n, "name")
            if name is None:
                continue
            nets[name] = [{"ref": _atom(nd, "ref"), "pin": _atom(nd, "pin"),
                           "pintype": _atom(nd, "pintype", ""),
                           "pinfunction": _pin_name(_atom(nd, "pin"),
                                                    _atom(nd, "pinfunction", ""))}
                          for nd in _kids(n, "node")]
    return {"components": comps, "nets": nets, "libparts": libparts}


def _memberships(parsed: dict) -> dict[str, tuple]:
    return {name: tuple(sorted((m["ref"], m["pin"]) for m in members))
            for name, members in parsed["nets"].items()}


def _base_type(pintype: str) -> str:
    return (pintype or "").split("+", 1)[0]


def _is_ground_name(net: str) -> bool:
    return bool(_GROUND_RE.search(net.lstrip("/").upper()))


# ground-named nets that are meant to sit apart (chassis, earth, shields)
_GROUND_EXEMPT_RE = re.compile(r"CHASSIS|EARTH|SHIELD|SHLD|PE$|FG$")
# two-terminal parts that join two grounds (net ties, 0R, beads, jumpers)
_BRIDGE_REF_RE = re.compile(r"^(R|L|FB|NT|JP|SB|J?P)\d")


def _ground_bridges(net: str, members: list[dict],
                    comp_pins: dict[str, dict[str, str]]) -> list[str]:
    """Members that tie this ground net to another ground net: a 2-pin
    R/L/FB/NT/JP whose far pin is ground-named, or any part with a pin on
    another ground net and a net-tie footprint."""
    out = []
    for m in members:
        pins = comp_pins.get(m["ref"], {})
        far = [n for p, n in pins.items() if p != m["pin"] and n != net]
        if any(_is_ground_name(n) for n in far) and (
                _BRIDGE_REF_RE.match(m["ref"] or "") or len(pins) == 2
                and not (m["ref"] or "").startswith("C")):
            out.append(m["ref"])
    return out


# bus signals that never cross: the net's name and the pin's name agree
_BUS_PARTNER = {"SDA": "SCL", "SCL": "SDA", "DP": "DM", "DM": "DP",
                "CANH": "CANL", "CANL": "CANH"}
_BUS_ALIASES = {"D+": "DP", "D-": "DM", "USB_DP": "DP", "USB_DM": "DM",
                "USB_D+": "DP", "USB_D-": "DM", "CAN_H": "CANH",
                "CAN_L": "CANL", "I2C_SDA": "SDA", "I2C_SCL": "SCL"}


def _bus_token(name: str) -> str | None:
    """SDA/SCL/DP/DM/CANH/CANL when a net or pin name is plainly that
    signal: the whole name or its last /._- separated part (CTL.SDA,
    /Audio/I2C_SCL, USB_D+), else None."""
    if not name:
        return None
    tail = re.split(r"[/.]", name.upper())[-1]
    for cand in (tail, tail.rsplit("_", 1)[-1]):
        cand = _BUS_ALIASES.get(cand, cand)
        if cand in _BUS_PARTNER:
            return cand
    return None


_STOCK_SOT23_RE = re.compile(r"^Package_TO_SOT_SMD:SOT-23(-3)?(_|$)")
_SOT23_ORDERS = (("G", "S", "D"), ("B", "E", "C"))


def _transistor_role(func: str | None) -> str | None:
    f = (func or "").upper()
    return {"G": "G", "GATE": "G", "S": "S", "SOURCE": "S", "D": "D",
            "DRAIN": "D", "B": "B", "BASE": "B", "E": "E", "EMITTER": "E",
            "C": "C", "COLLECTOR": "C"}.get(f)


def load_pin_requirements(path: Path = PIN_REQUIREMENTS) -> list[dict]:
    """reference/pin_requirements.yaml's parts (its header says the form)."""
    import yaml  # noqa: PLC0415
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"pin requirements {path}: {exc}") from exc
    return doc.get("parts") or []


def _norm_pin(name: str | None) -> str:
    return re.sub(r"~\{([^}]*)\}", r"\1", name or "").strip().upper()


_KIND_PREFIX = {"C": "C", "L": "L", "FB": "L", "R": "R"}


def _part_kind(ref: str) -> str | None:
    m = re.match(r"^([A-Z]+)\d", ref or "")
    return _KIND_PREFIX.get(m.group(1)) if m else None


def _req_window(req: dict) -> tuple[float | None, float | None]:
    """(min, max) a part's value must sit in: min/max as given, else one
    E3 step either side of the nominal value, else no bound."""
    lo, hi = req.get("min"), req.get("max")
    nom = engcalc.parse_value(req.get("value"))
    if nom and lo is None and hi is None:
        lo, hi = nom / 2.2, nom * 2.2
    return (float(lo) if lo is not None else None,
            float(hi) if hi is not None else None)


def _pin_requirements(nets: dict, comps: dict,
                      comp_pins: dict[str, dict[str, str]],
                      specs: list[dict]):
    """Yield (severity, net, ic, req, found, elsewhere, off_value) per
    datasheet-required pin part that is missing or not drawn with its IC."""
    hierarchical = len({c.get("sheet") for c in comps.values()} - {None}) > 1
    for spec in specs:
        rx = re.compile(spec["match"], re.I)
        aliases = {k.upper(): v.upper()
                   for k, v in (spec.get("aliases") or {}).items()}
        for ref in sorted(comps):
            comp = comps[ref]
            if not (rx.search(comp.get("value") or "")
                    or rx.search((comp.get("libsource") or ("", ""))[1])):
                continue
            by_pin: dict[str, set[str]] = {}
            for name, members in nets.items():
                for m in members:
                    if m["ref"] == ref:
                        pn = _norm_pin(m.get("pinfunction"))
                        by_pin.setdefault(aliases.get(pn, pn), set()).add(name)
            for req in spec.get("requires") or []:
                a = by_pin.get(req["pin"].upper())
                b = by_pin.get(req["to"].upper())
                if a is None or b is None:
                    continue  # this symbol has no such pin
                a = {n for n in a if not n.startswith("unconnected-")}
                b = {n for n in b if not n.startswith("unconnected-")}
                if not a and req.get("when") == "connected":
                    continue
                if a & b:
                    continue  # the two pins are one net: nothing to span
                lo, hi = _req_window(req)
                found, off = [], []
                for other, pins in sorted(comp_pins.items()):
                    if other == ref or _part_kind(other) not in req["kinds"]:
                        continue
                    if not (any(n in a for n in pins.values())
                            and any(n in b for n in pins.values())):
                        continue
                    val = engcalc.parse_value(comps.get(other, {}).get("value"))
                    if (lo is not None or hi is not None) and (
                            val is None or (lo is not None and val < lo * 0.999)
                            or (hi is not None and val > hi * 1.001)):
                        off.append(other)
                        continue
                    found.append(other)
                net = sorted(a)[0] if a else None
                if not found:
                    yield "error", net, ref, req, [], [], off
                    continue
                sheet = comp.get("sheet")
                if hierarchical and sheet and not any(
                        comps.get(f, {}).get("sheet") == sheet for f in found):
                    yield "warning", net, ref, req, [], found, off


# ------------------------------------------------------------ compare mode

# ------------------------------------------------------------ input common mode

OPAMP_CM_YAML = HERE.parent / "reference" / "opamp_cm.yaml"
# an amplifier input pin's name: + / -, IN+ / -IN, INA- / +IN2, NI / INV
_AMP_IN_RE = re.compile(r"^(?:[+-]|[+-]IN[A-D1-4]?|IN[A-D1-4]?[+-]|"
                        r"[+-]IN_?[A-D1-4]|IN_?[A-D1-4][+-]|NI|INV)$")
# its supply pins, when the symbol types them passive (Amplifier_Operational
# symbols with every pin passive): V+ / V-, VCC / VEE, VDD / VSS, GND
_AMP_SUPPLY_RE = re.compile(r"^(?:V[+-]|VS[+-]|VCC|VDD|VEE|VSS|GND)$")
_CONNECTOR_RE = re.compile(r"^(J|P|CN|X|CON)\d")
_OPEN_DC_RE = re.compile(r"^(C|TP)\d")
_DC_CLUSTER_MAX = 40


def _cm_table(path: Path = OPAMP_CM_YAML) -> list[dict]:
    import yaml  # noqa: PLC0415
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["parts"]
    for r in rows:
        r["_re"] = re.compile(r["match"], re.I)
    return rows


def _cm_row(comp: dict, table: list[dict]) -> dict | None:
    names = [comp.get("value") or "", (comp.get("libsource") or ("", ""))[1]]
    for r in table:
        if any(r["_re"].search(n.strip()) for n in names if n):
            return r
    return None


def load_pin_states(path: Path = PIN_STATES) -> list[dict]:
    """reference/pin_states.yaml's parts (its header says the form)."""
    import yaml  # noqa: PLC0415
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"pin states {path}: {exc}") from exc
    return doc.get("parts") or []


_DRIVE_TYPES = {"output", "bidirectional", "tri_state", "open_collector",
                "open_emitter"}
_POWER_TYPES = {"power_in", "power_out"}


def _spec_matches(spec: dict, comp: dict) -> bool:
    rx = re.compile(spec["match"], re.I)
    return bool(rx.search(comp.get("value") or "")
                or rx.search((comp.get("libsource") or ("", ""))[1]))


def _supply_net(name: str, members: list[dict], rails: dict) -> bool:
    return name in rails or any(_base_type(m.get("pintype")) in _POWER_TYPES
                                for m in members)


def _net_states(net: str, ref: str, pin: str, nets: dict, comps: dict,
                comp_pins: dict[str, dict[str, str]], rails: dict,
                own_volts: dict[str, float], _hop: int = 0
                ) -> tuple[set[str], float | None, list[str]]:
    """The states (reference/pin_states.yaml's header) of the net REF.PIN
    sits on, the net's volts when known, and the refs that make them."""
    members = nets.get(net, [])
    others = [m for m in members if not (m["ref"] == ref and m["pin"] == pin)]
    if net.startswith("unconnected-") or not others:
        return {"open"}, None, []
    states: set[str] = set()
    volts = rails.get(net)
    why: list[str] = []
    if _is_ground_name(net):
        states.add("ground")
        volts = 0.0
    elif _supply_net(net, members, rails):
        states.add("rail")
    supply = bool(states)  # on a ground or rail, a pin there drives nothing
    for m in others:
        r, bt = m["ref"], _base_type(m.get("pintype"))
        if r == ref and bt == "power_out":
            states.update({"own_supply", "rail"})
            pv = own_volts.get(_norm_pin(m.get("pinfunction")))
            if pv is not None:
                volts = pv
            why.append(f"{r}.{m['pin']} ({m.get('pinfunction') or '?'})")
        elif supply:
            continue
        elif r != ref and bt in _DRIVE_TYPES:
            states.add("driven")
            why.append(f"{r}.{m['pin']}")
        elif re.match(r"^(J|P|CN|X)\d", r or ""):
            states.add("external")
            why.append(r)
    # a resistor to ground or a rail: 0 ohm is a tie, anything else a pull
    for m in others if not supply else ():
        if _part_kind(m["ref"]) != "R":
            continue
        pins = comp_pins.get(m["ref"], {})
        if len(pins) != 2:
            continue
        far = next((n for p, n in pins.items() if p != m["pin"]), None)
        if not far or far == net:
            continue
        ohm = engcalc.parse_value(comps.get(m["ref"], {}).get("value"))
        fm = nets.get(far, [])
        if ohm == 0 and _hop < 2:
            fs, fv, _ = _net_states(far, m["ref"], next(
                p for p in pins if p != m["pin"]), nets, comps, comp_pins,
                rails, own_volts, _hop + 1)
            got = fs & {"ground", "rail", "driven", "external"}
            if got:
                states |= got
                volts = fv if volts is None else volts
                why.append(m["ref"])
        elif _is_ground_name(far):
            states.add("pulled_down")
            why.append(m["ref"])
        elif _supply_net(far, fm, rails):
            states.add("pulled_up")
            why.append(m["ref"])
        elif not states:
            why.append(f"{m['ref']} to {far}")
    return states, volts, why


def _pin_states(nets: dict, comps: dict,
                comp_pins: dict[str, dict[str, str]], rails: dict,
                specs: list[dict]):
    """Yield (rule, spec, ref, pin, net, states, volts, why, problem) per
    datasheet pin-state rule the netlist breaks."""
    for spec in specs:
        aliases = {k.upper(): v.upper()
                   for k, v in (spec.get("aliases") or {}).items()}
        own = {k.upper(): float(v)
               for k, v in (spec.get("supply_volts") or {}).items()}
        for ref in sorted(comps):
            if not _spec_matches(spec, comps[ref]):
                continue
            by_pin: dict[str, list[tuple[str, str]]] = {}
            for pnum, name in comp_pins.get(ref, {}).items():
                func = next((m.get("pinfunction") for m in nets.get(name, [])
                             if m["ref"] == ref and m["pin"] == pnum), "")
                pn = _norm_pin(func)
                by_pin.setdefault(aliases.get(pn, pn), []).append(
                    (pnum, name))
            for rule in spec.get("pins") or []:
                for pnum, name in by_pin.get(rule["pin"].upper(), []):
                    st, volts, why = _net_states(
                        name, ref, pnum, nets, comps, comp_pins, rails, own)
                    allow, forbid = (set(rule.get("allow") or ()),
                                     set(rule.get("forbid") or ()))
                    problem = None
                    if forbid & st:
                        problem = f"is {'/'.join(sorted(forbid & st))}"
                    elif allow and not allow & st:
                        problem = (f"is {'/'.join(sorted(st))}, "
                                   if st else "reaches no ground, rail, "
                                   "pull or driver, ") + (
                            f"not {' or '.join(sorted(allow))}")
                    elif (rule.get("max_v") is not None and volts is not None
                          and volts > float(rule["max_v"]) + 1e-6):
                        problem = (f"sits at {volts:g} V, above its "
                                   f"{float(rule['max_v']):g} V limit")
                    elif (rule.get("rail_pin") and "rail" in st
                          and "ground" not in st):
                        want = sorted({n for _, n in by_pin.get(
                            rule["rail_pin"].upper(), [])})
                        if want and name not in want:
                            problem = (f"is on a rail, not {rule['rail_pin']}"
                                       f"'s '{want[0]}'")
                    if problem:
                        yield (rule, spec, ref, pnum, name, st, volts, why,
                               problem)


def _load_yaml(path: Path, what: str) -> dict:
    import yaml  # noqa: PLC0415
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"{what} {path}: {exc}") from exc


def load_mcu_pins(path: Path = MCU_PINS) -> dict:
    """reference/mcu_pins.yaml (its header says the form)."""
    return _load_yaml(path, "mcu pins")


def load_i2c_addresses(path: Path = I2C_ADDRESSES) -> list[dict]:
    """reference/i2c_addresses.yaml's devices (its header says the form)."""
    return _load_yaml(path, "i2c addresses").get("devices") or []


def _pin_base(func: str | None) -> str:
    """A pin's first name: PA14-BOOT0 is PA14, RDY0/SLRD is RDY0."""
    return re.split(r"[-/]", _norm_pin(func))[0]


def _mcu_row(comp: dict, rows: list[dict]) -> dict | None:
    """The mcu_pins.yaml row for COMP, by its Value first: a C051 drawn
    with a C031 symbol is a C051."""
    for field in ((comp.get("value") or ""),
                  (comp.get("libsource") or ("", ""))[1]):
        for row in rows:
            if re.search(row["match"], field, re.I):
                return row
    return None


def _pin_funcs(ref: str, nets: dict, comp_pins: dict[str, dict[str, str]]
               ) -> dict[str, tuple[str, str]]:
    """REF's pins as {pinfunction: (pin number, net)}."""
    out = {}
    for pnum, name in comp_pins.get(ref, {}).items():
        func = next((m.get("pinfunction") for m in nets.get(name, [])
                     if m["ref"] == ref and m["pin"] == pnum), "")
        out[func or ""] = (pnum, name)
    return out


def _zero_ohm_reach(net: str, nets: dict, comps: dict,
                    comp_pins: dict[str, dict[str, str]]) -> list[str]:
    """NET and the nets one 0 ohm 2-pin resistor away."""
    out = [net]
    for m in nets.get(net, []):
        if _part_kind(m["ref"]) != "R":
            continue
        pins = comp_pins.get(m["ref"], {})
        if (len(pins) == 2 and engcalc.parse_value(
                comps.get(m["ref"], {}).get("value")) == 0):
            far = next((n for p, n in pins.items() if p != m["pin"]), None)
            if far and far not in out:
                out.append(far)
    return out


_TIM_RE = re.compile(r"^(TIM\d+)_CH(\d)N?$")


def _one_timer(inputs: list[tuple[str, list[str]]]) -> str | None:
    """A timer with a distinct channel for every input (CHx and CHxN share
    channel x), or None. INPUTS: (label, [TIMn_CHx...])."""
    opts = []
    for _, funcs in inputs:
        o: dict[str, set[int]] = {}
        for f in funcs:
            m = _TIM_RE.match(f.upper())
            if m:
                o.setdefault(m.group(1), set()).add(int(m.group(2)))
        opts.append(o)
    timers = set.intersection(*(set(o) for o in opts)) if opts else set()
    for tim in sorted(timers, key=lambda t: int(t[3:])):
        match: dict[int, int] = {}

        def place(i, seen, tim=tim, match=match):
            for ch in opts[i][tim]:
                if ch in seen:
                    continue
                seen.add(ch)
                if ch not in match or place(match[ch], seen):
                    match[ch] = i
                    return True
            return False
        if all(place(i, set()) for i in range(len(opts))):
            return tim
    return None


def _mcu_pwm_groups(nets: dict, comps: dict,
                    comp_pins: dict[str, dict[str, str]], rails: dict,
                    table: dict) -> tuple[list[tuple], int]:
    """([(row, mcu_ref, mcu_row, inputs)], groups checked): each group of
    PWM inputs (one load row's inputs fed from one MCU) that no single
    timer can drive, each input (load_ref, load_pin, mcu_pin,
    [TIMn_CHx...])."""
    mcus = table.get("mcus") or []
    found: list[tuple] = []
    checked = 0
    for row in table.get("loads") or []:
        aliases = {_norm_pin(k): v.upper()
                   for k, v in (row.get("aliases") or {}).items()}
        want = {p.upper() for p in row.get("pwm_group") or ()}
        when = row.get("when") or {}
        groups: dict[str, list] = {}
        for ref in sorted(comps):
            if not _spec_matches(row, comps[ref]):
                continue
            funcs = {aliases.get(_norm_pin(f), _norm_pin(f)): pn
                     for f, pn in _pin_funcs(ref, nets, comp_pins).items()}
            if when:
                at = funcs.get(when["pin"].upper())
                if not at:
                    continue
                st, _, _ = _net_states(at[1], ref, at[0], nets, comps,
                                       comp_pins, rails, {})
                if not st & set(when.get("states") or ()):
                    continue
            for pin in sorted(want & set(funcs)):
                for net in _zero_ohm_reach(funcs[pin][1], nets, comps,
                                           comp_pins):
                    for m in nets.get(net, []):
                        mrow = (_mcu_row(comps.get(m["ref"], {}), mcus)
                                if m["ref"] != ref else None)
                        if not mrow or "timers" not in mrow:
                            continue
                        mp = _pin_base(m.get("pinfunction"))
                        groups.setdefault(m["ref"], [mrow, []])[1].append(
                            (ref, pin, mp, list(
                                (mrow["timers"] or {}).get(mp) or [])))
        for mref, (mrow, inputs) in sorted(groups.items()):
            checked += 1
            if len(inputs) < 2 and all(i[3] for i in inputs):
                continue
            if _one_timer([(i[2], i[3]) for i in inputs]) is None:
                found.append((row, mref, mrow, inputs))
    return found, checked


_CS_RE = re.compile(r"^(N|X)?(CS|SS|SCS|CE)[#N]?$")


def _input_only_selects(nets: dict, comps: dict,
                        comp_pins: dict[str, dict[str, str]], table: dict):
    """Yield (mcu_row, mcu_ref, mcu_pin, pin_net, sel_ref, sel_pin, sel_net)
    per chip-select input wired (directly or over a 0 ohm link) to a pin
    the MCU can only read."""
    for row in table.get("mcus") or []:
        only = {p.upper() for p in row.get("input_only") or ()}
        if not only:
            continue
        for ref in sorted(comps):
            if _mcu_row(comps[ref], table.get("mcus") or []) is not row:
                continue
            for func, (pnum, net) in sorted(
                    _pin_funcs(ref, nets, comp_pins).items()):
                if _pin_base(func) not in only:
                    continue
                for reach in _zero_ohm_reach(net, nets, comps, comp_pins):
                    for m in nets.get(reach, []):
                        if (m["ref"] != ref and _part_kind(m["ref"]) is None
                                and _CS_RE.match(_pin_base(
                                    m.get("pinfunction")))
                                and _base_type(m.get("pintype"))
                                in ("input", "passive")):
                            yield (row, ref, _pin_base(func), net, m["ref"],
                                   _norm_pin(m.get("pinfunction")), reach)


def _i2c_buses(nets: dict, comps: dict,
               comp_pins: dict[str, dict[str, str]], rails: dict,
               devices: list[dict]) -> dict[str, list[tuple]]:
    """{SCL net: [(address, ref, row, strap)]} for every device in
    i2c_addresses.yaml whose strap reads plainly."""
    buses: dict[str, list[tuple]] = {}
    for row in devices:
        for ref in sorted(comps):
            if not _spec_matches(row, comps[ref]):
                continue
            funcs: dict[str, tuple[str, str]] = {}
            for f, pn in _pin_funcs(ref, nets, comp_pins).items():
                funcs.setdefault(_pin_base(f), pn)
            scl, sda = funcs.get("SCL"), funcs.get("SDA")
            strap = funcs.get(row["pin"].upper())
            if not scl or not strap:
                continue
            sup = funcs.get((row.get("supply") or "").upper())
            snet = strap[1]
            adr = row["addresses"]
            where = None
            if snet == scl[1]:
                where = "scl"
            elif sda and snet == sda[1]:
                where = "sda"
            else:
                st, _, _ = _net_states(snet, ref, strap[0], nets, comps,
                                       comp_pins, rails, {})
                if st & {"ground", "pulled_down"} and not st & {
                        "rail", "pulled_up", "driven"}:
                    where = "ground"
                elif sup and snet == sup[1] and "supply" in adr:
                    where = "supply"
                elif st & {"rail", "pulled_up"} and not st & {
                        "ground", "pulled_down", "driven"}:
                    where = "high" if "high" in adr else "supply"
            if where in adr:
                buses.setdefault(scl[1], []).append(
                    (int(adr[where]), ref, row, where))
    return buses


def _rail_volts(constraints: dict, nets: dict) -> dict[str, float]:
    """Volts of every net whose voltage hwde can name without a solve:
    constraints `voltages`, then the net's name (GND = 0, +3.3V, 3V3)."""
    from check_ratings import volts_from_name  # noqa: PLC0415
    out = {}
    for name in nets:
        v = volts_from_name(name)
        if v is not None:
            out[name] = v
    for ent in constraints.get("voltages", []) or []:
        if ent.get("net") in nets and isinstance(ent.get("voltage"),
                                                 (int, float)):
            out[ent["net"]] = float(ent["voltage"])
    return out



def load_pin_leakage(path: Path = PIN_LEAKAGE) -> list[dict]:
    """reference/pin_leakage.yaml's parts (its header says the form)."""
    import yaml  # noqa: PLC0415
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"pin leakage {path}: {exc}") from exc
    return doc.get("parts") or []


def _leak_row(specs: list[dict], comp: dict, m: dict
              ) -> tuple[dict, dict] | None:
    """(spec, pin row) of reference/pin_leakage.yaml for net member M."""
    if _base_type(m.get("pintype")) in _POWER_TYPES:
        return None
    func = _norm_pin(m.get("pinfunction")).split("/", 1)[0]
    for spec in specs:
        if not _spec_matches(spec, comp):
            continue
        for row in spec.get("pins") or []:
            rx = re.compile(row["pin"], re.I)
            if (func and rx.search(func)) or rx.search(str(m["pin"])):
                return spec, row
    return None


def _zero_ohm_groups(nets: dict, comps: dict,
                     comp_pins: dict[str, dict[str, str]]
                     ) -> list[list[str]]:
    """Nets joined by 0 ohm resistors, one list per group (singletons
    included), each sorted."""
    parent = {n: n for n in nets}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    for ref, pins in comp_pins.items():
        if _part_kind(ref) != "R" or len(pins) != 2:
            continue
        if engcalc.parse_value(comps.get(ref, {}).get("value")) != 0:
            continue
        a, b = pins.values()
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for n in nets:
        groups.setdefault(find(n), []).append(n)
    return [sorted(g) for g in groups.values()]


def _pull_vs_leakage(nets: dict, comps: dict,
                     comp_pins: dict[str, dict[str, str]],
                     rails: dict[str, float], specs: list[dict]):
    """Yield one dict per signal net (0 ohm groups as one) that has a pull
    to a rail of known volts and a pin listed in reference/pin_leakage.yaml
    sinking or sourcing current on it, with the worst-case level, the
    receivers' thresholds and the verdict ('error' when the level reads as
    the wrong state, 'warning' when it sits between VIL and VIH, None when
    clean). Nets with a push-pull output on them are skipped: the driver
    sets the level, not the pull."""
    for group in _zero_ohm_groups(nets, comps, comp_pins):
        gset = set(group)
        members = [(n, m) for n in group for m in nets[n]]
        if any(n.startswith("unconnected-") or _is_ground_name(n)
               or _supply_net(n, nets[n], rails) for n in group):
            continue
        if any(_base_type(m.get("pintype")) == "output"
               and _leak_row(specs, comps.get(m["ref"], {}), m) is None
               for _, m in members):
            continue
        pulls: list[tuple[float, float, str]] = []   # (volts, ohms, why)
        rows: list[tuple[str, str, dict, dict]] = []  # ref, pin, spec, row
        for n, m in members:
            ref = m["ref"] or ""
            pins = comp_pins.get(ref, {})
            if _part_kind(ref) == "R" and len(pins) == 2:
                far = next((nt for p, nt in pins.items() if p != m["pin"]),
                           None)
                if not far or far in gset:
                    continue
                ohm = engcalc.parse_value(comps.get(ref, {}).get("value"))
                fv = 0.0 if _is_ground_name(far) else rails.get(far)
                if ohm and fv is not None:
                    pulls.append((fv, ohm, f"{ref} "
                                  f"{comps[ref].get('value')} to {far}"))
                continue
            hit = _leak_row(specs, comps.get(ref, {}), m)
            if hit:
                rows.append((ref, m["pin"], hit[0], hit[1]))
        if not pulls or not rows:
            continue
        top = max(v for v, _, _ in pulls)
        # a listed internal pull-up (a reset pin's own) at its weakest
        for ref, pin, spec, row in rows:
            if row.get("rpu") and top > 0:
                pulls.append((top, float(max(row["rpu"])),
                               f"{ref}.{pin} internal pull-up "
                               f"{max(row['rpu']) / 1e3:g}k max"))
        g = sum(1 / r for _, r, _ in pulls)
        vth = sum(v / r for v, r, _ in pulls) / g
        rth = 1 / g
        up = top > 0 and vth > top / 2
        sink = sum(float(row.get("ipd" if up else "ipu") or 0)
                   + float(row.get("leak") or 0) for *_, row in rows)
        if sink <= 0:
            continue
        level = vth - rth * sink if up else vth + rth * sink
        level = min(max(level, 0.0), top)
        recv = []
        for ref, pin, spec, row in rows:
            vil = row.get("vil", (row["vil_frac"] * top
                                  if "vil_frac" in row else None))
            vih = row.get("vih", (row["vih_frac"] * top
                                  if "vih_frac" in row else None))
            if vil is not None and vih is not None:
                recv.append((f"{ref}.{pin}", float(vil), float(vih)))
        if not recv:
            recv = [("CMOS", 0.3 * top, 0.7 * top)]
        if up:
            wrong = [r for r in recv if level < r[1]]
            vague = [r for r in recv if level < r[2]]
            need = max(r[2] for r in recv)
        else:
            wrong = [r for r in recv if level > r[2]]
            vague = [r for r in recv if level > r[1]]
            need = min(r[1] for r in recv)
        sev = "error" if wrong else "warning" if vague else None
        # the largest single pull that would still hold `need`, others gone
        span = (top - need) if up else need
        r_max = span / sink if span > 0 else 0.0
        name = max(group, key=lambda n: (not n.startswith("Net-("), n))
        yield {"net": name, "group": group, "up": up, "level": level,
               "vth": vth, "rth": rth, "sink": sink, "pulls": pulls,
               "rows": rows, "receivers": recv, "need": need,
               "severity": sev, "wrong": wrong or vague, "r_max": r_max}


def _driver_contention(nets: dict, comps: dict,
                       comp_pins: dict[str, dict[str, str]],
                       rails: dict[str, float], specs: list[dict]):
    """Yield (net, reset pin 'REF.PIN', [driver 'REF.PIN'], spec) for each
    signal net (0 ohm groups as one) holding a reset pin of
    reference/pin_leakage.yaml and push-pull capable pins (output,
    bidirectional, tri-state) of two or more other parts: any two of them
    can fight on the line. Connectors and open-collector pins don't
    count."""
    for group in _zero_ohm_groups(nets, comps, comp_pins):
        if any(_is_ground_name(n) or _supply_net(n, nets[n], rails)
               for n in group):
            continue
        members = [m for n in group for m in nets[n]]
        resets = []
        for m in members:
            hit = _leak_row(specs, comps.get(m["ref"], {}), m)
            if hit and hit[1].get("reset"):
                resets.append((m, hit[0]))
        if not resets:
            continue
        rm, spec = resets[0]
        drivers: dict[str, str] = {}
        for m in members:
            ref = m["ref"] or ""
            if ref == rm["ref"] or re.match(r"^(J|P|CN|X|TP)\d", ref):
                continue
            if _base_type(m.get("pintype")) in {"output", "bidirectional",
                                                "tri_state"}:
                drivers.setdefault(ref, f"{ref}.{m['pin']}")
        if len(drivers) >= 2:
            name = max(group, key=lambda n: (not n.startswith("Net-("), n))
            yield (name, f"{rm['ref']}.{rm['pin']}",
                   sorted(drivers.values()), spec)


def load_io_levels(path: Path = IO_LEVELS) -> dict:
    """reference/io_levels.yaml as {drivers: [...], receivers: [...]}."""
    import yaml  # noqa: PLC0415
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise checklib.CheckError(f"io levels {path}: {exc}") from exc
    return {"drivers": doc.get("drivers") or [],
            "receivers": doc.get("receivers") or []}


def _io_receiver(table: dict, comp: dict, m: dict) -> dict | None:
    """The io_levels.yaml receiver row for net member M, or None."""
    if _base_type(m.get("pintype")) in _POWER_TYPES:
        return None
    func = _norm_pin(m.get("pinfunction"))
    return next((r for r in table.get("receivers") or []
                 if _spec_matches(r, comp) and re.search(r["pins"], func,
                                                         re.I)), None)


def _io_overvoltage(nets: dict, comps: dict,
                    comp_pins: dict[str, dict[str, str]],
                    rails: dict[str, float], table: dict):
    """Yield one dict per (driver output, receiver I/O pin) pair of
    reference/io_levels.yaml joined directly or through one 2-pin resistor
    in series, with the receiver net's level solved from the driver's
    minimum high behind the series resistance and that net's other
    resistors to known rails or ground; 'over' is True when the level
    exceeds the receiver's vin_max. The last item is {'checked': n}: the
    driver outputs with a known supply that were followed."""
    checked = 0
    for dref, comp in sorted(comps.items()):
        drow = next((r for r in table.get("drivers") or []
                     if _spec_matches(r, comp)), None)
        if not drow:
            continue
        funcs = {}
        for pnum, net in comp_pins.get(dref, {}).items():
            m = next((x for x in nets.get(net, []) if x["ref"] == dref
                      and x["pin"] == pnum), {})
            funcs[pnum] = (_norm_pin(m.get("pinfunction")), net)
        sup = [rails[n] for f, n in funcs.values()
               if re.search(drow["supply"], f, re.I) and n in rails]
        if not sup:
            continue
        vdd = max(sup)
        vhigh = vdd - float(drow.get("voh_drop") or 0)
        prx = re.compile(drow["pins"], re.I)
        for pnum, (func, net) in sorted(funcs.items()):
            if (not prx.search(func) or net.startswith("unconnected-")
                    or _is_ground_name(net) or net in rails):
                continue
            checked += 1
            reach: list[tuple[str, float, str | None]] = [(net, 0.0, None)]
            for m in nets.get(net, []):
                rp = comp_pins.get(m["ref"], {})
                if _part_kind(m["ref"]) != "R" or len(rp) != 2:
                    continue
                far = next((n for p, n in rp.items() if p != m["pin"]), None)
                ohm = engcalc.parse_value(comps.get(m["ref"], {})
                                          .get("value"))
                if (far and ohm is not None and far not in rails
                        and not _is_ground_name(far)):
                    reach.append((far, float(ohm), m["ref"]))
            for rnet, rs, rref in reach:
                for m in nets.get(rnet, []):
                    if m["ref"] == dref:
                        continue
                    rrow = _io_receiver(table, comps.get(m["ref"], {}), m)
                    if not rrow:
                        continue
                    srcs = [(vhigh, rs)] if rs > 0 else []
                    divs = []
                    for x in nets.get(rnet, []) if rs > 0 else []:
                        xp = comp_pins.get(x["ref"], {})
                        if (x["ref"] == rref or _part_kind(x["ref"]) != "R"
                                or len(xp) != 2):
                            continue
                        far = next((n for p, n in xp.items()
                                    if p != x["pin"]), None)
                        fv = (0.0 if far and _is_ground_name(far)
                              else rails.get(far))
                        ohm = engcalc.parse_value(
                            comps.get(x["ref"], {}).get("value"))
                        if fv is not None and ohm:
                            srcs.append((fv, float(ohm)))
                            divs.append(f"{x['ref']} "
                                        f"{comps[x['ref']].get('value')} "
                                        f"to {far}")
                    level = (sum(v / r for v, r in srcs)
                             / sum(1 / r for _, r in srcs)) if srcs else vhigh
                    yield {"driver": dref, "dpin": pnum, "dfunc": func,
                           "drow": drow, "vdd": vdd, "vhigh": vhigh,
                           "net": rnet, "series": rref, "rs": rs,
                           "divider": divs, "receiver": m["ref"],
                           "rpin": m["pin"],
                           "rfunc": _norm_pin(m.get("pinfunction")),
                           "rrow": rrow, "level": level,
                           "over": level > float(rrow["vin_max"])}
    yield {"checked": checked}


_PG_FUNC_RE = re.compile(
    r"^(PG|PGOOD\d*|PGD|POK|PWRGD|PWR_?GOOD|PWR_?OK|PGOOD_?\w)$", re.I)
_NOT_A_READER_RE = re.compile(r"^(R|RN|C|L|FB|D|LED|TP|JP|SJ|H|MH)\d")
# A part with a load-switch pin (inrush slew, current limit or monitor, fault
# timer) and no converter or regulator pin is an eFuse, hot-swap controller
# or load switch: its output is its input switched through, not a rail it
# brings into regulation, and its power-good only says the FET is on. TI's
# TPS1663 (SLVSET9G 8.3.8): PGOOD "can be used for enable-disable control of
# the downstream loads"; pin table: "If PGOOD is unused, then connect to GND
# or leave it floating." Such a PG is a status output and is not judged.
_LOAD_SWITCH_PIN_RE = re.compile(r"^(DVDT|IMON|ILIM|TIMER)$")
_REGULATOR_PIN_RE = re.compile(r"^(SW\d*|LX\d*|PH|FB|VFB|FB\d|BOOT|BST|CB)$")


def _pg_unused(nets: dict, comps: dict,
               comp_pins: dict[str, dict[str, str]], rails: dict[str, float]):
    """Yield (ref, pin, func, net, members) for each IC power-good output
    whose net, and the nets one 2-pin resistor away (not rails or ground),
    hold no reader: no pin of an active part or connector other than
    power-good pins. The last item is ('checked', n)."""
    checked = 0
    funcs: dict[str, set[str]] = {}
    for members in nets.values():
        for m in members:
            funcs.setdefault(m["ref"], set()).add(
                _norm_pin(m.get("pinfunction")))
    switches = {r for r, fs in funcs.items()
                if any(_LOAD_SWITCH_PIN_RE.match(f) for f in fs)
                and not any(_REGULATOR_PIN_RE.match(f) for f in fs)}
    for net, members in sorted(nets.items()):
        if net.startswith("unconnected-") or net in rails:
            continue
        for m in members:
            func = _norm_pin(m.get("pinfunction"))
            if (not m["ref"].startswith("U") or not _PG_FUNC_RE.match(func)
                    or _base_type(m.get("pintype")) in _POWER_TYPES
                    or m["ref"] in switches):
                continue
            checked += 1
            if len(members) < 2:
                continue
            reach = {net}
            for x in members:
                xp = comp_pins.get(x["ref"], {})
                if _part_kind(x["ref"]) == "R" and len(xp) == 2:
                    far = next((n for p, n in xp.items() if p != x["pin"]),
                               None)
                    if far and far not in rails and not _is_ground_name(far):
                        reach.add(far)
            seen = [y for n in sorted(reach) for y in nets.get(n, [])]
            readers = [y for y in seen if y is not m
                       and not _NOT_A_READER_RE.match(y["ref"])
                       and not (_PG_FUNC_RE.match(
                           _norm_pin(y.get("pinfunction")))
                           and y["ref"].startswith("U"))]
            if not readers:
                yield (m["ref"], m["pin"], func, net,
                       sorted({y["ref"] for y in seen} - {m["ref"]}))
    yield ("checked", checked, "", "", [])


def _dc_level(net: str, nets: dict, comps: dict,
              comp_pins: dict[str, dict[str, str]],
              rails: dict[str, float]) -> tuple[float | None, list[str], str]:
    """The DC volts on `net` from the resistors around it: every net reached
    through 2-pin resistors from `net` (stopping at nets of known volts) is
    solved by nodal analysis. Capacitors and test points are open, connector
    pins open (an external part unplugged: listed in the second return),
    input pins high-Z. Any other pin on a reached net (an output, a diode,
    a transistor) makes the level unknown: (None, [], why)."""
    from engcalc import parse_value  # noqa: PLC0415
    unknown: list[str] = []
    edges: list[tuple[str, str, float]] = []
    opened: set[str] = set()
    stamped: set[str] = set()
    todo, seen = [net], {net}
    while todo:
        n = todo.pop()
        unknown.append(n)
        if len(unknown) > _DC_CLUSTER_MAX:
            return None, [], "resistor network too large"
        for m in nets[n]:
            ref = m["ref"] or ""
            pins = comp_pins.get(ref, {})
            if ref.startswith("R") and len(pins) == 2:
                if ref in stamped:
                    continue  # met from its other end: stamped once
                stamped.add(ref)
                ohms = parse_value(comps.get(ref, {}).get("value"))
                if ohms is None:
                    return None, [], f"{ref} has no readable value"
                far = next(nt for p, nt in pins.items() if p != m["pin"])
                if far == n:
                    continue
                edges.append((n, far, max(ohms, 1e-3)))
                if far not in rails and far not in seen:
                    seen.add(far)
                    todo.append(far)
            elif _OPEN_DC_RE.match(ref):
                continue
            elif _CONNECTOR_RE.match(ref):
                opened.add(ref)
            elif _base_type(m["pintype"]) == "input" or _AMP_IN_RE.match(
                    (m.get("pinfunction") or "").upper()):
                continue
            else:
                return None, [], (f"{ref}.{m['pin']} "
                                  f"({m['pintype'] or 'no type'}) on {n}")
    idx = {n: i for i, n in enumerate(unknown)}
    k = len(unknown)
    g = [[0.0] * (k + 1) for _ in range(k)]
    for a, b, ohms in edges:
        y = 1.0 / ohms
        for x, o in ((a, b), (b, a)):
            if x not in idx:
                continue
            g[idx[x]][idx[x]] += y
            if o in idx:
                g[idx[x]][idx[o]] -= y
            else:
                g[idx[x]][k] += y * rails[o]
    # Gauss-Jordan; a node with no path to a known rail is singular
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(g[r][c]))
        if abs(g[piv][c]) < 1e-15:
            return None, [], f"{net} has no resistive path to a known rail"
        g[c], g[piv] = g[piv], g[c]
        for r in range(k):
            if r != c and g[r][c]:
                f = g[r][c] / g[c][c]
                g[r] = [x - f * y for x, y in zip(g[r], g[c])]
    return g[0][k] / g[0][0], sorted(opened), ""


# ------------------------------------------------------------ min stable gain

OPAMP_MIN_GAIN_YAML = HERE.parent / "reference" / "opamp_min_gain.yaml"
# an op-amp's inverting input and its output, with the unit's tag (A, 2)
_AMP_INV_RE = re.compile(r"^(?:-|-IN_?([A-D1-4])?|IN_?([A-D1-4])?-|INV)$")
_AMP_OUT_RE = re.compile(r"^(?:~|V?OUT_?([A-D1-4])?|VO_?([A-D1-4])?|"
                         r"([A-D1-4])OUT)$")
# -in pin -> output pin of the standard single / dual / quad pinouts
_STD_LOOPS = ({"2": "6"}, {"2": "1", "6": "7"},
              {"2": "1", "6": "7", "9": "8", "13": "14"})
_DRIVEN_TYPES = ("output", "power_out", "power_in", "tri_state")


def _min_gain_table(path: Path = OPAMP_MIN_GAIN_YAML) -> list[dict]:
    import yaml  # noqa: PLC0415
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["parts"]
    for r in rows:
        r["_re"] = re.compile(r["match"], re.I)
        # YAML 1.1 reads 80.0e6 (no exponent sign) as a string
        r["min_gain"], r["gbw_hz"] = float(r["min_gain"]), float(r["gbw_hz"])
    return rows


def _amp_loops(types: dict[str, dict]) -> list[tuple[str, str]]:
    """(inverting input pin, output pin) of each amplifier in one part:
    the only pair, else paired by unit tag (INA-/OUTA), else by the
    standard single, dual or quad op-amp pinout."""
    def tag(rx, m):
        hit = rx.match((m.get("pinfunction") or "").upper())
        return next((g for g in hit.groups() if g), "") if hit else None
    invs = {p: tag(_AMP_INV_RE, m) for p, m in types.items()
            if tag(_AMP_INV_RE, m) is not None}
    outs = {p: tag(_AMP_OUT_RE, m) for p, m in types.items()
            if tag(_AMP_OUT_RE, m) is not None
            or (_base_type(m["pintype"]) == "output"
                and not _AMP_SUPPLY_RE.match((m.get("pinfunction")
                                              or "").upper()))}
    if len(invs) == 1 and len(outs) == 1:
        return [(next(iter(invs)), next(iter(outs)))]
    pairs = [(i, o) for i, ti in invs.items() for o, to in outs.items()
             if ti and ti == to]
    if len(pairs) == len(invs):
        return sorted(pairs)
    for std in _STD_LOOPS:
        if set(std) == set(invs) and set(std.values()) <= set(outs):
            return sorted(std.items())
    return []


def _noise_gain(inv: str, out: str, own: str, f_hz: float, nets: dict,
                comps: dict, comp_pins: dict[str, dict[str, str]],
                rails: dict[str, float]) -> tuple[float | None, str]:
    """|1/beta| at f_hz: the output net driven with 1 V and every source
    zeroed, the inverse of the share that comes back to the inverting input
    through the resistors and capacitors around it. Rails, nets with a
    driven pin (an output, a supply pin) and connector pins (a source
    plugged in) are AC ground; inputs and test points are open. Any other
    pin on the way (a diode, a transistor, a bead) makes it unknown:
    (None, why). No passive path back from the output is no feedback here
    (open loop, a comparator, or a loop through another stage): (None, why)."""
    import cmath  # noqa: PLC0415
    from engcalc import parse_value  # noqa: PLC0415
    if inv == out:
        return 1.0, ""
    jw = 2j * cmath.pi * f_hz
    fixed: dict[str, complex] = {out: 1.0}
    unknown: list[str] = []
    edges: list[tuple[str, str, complex]] = []
    stamped: set[str] = set()
    todo, seen = [inv], {inv, out}
    while todo:
        n = todo.pop()
        members = nets[n]
        if n != inv and (n in rails or any(
                _CONNECTOR_RE.match(m["ref"] or "")
                or _base_type(m["pintype"]) in _DRIVEN_TYPES
                or (m["ref"] == own and _AMP_OUT_RE.match(
                    (m.get("pinfunction") or "").upper()))
                for m in members)):
            fixed[n] = 0.0
            continue
        unknown.append(n)
        if len(unknown) > _DC_CLUSTER_MAX:
            return None, "feedback network too large"
        for m in members:
            ref = m["ref"] or ""
            pins = comp_pins.get(ref, {})
            kind = _part_kind(ref) if len(pins) == 2 else None
            if kind in ("R", "C"):
                if ref in stamped:
                    continue
                stamped.add(ref)
                val = parse_value(comps.get(ref, {}).get("value"))
                if val is None:
                    return None, f"{ref} has no readable value"
                far = next(nt for p, nt in pins.items() if p != m["pin"])
                if far == n:
                    continue
                y = (1.0 / max(val, 1e-3) if kind == "R" else jw * val)
                edges.append((n, far, y))
                if far not in seen:
                    seen.add(far)
                    todo.append(far)
            elif _OPEN_DC_RE.match(ref) or ref == own:
                continue
            elif _base_type(m["pintype"]) == "input" or _AMP_IN_RE.match(
                    (m.get("pinfunction") or "").upper()):
                continue
            else:
                return None, (f"{ref}.{m['pin']} "
                              f"({m['pintype'] or 'no type'}) on {n}")
    if not any(out in (a, b) for a, b, _ in edges):
        return None, "no passive feedback from the output"
    idx = {n: i for i, n in enumerate(unknown)}
    k = len(unknown)
    g = [[0j] * (k + 1) for _ in range(k)]
    for a, b, y in edges:
        for x, o in ((a, b), (b, a)):
            if x not in idx:
                continue
            g[idx[x]][idx[x]] += y
            if o in idx:
                g[idx[x]][idx[o]] -= y
            else:
                g[idx[x]][k] += y * fixed[o]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(g[r][c]))
        if abs(g[piv][c]) < 1e-15:
            return None, f"{inv} has no path to a fixed node"
        g[c], g[piv] = g[piv], g[c]
        for r in range(k):
            if r != c and g[r][c]:
                f = g[r][c] / g[c][c]
                g[r] = [x - f * y for x, y in zip(g[r], g[c])]
    beta = abs(g[0][k] / g[0][0])
    if beta < 1e-9:
        return None, "no signal fed back"
    return 1.0 / beta, ""


# ------------------------------------------------------------ amplifier DC path

# op-amp, comparator and in-amp symbol libraries; a part from one, or in
# opamp_cm.yaml, or with a common op-amp part number, is an amplifier whose
# +/- inputs need a DC path. Audio power amps (TPA31xx, PAM8xxx) are left out
# on purpose: most bias their own inputs, so an AC-coupled input is right.
_AMP_LIB_RE = re.compile(r"^(Amplifier_Operational|Amplifier_Instrumentation|"
                         r"Comparator)$")
_AMP_VALUE_RE = re.compile(
    r"^(NE55[3-4]\d|OPA\d|TL0[678]\d|LM(358|324|833|4562|6171|6172|2904|"
    r"2902|393|339|311)|MCP6\d|TLV[23469]\d|TLV8[5-9]\d|LMV\d|ADA4\d|AD8[0-6]\d\d|RC4558|"
    r"NJM\d|LT1\d\d\d|LTC6\d|INA\d)", re.I)
# two-terminal parts that carry DC from one net to the other
_DC_PASS_RE = re.compile(r"^(R|L|FB|NT|JP|SB|F|RV)\d")
# parts whose pins are no DC path for a bias current: capacitors, test
# points, diodes (clamps conduct only past a rail), crystals
_DC_BLOCK_RE = re.compile(r"^(C|TP|D|Y|X)\d")
# polarized capacitors: symbol or footprint
_POLCAP_SYM_RE = re.compile(r"^(C_Polarized|CP|C_Pol|CP_Small|"
                            r"C_Polarized_Small|CP1|CP1_Small)(_|$)", re.I)
_POLCAP_FP_RE = re.compile(r"(CP_Elec|CP_Radial|CP_Axial|C_Elec|CP_Tantalum|"
                           r"Tantalum|C_Pol)", re.I)
POLCAP_REVERSE_V = 0.5


def _is_amp(comp: dict, table: list[dict]) -> bool:
    lib, part = comp.get("libsource") or ("", "")
    return bool(_AMP_LIB_RE.match(lib) or _cm_row(comp, table)
                or _AMP_VALUE_RE.match((comp.get("value") or "").strip())
                or _AMP_VALUE_RE.match(part or ""))


def _dc_path(net: str, nets: dict, comps: dict,
             comp_pins: dict[str, dict[str, str]], rails: dict[str, float],
             amp_refs: set[str]) -> tuple[str, list[str], list[str]]:
    """Where an amplifier input's bias current can flow: ("rail", ...) when
    a resistor/inductor/jumper chain reaches a ground or a net of known
    volts or any driving pin (an output, a power pin, an IC pin of unknown
    type), ("connector", refs, nets) when the only way out is a connector
    pin (it floats when nothing is plugged in), ("none", [], nets) when it
    reaches only capacitors, diodes, test points and other inputs."""
    seen, todo = {net}, [net]
    via_conn: set[str] = set()
    while todo:
        n = todo.pop()
        if n in rails or _is_ground_name(n):
            return "rail", [], sorted(seen)
        for m in nets[n]:
            ref = m["ref"] or ""
            pins = comp_pins.get(ref, {})
            func = (m.get("pinfunction") or "").upper()
            if _DC_PASS_RE.match(ref) and len(pins) == 2:
                far = next((nt for p, nt in pins.items() if p != m["pin"]),
                           n)
                if far not in seen:
                    seen.add(far)
                    todo.append(far)
            elif _DC_BLOCK_RE.match(ref):
                continue
            elif _CONNECTOR_RE.match(ref):
                via_conn.add(ref)
            elif _base_type(m["pintype"]) == "input" or (
                    ref in amp_refs and _AMP_IN_RE.match(func)):
                continue
            elif _base_type(m["pintype"]) in ("no_connect", "unspecified") \
                    and not func:
                continue
            else:
                return "rail", [], sorted(seen)
    if via_conn:
        return "connector", sorted(via_conn), sorted(seen)
    return "none", [], sorted(seen)


def _polcap_pins(ref: str, comp: dict, nets: dict,
                 comp_pins: dict[str, dict[str, str]]) -> tuple | None:
    """(+ net, - net) of a polarized capacitor, or None if it isn't one.
    KiCad's C_Polarized and CP_* footprints put + on pin 1; a pin named
    + or - overrides that."""
    lib, part = comp.get("libsource") or ("", "")
    if not (_POLCAP_SYM_RE.match(part or "") or
            _POLCAP_FP_RE.search(comp.get("footprint") or "")):
        return None
    pins = comp_pins.get(ref, {})
    if len(pins) != 2:
        return None
    named = {}
    for p, n in pins.items():
        for m in nets[n]:
            if m["ref"] == ref and m["pin"] == p:
                f = (m.get("pinfunction") or "").strip()
                if f in ("+", "-"):
                    named[f] = n
    if len(named) == 2:
        return named["+"], named["-"]
    if "1" in pins and "2" in pins:
        return pins["1"], pins["2"]
    return None


def compare_netlists(a: dict, b: dict) -> dict:
    """Strict electrical identity: same net names, same (ref, pin) members."""
    ma, mb = _memberships(a), _memberships(b)
    only_a = sorted(set(ma) - set(mb))
    only_b = sorted(set(mb) - set(ma))
    diffs = []
    for net in sorted(set(ma) & set(mb)):
        if ma[net] != mb[net]:
            diffs.append({
                "net": net,
                "only_in_a": sorted(set(ma[net]) - set(mb[net])),
                "only_in_b": sorted(set(mb[net]) - set(ma[net])),
            })
    by_members = {mb[n]: n for n in only_b}
    renamed = [{"a": n, "b": by_members[ma[n]], "members": len(ma[n])}
               for n in only_a if ma[n] in by_members]
    return {"identical": not (only_a or only_b or diffs),
            "nets_only_in_a": only_a, "nets_only_in_b": only_b,
            "membership_diffs": diffs, "renamed": renamed,
            "net_counts": {"a": len(ma), "b": len(mb)}}


def run_compare(net_a: Path, net_b: Path, out: str | None):
    a = parse_netlist(net_a)
    b = parse_netlist(net_b)
    cmp_result = compare_netlists(a, b)
    violations = []

    def v(kind, net, msg, **extra):
        violations.append(checklib.violation(
            SCRIPT, "error", None, None, net, [], msg, SOURCE,
            kind="netlist_diff", diff=kind, **extra))

    for net in cmp_result["nets_only_in_a"]:
        if any(r["a"] == net for r in cmp_result["renamed"]):
            continue
        v("missing_in_b", net, f"net '{net}' only in {net_a.name}")
    for net in cmp_result["nets_only_in_b"]:
        if any(r["b"] == net for r in cmp_result["renamed"]):
            continue
        v("missing_in_a", net, f"net '{net}' only in {net_b.name}")
    for r in cmp_result["renamed"]:
        v("renamed", r["a"],
          f"net '{r['a']}' ({net_a.name}) matches membership of "
          f"'{r['b']}' ({net_b.name}) - renamed")
    for d in cmp_result["membership_diffs"]:
        v("membership", d["net"],
          f"net '{d['net']}' membership differs: only in {net_a.name}: "
          f"{d['only_in_a']}, only in {net_b.name}: {d['only_in_b']}",
          only_in_a=d["only_in_a"], only_in_b=d["only_in_b"])

    payload = checklib.report(SCRIPT, str(net_a), violations,
                              mode="compare", a=str(net_a), b=str(net_b),
                              **cmp_result)
    return payload, out


# ------------------------------------------------------------ audit mode

def _constraint_nets(constraints: dict) -> dict[str, list[str]]:
    """Every net name constraints.json references, keyed by where."""
    where: dict[str, list[str]] = {}

    def add(net, src):
        if isinstance(net, str) and net:
            where.setdefault(net, []).append(src)

    for i, ent in enumerate(constraints.get("high_speed", []) or []):
        add(ent.get("net"), f"high_speed[{i}].net")
        ref = ent.get("reference")
        if isinstance(ref, str):
            add(ref, f"high_speed[{i}].reference")
        elif isinstance(ref, dict):
            for lay, rnet in ref.items():
                add(rnet, f"high_speed[{i}].reference[{lay}]")
    for i, ent in enumerate(constraints.get("power", []) or []):
        add(ent.get("net"), f"power[{i}].net")
    for i, ent in enumerate(constraints.get("voltages", []) or []):
        add(ent.get("net"), f"voltages[{i}].net")
    for i, ent in enumerate(constraints.get("thermal", []) or []):
        add(ent.get("net"), f"thermal[{i}].net")
    for i, ent in enumerate(constraints.get("diff_pairs", []) or []):
        add(ent.get("p"), f"diff_pairs[{i}].p")
        add(ent.get("n"), f"diff_pairs[{i}].n")
    return where


# single-ended active-low control signals spelled <STEM>_N: their _N means
# "active low", not the minus leg of a pair (ultra-front /FRONT_INT_N)
_ACTIVE_LOW_STEM_RE = re.compile(
    r"(?:^|[/_.])(?:INT|IRQ|RST|RESET|NRST|CS|SS|EN|ENABLE|WAKE|ALERT|"
    r"RDY|READY|OE|WE|RE|WR|RD|PD|PWDN|SHDN|SD|FAULT|FLT|BUSY|PG|PGOOD|"
    r"STBY|SLEEP|LDAC|CLR|DRDY|HOLD|WP|ACK|BOOT|MUTE)$", re.I)


def _suffix_partner(name: str) -> str | None:
    """Expected partner net name if `name` carries a strong diff suffix."""
    up = name.upper()
    if up.endswith("_N") and _ACTIVE_LOW_STEM_RE.search(up[:-2]):
        return None
    for a, b in STRONG_SUFFIXES:
        for tok, other in ((a, b), (b, a)):
            if up.endswith(tok):
                return name[: len(name) - len(tok)] + other
    return None


def _pair_follows_convention(p: str, n: str) -> bool:
    up_p, up_n = p.upper(), n.upper()
    for a, b in STRONG_SUFFIXES:
        if (up_p.endswith(a) and up_n.endswith(b)
                and up_p[: -len(a)] == up_n[: -len(b)]):
            return True
    return False


def _constraint_refs(constraints: dict) -> dict[str, list[str]]:
    """Every refdes constraints.json references, keyed by where."""
    where: dict[str, list[str]] = {}

    def add(ref, src):
        if isinstance(ref, str) and ref:
            where.setdefault(ref, []).append(src)

    placement = constraints.get("placement") or {}
    for i, e in enumerate(placement.get("edges") or []):
        add(e.get("ref"), f"placement.edges[{i}].ref")
    for i, g in enumerate(placement.get("groups") or []):
        add(g.get("anchor"), f"placement.groups[{i}].anchor")
        for j, m in enumerate(g.get("members") or []):
            add(m, f"placement.groups[{i}].members[{j}]")
    for i, r in enumerate(placement.get("fixed") or []):
        add(r, f"placement.fixed[{i}]")
    for i, s in enumerate(placement.get("separation") or []):
        for side in ("a", "b"):
            for j, r in enumerate(s.get(side) or []):
                add(r, f"placement.separation[{i}].{side}[{j}]")
    for i, t in enumerate(constraints.get("thermal") or []):
        add(t.get("ref"), f"thermal[{i}].ref")
    return where


# an IC supply pin the symbol types passive (ES9018K2M AVCC_L, PCM5102A
# CPVDD, ES7243E VDDA, ES8388 HPVDD): a consumer all the same
_SUPPLY_PIN_RE = re.compile(r"^[A-Z]{0,3}V(?:DD|CC)[A-Z0-9_]*$|^V[+]$|^VS[+]?$")
# a transistor on a rail: a low-side switch's drain / collector carries the
# load current down to its source (ultra-front /BL_K: backlight cathode)
_SWITCH_REF_RE = re.compile(r"^Q\d")
# a series element that makes a filtered tap of a rail (RC / LC / bead)
_FILTER_REF_RE = re.compile(r"^(R|L|FB)\d")


def _supply_pin_consumers(members: list[dict],
                          comp_pins: dict[str, dict[str, str]]) -> list[str]:
    """Members on a non-2-pin part whose pin is named as a supply input
    (VDD/VCC/AVDD/VCCA/...), whatever pintype the symbol gave it."""
    return sorted({m["ref"] for m in members
                   if len(comp_pins.get(m["ref"], {})) > 2
                   and _SUPPLY_PIN_RE.match(
                       (m.get("pinfunction") or "").upper())})


def _filtered_from(net: str, members: list[dict],
                   comp_pins: dict[str, dict[str, str]],
                   declared: set) -> list[str]:
    """Declared rails this net taps through a 2-pin R/L/bead member: an
    RC-filtered sub-rail of a declared rail (ultra-front /IR_VCC)."""
    out = set()
    for m in members:
        pins = comp_pins.get(m["ref"], {})
        if len(pins) != 2 or not _FILTER_REF_RE.match(m["ref"]):
            continue
        other = next((n for p, n in pins.items() if p != m["pin"]), None)
        if other and other != net and other in declared:
            out.add(other)
    return sorted(out)


# a regulator's bootstrap pin and its switch node, by name: the same shape
# check_ratings covers by rule (bootstrap_net), so the two checks agree
_BOOT_PIN_RE = re.compile(r"^(BS|BOOT)")
_SWITCH_PIN_RE = re.compile(r"^(OUT|SW|LX|PH)")


def _bootstrap_net(net: str, members: list[dict],
                   comp_pins: dict[str, dict[str, str]],
                   nets: dict[str, list]) -> bool:
    """A buck's bootstrap net (TPS56637 BOOT): one BOOT/BS pin and one 2-pin
    capacitor, whose other pin is on a net that holds the same part's
    switch pin (SW/LX/PH/OUT). Its voltage is the switch node plus the
    part's own drive, so it is no rail to declare."""
    if len(members) != 2:
        return False
    boot = next((m for m in members
                 if _base_type(m["pintype"]) == "power_in"
                 and _BOOT_PIN_RE.match((m.get("pinfunction") or "").upper())),
                None)
    cap = next((m for m in members if m is not boot), None)
    if boot is None or cap is None or not re.match(r"^C\d", cap["ref"] or ""):
        return False
    pins = comp_pins.get(cap["ref"], {})
    if len(pins) != 2:
        return False
    other = next((n for p, n in pins.items() if p != cap["pin"]), None)
    if other is None or other == net:
        return False
    return any(m["ref"] == boot["ref"] and m["pin"] != boot["pin"]
               and _SWITCH_PIN_RE.match((m.get("pinfunction") or "").upper())
               for m in nets.get(other, []))


def _series_escapes(rail: str, members: list[dict],
                    comp_pins: dict[str, dict[str, str]]) -> list[str]:
    """2-pin members whose far pin leaves the rail through a non-ground,
    non-NC net: current has a path out through a series/load element
    (polyfuse, dropper, LED resistor), which never carries pintype
    power_in. A typo'd dead rail (source + caps to GND only) has none.
    A transistor on the rail whose control pin is driven (a net that is
    neither the rail, ground nor NC) switches a load through it too."""
    out = set()
    for m in members:
        pins = comp_pins.get(m["ref"], {})
        if _SWITCH_REF_RE.match(m["ref"]) and len(pins) >= 3:
            if any(n != rail and not _is_ground_name(n)
                   and not n.startswith("unconnected-")
                   for p, n in pins.items() if p != m["pin"]):
                out.add(m["ref"])
            continue
        if len(pins) != 2:
            continue
        other = next((n for p, n in pins.items() if p != m["pin"]), None)
        if (other and other != rail and not _is_ground_name(other)
                and not other.startswith("unconnected-")):
            out.add(m["ref"])
    return sorted(out)


def _board_only_refs(pcb: Path) -> set[str]:
    """Refs of the board's footprints marked (attr ... board_only): KiCad's
    flag for a part that lives on the board and not in the schematic
    (mounting holes, fiducials, logos), so never in the netlist."""
    out: set[str] = set()
    try:
        text = pcb.read_text(errors="replace")
    except OSError:
        return out
    for blk in re.split(r"\n\s*\(footprint ", text)[1:]:
        if not re.search(r"\(attr[^)]*\bboard_only\b", blk):
            continue
        m = (re.search(r'\(property "Reference" "([^"]+)"', blk)
             or re.search(r'\(fp_text reference "?([^"\s)]+)', blk))
        if m:
            out.add(m.group(1))
    return out


def audit(parsed: dict, constraints: dict,
          decoupling: dict | None,
          pin_requirements: list[dict] | None = None,
          board_only_refs: set[str] | None = None,
          pin_states: list[dict] | None = None,
          mcu_pins: dict | None = None,
          i2c_addresses: list[dict] | None = None,
          pin_leakage: list[dict] | None = None,
          io_levels: dict | None = None
          ) -> tuple[list[dict], dict]:
    nets = parsed["nets"]
    comps = parsed["components"]
    violations: list[dict] = []

    def v(sev, kind, net, refs, msg, **extra):
        violations.append(checklib.violation(
            SCRIPT, sev, None, None, net, refs, msg, SOURCE,
            kind=kind, **extra))

    # 1. every constraints-referenced net exists
    referenced = _constraint_nets(constraints)
    for net, sources in sorted(referenced.items()):
        if net not in nets:
            v("error", "missing_net", net, [],
              f"constraints net '{net}' ({', '.join(sources)}) not in "
              f"netlist", constraint_sources=sources)

    # 1b. every constraints-referenced refdes exists (ladder row 86: a
    # refdes rename/split must not silently mute a placement constraint)
    ref_where = _constraint_refs(constraints)
    board_only_refs = board_only_refs or set()
    for ref, sources in sorted(ref_where.items()):
        if ref not in comps and ref not in board_only_refs:
            v("error", "missing_ref", None, [ref],
              f"constraints ref '{ref}' ({', '.join(sources)}) not in "
              f"netlist components", constraint_sources=sources)

    # 2. explicit diff pairs follow the _P/_N (or DP/DM, D+/D-) convention
    for i, ent in enumerate(constraints.get("diff_pairs", []) or []):
        p, n = ent.get("p"), ent.get("n")
        if p and n and not _pair_follows_convention(p, n):
            v("warning", "diffpair_naming", p, [],
              f"diff_pairs[{i}] ('{p}', '{n}') does not follow a "
              f"differential suffix convention (_P/_N, DP/DM, D+/D-)")

    # 3. unpaired differential-looking nets in the netlist
    for name in sorted(nets):
        if name.startswith("unconnected-"):
            continue
        partner = _suffix_partner(name)
        if partner and partner not in nets:
            v("warning", "diffpair_unpaired", name, [],
              f"net '{name}' looks differential but partner "
              f"'{partner}' does not exist")

    # per-component pin->net map over ALL nets (incl. unconnected-*):
    # serves the series-escape scan (4) and the pin inventory (4c)
    comp_pins: dict[str, dict[str, str]] = {}
    for name, members in nets.items():
        for m in members:
            if m["ref"]:
                comp_pins.setdefault(m["ref"], {})[m["pin"]] = name

    # 4. power tree: declared rails have consumers; feeders are declared
    declared_power = [ent.get("net")
                     for ent in constraints.get("power", []) or []]
    declared_all = set(declared_power) | {
        ent.get("net") for ent in constraints.get("voltages", []) or []}
    power_facts = []
    for net in declared_power:
        if net not in nets:
            continue  # already an error above
        members = nets[net]
        n_in = sum(1 for m in members if _base_type(m["pintype"]) == "power_in")
        n_out = sum(1 for m in members
                    if _base_type(m["pintype"]) == "power_out")
        fact = {"net": net, "nodes": len(members),
                "power_in": n_in, "power_out": n_out}
        if n_in == 0:
            # passive loads (LEDs, droppers, fuses) never carry power_in:
            # a series escape means the rail feeds SOMETHING - record the
            # fact instead of a false-positive warning (T6 calibration);
            # so does an IC supply pin the symbol typed passive
            escapes = _series_escapes(net, members, comp_pins)
            supply = _supply_pin_consumers(members, comp_pins)
            if escapes:
                fact["passive_fed_via"] = escapes
            if supply:
                fact["supply_pins_typed_passive"] = supply
            if not escapes and not supply:
                v("warning", "power_no_consumers", net,
                  sorted({m["ref"] for m in members}),
                  f"declared power net '{net}' feeds no power_in pin")
        power_facts.append(fact)
    for name in sorted(nets):
        if name in declared_all or name.startswith("unconnected-"):
            continue
        if _is_ground_name(name):
            continue
        feeders = [m for m in nets[name]
                   if _base_type(m["pintype"]) == "power_in"]
        if feeders and _filtered_from(name, nets[name], comp_pins,
                                      declared_all):
            continue  # a filtered tap of a declared rail is that rail
        if feeders and _bootstrap_net(name, nets[name], comp_pins, nets):
            continue  # rides on the part's own switch node
        if feeders:
            v("warning", "power_undeclared", name,
              sorted({m["ref"] for m in feeders}),
              f"net '{name}' feeds power_in pins "
              f"({', '.join(f'{m['ref']}.{m['pin']}' for m in feeders)}) "
              f"but is not declared in constraints power[]/voltages[]")

    # 4c. pin inventory vs net membership (ladder row 68): hierarchical
    # exports DROP no-connect singleton nets; a flat export's NC pins live
    # in unconnected-* nets (counted as connected here) and must not fire.
    libparts = parsed.get("libparts") or {}
    pins_expected = pins_connected = 0
    for ref in sorted(comps):
        comp = comps[ref]
        exp = set(comp.get("unit_pins") or ())
        if not exp:
            exp = set(libparts.get(comp.get("libsource")) or ())
        if not exp:
            continue  # no inventory in this export (synthetic/old netlist)
        have = set(comp_pins.get(ref, ()))
        pins_expected += len(exp)
        pins_connected += len(exp & have)
        missing = sorted(exp - have,
                         key=lambda p: (0, int(p)) if p.isdigit() else (1, p))
        if missing:
            v("warning", "pin_no_net", None, [ref],
              f"pin(s) {', '.join(ref + '.' + p for p in missing)} exist "
              f"in the symbol but reach NO net (hierarchical exports drop "
              f"no-connect singletons; these pads arrive at board_init "
              f"with no net at all)", pins=missing)

    # 5. dangling single-pin nets (label-typo signature)
    for name in sorted(nets):
        if name.startswith("unconnected-"):
            continue
        if len(nets[name]) == 1:
            m = nets[name][0]
            v("warning", "dangling_net", name, [m["ref"]],
              f"net '{name}' has a single pin ({m['ref']}.{m['pin']}) - "
              f"label typo?")

    # 6. decoupling associations vs the netlist
    n_assoc = 0
    if decoupling is not None:
        assocs = decoupling.get("associations")
        if not isinstance(assocs, list):
            raise checklib.CheckError(
                "decoupling metadata has no associations[] list")
        n_assoc = len(assocs)
        member_index = {(m["ref"], m["pin"]): name
                        for name, members in nets.items() for m in members}
        for a in assocs:
            cap, ic = a.get("cap"), a.get("ic")
            pin, rail = str(a.get("pin")), a.get("rail")
            gnd = a.get("gnd", "GND")
            ctx = f"association {cap}->{ic}.{pin}"
            if cap not in comps:
                v("error", "metadata_mismatch", rail, [c for c in (ic,) if c],
                  f"{ctx}: cap '{cap}' not in netlist")
                continue
            if ic not in comps:
                v("error", "metadata_mismatch", rail, [cap],
                  f"{ctx}: ic '{ic}' not in netlist")
                continue
            if rail not in nets:
                v("error", "metadata_mismatch", rail, [cap, ic],
                  f"{ctx}: rail net '{rail}' not in netlist")
                continue
            if member_index.get((ic, pin)) != rail:
                v("error", "metadata_mismatch", rail, [cap, ic],
                  f"{ctx}: {ic}.{pin} is on net "
                  f"'{member_index.get((ic, pin), '<none>')}', not '{rail}'")
            cap_nets = {name for (r, _), name in member_index.items()
                        if r == cap}
            if rail not in cap_nets:
                v("error", "metadata_mismatch", rail, [cap],
                  f"{ctx}: no pin of '{cap}' is on rail '{rail}' "
                  f"(cap nets: {sorted(cap_nets)})")
            if gnd not in cap_nets:
                v("error", "metadata_mismatch", gnd, [cap],
                  f"{ctx}: no pin of '{cap}' is on return net '{gnd}' "
                  f"(cap nets: {sorted(cap_nets)})")
            want, have = a.get("value"), comps[cap]["value"]
            if want and have and want != have:
                v("warning", "metadata_mismatch", rail, [cap],
                  f"{ctx}: metadata value '{want}' != netlist value "
                  f"'{have}'")

    # 7. a ground-named net that reaches no ground: no power pin on it and
    # no resistor, inductor, bead, jumper or net tie across to another
    # ground net (errata corpus: digits v1 MIC_GND, a local label where the
    # GND symbol belonged, so the mic return floated)
    for name in sorted(nets):
        if not _is_ground_name(name) or _GROUND_EXEMPT_RE.search(name.upper()):
            continue
        members = nets[name]
        if len(members) < 2 or any(_base_type(m["pintype"]) in
                                   ("power_in", "power_out") for m in members):
            continue
        if _ground_bridges(name, members, comp_pins):
            continue
        others = [n for n in nets if n != name and _is_ground_name(n)]
        if not others:
            continue  # the board's only ground: nothing to be cut off from
        v("warning", "ground_island", name, sorted({m["ref"] for m in members}),
          f"ground net '{name}' carries no power pin and no part ties it to "
          f"another ground ({', '.join(sorted(others)[:3])}): it floats. A "
          f"local label where the ground symbol belonged?")

    # 8. a bus net on its partner's pin: SDA landing on a pin named SCL
    # (errata class: pandore rev A0 CS4272, SDA/SCL transposed at the codec)
    for name in sorted(nets):
        token = _bus_token(name)
        if not token:
            continue
        partner = _BUS_PARTNER[token]
        for m in nets[name]:
            if _bus_token(m.get("pinfunction") or "") == partner:
                v("error", "pin_net_swap", name, [m["ref"]],
                  f"net '{name}' lands on {m['ref']}.{m['pin']}, a pin "
                  f"named '{m['pinfunction']}': {token} and {partner} are "
                  f"crossed at {m['ref']}", pin=m["pin"])

    # 9. a stock SOT-23 transistor whose symbol pin order isn't the
    # package's: nearly every SOT-23 MOSFET is 1=G 2=S 3=D and every BJT
    # 1=B 2=E 3=C (errata corpus: oasis v0.2, Q_NMOS_GDS on SOT-23-3 put
    # the SI2328DS's source where the drain goes)
    for ref in sorted(comps):
        fp = comps[ref].get("footprint") or ""
        if not _STOCK_SOT23_RE.match(fp):
            continue
        funcs = {}
        for name, members in nets.items():
            for m in members:
                if m["ref"] == ref:
                    funcs[m["pin"]] = _transistor_role(m.get("pinfunction"))
        roles = (funcs.get("1"), funcs.get("2"), funcs.get("3"))
        for want in _SOT23_ORDERS:
            if set(roles) == set(want) and roles != want:
                v("warning", "sot23_pinout", None, [ref],
                  f"{ref} ({comps[ref].get('value')}) on {fp}: symbol pins "
                  f"1-2-3 are {'-'.join(roles)}, the SOT-23 standard is "
                  f"{'-'.join(want)}; check the part's datasheet pinout "
                  f"(or use the matching symbol variant)",
                  pins=dict(zip("123", roles)))

    # 10. an op-amp or comparator input whose DC level sits outside the
    # part's input common-mode range (errata corpus: minuet v3, an LMV331
    # whose inputs rest at 3.1-3.3 V on a 3.3 V supply, above its Vcc-0.7 V:
    # the rain sensor never worked)
    table = _cm_table()
    rails = _rail_volts(constraints, nets)
    cm_checked = 0
    for ref in sorted(comps):
        row = _cm_row(comps[ref], table)
        if not row:
            continue
        pins = comp_pins.get(ref, {})
        types = {m["pin"]: m for n in set(pins.values()) for m in nets[n]
                 if m["ref"] == ref}
        supply = [rails[pins[p]] for p, m in types.items()
                  if (_base_type(m["pintype"]) == "power_in"
                      or _AMP_SUPPLY_RE.match((m.get("pinfunction")
                                               or "").upper()))
                  and pins[p] in rails]
        if len(supply) < 2:
            continue
        vneg, vpos = min(supply), max(supply)
        lo, hi = vneg + row["vcm"][0], vpos + row["vcm"][1]
        for p, m in sorted(types.items()):
            if _base_type(m["pintype"]) not in ("input", "passive") or \
                    not _AMP_IN_RE.match((m.get("pinfunction") or "").upper()):
                continue
            net = pins[p]
            if net in rails:
                level, opened = rails[net], []
            else:
                level, opened, _why = _dc_level(net, nets, comps,
                                                comp_pins, rails)
                if level is None:
                    continue
            cm_checked += 1
            if lo - 0.005 <= level <= hi + 0.005:
                continue
            side = (f"above the top of its input common-mode range "
                    f"(V+ {hi - vpos:+.2f} V = {hi:.2f} V" if level > hi else
                    f"below the bottom of its input common-mode range "
                    f"(V- {lo - vneg:+.2f} V = {lo:.2f} V")
            v("warning", "input_common_mode", net, [ref],
              f"{ref} ({comps[ref].get('value')}, {row['kind']}) input "
              f"{m['pinfunction']} (pin {p}) rests at {level:.2f} V on "
              f"'{net}'" + (f" with {', '.join(opened)} open" if opened
                            else "") +
              f", {side} on a {vneg:g}..{vpos:g} V supply): the output "
              f"is undefined there. "
              f"Move the bias inside the range or pick a part whose range "
              f"covers it (range from {row['source'].split(';')[0]}"
              + ("" if row.get("verified") else ", not yet re-read") + ")",
              pin=p, level_v=round(level, 3), vcm_v=[round(lo, 3),
                                                     round(hi, 3)],
              opened=opened)

    # 11. a part the IC's datasheet requires between two of its pins
    # (reference/pin_requirements.yaml; errata corpus: minuet v3 shipped the
    # MCF8316D with no CBK drawn from FB_BK to GND_BK - the +3.3V rail's
    # caps were all drawn on other sheets for other parts)
    specs = (load_pin_requirements() if pin_requirements is None
             else pin_requirements)
    for sev, net, ic, req, found, elsewhere, off in _pin_requirements(
            nets, comps, comp_pins, specs):
        want = req.get("value") or "/".join(req["kinds"])
        what = (f"{ic} ({comps[ic].get('value')}) needs {req['id']} "
                f"({want}) from {req['pin']} to {req['to']} "
                f"[{req.get('source', 'datasheet')}]")
        if sev == "error":
            extra = (f"; off-value parts across it: {', '.join(off)}"
                     if off else "")
            v("error", "pin_part_missing", net, [ic],
              f"{what}: no part spans those pins{extra}",
              part=req["id"], pin=req["pin"], to=req["to"],
              off_value=off)
        else:
            v("warning", "pin_part_missing", net, [ic],
              f"{what}: only {', '.join(elsewhere)} span them, drawn on "
              f"other sheets than {ic}'s {comps[ic].get('sheet')} - the "
              f"rail's parts serve other ICs and none was drawn for this "
              f"pin; draw it with {ic} or confirm one sits at the pin on "
              f"the board", part=req["id"], pin=req["pin"], to=req["to"],
              drawn_elsewhere=elsewhere)

    # 12. an op-amp, comparator or in-amp input with no DC path for its bias
    # current: an AC-coupled input with no bias resistor, or a follower fed
    # only through a capacitor. The bias current charges the coupling cap
    # (Ib/C: 10 nA into 1 uF walks 10 mV/s) until the input sits on a rail.
    # A DC-coupled input whose only path is a connector floats when the
    # cable is out: warned, since the source may be part of the design.
    amp_refs = {r for r in comps if _is_amp(comps[r], table)}
    dc_checked = 0
    for ref in sorted(amp_refs):
        pins = comp_pins.get(ref, {})
        for p, net in sorted(pins.items()):
            mem = next((m for m in nets[net] if m["ref"] == ref
                        and m["pin"] == p), None)
            if not mem or not _AMP_IN_RE.match(
                    (mem.get("pinfunction") or "").upper()):
                continue
            if net.startswith("unconnected-"):
                continue
            dc_checked += 1
            how, conns, reached = _dc_path(net, nets, comps, comp_pins,
                                           rails, amp_refs)
            if how == "rail":
                continue
            caps = sorted({m["ref"] for n in reached for m in nets[n]
                           if (m["ref"] or "").startswith("C")})
            name = f"{ref} ({comps[ref].get('value')}) input " \
                   f"{mem['pinfunction']} (pin {p}) on '{net}'"
            if how == "none":
                v("error", "opamp_input_no_dc_path", net, [ref] + caps,
                  f"{name} has no DC path for its bias current: it "
                  f"reaches only " + (f"capacitors ({', '.join(caps)}), "
                                      if caps else "") +
                  "diodes, test points and other inputs. The bias current "
                  "charges the node until the input sits at a rail. Add a "
                  "resistor from the input to ground or to the stage's bias "
                  "point (ADI Analog Dialogue 41-08, TI SLOD006 ch. 12)",
                  pin=p, reached=reached)
            else:
                v("warning", "opamp_input_dc_via_connector_only", net,
                  [ref] + conns,
                  f"{name}: its only DC path is through {', '.join(conns)}"
                  f" - with nothing plugged in the input floats (rails, "
                  f"hum, a thump on plug-in). A resistor to ground or the "
                  f"bias point (100k-1M) holds it; ignore if the cable is "
                  f"never unplugged", pin=p, connectors=conns)

    # 13. a polarized capacitor whose + terminal rests below its -
    # terminal, from the same rail/resistor solve input_common_mode uses
    # (aluminium electrolytics and tantalums take no sustained reverse
    # bias; see reference/ingest/analog-audio.md)
    pol_checked = 0
    for ref in sorted(comps):
        pn = _polcap_pins(ref, comps[ref], nets, comp_pins)
        if not pn:
            continue
        levels = []
        for net in pn:
            if net in rails:
                levels.append(rails[net])
            else:
                lvl, _o, _w = _dc_level(net, nets, comps, comp_pins, rails)
                levels.append(lvl)
        if None in levels:
            continue
        pol_checked += 1
        vrev = levels[1] - levels[0]
        if vrev > POLCAP_REVERSE_V:
            v("warning", "polarized_cap_reverse", pn[0], [ref],
              f"{ref} ({comps[ref].get('value')}) is polarized but its + "
              f"terminal ('{pn[0]}', {levels[0]:.2f} V) rests {vrev:.2f} V "
              f"below its - terminal ('{pn[1]}', {levels[1]:.2f} V): "
              f"reverse biased at DC. Turn it round, or use a non-polar "
              f"part if the bias can go either way",
              plus_v=round(levels[0], 3), minus_v=round(levels[1], 3))

    # 14. a decompensated op-amp run below its minimum stable noise gain
    # (reference/opamp_min_gain.yaml): a follower or a G = -1 stage around
    # an OPA637 or an uncompensated NE5534 oscillates. Noise gain, 1/beta,
    # read where the loop crosses over at the minimum gain, so a feedback
    # cap that drops it there counts (ADI MT-033, AN148)
    gain_table = _min_gain_table()
    loops_checked = 0
    for ref in sorted(comps):
        row = _cm_row(comps[ref], gain_table)
        if not row:
            continue
        pins = comp_pins.get(ref, {})
        types = {m["pin"]: m for n in set(pins.values()) for m in nets[n]
                 if m["ref"] == ref}
        if row.get("comp_pins"):
            comp_re = re.compile(row["comp_pins"], re.I)
            comp_nets = {pins[p] for p, m in types.items()
                         if comp_re.search(m.get("pinfunction") or "")}
            if any((m["ref"] or "").startswith("C") for n in comp_nets
                   for m in nets[n]):
                continue  # compensated by hand
        f_hz = row["gbw_hz"] / row["min_gain"]
        for inv_p, out_p in _amp_loops(types):
            ng, _why = _noise_gain(pins[inv_p], pins[out_p], ref, f_hz,
                                   nets, comps, comp_pins, rails)
            if ng is None:
                continue
            loops_checked += 1
            if ng >= row["min_gain"] * 0.98:
                continue
            sib = (f", or the unity-stable {row['unity_stable']}"
                   if row.get("unity_stable") else "")
            v("warning", "below_min_stable_gain", pins[inv_p], [ref],
              f"{ref} ({comps[ref].get('value')}) is decompensated, stable "
              f"only at a noise gain of {row['min_gain']:g} or more, but "
              f"its loop from {pins[out_p]} (pin {out_p}) back to "
              f"{pins[inv_p]} (pin {inv_p}) gives a noise gain of {ng:.2f} "
              f"at {f_hz / 1e6:.3g} MHz (a follower is 1, an inverting -1 "
              f"stage 2): it will ring or oscillate. Raise the noise gain "
              f"(a smaller gain resistor, or an R-C from the inverting "
              f"input to ground){sib} (from {row['source'].split(';')[0]}"
              + ("" if row.get("verified") else ", not yet re-read") + ")",
              pin=inv_p, out_pin=out_p, noise_gain=round(ng, 3),
              min_gain=row["min_gain"], at_hz=f_hz)

    # 15. a pin the datasheet requires in a state it isn't in
    # (reference/pin_states.yaml, #1033; errata corpus: glasgow revD0 left
    # the PCA6416A RESET floating, bedlift v0.2 tied the DRV8434S nSCS to
    # GND and its VREF to its own 5 V DVDD, echoplayer grounded the
    # USB3320C ID on a device-only board, glasgow put the NAFE71388 AICOM
    # on a remote common)
    ps_specs = load_pin_states() if pin_states is None else pin_states
    ps_rails = _rail_volts(constraints, nets)
    pin_states_checked = 0
    for spec in ps_specs:
        pin_states_checked += sum(1 for r in comps
                                  if _spec_matches(spec, comps[r]))
    for (rule, spec, ref, pnum, name, st, volts, why,
         problem) in _pin_states(nets, comps, comp_pins, ps_rails, ps_specs):
        via = f" (via {', '.join(why)})" if why else ""
        v(rule.get("severity", "error"), "pin_state", name, [ref],
          f"{ref} ({comps[ref].get('value')}) pin {pnum} {rule['pin']} on "
          f"'{name}' {problem}{via}: {' '.join(rule['why'].split())} "
          f"[{' '.join(rule['source'].split()).split(':')[0]}]"
          + ("" if spec.get("verified") else " (not yet re-read)"),
          pin=rule["pin"], pin_number=pnum, states=sorted(st),
          volts=volts, part=spec["mpn"])

    # 16. an MCU pin asked for what it can't do (reference/mcu_pins.yaml,
    # #1032; errata corpus: bedlift v0.2 spread its two DRV8874s' PWM
    # inputs over pins no one timer reaches, glasgow revC3 put the flash
    # chip select on the FX2's input-only RDY0)
    mp_table = load_mcu_pins() if mcu_pins is None else mcu_pins
    pwm_found, pwm_checked = _mcu_pwm_groups(nets, comps, comp_pins,
                                             ps_rails, mp_table)
    for row, mref, mrow, inputs in pwm_found:
        pins = "; ".join(
            f"{lr} {lp} on {mp} ({'/'.join(fs) if fs else 'no timer'})"
            for lr, lp, mp, fs in inputs)
        v(row.get("severity", "warning"), "mcu_pwm_timer", None,
          sorted({i[0] for i in inputs} | {mref}),
          f"{mref} ({comps[mref].get('value')}) drives the PWM inputs of "
          f"{row['mpn']} {', '.join(sorted({i[0] for i in inputs}))} from "
          f"pins no single timer gives each a channel: {pins}. "
          f"{' '.join(row['why'].split())} "
          f"[{' '.join(row['source'].split()).split(',')[0]}; "
          f"{' '.join(mrow['source'].split()).split('(')[0].strip()}]"
          + ("" if row.get("verified") and mrow.get("verified")
             else " (not yet re-read)"),
          mcu=mref, part=row["mpn"],
          inputs=[{"ref": lr, "pin": lp, "mcu_pin": mp, "timers": fs}
                  for lr, lp, mp, fs in inputs])
    io_checked = 0
    for row in mp_table.get("mcus") or []:
        if row.get("input_only"):
            io_checked += sum(1 for r in comps
                              if _mcu_row(comps[r], mp_table.get("mcus")
                                          or []) is row)
    for (mrow, mref, mpin, mnet, sref, spin,
         snet) in _input_only_selects(nets, comps, comp_pins, mp_table):
        via = "" if snet == mnet else f" over a 0 ohm link from '{mnet}'"
        v("warning", "mcu_input_only_drive", snet, [mref, sref],
          f"{sref} ({comps[sref].get('value')}) chip select {spin} on "
          f"'{snet}' is wired to {mref} ({comps[mref].get('value')}) "
          f"{mpin}{via}, a pin the MCU can only read: it can watch that "
          f"select but never drive it "
          f"[{' '.join(mrow['source'].split()).split(',')[0]}]"
          + ("" if mrow.get("verified") else " (not yet re-read)"),
          mcu=mref, mcu_pin=mpin, part=mrow["mpn"])

    # 17. two I2C devices on one bus strapped to one address
    # (reference/i2c_addresses.yaml, #1034; errata corpus: glasgow revC2
    # strapped its TMP112 and DAC43204 both to 0x48)
    i2c_rows = load_i2c_addresses() if i2c_addresses is None \
        else i2c_addresses
    buses = _i2c_buses(nets, comps, comp_pins, ps_rails, i2c_rows)
    for scl, devs in sorted(buses.items()):
        by_adr: dict[int, list[tuple]] = {}
        for d in devs:
            by_adr.setdefault(d[0], []).append(d)
        for adr, same in sorted(by_adr.items()):
            if len(same) < 2:
                continue
            who = ", ".join(f"{r} ({comps[r].get('value')}, "
                            f"{row['pin']} to {where})"
                            for _, r, row, where in same)
            v("error", "i2c_address_collision", scl,
              sorted(d[1] for d in same),
              f"I2C bus '{scl}': {who} all answer at 0x{adr:02X} "
              f"[{'; '.join(sorted({' '.join(d[2]['source'].split()).split(' Table')[0] for d in same}))}]"
              + ("" if all(d[2].get("verified") for d in same)
                 else " (not yet re-read)"),
              address=f"0x{adr:02X}")
    # 18. a pull too weak for what the pins on its net sink or source, and
    # a reset line that two parts can drive (reference/pin_leakage.yaml,
    # #1026; errata corpus: glasgow revD0 held the STM32 NRST at 1.2 V
    # against an unprogrammed FX2 and an unconfigured ECP5's 150 uA
    # pull-downs, with both of them able to drive it; its 100k nALERT
    # pull-up lost to the same pull-down)
    pl_specs = load_pin_leakage() if pin_leakage is None else pin_leakage
    pl_rails = _rail_volts(constraints, nets)
    pulls_checked = 0
    for hit in _pull_vs_leakage(nets, comps, comp_pins, pl_rails, pl_specs):
        pulls_checked += 1
        if not hit["severity"]:
            continue
        way = "up" if hit["up"] else "down"
        who = ", ".join(
            f"{r}.{p} {s['mpn']}"
            + ("" if s.get("verified") else " (not yet re-read)")
            for r, p, s, _ in hit["rows"])
        recv = ", ".join(f"{n} VIL {lo:.2g} / VIH {hi:.2g} V"
                         for n, lo, hi in hit["wrong"])
        refs = sorted({r for r, *_ in hit["rows"]}
                      | {w.split()[0].split(".")[0]
                         for _, _, w in hit["pulls"]})
        srcs = "; ".join(sorted({" ".join(row["source"].split()).split(":")[0]
                                 for *_, row in hit["rows"]}))
        v(hit["severity"], "pull_vs_leakage", hit["net"], refs,
          f"'{hit['net']}' is pulled {way} by "
          f"{'; '.join(w for _, _, w in hit['pulls'])} "
          f"(Thevenin {hit['vth']:.3g} V through {hit['rth'] / 1e3:.3g}k), "
          f"but {who} {'sink' if hit['up'] else 'source'} up to "
          f"{hit['sink'] * 1e6:.3g} uA there before they are set up, so it "
          f"can sit at {hit['level']:.2g} V: "
          + ("the wrong state" if hit["severity"] == "error"
             else "between the thresholds")
          + f" for {recv}. A pull of at most "
          f"{hit['r_max'] / 1e3:.3g}k alone holds {hit['need']:.2g} V "
          f"[{srcs}]",
          level_v=round(hit["level"], 3), sink_a=hit["sink"],
          thevenin_v=round(hit["vth"], 3), thevenin_ohm=round(hit["rth"]),
          max_pull_ohm=round(hit["r_max"]), nets=hit["group"])
    contention_checked = 0
    for name, rpin, drivers, spec in _driver_contention(
            nets, comps, comp_pins, pl_rails, pl_specs):
        contention_checked += 1
        v("warning", "driver_contention", name,
          sorted({d.split(".")[0] for d in drivers}),
          f"reset input {rpin} ({spec['mpn']}) on '{name}' can be driven "
          f"by {', '.join(drivers)}: two push-pull pins from different "
          f"parts fight if both drive at once; give one of them the line "
          f"(a gate or open-drain) or confirm firmware keeps all but one "
          f"as inputs", reset_pin=rpin, drivers=drivers)

    # 19. a driver on a higher rail than its receiver's I/O can stand,
    # through series resistors only (reference/io_levels.yaml, #1029;
    # errata corpus: megabuzz revA ran the YMF262 at 5 V into the Spartan-6
    # through 27 ohm dropping resistors)
    io_table = load_io_levels() if io_levels is None else io_levels
    iolv_checked = 0
    for hit in _io_overvoltage(nets, comps, comp_pins, pl_rails, io_table):
        if "checked" in hit:
            iolv_checked = hit["checked"]
            continue
        if not hit["over"]:
            continue
        d, r = hit["drow"], hit["rrow"]
        via = (f" through {hit['series']} "
               f"{comps[hit['series']].get('value')}" if hit["series"]
               else " directly")
        div = (f", divided by {'; '.join(hit['divider'])}"
               if hit["divider"] else "")
        v("error", "io_overvoltage", hit["net"],
          sorted({hit["driver"], hit["receiver"]}
                 | ({hit["series"]} if hit["series"] else set())),
          f"{hit['driver']} ({comps[hit['driver']].get('value')}) "
          f"{hit['dfunc'] or hit['dpin']} on a {hit['vdd']:g} V supply "
          f"drives {hit['receiver']} "
          f"({comps[hit['receiver']].get('value')}) "
          f"{hit['rfunc'] or hit['rpin']}{via}{div}: its high is at least "
          f"{hit['level']:.3g} V, over the {float(r['vin_max']):g} V that "
          f"pin takes. A series resistor only limits the clamp current; "
          f"shift the level (a translator or a divider) or run the driver "
          f"from the receiver's rail "
          f"[{' '.join(d['source'].split()).split(',')[0]}; "
          f"{' '.join(r['source'].split()).split(' Table')[0]}]"
          + ("" if d.get("verified") and r.get("verified")
             else " (not yet re-read)"),
          level_v=round(hit["level"], 3), vin_max=float(r["vin_max"]),
          driver_supply_v=hit["vdd"], series_ohm=hit["rs"])

    # 20. a power-good output that gates nothing (#1030; errata corpus:
    # pdlc v1's LM5158 PGOOD lit an LED and the half-bridge drove the panel
    # before the boost was in regulation)
    pg_checked = 0
    for ref, pin, func, net, others in _pg_unused(nets, comps, comp_pins,
                                                  pl_rails):
        if ref == "checked":
            pg_checked = pin
            continue
        v("warning", "power_good_unused", net, [ref] + others,
          f"{ref} ({comps.get(ref, {}).get('value')}) {func} (pin {pin}) "
          f"reaches only {', '.join(others)}: no enable, reset or MCU input "
          f"reads it, so the load starts before the rail is in regulation. "
          f"Gate the load's enable on it, or leave it unconnected if "
          f"sequencing is not needed", pg_pin=f"{ref}.{pin}")

    facts = {
        "nets": len(nets),
        "components": len(comps),
        "unconnected_pins": sum(1 for n in nets
                                if n.startswith("unconnected-")),
        "constraint_nets_checked": len(referenced),
        "constraint_refs_checked": len(ref_where),
        "pins_expected": pins_expected,
        "pins_connected": pins_connected,
        "decoupling_associations": n_assoc,
        "power": power_facts,
        "common_mode_inputs_checked": cm_checked,
        "amp_inputs_dc_checked": dc_checked,
        "polarized_caps_checked": pol_checked,
        "min_gain_loops_checked": loops_checked,
        "pin_state_parts_checked": pin_states_checked,
        "mcu_pwm_groups_checked": pwm_checked,
        "mcu_input_only_parts_checked": io_checked,
        "i2c_buses_checked": len(buses),
        "i2c_devices_placed": sum(len(d) for d in buses.values()),
        "pulls_vs_leakage_checked": pulls_checked,
        "reset_contention_found": contention_checked,
        "io_drive_pins_checked": iolv_checked,
        "power_good_pins_checked": pg_checked,
    }
    return violations, facts


def run_audit(netlist: Path, constraints_path: Path | None,
              decoupling_path: Path | None, out: str | None,
              pcb: Path | None = None):
    """Audit NETLIST. With no constraints file the netlist's own faults
    still count (dangling_net, ground_island, sot23_pinout, ...); only
    power_undeclared is dropped, since with nothing declared every rail
    would fire."""
    parsed = parse_netlist(netlist)
    if not parsed["components"] and not parsed["nets"]:
        # an empty export is no clean pass (#1023)
        payload = checklib.report(
            SCRIPT, str(netlist), [], mode="audit",
            nothing_to_judge=f"{Path(netlist).name} has 0 components")
        payload["status"] = "pass"
        return payload, out
    constraints = (checklib.load_json(constraints_path, "constraints")
                   if constraints_path else {})
    decoupling = (checklib.load_json(decoupling_path, "decoupling metadata")
                  if decoupling_path else None)
    violations, facts = audit(
        parsed, constraints, decoupling,
        board_only_refs=_board_only_refs(pcb) if pcb else None)
    if constraints_path is None:
        violations = [x for x in violations
                      if x.get("kind") != "power_undeclared"]
    payload = checklib.report(SCRIPT, str(netlist), violations,
                              mode="audit", **facts)
    # warnings alone do not fail the audit (fp_verify precedent)
    has_error = any(x["severity"] == "error" for x in violations)
    payload["status"] = "violations" if has_error else "pass"
    return payload, out


# ------------------------------------------------------------ CLI

def _modern_netlist(net: Path) -> Path:
    """An eeschema 4/5 netlist names no pins; fill them from its libparts."""
    from lib import legacy_net  # noqa: PLC0415
    if not legacy_net.is_legacy_net(net):
        return net
    out = Path(tempfile.mkdtemp(prefix="aiee_audit_")) / net.name
    try:
        legacy_net.from_shipped(net, out)
    except (OSError, legacy_net.LegacyError, ValueError) as exc:
        raise checklib.CheckError(f"{net.name} (KiCad 4/5): {exc}") from exc
    return out


def _export_netlist(sch: Path) -> Path:
    """Export the schematic's netlist via kc.py into a temp file."""
    out = Path(tempfile.mkdtemp(prefix="aiee_audit_")) / (sch.stem + ".net")
    from lib import legacy_net  # noqa: PLC0415
    if legacy_net.is_legacy(sch):  # KiCad 4/5: kicad-cli exports it empty
        r = legacy_net.netlist(sch, out)
        if r.get("status") != "pass":
            raise checklib.CheckError(r["error"])
        return out
    import kc  # noqa: PLC0415  (deferred: only the --sch path needs kicad-cli)
    from lib import env  # noqa: PLC0415
    cli = env.find_kicad_cli()
    r = kc.export_netlist(cli, sch, out)
    if r.get("status") != "pass":
        raise checklib.CheckError(
            f"netlist export failed for {sch}: {r.get('error') or r}")
    return out


def _have_kicad_cli() -> bool:
    """A host with no KiCad install can't export a schematic; a bad
    explicit pin still raises (env.EnvError) rather than passing."""
    from lib import env  # noqa: PLC0415
    return env.find_kicad_cli() is not None


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--netlist", help="kicadsexpr netlist to audit")
    ap.add_argument("--sch", help="schematic; netlist is exported first "
                                  "(needs kicad-cli)")
    ap.add_argument("--pcb", help="board; audits <stem>.kicad_sch beside "
                                  "it (else <stem>.net) - the verify gate's "
                                  "call")
    ap.add_argument("--constraints", help="constraints.json (audit mode); "
                                          "without it power_undeclared is "
                                          "not judged")
    ap.add_argument("--decoupling", help="decoupling.json metadata to "
                                         "cross-check (audit mode)")
    ap.add_argument("--compare", help="second netlist: strict electrical "
                                      "identity diff instead of the audit")
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)

    if not args.netlist and not args.sch and not args.pcb:
        raise checklib.CheckError("give --netlist, --sch or --pcb")
    netlist = Path(args.netlist) if args.netlist else None
    sch = Path(args.sch) if args.sch else None
    if not netlist and not sch:
        pcb = Path(args.pcb)
        own_sch, own_net = (pcb.with_suffix(".kicad_sch"),
                            pcb.with_suffix(".net"))
        legacy = pcb.with_suffix(".sch")
        why = None
        from lib import legacy_net  # noqa: PLC0415
        if not own_sch.is_file() and legacy_net.is_legacy(legacy):
            sch = legacy  # KiCad 4/5: read its shipped .net or the board
        elif own_sch.is_file() and _have_kicad_cli():
            sch = own_sch
        elif own_net.is_file():
            netlist = own_net
        elif own_sch.is_file():
            why = (f"no kicad-cli on this host to export {own_sch.name}, "
                   f"and no {own_net.name} beside the board")
        else:
            why = f"no {own_sch.name} or {own_net.name} beside the board"
        if why:
            payload = checklib.report(SCRIPT, str(pcb), [], mode="audit",
                                      nothing_to_judge=why)
            payload["status"] = "pass"
            return payload, args.out
    if sch and not netlist:
        netlist = _export_netlist(sch)
    netlist = _modern_netlist(netlist)

    if args.compare:
        return run_compare(netlist, Path(args.compare), args.out)
    return run_audit(netlist,
                     Path(args.constraints) if args.constraints else None,
                     Path(args.decoupling) if args.decoupling else None,
                     args.out, pcb=Path(args.pcb) if args.pcb else None)


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    sys.exit(main())
